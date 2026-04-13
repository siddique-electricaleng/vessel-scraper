"""
main.py — Vessel Image API
FastAPI service that scrapes ship photos from maritime tracking sites
and serves them as raw image bytes.

Run:
    uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4

Endpoint:
    GET /vessel/image?mmsi={mmsi}&name={name}
    → binary image (image/jpeg, image/png, image/webp, …)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from cachetools import TTLCache
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, Response

from scrapers import ALL_SCRAPERS, probe_html

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
)
logger = logging.getLogger("vessel_image_api")


# ── Trusted image hosts ───────────────────────────────────────────────────────

TRUSTED_IMAGE_HOSTS: frozenset[str] = frozenset(
    [
        # MarineTraffic
        "photos.marinetraffic.com",
        "thumb.marinetraffic.com",
        "img.marinetraffic.com",
        "marinetraffic.com",
        # VesselFinder
        "cdn.vesselfinder.com",
        "photos.vesselfinder.com",
        "static.vesselfinder.com",
        "static.vesselfinder.net",
        "vesselfinder.com",
        # VesselTracker
        "photos.vesseltracker.com",
        "media.vesseltracker.com",
        "img.vesseltracker.com",
        "vesseltracker.com",
        # FleetPhoto
        "media.fleetphoto.ru",
        "cdn.fleetphoto.ru",
        "fleetphoto.ru",
        "fleetphoto.de",
        # ShipSpotting
        "images.shipspotting.com",
        "img.shipspotting.com",
        "shipspotting.com",
        # FleetMon
        "photos.fleetmon.com",
        "cdn.fleetmon.com",
        "img.fleetmon.com",
        "fleetmon.com",
        # Others
        "maritimeoptima.com",
        "myshiptracking.com",
    ]
)

# Base HTTP headers — realistic browser profile
_BASE_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}

# Global shared resources
_http_client: Optional[httpx.AsyncClient] = None
_image_cache: TTLCache = TTLCache(maxsize=1_000, ttl=3_600)  # 1 h TTL


# ── Lifespan: create / destroy the shared HTTP client ─────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http_client
    _http_client = httpx.AsyncClient(
        headers=_BASE_HEADERS,
        timeout=httpx.Timeout(connect=6.0, read=12.0, write=6.0, pool=5.0),
        follow_redirects=True,
        limits=httpx.Limits(
            max_connections=200,
            max_keepalive_connections=60,
            keepalive_expiry=30,
        ),
        http2=True,
    )
    logger.info("HTTP client pool started (HTTP/2 enabled)")
    yield
    await _http_client.aclose()
    logger.info("HTTP client pool closed")


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="Vessel Image API",
    description=(
        "Given a vessel MMSI and name, scrapes maritime tracking sites "
        "and returns the ship photo as raw binary."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_trusted(url: str) -> bool:
    """Return True iff the URL's hostname is in TRUSTED_IMAGE_HOSTS."""
    try:
        host = urlparse(url).netloc.lower().lstrip("www.")
        return host in TRUSTED_IMAGE_HOSTS or any(
            host.endswith("." + t) for t in TRUSTED_IMAGE_HOSTS
        )
    except Exception:
        return False


def _find_urls_in_obj(obj, depth: int = 0) -> list[str]:
    """Recursively walk a decoded JSON object and collect all string values that look like URLs."""
    if depth > 12:
        return []
    if isinstance(obj, str):
        return [obj] if obj.startswith("http") else []
    if isinstance(obj, dict):
        out = []
        for v in obj.values():
            out.extend(_find_urls_in_obj(v, depth + 1))
        return out
    if isinstance(obj, list):
        out = []
        for item in obj:
            out.extend(_find_urls_in_obj(item, depth + 1))
        return out
    return []


# Pre-compiled: matches any http(s) URL ending in a common image extension
_IMG_URL_RE = re.compile(
    r'https?://[^\s\'"<>\\]+\.(?:jpe?g|png|webp|avif|gif)(?:[^\s\'"<>\\]*)?',
    re.IGNORECASE,
)

