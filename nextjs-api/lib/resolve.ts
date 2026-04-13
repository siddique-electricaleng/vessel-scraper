import { BASE_HEADERS } from "./constants";
import { getCached, setCached } from "./cache";
import { ALL_SCRAPERS } from "./scrapers";

async function fetchImage(
  url: string
): Promise<{ bytes: Buffer; contentType: string } | null> {
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
    if (res.ok && ct.startsWith("image/")) {
      const bytes = Buffer.from(await res.arrayBuffer());
      return { bytes, contentType: ct };
    }
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

  // Run all scrapers concurrently
  const results = await Promise.allSettled(
    ALL_SCRAPERS.map((scraper) => scraper(mmsi, name))
  );

  const candidates: string[] = [];
  for (const r of results) {
    if (r.status === "fulfilled" && r.value && !candidates.includes(r.value)) {
      candidates.push(r.value);
    }
  }

  if (candidates.length === 0) {
    throw new Error(
      `No vessel image found for MMSI ${mmsi} / '${name}' across all sources.`
    );
  }

  // Try each candidate
  for (const imgUrl of candidates) {
    const data = await fetchImage(imgUrl);
    if (data) {
      setCached(cacheKey, data);
      return { ...data, cacheHit: false };
    }
  }

  throw new Error(
    "Image URLs found but all download attempts failed (sources may be rate-limiting)."
  );
}
