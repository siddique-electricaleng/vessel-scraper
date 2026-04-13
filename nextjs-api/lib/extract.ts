import * as cheerio from "cheerio";
import { TRUSTED_IMAGE_HOSTS } from "./constants";

const IMG_URL_RE =
  /https?:\/\/[^\s'"<>\\]+\.(?:jpe?g|png|webp|avif|gif)(?:[^\s'"<>\\]*)?/gi;

const VF_PHOTO_RE =
  /https:\/\/static\.vesselfinder\.net\/ship-photo\/[0-9]+-[0-9]+(?:-[0-9a-f]+)?\/[0-3]/g;

function isTrusted(url: string): boolean {
  try {
    const host = new URL(url).hostname.toLowerCase().replace(/^www\./, "");
    if (TRUSTED_IMAGE_HOSTS.has(host)) return true;
    for (const t of TRUSTED_IMAGE_HOSTS) {
      if (host.endsWith("." + t)) return true;
    }
  } catch {}
  return false;
}

/**
 * Multi-strategy image URL extractor — works on both SSR and SPA pages.
 *
 * Priority:
 *   1. <meta og:image> / <meta twitter:image>
 *   2. Next.js __NEXT_DATA__ JSON
 *   3. Regex over <script> tags
 *   4a. VesselFinder ship-photo URLs (extensionless)
 *   4b. Regex over entire HTML
 *   5. <img data-src> / <img src>
 */
export function extractImageUrl(html: string): string | null {
  const $ = cheerio.load(html);

  // 1. Open Graph / Twitter meta tags
  for (const prop of ["og:image", "og:image:url", "og:image:secure_url"]) {
    const url = $(`meta[property="${prop}"]`).attr("content")?.trim();
    if (url && isTrusted(url)) return url;
  }
  const twitterImg = $('meta[name="twitter:image"]').attr("content")?.trim();
  if (twitterImg && isTrusted(twitterImg)) return twitterImg;

  // 2. Next.js __NEXT_DATA__
  const ndScript = $("#__NEXT_DATA__").html();
  if (ndScript) {
    try {
      const urls = findUrlsInObj(JSON.parse(ndScript));
      for (const url of urls) {
        if (isTrusted(url) && IMG_URL_RE.test(url)) return url;
      }
    } catch {}
  }

  // 3. Regex over <script> blocks
  const scripts = $("script").toArray();
  for (const el of scripts) {
    const text = $(el).html() || "";
    IMG_URL_RE.lastIndex = 0;
    let match;
    while ((match = IMG_URL_RE.exec(text)) !== null) {
      if (isTrusted(match[0])) return match[0];
    }
  }

  // 4a. VesselFinder ship-photo URLs
  VF_PHOTO_RE.lastIndex = 0;
  const vfMatch = VF_PHOTO_RE.exec(html);
  if (vfMatch) return vfMatch[0];

  // 4b. Regex over entire HTML
  IMG_URL_RE.lastIndex = 0;
  let imgMatch;
  while ((imgMatch = IMG_URL_RE.exec(html)) !== null) {
    if (isTrusted(imgMatch[0])) return imgMatch[0];
  }

  // 5. <img> attributes
  for (const attr of ["data-src", "data-lazy-src", "data-original", "data-url", "data-photo"]) {
    const url = $(`img[${attr}]`).first().attr(attr)?.trim();
    if (url && isTrusted(url)) return url;
  }
  const imgSrc = $("img").first().attr("src")?.trim();
  if (imgSrc && isTrusted(imgSrc)) return imgSrc;

  return null;
}

function findUrlsInObj(obj: unknown, depth = 0): string[] {
  if (depth > 12) return [];
  if (typeof obj === "string") return obj.startsWith("http") ? [obj] : [];
  if (Array.isArray(obj)) return obj.flatMap((v) => findUrlsInObj(v, depth + 1));
  if (obj && typeof obj === "object") {
    return Object.values(obj).flatMap((v) => findUrlsInObj(v, depth + 1));
  }
  return [];
}
