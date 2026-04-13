#!/usr/bin/env node
/**
 * stress_test.mjs — Vessel Image API stress tester (Node.js)
 *
 * Usage:
 *   node scripts/stress_test.mjs                        # defaults: 3 req/s, 30s
 *   node scripts/stress_test.mjs --rps 5 --duration 60  # custom
 *   node scripts/stress_test.mjs --url http://server:3000
 */

import { parseArgs } from "node:util";

const { values: args } = parseArgs({
  options: {
    url: { type: "string", default: "http://127.0.0.1:3000" },
    rps: { type: "string", default: "3" },
    duration: { type: "string", default: "30" },
    warmup: { type: "string", default: "3" },
  },
});

const BASE_URL = args.url.replace(/\/$/, "");
const TARGET_RPS = parseFloat(args.rps);
const DURATION = parseInt(args.duration);
const WARMUP = parseInt(args.warmup);

const TEST_VESSELS = [
  { mmsi: "311052100", name: "ANASTASIA K" },
];

// ── Single request ──────────────────────────────────────────────────────────

async function singleRequest(vessel) {
  const url = `${BASE_URL}/api/vessel/image?mmsi=${vessel.mmsi}&name=${encodeURIComponent(vessel.name)}`;
  const t0 = performance.now();
  try {
    const res = await fetch(url, { signal: AbortSignal.timeout(30_000) });
    const elapsed = performance.now() - t0;
    const body = res.ok ? await res.arrayBuffer() : null;
    return {
      status: res.status,
      elapsedMs: elapsed,
      cacheHeader: res.headers.get("x-cache") ?? "UNKNOWN",
      imageBytes: body ? body.byteLength : 0,
      error: null,
    };
  } catch (err) {
    return {
      status: 0,
      elapsedMs: performance.now() - t0,
      cacheHeader: "UNKNOWN",
      imageBytes: 0,
      error: err.message?.slice(0, 80) ?? "unknown",
    };
  }
}

// ── Main ────────────────────────────────────────────────────────────────────

async function main() {
  // Health check
  process.stdout.write(`\n  Checking server at ${BASE_URL} ... `);
  try {
    const r = await fetch(`${BASE_URL}/api/health`, { signal: AbortSignal.timeout(5000) });
    if (r.ok) {
      console.log(`OK  ${JSON.stringify(await r.json())}`);
    } else {
      console.log(`WARNING: HTTP ${r.status}`);
    }
  } catch (err) {
    console.log(`\n  Cannot reach server: ${err.message}`);
    process.exit(1);
  }

  // Warm-up
  console.log(`\n  Warm-up (${WARMUP}s) - priming cache ...`);
  for (const v of TEST_VESSELS) {
    const res = await singleRequest(v);
    const icon = res.status === 200 ? "OK" : "FAIL";
    console.log(`    ${icon} ${v.name.padEnd(20)}  HTTP ${res.status}  ${res.elapsedMs.toFixed(0)}ms  cache=${res.cacheHeader}`);
  }
  if (WARMUP > 0) await new Promise((r) => setTimeout(r, WARMUP * 1000));

  // Main loop
  console.log(`\n  Running ${TARGET_RPS} req/s for ${DURATION}s ...`);
  const interval = 1000 / TARGET_RPS;
  const results = [];
  const pending = [];
  let seq = 0;
  const deadline = Date.now() + DURATION * 1000;
  const startTime = Date.now();
  let lastPrint = Date.now();
  const printInterval = Math.max(1000, (DURATION / 10) * 1000);

  while (Date.now() < deadline) {
    const tick = Date.now();
    const vessel = TEST_VESSELS[seq % TEST_VESSELS.length];
    seq++;
    pending.push(singleRequest(vessel));

    const now = Date.now();
    if (now - lastPrint >= printInterval) {
      console.log(`    ... ${seq} sent, ${((now - startTime) / 1000).toFixed(0)}s elapsed`);
      lastPrint = now;
    }

    const sleepFor = Math.max(0, interval - (Date.now() - tick));
    if (sleepFor > 0) await new Promise((r) => setTimeout(r, sleepFor));
  }

  console.log(`  Done dispatching ${seq} requests. Waiting for stragglers ...`);
  const settled = await Promise.allSettled(pending);
  for (const s of settled) {
    if (s.status === "fulfilled") results.push(s.value);
  }

  // Report
  const total = results.length;
  const successes = results.filter((r) => r.status === 200);
  const failures = results.filter((r) => r.status !== 200);
  const cacheHits = successes.filter((r) => r.cacheHeader === "HIT").length;
  const actualRps = total / DURATION;
  const successRate = total > 0 ? successes.length / total : 0;

  const lats = successes.map((r) => r.elapsedMs).sort((a, b) => a - b);
  const pct = (p) => lats[Math.min(Math.floor(lats.length * p), lats.length - 1)] ?? 0;
  const avg = lats.length > 0 ? lats.reduce((a, b) => a + b, 0) / lats.length : 0;

  const w = 62;
  const sep = "=".repeat(w);
  console.log(`\n  ${sep}`);
  console.log(`  Vessel Image API - Stress Test Report`);
  console.log(`  ${sep}`);
  console.log(`  Target RPS : ${TARGET_RPS}`);
  console.log(`  Duration   : ${DURATION}s`);
  console.log(`  Total sent         : ${total}`);
  console.log(`  Successful (200)   : ${successes.length}  (${(successRate * 100).toFixed(1)}%)`);
  console.log(`  Failed             : ${failures.length}`);
  console.log(`  Cache HITs         : ${cacheHits}`);
  console.log(`  Actual RPS         : ${actualRps.toFixed(2)}`);

  if (lats.length > 0) {
    console.log(`\n  Latency (ms)`);
    console.log(`  Min      : ${lats[0].toFixed(1)}`);
    console.log(`  Avg      : ${avg.toFixed(1)}`);
    console.log(`  Median   : ${pct(0.5).toFixed(1)}`);
    console.log(`  p95      : ${pct(0.95).toFixed(1)}`);
    console.log(`  p99      : ${pct(0.99).toFixed(1)}`);
    console.log(`  Max      : ${lats[lats.length - 1].toFixed(1)}`);
  }

  const passed = actualRps >= TARGET_RPS * 0.9 && successRate >= 0.95;
  console.log(`\n  ${passed ? "PASSED" : "FAILED"}  -  ${actualRps.toFixed(2)} req/s  |  ${(successRate * 100).toFixed(1)}% success\n`);

  if (failures.length > 0) {
    console.log(`  Errors (first 10):`);
    for (const r of failures.slice(0, 10)) {
      console.log(`    HTTP ${r.status}  ${r.error ?? "(no detail)"}`);
    }
  }
}

main();
