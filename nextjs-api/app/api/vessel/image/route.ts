import { NextRequest, NextResponse } from "next/server";
import { resolveImage } from "@/lib/resolve";

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

  if (mmsi.length < 7 || mmsi.length > 15) {
    return NextResponse.json(
      { detail: "MMSI must be 7-15 characters." },
      { status: 400 }
    );
  }

  try {
    const { bytes, contentType, cacheHit } = await resolveImage(mmsi, name);
    return new NextResponse(new Uint8Array(bytes), {
      status: 200,
      headers: {
        "Content-Type": contentType,
        "X-Cache": cacheHit ? "HIT" : "MISS",
        "X-Vessel-Name": name,
        "X-Vessel-MMSI": mmsi,
        "Cache-Control": "public, max-age=3600",
      },
    });
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : "Unknown error";
    const status = message.includes("No vessel image found") ? 404 : 502;
    return NextResponse.json({ detail: message }, { status });
  }
}
