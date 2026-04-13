"""
scrapers.py — per-source vessel image scrapers
Each scraper receives (mmsi, name, client, extract_fn) and returns an image URL or None.

Includes per-source rate limiting so bulk jobs don't trigger IP bans.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Callable, Optional

import httpx

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ── Per-source rate limiter ──────────────────────────────────────────────────

class SourceThrottle:
    """
    Ensures a minimum delay between requests to each source domain.
    Prevents rate-limit bans during bulk scraping.
    """

    def __init__(self, min_interval: float = 2.0):
        self._min_interval = min_interval
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_request: dict[str, float] = {}

    def _get_lock(self, source: str) -> asyncio.Lock:
        if source not in self._locks:
            self._locks[source] = asyncio.Lock()
        return self._locks[source]

    async def wait(self, source: str):
        """Wait until it's safe to make another request to this source."""
        lock = self._get_lock(source)
        async with lock:
            now = time.monotonic()
            last = self._last_request.get(source, 0.0)
            wait_for = self._min_interval - (now - last)
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            self._last_request[source] = time.monotonic()


# Global throttle — 2 seconds between requests to the same source
_throttle = SourceThrottle(min_interval=2.0)


# ── Shared fetch helper ────────────────────────────────────────────────────────

async def _get_html(
    client: httpx.AsyncClient,
    url: str,
    referer: str = "",
    extra_headers: dict | None = None,
    source: str = "",
) -> Optional[str]:
    if source:
        await _throttle.wait(source)
    headers: dict = {}
    if referer:
        headers["Referer"] = referer
    if extra_headers:
        headers.update(extra_headers)
    try:
        r = await client.get(url, headers=headers)
        if r.status_code == 200:
            return r.text
        logger.debug("HTTP %s from %s", r.status_code, url)
    except httpx.TimeoutException:
        logger.warning("Timeout fetching %s", url)
    except Exception as exc:
        logger.warning("Error fetching %s: %s", url, exc)
    return None


# ── Individual scrapers ────────────────────────────────────────────────────────

async def scrape_marinetraffic(
    mmsi: str,
    name: str,
    client: httpx.AsyncClient,
    extract: Callable[[str], Optional[str]],
) -> Optional[str]:
    """
    MarineTraffic — try the vessel detail page first, then the photo gallery.
    MarineTraffic uses Next.js, so __NEXT_DATA__ JSON is embedded in the HTML.
    """
    base = "https://www.marinetraffic.com"

    # 1. Detail page (contains __NEXT_DATA__ with vessel data including photos)
    detail_url = f"{base}/en/ais/details/ships/mmsi:{mmsi}"
    html = await _get_html(client, detail_url, referer=base + "/", source="marinetraffic")
    if html:
        img = extract(html)
        if img:
            logger.info("[marinetraffic/detail] found: %s", img)
            return img

    # 2. Photo gallery page for this vessel
    gallery_url = f"{base}/en/photos/of/ships/mmsi:{mmsi}"
    html = await _get_html(client, gallery_url, referer=detail_url, source="marinetraffic")
    if html:
        img = extract(html)
        if img:
            logger.info("[marinetraffic/gallery] found: %s", img)
            return img

    return None


async def scrape_vesselfinder(
    mmsi: str,
    name: str,
    client: httpx.AsyncClient,
    extract: Callable[[str], Optional[str]],
) -> Optional[str]:
    """
    VesselFinder vessel detail page.

    Strategy:
      1. Fetch the detail page and extract the IMO from the embedded JS var ``vu_imo``.
      2. Construct the direct photo URL:
         ``https://static.vesselfinder.net/ship-photo/{imo}-{mmsi}/0``
      3. HEAD-check the URL to confirm the image exists (avoids returning a
         placeholder / 404 page).
      4. Fall back to the generic HTML extractor if the direct approach fails.
    """
    import re as _re

    base_url = f"https://www.vesselfinder.com/vessels/details/{mmsi}"
    html = await _get_html(client, base_url, referer="https://www.vesselfinder.com/", source="vesselfinder")
    if not html:
        return None

    # 1. Extract IMO from ``var vu_imo=<digits>`` embedded in the page
    imo_match = _re.search(r"vu_imo\s*=\s*(\d{5,9})", html)
    if imo_match:
        imo = imo_match.group(1)
        # Size 0 = original/large; 1 = medium; 3 = thumb
        direct_url = f"https://static.vesselfinder.net/ship-photo/{imo}-{mmsi}/0"
        try:
            probe = await client.head(
                direct_url,
                headers={"Referer": "https://www.vesselfinder.com/"},
            )
            ct = probe.headers.get("content-type", "")
            if probe.status_code == 200 and ct.startswith("image/"):
                logger.info("[vesselfinder/direct] found: %s", direct_url)
                return direct_url
        except Exception as exc:
            logger.debug("[vesselfinder/direct] HEAD failed: %s", exc)

    # 2. Also try data-src ship-photo URLs from the HTML (lazy-loaded images)
    photo_matches = _re.findall(
        r"https://static\.vesselfinder\.net/ship-photo/[^\s\"'<>?]+",
        html,
    )
    for photo_url in photo_matches:
        # Only use photos that match THIS vessel's MMSI (not related-vessels sidebar)
        if mmsi in photo_url:
            logger.info("[vesselfinder/data-src] found: %s", photo_url)
            return photo_url

    # 3. Generic HTML extraction as last resort
    img = extract(html)
    if img:
        logger.info("[vesselfinder/extract] found: %s", img)
        return img

    return None


