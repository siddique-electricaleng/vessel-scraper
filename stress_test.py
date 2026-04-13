"""
stress_test.py — Vessel Image API stress tester
------------------------------------------------
Fires requests at a configurable rate (default 3 req/s) for a set duration,
then prints a detailed latency + success report.

Usage:
    python stress_test.py                          # defaults: 3 req/s, 30s
    python stress_test.py --rps 5 --duration 60   # custom
    python stress_test.py --url http://server:8000 # remote target

The first N seconds are a warm-up pass so the server can prime its cache.
The actual measurements start after warm-up.
"""

from __future__ import annotations

import os
import sys

# Force UTF-8 output on Windows to avoid charmap codec errors with Unicode symbols
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import argparse
import asyncio
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

# ── Test vessels ─────────────────────────────────────────────────────────────

TEST_VESSELS = [
    {"mmsi": "311052100", "name": "ANASTASIA K"},
    # Add more vessels here to exercise different cache keys
    # {"mmsi": "123456789", "name": "EXAMPLE SHIP"},
]

# ── Result model ─────────────────────────────────────────────────────────────


@dataclass
class RequestResult:
    status: int
    elapsed_ms: float
    cache_header: str = "UNKNOWN"
    image_bytes: int = 0
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == 200


@dataclass
class BenchmarkReport:
    target_rps: float
    duration_s: int
    results: list[RequestResult] = field(default_factory=list)

    # Computed properties
    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def successes(self) -> list[RequestResult]:
        return [r for r in self.results if r.ok]

    @property
    def failures(self) -> list[RequestResult]:
        return [r for r in self.results if not r.ok]

    @property
    def cache_hits(self) -> int:
        return sum(1 for r in self.successes if r.cache_header == "HIT")

    @property
    def actual_rps(self) -> float:
        return self.total / self.duration_s if self.duration_s else 0

    @property
    def success_rate(self) -> float:
        return len(self.successes) / self.total if self.total else 0

    def latency_stats(self) -> dict:
        lats = [r.elapsed_ms for r in self.successes]
        if not lats:
            return {}
        lats_sorted = sorted(lats)
        n = len(lats_sorted)
        return {
            "min": lats_sorted[0],
            "avg": statistics.mean(lats),
            "median": statistics.median(lats),
            "p75": lats_sorted[int(n * 0.75)],
            "p95": lats_sorted[int(n * 0.95)],
            "p99": lats_sorted[min(int(n * 0.99), n - 1)],
            "max": lats_sorted[-1],
            "stdev": statistics.stdev(lats) if n > 1 else 0,
        }

    def print(self):
        w = 62
        sep = "─" * w
        thick = "═" * w

        print(f"\n╔{thick}╗")
        print(f"║{'  Vessel Image API — Stress Test Report':^{w}}║")
        print(f"╚{thick}╝")

        print(f"\n  Target RPS : {self.target_rps:.1f}")
        print(f"  Duration   : {self.duration_s}s")
        print(f"\n{sep}")
        print(f"  {'Requests':30s} {'':>10s}")
        print(sep)
        print(f"  {'Total sent':<30s} {self.total:>10d}")
        print(
            f"  {'Successful (HTTP 200)':<30s} {len(self.successes):>10d}  ({self.success_rate*100:.1f}%)")
        print(
            f"  {'Failed':<30s} {len(self.failures):>10d}  ({(1-self.success_rate)*100:.1f}%)")
        print(f"  {'Cache HITs (of successes)':<30s} {self.cache_hits:>10d}")
        print(f"  {'Actual RPS achieved':<30s} {self.actual_rps:>10.2f}")

        stats = self.latency_stats()
        if stats:
            print(f"\n{sep}")
            print(f"  {'Latency (successful requests only)':<30s} {'ms':>10s}")
            print(sep)
            for label, key in [
                ("Min", "min"),
                ("Average", "avg"),
                ("Median (p50)", "median"),
                ("p75", "p75"),
                ("p95", "p95"),
                ("p99", "p99"),
                ("Max", "max"),
                ("Std dev", "stdev"),
            ]:
                print(f"  {label:<30s} {stats[key]:>10.1f}")

        if self.failures:
            print(f"\n{sep}")
            print(f"  Error breakdown (first 10):")
            print(sep)
            for r in self.failures[:10]:
                print(f"  HTTP {r.status:3d}  {r.error or '(no detail)'}")

        print(f"\n{sep}")
        passed = self.actual_rps >= self.target_rps * 0.90 and self.success_rate >= 0.95
        if passed:
            verdict = "✅  PASSED"
            detail = f"{self.actual_rps:.2f} req/s  |  {self.success_rate*100:.1f}% success"
        else:
            verdict = "❌  FAILED"
            issues = []
            if self.actual_rps < self.target_rps * 0.90:
                issues.append(
                    f"RPS too low ({self.actual_rps:.2f} < {self.target_rps * 0.9:.2f})")
            if self.success_rate < 0.95:
                issues.append(
                    f"success rate too low ({self.success_rate*100:.1f}% < 95%)")
            detail = " | ".join(issues)

        print(f"\n  {verdict}  —  {detail}\n")


