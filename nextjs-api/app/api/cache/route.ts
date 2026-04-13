import { NextResponse } from "next/server";
import { imageCache } from "@/lib/cache";

export async function DELETE() {
  const count = imageCache.keys().length;
  imageCache.flushAll();
  return NextResponse.json({ cleared: count });
}
