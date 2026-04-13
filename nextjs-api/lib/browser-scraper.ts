/**
 * browser-scraper.ts — Headless browser fallback for JS-rendered maritime sites.
 *
 * Uses Playwright (Chromium) to render pages with full JavaScript execution,
 * then extracts vessel image URLs from the rendered DOM.
 *
 * Only imported when fast HTTP scrapers fail, so Playwright startup cost
 * (~2-3s) is only paid when needed.
 */

import type { Browser, Page } from "playwright";

const PLACEHOLDER_SUBSTRINGS = [
  "cool-ship",
  "placeholder.svg",
  "placeholder.",
  "gen_img_ship",
  "no-photo",
  "nophoto",
  "no_photo",
  "no-image",
  "noimage",
  "default-vessel",
  "default-ship",
  "ship-icon",
  "vessel-icon",
  "no_vessel",
];

const IMG_EXT_RE = /\.(jpe?g|png|webp|avif|gif)/i;

function isRealImageUrl(url: string): boolean {
  const lower = url.toLowerCase();
  if (PLACEHOLDER_SUBSTRINGS.some((p) => lower.includes(p))) return false;
  if (lower.includes("svg") && !IMG_EXT_RE.test(lower)) return false;
  return true;
}

interface SiteTarget {
  source: string;
  url: string;
  referer: string;
}

function buildUrls(mmsi: string, name: string): SiteTarget[] {
  const slugLower = name.toLowerCase().replace(/ /g, "-");
  const slugTitle = name
    .split(" ")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase())
    .join("-");
  const query = name.replace(/ /g, "+");

  return [
    {
      source: "marinetraffic",
      url: `https://www.marinetraffic.com/en/ais/details/ships/mmsi:${mmsi}`,
      referer: "https://www.marinetraffic.com/",
    },
    {
      source: "vesselfinder",
      url: `https://www.vesselfinder.com/vessels/details/${mmsi}`,
      referer: "https://www.vesselfinder.com/",
    },
    {
      source: "fleetmon",
      url: `https://www.fleetmon.com/vessels/${slugLower}/${mmsi}/`,
      referer: "https://www.fleetmon.com/",
    },
    {
      source: "vesseltracker",
      url: `https://www.vesseltracker.com/en/Ships/${slugTitle}-${mmsi}.html`,
      referer: "https://www.vesseltracker.com/",
    },
    {
      source: "shipspotting",
      url: `https://www.shipspotting.com/photos/gallery?ship_name=${query}`,
      referer: "https://www.shipspotting.com/",
    },
    {
      source: "myshiptracking",
      url: `https://www.myshiptracking.com/vessels?mmsi=${mmsi}`,
      referer: "https://www.myshiptracking.com/",
    },
  ];
}

async function extractFromPage(page: Page, source: string): Promise<string | null> {
  // 1. OG image meta tag
  try {
    const og = await page
      .locator('meta[property="og:image"]')
      .getAttribute("content", { timeout: 2000 });
    if (og && og.startsWith("http") && isRealImageUrl(og)) {
      console.log(`[browser/${source}] og:image found: ${og}`);
      return og;
    }
  } catch {}

  // 2. <img> tags — look for large vessel photos
  try {
    const imgs = await page.locator("img").all();
    for (const img of imgs) {
      const src = (await img.getAttribute("src")) ?? "";
      if (!src || !src.startsWith("http") || !isRealImageUrl(src)) continue;
      try {
        const box = await img.boundingBox({ timeout: 1000 } as any);
        if (box && box.width > 100 && box.height > 100) {
          console.log(`[browser/${source}] large img: ${src} (${box.width}x${box.height})`);
          return src;
        }
      } catch {
        if (IMG_EXT_RE.test(src)) {
          console.log(`[browser/${source}] img with extension: ${src}`);
          return src;
        }
      }
    }
  } catch {}

  // 3. Background images on photo containers
  try {
    const selectors = [
      ".vessel-photo",
      ".ship-photo",
      ".main-photo",
      "[class*='photo']",
      "[class*='image']",
    ];
    for (const selector of selectors) {
      const els = await page.locator(selector).all();
      for (const el of els.slice(0, 3)) {
        const bg: string = await el.evaluate(
          (e: Element) => getComputedStyle(e).backgroundImage
        );
        if (bg && bg.startsWith("url(")) {
          const url = bg.slice(4, -1).replace(/["']/g, "");
          if (url.startsWith("http") && isRealImageUrl(url)) {
            console.log(`[browser/${source}] bg-image: ${url}`);
            return url;
          }
        }
      }
    }
  } catch {}

  return null;
}

/**
 * Launch headless Chromium, visit each maritime site, wait for JS to render,
 * and extract vessel image URLs.
 *
 * Returns the first image URL found, or null if all sites fail.
 */
export async function browserScrape(
  mmsi: string,
  name: string,
  timeoutPerSite = 15_000
): Promise<string | null> {
  let chromium: typeof import("playwright").chromium;
  try {
    const pw = await import("playwright");
    chromium = pw.chromium;
  } catch {
    console.warn("Playwright not installed — browser fallback unavailable");
    return null;
  }

  const sites = buildUrls(mmsi, name);
  let resultUrl: string | null = null;

  const browser: Browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    userAgent:
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " +
      "AppleWebKit/537.36 (KHTML, like Gecko) " +
      "Chrome/124.0.0.0 Safari/537.36",
    viewport: { width: 1920, height: 1080 },
    locale: "en-US",
  });

  try {
    for (const site of sites) {
      if (resultUrl) break;

      const page = await context.newPage();
      try {
        console.log(`[browser/${site.source}] loading ${site.url}`);
        await page.goto(site.url, {
          waitUntil: "domcontentloaded",
          timeout: timeoutPerSite,
        });
        // Wait for images to load via JS
        await page.waitForTimeout(3000);

        const url = await extractFromPage(page, site.source);
        if (url) resultUrl = url;
      } catch (err) {
        console.debug(`[browser/${site.source}] failed:`, err);
      } finally {
        await page.close();
      }
    }
  } finally {
    await browser.close();
  }

  return resultUrl;
}