# ── Request worker ────────────────────────────────────────────────────────────

async def single_request(
    client: httpx.AsyncClient,
    base_url: str,
    vessel: dict,
) -> RequestResult:
    url = f"{base_url}/vessel/image"
    params = {"mmsi": vessel["mmsi"], "name": vessel["name"]}
    t0 = time.perf_counter()
    try:
        r = await client.get(url, params=params, timeout=30.0)
        elapsed = (time.perf_counter() - t0) * 1000
        return RequestResult(
            status=r.status_code,
            elapsed_ms=elapsed,
            cache_header=r.headers.get("X-Cache", "UNKNOWN"),
            image_bytes=len(r.content) if r.status_code == 200 else 0,
        )
    except httpx.TimeoutException:
        elapsed = (time.perf_counter() - t0) * 1000
        return RequestResult(status=0, elapsed_ms=elapsed, error="TIMEOUT")
    except Exception as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        return RequestResult(status=0, elapsed_ms=elapsed, error=str(exc)[:80])


# ── Main benchmark loop ───────────────────────────────────────────────────────

async def run_benchmark(
    base_url: str,
    target_rps: float,
    duration_s: int,
    warmup_s: int,
) -> BenchmarkReport:
    report = BenchmarkReport(target_rps=target_rps, duration_s=duration_s)
    interval = 1.0 / target_rps

    async with httpx.AsyncClient(
        follow_redirects=True,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=30),
    ) as client:

        # ── Health check ──────────────────────────────────────────────────
        print(f"\n  Checking server at {base_url} …", end=" ", flush=True)
        try:
            r = await client.get(f"{base_url}/health", timeout=5.0)
            if r.status_code == 200:
                print(f"OK  {r.json()}")
            else:
                print(f"WARNING: HTTP {r.status_code}")
        except Exception as exc:
            print(f"\n  ❌ Cannot reach server: {exc}")
            sys.exit(1)

        # ── Warm-up: prime the cache ───────────────────────────────────────
        print(f"\n  Warm-up ({warmup_s}s) — priming cache …")
        warmup_tasks = []
        for v in TEST_VESSELS:
            t = asyncio.create_task(single_request(client, base_url, v))
            warmup_tasks.append(t)
        warmup_results = await asyncio.gather(*warmup_tasks, return_exceptions=True)
        for v, res in zip(TEST_VESSELS, warmup_results):
            if isinstance(res, RequestResult):
                status = "✓" if res.ok else "✗"
                print(
                    f"    {status} {v['name']:20s}  HTTP {res.status}  {res.elapsed_ms:.0f}ms  cache={res.cache_header}")
        if warmup_s > 0:
            await asyncio.sleep(warmup_s)

        # ── Main test loop ─────────────────────────────────────────────────
        print(f"\n  Running {target_rps:.1f} req/s for {duration_s}s …")
        pending: list[asyncio.Task] = []
        seq = 0
        deadline = time.perf_counter() + duration_s
        print_interval = max(1, duration_s // 10)
        last_print = time.perf_counter()

        while time.perf_counter() < deadline:
            tick = time.perf_counter()
            vessel = TEST_VESSELS[seq % len(TEST_VESSELS)]
            seq += 1
            task = asyncio.create_task(
                single_request(client, base_url, vessel))
            pending.append(task)

            # Periodic progress print
            now = time.perf_counter()
            if now - last_print >= print_interval:
                done = sum(1 for t in pending if t.done())
                print(
                    f"    … {seq} sent, {done} complete, {now - (deadline - duration_s):.0f}s elapsed")
                last_print = now

            # Sleep to maintain rate
            sleep_for = max(0.0, interval - (time.perf_counter() - tick))
            await asyncio.sleep(sleep_for)

        print(f"  ✓ All {seq} requests dispatched. Waiting for stragglers …")
        done_results = await asyncio.gather(*pending, return_exceptions=True)
        for res in done_results:
            if isinstance(res, RequestResult):
                report.results.append(res)

    return report


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Vessel Image API stress test")
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8000",
        help="Base URL of the API (default: http://127.0.0.1:8000)",
    )
    parser.add_argument(
        "--rps",
        type=float,
        default=3.0,
        help="Target requests per second (default: 3.0)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=30,
        help="Test duration in seconds (default: 30)",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=3,
        help="Warm-up pause after cache priming (default: 3s)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    report = asyncio.run(
        run_benchmark(
            base_url=args.url.rstrip("/"),
            target_rps=args.rps,
            duration_s=args.duration,
            warmup_s=args.warmup,
        )
    )
    report.print()