# Matches VesselFinder direct ship-photo URLs (no file extension)
_VF_PHOTO_RE = re.compile(
    r'https://static\.vesselfinder\.net/ship-photo/[0-9]+-[0-9]+(?:-[0-9a-f]+)?/[0-3]',
)


def _extract_image_url(html: str) -> Optional[str]:
    """
    Multi-strategy extractor — works on both SSR and SPA pages.

    Priority:
      1. <meta og:image> / <meta twitter:image>   — clean, high-quality
      2. Next.js __NEXT_DATA__ JSON               — MarineTraffic, VesselFinder
      3. Regex over ALL <script> tag text         — catches any JSON blob
      4. Regex over the entire raw HTML           — last resort
      5. <img data-src> / <img src>               — traditional static pages
    """
    soup = BeautifulSoup(html, "html.parser")

    # 1. Open Graph / Twitter meta tags
    for prop in ("og:image", "og:image:url", "og:image:secure_url"):
        tag = soup.find("meta", property=prop)
        if tag:
            url = (tag.get("content") or "").strip()
            if url and _is_trusted(url):
                return url
    tag = soup.find("meta", attrs={"name": "twitter:image"})
    if tag:
        url = (tag.get("content") or "").strip()
        if url and _is_trusted(url):
            return url

    # 2. Next.js __NEXT_DATA__ (MarineTraffic, VesselFinder, and many others)
    nd = soup.find("script", id="__NEXT_DATA__")
    if nd and nd.string:
        try:
            data = json.loads(nd.string)
            for url in _find_urls_in_obj(data):
                if _is_trusted(url) and _IMG_URL_RE.match(url):
                    return url
        except Exception:
            pass

    # 3. Regex over every <script> block (catches window.__STATE__, REDUX, etc.)
    for script in soup.find_all("script"):
        text = script.string or ""
        for url in _IMG_URL_RE.findall(text):
            if _is_trusted(url):
                return url

    # 4a. VesselFinder direct ship-photo URLs (extensionless)
    for url in _VF_PHOTO_RE.findall(html):
        return url

    # 4b. Regex over the entire raw HTML — catches URLs in data-* attrs, JSON-LD, etc.
    for url in _IMG_URL_RE.findall(html):
        if _is_trusted(url):
            return url

    # 5. HTML attributes: lazy-loaded then plain src
    for attr in ("data-src", "data-lazy-src", "data-original", "data-url", "data-photo"):
        for img in soup.find_all("img", attrs={attr: True}):
            url = (img.get(attr) or "").strip()
            if url and _is_trusted(url):
                return url
    for img in soup.find_all("img"):
        url = (img.get("src") or "").strip()
        if url and _is_trusted(url):
            return url

    return None


async def _fetch_image(url: str) -> Optional[tuple[bytes, str]]:
    """Download image bytes from a trusted URL. Returns (bytes, content_type) or None."""
    try:
        r = await _http_client.get(
            url,
            headers={
                "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
                "Referer": f"https://{urlparse(url).netloc}/",
            },
        )
        ct: str = r.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        if r.status_code == 200 and ct.startswith("image/"):
            return r.content, ct
        logger.debug("_fetch_image got HTTP %s content-type=%s for %s", r.status_code, ct, url)
    except Exception as exc:
        logger.warning("_fetch_image failed for %s: %s", url, exc)
    return None


# ── Core resolution logic ─────────────────────────────────────────────────────

