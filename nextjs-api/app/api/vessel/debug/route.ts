import { NextRequest, NextResponse } from "next/server";
import { ALL_SCRAPERS, probeHtml } from "@/lib/scrapers";

export async function GET(req: NextRequest) {
  const { searchParams } = req.nextUrl;
  const mmsi = searchParams.get("mmsi")?.trim();
  const name = searchParams.get("name")?.trim();

  if (!mmsi || !name) {
    return NextResponse.json(
      { detail: "Both 'mmsi' and 'name' query parameters are required." },
      { status: 400 }
    );
  }

  // Run each scraper and collect results
  const scraperResults = await Promise.all(
    ALL_SCRAPERS.map(async (scraper) => {
      const source = scraper.name.replace("scrape", "").replace(/^./, (c) => c.toLowerCase());
      try {
        const url = await scraper(mmsi, name);
        return { source, image_url: url, error: null };
      } catch (err: unknown) {
        return {
          source,
          image_url: null,
          error: err instanceof Error ? err.message : String(err),
        };
      }
    })
  );

  const htmlProbes = await probeHtml(mmsi, name);
  const found = scraperResults.filter((r) => r.image_url);

  return NextResponse.json({
    mmsi,
    name,
    found: found.length,
    scraper_results: scraperResults,
    html_probes: htmlProbes,
  });
}
