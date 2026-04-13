#!/usr/bin/env node
/**
 * bulk_download.mjs — Bulk vessel image downloader (Node.js)
 *
 * Usage:
 *   node scripts/bulk_download.mjs                                  # defaults
 *   node scripts/bulk_download.mjs --csv ships.csv --out images/    # custom
 *   node scripts/bulk_download.mjs --concurrency 3 --delay 2        # tuning
 */

import { createReadStream, mkdirSync, writeFileSync } from "node:fs";
import { createInterface } from "node:readline";
import { parseArgs } from "node:util";
import { join } from "node:path";

const { values: args } = parseArgs({
  options: {
    csv: { type: "string", default: "ships.csv" },
    out: { type: "string", default: "images" },
    url: { type: "string", default: "http://127.0.0.1:3000" },
    concurrency: { type: "string", default: "2" },
    retries: { type: "string", default: "3" },
    delay: { type: "string", default: "1" },
    report: { type: "string", default: "report.csv" },
  },
});

const BASE_URL = args.url.replace(/\/$/, "");
const CONCURRENCY = parseInt(args.concurrency);
const RETRIES = parseInt(args.retries);
const DELAY = parseFloat(args.delay) * 1000;
const OUT_DIR = args.out;
const REPORT_PATH = args.report;

// ── Read CSV ────────────────────────────────────────────────────────────────

async function readCsv(path) {
  const vessels = [];
  const rl = createInterface({ input: createReadStream(path, "utf-8") });
  let headers = null;

  for await (const raw of rl) {
    const line = raw.replace(/^\uFEFF/, "").trim();
    if (!line) continue;
    const cols = line.split(",").map((c) => c.trim());
    if (!headers) {
      headers = cols.map((h) => h.toLowerCase());
      continue;
    }
    const row = Object.fromEntries(headers.map((h, i) => [h, cols[i] ?? ""]));
    const mmsi = row.mmsi?.trim();
    const name = (row.name || row.vessel_name || "").trim();
    if (mmsi && name) vessels.push({ mmsi, name });
  }
  return vessels;
}

// ── Filename helpers ────────────────────────────────────────────────────────

function safeName(name) {
  return name.replace(/[^\w-]/g, "_").replace(/_+/g, "_").replace(/^_|_$/g, "");
}

const CT_TO_EXT = {
  "image/jpeg": ".jpg",
  "image/png": ".png",
  "image/webp": ".webp",
  "image/avif": ".avif",
  "image/gif": ".gif",
};

// ── Download one vessel ─────────────────────────────────────────────────────

async function downloadOne(vessel) {
  const url = `${BASE_URL}/api/vessel/image?mmsi=${vessel.mmsi}&name=${encodeURIComponent(vessel.name)}`;
  let lastErr = "";

  for (let attempt = 1; attempt <= RETRIES; attempt++) {
    const t0 = performance.now();
    try {
      const res = await fetch(url, { signal: AbortSignal.timeout(30_000) });
      const elapsed = performance.now() - t0;

      if (res.ok) {
        const ct = (res.headers.get("content-type") ?? "image/jpeg").split(";")[0].trim();
        const ext = CT_TO_EXT[ct] || ".jpg";
        const filename = `${safeName(vessel.name)}_${vessel.mmsi}${ext}`;
        const filepath = join(OUT_DIR, filename);
        const buf = Buffer.from(await res.arrayBuffer());
        writeFileSync(filepath, buf);
        return {
          success: true,
          filepath,
          sizeBytes: buf.length,
          elapsedMs: elapsed,
          cached: res.headers.get("x-cache") === "HIT",
          error: null,
        };
      }
      lastErr = `HTTP ${res.status}`;
    } catch (err) {
      lastErr = err.message?.slice(0, 120) ?? "unknown";
    }

    if (attempt < RETRIES) {
      await new Promise((r) => setTimeout(r, 1000 * attempt));
    }
  }

  return { success: false, filepath: null, sizeBytes: 0, elapsedMs: 0, cached: false, error: lastErr };
}

// ── Main ────────────────────────────────────────────────────────────────────

