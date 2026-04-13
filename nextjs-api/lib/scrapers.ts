import { BASE_HEADERS } from "./constants";
import { extractImageUrl } from "./extract";
import { throttle } from "./throttle";

type ExtractFn = (html: string) => string | null;

async function getHtml(
  url: string,
  source: string,
  referer = ""
): Promise<string | null> {
  await throttle.wait(source);
  try {
    const headers: Record<string, string> = { ...BASE_HEADERS };
    if (referer) headers["Referer"] = referer;
    const res = await fetch(url, {
      headers,
      redirect: "follow",
      signal: AbortSignal.timeout(12_000),
    });
    if (res.ok) return await res.text();
  } catch {}
  return null;
}

// ── VesselFinder ────────────────────────────────────────────────────────────

export async function scrapeVesselfinder(
  mmsi: string,
  name: string,
  extract: ExtractFn = extractImageUrl
): Promise<string | null> {
  const baseUrl = `https://www.vesselfinder.com/vessels/details/${mmsi}`;
  const html = await getHtml(baseUrl, "vesselfinder", "https://www.vesselfinder.com/");
  if (!html) return null;

  // 1. Extract IMO from embedded JS var
  const imoMatch = html.match(/vu_imo\s*=\s*(\d{5,9})/);
  if (imoMatch) {
    const imo = imoMatch[1];
    const directUrl = `https://static.vesselfinder.net/ship-photo/${imo}-${mmsi}/0`;
    try {
      const probe = await fetch(directUrl, {
        method: "HEAD",
        headers: { Referer: "https://www.vesselfinder.com/" },
        signal: AbortSignal.timeout(8_000),
      });
      const ct = probe.headers.get("content-type") ?? "";
      if (probe.ok && ct.startsWith("image/")) {
        return directUrl;
      }
    } catch {}
  }

  // 2. data-src ship-photo URLs matching this MMSI
  const photoRe = /https:\/\/static\.vesselfinder\.net\/ship-photo\/[^\s"'<>?]+/g;
  let match;
  while ((match = photoRe.exec(html)) !== null) {
    if (match[0].includes(mmsi)) return match[0];
  }

  // 3. Generic extraction
  return extract(html);
}

// ── MarineTraffic ───────────────────────────────────────────────────────────

export async function scrapeMarinetraffic(
  mmsi: string,
  name: string,
  extract: ExtractFn = extractImageUrl
): Promise<string | null> {
  const base = "https://www.marinetraffic.com";
  const detailUrl = `${base}/en/ais/details/ships/mmsi:${mmsi}`;
  const html = await getHtml(detailUrl, "marinetraffic", base + "/");
  if (html) {
    const img = extract(html);
    if (img) return img;
  }

  const galleryUrl = `${base}/en/photos/of/ships/mmsi:${mmsi}`;
  const gHtml = await getHtml(galleryUrl, "marinetraffic", detailUrl);
  if (gHtml) {
    const img = extract(gHtml);
    if (img) return img;
  }
  return null;
}

// ── FleetMon ────────────────────────────────────────────────────────────────

export async function scrapeFleetmon(
  mmsi: string,
  name: string,
  extract: ExtractFn = extractImageUrl
): Promise<string | null> {
  const slug = name.toLowerCase().replace(/ /g, "-");
  const url = `https://www.fleetmon.com/vessels/${slug}/${mmsi}/`;
  const html = await getHtml(url, "fleetmon", "https://www.fleetmon.com/");
  if (html) return extract(html);
  return null;
}

// ── VesselTracker ───────────────────────────────────────────────────────────

export async function scrapeVesseltracker(
  mmsi: string,
  name: string,
  extract: ExtractFn = extractImageUrl
): Promise<string | null> {
  const slug = name
    .split(" ")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase())
    .join("-");
  const url = `https://www.vesseltracker.com/en/Ships/${slug}-${mmsi}.html`;
  const html = await getHtml(url, "vesseltracker", "https://www.vesseltracker.com/");
  if (html) return extract(html);
  return null;
}

// ── ShipSpotting ────────────────────────────────────────────────────────────

export async function scrapeShipspotting(
  mmsi: string,
  name: string,
  extract: ExtractFn = extractImageUrl
): Promise<string | null> {
  const query = name.replace(/ /g, "+");
  const url = `https://www.shipspotting.com/photos/gallery?ship_name=${query}`;
  const html = await getHtml(url, "shipspotting", "https://www.shipspotting.com/");
  if (html) return extract(html);
  return null;
}

// ── MyShipTracking ──────────────────────────────────────────────────────────

export async function scrapeMyshiptracking(
  mmsi: string,
  name: string,
  extract: ExtractFn = extractImageUrl
): Promise<string | null> {
  const url = `https://www.myshiptracking.com/vessels?mmsi=${mmsi}`;
  const html = await getHtml(url, "myshiptracking", "https://www.myshiptracking.com/");
  if (html) return extract(html);
  return null;
}

// ── Registry ────────────────────────────────────────────────────────────────

export const ALL_SCRAPERS = [
  scrapeMarinetraffic,
  scrapeVesselfinder,
  scrapeFleetmon,
  scrapeVesseltracker,
  scrapeShipspotting,
  scrapeMyshiptracking,
];

// ── Probe HTML (for debug endpoint) ─────────────────────────────────────────

export async function probeHtml(
  mmsi: string,
  name: string
): Promise<Record<string, { url: string; htmlSnippet: string | null; length: number }>> {
  const slug = name.toLowerCase().replace(/ /g, "-");
  const titleSlug = name
    .split(" ")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase())
    .join("-");

  const urls: Record<string, string> = {
    marinetraffic: `https://www.marinetraffic.com/en/ais/details/ships/mmsi:${mmsi}`,
    vesselfinder: `https://www.vesselfinder.com/vessels/details/${mmsi}`,
    fleetmon: `https://www.fleetmon.com/vessels/${slug}/${mmsi}/`,
    vesseltracker: `https://www.vesseltracker.com/en/Ships/${titleSlug}-${mmsi}.html`,
    shipspotting: `https://www.shipspotting.com/photos/gallery?ship_name=${name.replace(/ /g, "+")}`,
    myshiptracking: `https://www.myshiptracking.com/vessels?mmsi=${mmsi}`,
  };

  const result: Record<string, { url: string; htmlSnippet: string | null; length: number }> = {};
  for (const [source, url] of Object.entries(urls)) {
    const html = await getHtml(url, source);
    result[source] = html
      ? { url, htmlSnippet: html.slice(0, 600), length: html.length }
      : { url, htmlSnippet: null, length: 0 };
  }
  return result;
}
