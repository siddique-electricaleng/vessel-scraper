"""
browser_scraper.py — Headless browser fallback for sites that block plain HTTP.

Uses Playwright (Chromium) to render pages with full JavaScript execution,
then extracts vessel image URLs from the rendered DOM.

This module is only imported when the fast HTTP scrapers fail, so Playwright
startup cost (~2-3s) is only paid when needed.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger("browser_scraper")

# Same trusted hosts check as main.py — import would create circular dep
_PLACEHOLDER_SUBSTRINGS = (
    "cool-ship", "placeholder.svg", "placeholder.", "gen_img_ship",
    "no-photo", "nophoto", "no_photo", "no-image", "noimage",
    "default-vessel", "default-ship", "ship-icon", "vessel-icon", "no_vessel",
)

_IMG_EXTENSIONS = re.compile(r"\.(jpe?g|png|webp|avif|gif)", re.I)


def _is_real_image_url(url: str) -> bool:
    """Quick check: looks like a real vessel photo URL, not a placeholder."""
    lower = url.lower()
    if any(p in lower for p in _PLACEHOLDER_SUBSTRINGS):
        return False
    if "svg" in lower and not _IMG_EXTENSIONS.search(lower):
        return False
    return True


# ── Per-site URL builders ────────────────────────────────────────────────────

def _build_urls(mmsi: str, name: str) -> list[dict]:
    """Build a list of {source, url, referer} dicts for all maritime sites."""
    slug_lower = name.lower().replace(" ", "-")
    slug_title = name.title().replace(" ", "-")
    query = name.replace(" ", "+")

    # Only sites that benefit from JS rendering — skip ones already covered
    # by HTTP scrapers (VesselFinder) or permanently blocked (MarineTraffic/Cloudflare)
    return [
        {
            "source": "shipspotting",
            "url": f"https://www.shipspotting.com/photos/gallery?ship_name={query}",
            "referer": "https://www.shipspotting.com/",
        },
        {
            "source": "marinetraffic",
            "url": f"https://www.marinetraffic.com/en/ais/details/ships/mmsi:{mmsi}",
            "referer": "https://www.marinetraffic.com/",
        },
        {
            "source": "myshiptracking",
            "url": f"https://www.myshiptracking.com/vessels?mmsi={mmsi}",
            "referer": "https://www.myshiptracking.com/",
        },
    ]


# ── Image extraction from rendered page ─────────────────────────────────────

async def _extract_from_page(page, source: str, vessel_name: str = "") -> Optional[str]:
    """
    Extract a vessel image URL from an already-loaded Playwright page.

    Strategies:
      1. <meta og:image>
      2. <img> tags with vessel photo patterns (name-filtered for galleries)
      3. CSS background-image on vessel photo containers
    """
    name_upper = vessel_name.upper()

    # 1. OG image meta tag
    try:
        og = await page.locator('meta[property="og:image"]').get_attribute("content", timeout=2000)
        if og and og.startswith("http") and _is_real_image_url(og):
            logger.info("[browser/%s] og:image found: %s", source, og)
            return og
    except Exception:
        pass

    # 2. <img> tags — look for large vessel photos
    try:
        imgs = await page.locator("img").all()
        for img in imgs:
            src = await img.get_attribute("src") or ""
            if not src or not src.startswith("http"):
                continue
            if not _is_real_image_url(src):
                continue

            # For gallery pages (ShipSpotting), verify vessel name matches alt/title
            if source == "shipspotting" and name_upper:
                alt = ((await img.get_attribute("alt")) or "").upper()
                title = ((await img.get_attribute("title")) or "").upper()
                try:
                    parent_text = await img.evaluate(
                        "el => (el.closest('a')?.textContent ?? '').toUpperCase()"
                    )
                except Exception:
                    parent_text = ""
                if name_upper not in alt and name_upper not in title and name_upper not in parent_text:
                    logger.debug("[browser/%s] skipping non-matching img: alt=%r", source, alt)
                    continue

            # Check natural size — real photos are large
            try:
                box = await img.bounding_box(timeout=1000)
                if box and box["width"] > 100 and box["height"] > 100:
                    logger.info("[browser/%s] large img found: %s (%dx%d)", source, src, box["width"], box["height"])
                    return src
            except Exception:
                # Can't get size, check URL patterns instead
                if _IMG_EXTENSIONS.search(src):
                    logger.info("[browser/%s] img with extension found: %s", source, src)
                    return src
    except Exception:
        pass

    # 3. Background images on photo containers
    try:
        for selector in [".vessel-photo", ".ship-photo", ".main-photo", "[class*='photo']", "[class*='image']"]:
            els = await page.locator(selector).all()
            for el in els[:3]:
                bg = await el.evaluate("e => getComputedStyle(e).backgroundImage")
                if bg and bg.startswith("url("):
                    url = bg[4:-1].strip('"').strip("'")
                    if url.startswith("http") and _is_real_image_url(url):
                        logger.info("[browser/%s] bg-image found: %s", source, url)
                        return url
    except Exception:
        pass

    return None


# ── Main browser scraper ─────────────────────────────────────────────────────

async def browser_scrape(
    mmsi: str,
    name: str,
    timeout_per_site: int = 15_000,
) -> Optional[str]:
    """
    Launch headless Chromium, visit each maritime site, wait for JS to render,
    and extract vessel image URLs.

    Returns the first image URL found, or None if all sites fail.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning("Playwright not installed — browser fallback unavailable")
        return None

    sites = _build_urls(mmsi, name)
    result_url: Optional[str] = None

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
        )

        for site in sites:
            if result_url:
                break

            page = await context.new_page()
            try:
                logger.info("[browser/%s] loading %s", site["source"], site["url"])
                await page.goto(
                    site["url"],
                    wait_until="domcontentloaded",
                    timeout=timeout_per_site,
                )
                # Wait extra for images to load via JS
                await page.wait_for_timeout(3000)

                url = await _extract_from_page(page, site["source"], name)
                if url:
                    result_url = url
            except Exception as exc:
                logger.debug("[browser/%s] failed: %s", site["source"], exc)
            finally:
                await page.close()

        await browser.close()

    return result_url