async function main() {
  const vessels = await readCsv(args.csv);
  if (vessels.length === 0) {
    console.log(`  No vessels found in ${args.csv}. Expected CSV with columns: mmsi, name`);
    process.exit(1);
  }

  mkdirSync(OUT_DIR, { recursive: true });
  console.log(`  Loaded ${vessels.length} vessels from ${args.csv}`);

  // Health check
  process.stdout.write(`\n  Checking API at ${BASE_URL} ... `);
  try {
    const r = await fetch(`${BASE_URL}/api/health`, { signal: AbortSignal.timeout(5000) });
    if (r.ok) console.log("OK");
    else console.log(`WARNING: HTTP ${r.status}`);
  } catch (err) {
    console.log(`\n  Cannot reach API: ${err.message}`);
    console.log("  Start the server first: npm run dev");
    process.exit(1);
  }

  const etaMin = (vessels.length * DELAY / 1000) / 60;
  console.log(`  Downloading ${vessels.length} vessel images (concurrency=${CONCURRENCY}, delay=${DELAY/1000}s)`);
  console.log(`  Estimated time: ~${etaMin.toFixed(0)} minutes\n`);

  const results = [];
  const total = vessels.length;
  let doneCount = 0;
  const startTime = performance.now();
  const pad = String(total).length;

  // Semaphore via simple queue
  let running = 0;
  const queue = [...vessels];
  const allDone = [];

  async function worker() {
    while (queue.length > 0) {
      const vessel = queue.shift();
      if (!vessel) break;
      const result = await downloadOne(vessel);
      doneCount++;
      const icon = result.success ? "OK" : "FAIL";
      const elapsed = (performance.now() - startTime) / 1000;
      const rate = doneCount / elapsed;
      console.log(
        `  [${String(doneCount).padStart(pad)}/${total}] ${icon.padEnd(4)}  ${vessel.name.padEnd(30)}  MMSI=${vessel.mmsi}  ${result.elapsedMs.toFixed(0).padStart(7)}ms  (${rate.toFixed(1)}/s)`
      );
      results.push({ vessel, ...result });
      await new Promise((r) => setTimeout(r, DELAY));
    }
  }

  const workers = Array.from({ length: CONCURRENCY }, () => worker());
  await Promise.all(workers);

  // Report CSV
  const reportLines = [
    "mmsi,name,status,filepath,size_bytes,elapsed_ms,error",
    ...results.map((r) =>
      [
        r.vessel.mmsi,
        r.vessel.name,
        r.success ? "OK" : "FAIL",
        r.filepath ?? "",
        r.sizeBytes,
        r.elapsedMs.toFixed(0),
        r.error ?? "",
      ].join(",")
    ),
  ];
  writeFileSync(REPORT_PATH, reportLines.join("\n") + "\n");
  console.log(`\n  Report written to ${REPORT_PATH}`);

  // Summary
  const ok = results.filter((r) => r.success).length;
  const fail = total - ok;
  const totalBytes = results.reduce((s, r) => s + r.sizeBytes, 0);
  const wallTime = (performance.now() - startTime) / 1000;

  console.log(`\n  ${"=".repeat(56)}`);
  console.log(`  BULK DOWNLOAD COMPLETE`);
  console.log(`  ${"=".repeat(56)}`);
  console.log(`  Total vessels:   ${total}`);
  console.log(`  Successful:      ${ok}  (${((ok / total) * 100).toFixed(1)}%)`);
  console.log(`  Failed:          ${fail}`);
  console.log(`  Total size:      ${(totalBytes / 1024 / 1024).toFixed(1)} MB`);
  console.log(`  Wall time:       ${wallTime.toFixed(1)}s`);
  console.log(`  Throughput:      ${(total / wallTime).toFixed(1)} vessels/s`);
  console.log(`  ${"=".repeat(56)}\n`);

  if (fail > 0) {
    console.log(`  Failed vessels:`);
    for (const r of results.filter((r) => !r.success)) {
      console.log(`    ${r.vessel.name.padEnd(30)}  MMSI=${r.vessel.mmsi}  error=${r.error}`);
    }
  }
}

main();
