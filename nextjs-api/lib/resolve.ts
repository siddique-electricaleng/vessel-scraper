import { BASE_HEADERS } from "./constants";
import { getCached, setCached } from "./cache";
import { ALL_SCRAPERS } from "./scrapers";

// URL substrings that indicate a placeholder / default "no photo" image
const PLACEHOLDER_SUBSTRINGS = [
  "cool-ship",         // VesselFinder: cool-ship2@2.png
  "placeholder.svg",   // VesselFinder: generic placeholder
  "placeholder.",       // Generic placeholder pattern
  "gen_img_ship",      // VesselTracker: generic ship silhouette
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

const MIN_IMAGE_BYTES = 5_000;  // Real photos are 50KB+; placeholders are < 5KB
const MIN_IMAGE_WIDTH = 300;    // Real vessel photos are at least 300px wide
const MIN_IMAGE_HEIGHT = 200;   // Real vessel photos are at least 200px tall

function isPlaceholderUrl(url: string): boolean {
  const lower = url.toLowerCase();
  return PLACEHOLDER_SUBSTRINGS.some((p) => lower.includes(p));
}

/** Parse image dimensions from raw bytes (JPEG, PNG, WebP — no dependencies). */
function getImageDimensions(buf: Buffer): { width: number; height: number } | null {
  // PNG: bytes 16-23 contain width (4B) and height (4B) as big-endian uint32
  if (buf[0] === 0x89 && buf[1] === 0x50 && buf[2] === 0x4e && buf[3] === 0x47) {
    if (buf.length < 24) return null;
    return { width: buf.readUInt32BE(16), height: buf.readUInt32BE(20) };
  }

  // JPEG: scan for SOF0/SOF2 markers (0xFFC0 / 0xFFC2) which contain dimensions
  if (buf[0] === 0xff && buf[1] === 0xd8) {
    let offset = 2;
    while (offset < buf.length - 9) {
      if (buf[offset] !== 0xff) { offset++; continue; }
      const marker = buf[offset + 1];
      // SOF0, SOF1, SOF2, SOF3
      if (marker >= 0xc0 && marker <= 0xc3) {
        const height = buf.readUInt16BE(offset + 5);
        const width = buf.readUInt16BE(offset + 7);
        return { width, height };
      }
      // Skip to next marker
      const segLen = buf.readUInt16BE(offset + 2);
      offset += 2 + segLen;
    }
    return null;
  }

  // WebP: RIFF header, "WEBP" at byte 8, then VP8 chunk has dimensions
  if (buf.length > 30 && buf.toString("ascii", 0, 4) === "RIFF" && buf.toString("ascii", 8, 12) === "WEBP") {
    const chunk = buf.toString("ascii", 12, 16);
    if (chunk === "VP8 " && buf.length > 30) {
      // Lossy VP8: width at 26, height at 28 (little-endian 16-bit, lower 14 bits)
      const width = buf.readUInt16LE(26) & 0x3fff;
      const height = buf.readUInt16LE(28) & 0x3fff;
      return { width, height };
    }
    if (chunk === "VP8L" && buf.length > 25) {
      // Lossless VP8L: dimensions packed in bits 21-24
      const b0 = buf[21], b1 = buf[22], b2 = buf[23], b3 = buf[24];
      const width = ((b0 | (b1 << 8)) & 0x3fff) + 1;
      const height = ((((b1 >> 6) | (b2 << 2) | (b3 << 10)) & 0x3fff)) + 1;
      return { width, height };
    }
  }

  return null;
}

async function fetchImage(
  url: string
): Promise<{ bytes: Buffer; contentType: string } | null> {
  if (isPlaceholderUrl(url)) return null;
  try {
    const res = await fetch(url, {
      headers: {
        ...BASE_HEADERS,
        Accept: "image/avif,image/webp,image/*,*/*;q=0.8",
        Referer: `https://${new URL(url).hostname}/`,
      },
      signal: AbortSignal.timeout(12_000),
    });
    const ct = (res.headers.get("content-type") ?? "image/jpeg").split(";")[0].trim();
    if (!res.ok || !ct.startsWith("image/")) return null;
    // Reject SVG — always a placeholder on vessel photo CDNs
    if (ct === "image/svg+xml") return null;
    const bytes = Buffer.from(await res.arrayBuffer());
    // Reject tiny images — placeholder icons, not real photos
    if (bytes.length < MIN_IMAGE_BYTES) return null;
    // Reject small dimensions — generic icons / wrong vessel thumbnails
    const dims = getImageDimensions(bytes);
    if (dims) {
      if (dims.width < MIN_IMAGE_WIDTH || dims.height < MIN_IMAGE_HEIGHT) {
        console.log(`Rejected ${url}: ${dims.width}x${dims.height} too small`);
        return null;
      }
      console.log(`Accepted ${url}: ${dims.width}x${dims.height}, ${bytes.length} bytes`);
    }
    return { bytes, contentType: ct };
  } catch {}
  return null;
}

export async function resolveImage(
  mmsi: string,
  name: string
): Promise<{ bytes: Buffer; contentType: string; cacheHit: boolean }> {
  const cacheKey = `${mmsi.trim()}:${name.trim().toUpperCase()}`;

  // Cache hit
  const cached = getCached(cacheKey);
  if (cached) {
    return { ...cached, cacheHit: true };
  }

  // Run HTTP scrapers and browser fallback in parallel
  const browserPromise = (async (): Promise<string | null> => {
    try {
      const { browserScrape } = await import("./browser-scraper");
      return await browserScrape(mmsi, name);
    } catch (err) {
      console.warn("Browser fallback failed:", err);
      return null;
    }
  })();

  const scraperResults = await Promise.allSettled(
    ALL_SCRAPERS.map((scraper) => scraper(mmsi, name))
  );

  const candidates: string[] = [];
  for (const r of scraperResults) {
    if (r.status === "fulfilled" && r.value && !candidates.includes(r.value)) {
      candidates.push(r.value);
    }
  }

  // Try each HTTP scraper candidate
  for (const imgUrl of candidates) {
    const data = await fetchImage(imgUrl);
    if (data) {
      setCached(cacheKey, data);
      browserPromise.catch(() => {});
      return { ...data, cacheHit: false };
    }
  }

  // Wait for browser fallback if HTTP scrapers failed
  const browserUrl = await browserPromise;
  if (browserUrl) {
    const data = await fetchImage(browserUrl);
    if (data) {
      console.log(`Image served via browser from ${browserUrl} (${data.bytes.length} bytes)`);
      setCached(cacheKey, data);
      return { ...data, cacheHit: false };
    }
  }

  // Nothing worked
  if (candidates.length > 0) {
    throw new Error(
      "Image URLs found but all download attempts failed (sources may be rate-limiting)."
    );
  }
  throw new Error(
    `No vessel image found for MMSI ${mmsi} / '${name}' across all sources.`
  );
}