async def scrape_fleetmon(
    mmsi: str,
    name: str,
    client: httpx.AsyncClient,
    extract: Callable[[str], Optional[str]],
) -> Optional[str]:
    """
    FleetMon vessel page.
    URL pattern: /vessels/{slug}/{mmsi}/
    """
    slug = name.lower().replace(" ", "-")
    url = f"https://www.fleetmon.com/vessels/{slug}/{mmsi}/"
    html = await _get_html(client, url, referer="https://www.fleetmon.com/", source="fleetmon")
    if html:
        img = extract(html)
        if img:
            logger.info("[fleetmon] found: %s", img)
            return img
    return None


async def scrape_vesseltracker(
    mmsi: str,
    name: str,
    client: httpx.AsyncClient,
    extract: Callable[[str], Optional[str]],
) -> Optional[str]:
    """
    VesselTracker detail page.
    URL pattern: /en/Ships/{slug}-{mmsi}.html
    """
    slug = name.title().replace(" ", "-")
    url = f"https://www.vesseltracker.com/en/Ships/{slug}-{mmsi}.html"
    html = await _get_html(client, url, referer="https://www.vesseltracker.com/", source="vesseltracker")
    if html:
        img = extract(html)
        if img:
            logger.info("[vesseltracker] found: %s", img)
            return img
    return None


async def scrape_shipspotting(
    mmsi: str,
    name: str,
    client: httpx.AsyncClient,
    extract: Callable[[str], Optional[str]],
) -> Optional[str]:
    """
    ShipSpotting photo search by vessel name.
    """
    query = name.replace(" ", "+")
    url = f"https://www.shipspotting.com/photos/search?query={query}"
    html = await _get_html(client, url, referer="https://www.shipspotting.com/", source="shipspotting")
    if html:
        img = extract(html)
        if img:
            logger.info("[shipspotting] found: %s", img)
            return img
    return None


async def scrape_myshiptracking(
    mmsi: str,
    name: str,
    client: httpx.AsyncClient,
    extract: Callable[[str], Optional[str]],
) -> Optional[str]:
    """
    MyShipTracking vessel lookup by MMSI.
    """
    url = f"https://www.myshiptracking.com/vessels?mmsi={mmsi}"
    html = await _get_html(client, url, referer="https://www.myshiptracking.com/", source="myshiptracking")
    if html:
        img = extract(html)
        if img:
            logger.info("[myshiptracking] found: %s", img)
            return img
    return None


# ── Raw HTML probe — for the /vessel/debug endpoint ──────────────────────────

async def probe_html(
    client: httpx.AsyncClient,
    mmsi: str,
    name: str,
) -> dict:
    """
    Fetch the first page from each source and return a snippet of raw HTML.
    Useful for diagnosing why extraction fails (e.g. JS-gated content, bot blocks).
    """
    urls = {
        "marinetraffic": f"https://www.marinetraffic.com/en/ais/details/ships/mmsi:{mmsi}",
        "vesselfinder":  f"https://www.vesselfinder.com/vessels/details/{mmsi}",
        "fleetmon":      f"https://www.fleetmon.com/vessels/{name.lower().replace(' ', '-')}/{mmsi}/",
        "vesseltracker": f"https://www.vesseltracker.com/en/Ships/{name.title().replace(' ', '-')}-{mmsi}.html",
        "shipspotting":  f"https://www.shipspotting.com/photos/search?query={name.replace(' ', '+')}",
        "myshiptracking": f"https://www.myshiptracking.com/vessels?mmsi={mmsi}",
    }
    result = {}
    for source, url in urls.items():
        html = await _get_html(client, url)
        if html:
            # Return first 600 chars — enough to see if it's a real page or a bot wall
            result[source] = {"url": url, "html_snippet": html[:600], "length": len(html)}
        else:
            result[source] = {"url": url, "html_snippet": None, "length": 0}
    return result


# ── Registry — ordered by reliability / image quality ─────────────────────────

ALL_SCRAPERS = [
    scrape_marinetraffic,
    scrape_vesselfinder,
    scrape_fleetmon,
    scrape_vesseltracker,
    scrape_shipspotting,
    scrape_myshiptracking,
]