async def resolve_image(mmsi: str, name: str) -> tuple[bytes, str]:
    """
    Run all scrapers concurrently. For each image URL returned, attempt
    to download it. Return the first success.

    Raises HTTPException on failure.
    """
    # Fire all scrapers simultaneously
    tasks = [
        scraper(mmsi, name, _http_client, _extract_image_url)
        for scraper in ALL_SCRAPERS
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Collect valid image URLs in priority order
    candidates: list[str] = []
    for r in results:
        if isinstance(r, str) and r and r not in candidates:
            candidates.append(r)

    if not candidates:
        logger.warning("No image URL found for MMSI=%s name=%s", mmsi, name)
        raise HTTPException(
            status_code=404,
            detail=f"No vessel image found for MMSI {mmsi} / '{name}' across all sources.",
        )

    # Try each candidate until one yields bytes
    for img_url in candidates:
        data = await _fetch_image(img_url)
        if data:
            logger.info("Image served from %s (%d bytes)", img_url, len(data[0]))
            return data

    raise HTTPException(
        status_code=502,
        detail="Image URLs found but all download attempts failed (sources may be rate-limiting).",
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get(
    "/vessel/image",
    response_class=Response,
    summary="Fetch vessel photo",
    description=(
        "Returns the vessel photo as raw image bytes.\n\n"
        "The `X-Cache` response header indicates whether the result came from "
        "cache (`HIT`) or was freshly scraped (`MISS`)."
    ),
    responses={
        200: {
            "content": {
                "image/jpeg": {},
                "image/png": {},
                "image/webp": {},
                "image/avif": {},
            },
            "description": "Raw image binary",
        },
        404: {"description": "No image found for this vessel"},
        502: {"description": "Image source reachable but download failed"},
    },
    tags=["Vessel"],
)
async def get_vessel_image(
    mmsi: str = Query(
        ...,
        description="9-digit MMSI number",
        example="311052100",
        min_length=7,
        max_length=15,
    ),
    name: str = Query(
        ...,
        description="Vessel name (used to construct source URLs)",
        example="ANASTASIA K",
        min_length=1,
        max_length=120,
    ),
) -> Response:
    cache_key = f"{mmsi.strip()}:{name.strip().upper()}"

    # ── Cache hit ──────────────────────────────────────────────────────────
    cached = _image_cache.get(cache_key)
    if cached is not None:
        image_bytes, content_type = cached
        return Response(
            content=image_bytes,
            media_type=content_type,
            headers={
                "X-Cache": "HIT",
                "X-Vessel-Name": name,
                "X-Vessel-MMSI": mmsi,
                "Cache-Control": "public, max-age=3600",
            },
        )

    # ── Cache miss: scrape ─────────────────────────────────────────────────
    image_bytes, content_type = await resolve_image(mmsi.strip(), name.strip())
    _image_cache[cache_key] = (image_bytes, content_type)

    return Response(
        content=image_bytes,
        media_type=content_type,
        headers={
            "X-Cache": "MISS",
            "X-Vessel-Name": name,
            "X-Vessel-MMSI": mmsi,
            "Cache-Control": "public, max-age=3600",
        },
    )


@app.get(
    "/health",
    summary="Health check",
    tags=["Meta"],
)
async def health_check():
    return {
        "status": "ok",
        "cache_entries": len(_image_cache),
        "cache_max": _image_cache.maxsize,
        "cache_ttl_seconds": int(_image_cache.ttl),
    }


@app.delete(
    "/cache",
    summary="Clear image cache",
    tags=["Meta"],
)
async def clear_cache():
    count = len(_image_cache)
    _image_cache.clear()
    return {"cleared": count}


@app.get(
    "/vessel/debug",
    summary="Per-scraper diagnostic — shows what each source returned",
    tags=["Meta"],
)
async def debug_vessel(
    mmsi: str = Query(..., example="311052100"),
    name: str = Query(..., example="ANASTASIA K"),
) -> JSONResponse:
    """
    Runs every scraper and reports the image URL (or failure reason) for each.
    Also shows first 500 chars of the raw HTML so you can see what the site returned.
    Does NOT cache results.
    """
    async def _run_one(scraper_fn) -> dict:
        source = scraper_fn.__name__.replace("scrape_", "")
        try:
            url = await scraper_fn(mmsi, name, _http_client, _extract_image_url)
            return {"source": source, "image_url": url, "error": None}
        except Exception as exc:
            return {"source": source, "image_url": None, "error": str(exc)}

    scraper_results, html_probes = await asyncio.gather(
        asyncio.gather(*[_run_one(s) for s in ALL_SCRAPERS]),
        probe_html(_http_client, mmsi, name),
    )
    found = [r for r in scraper_results if r["image_url"]]
    return JSONResponse({
        "mmsi": mmsi,
        "name": name,
        "found": len(found),
        "scraper_results": scraper_results,
        "html_probes": html_probes,
    })
