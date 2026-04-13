import { NextResponse } from "next/server";
import { imageCache } from "@/lib/cache";

export async function GET() {
  return NextResponse.json({
    status: "ok",
    cache_entries: imageCache.keys().length,
    cache_max: 1000,
    cache_ttl_seconds: 3600,
  });
}
