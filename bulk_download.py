"""
bulk_download.py — Bulk vessel image downloader
-------------------------------------------------
Reads a CSV of vessels (mmsi, name) and downloads images via the
Vessel Image API, saving them with meaningful filenames.

Usage:
    python bulk_download.py                                # defaults
    python bulk_download.py --csv ships.csv --out images/  # custom
    python bulk_download.py --concurrency 10 --retries 3   # tuning
    python bulk_download.py --url http://remote:8000       # remote API

CSV format (header required):
    mmsi,name
    311052100,ANASTASIA K
    235113366,QUEEN MARY 2

Output:
    images/ANASTASIA_K_311052100.jpg
    images/QUEEN_MARY_2_235113366.jpg
    report.csv  (success/fail log for every vessel)
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

# Fix Windows console encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ── Models ───────────────────────────────────────────────────────────────────

@dataclass
class Vessel:
    mmsi: str
    name: str

    @property
    def safe_name(self) -> str:
        """Filesystem-safe version of the vessel name."""
        return re.sub(r"[^\w\-]", "_", self.name.strip()).strip("_")

    @property
    def filename_stem(self) -> str:
        return f"{self.safe_name}_{self.mmsi}"


@dataclass
class DownloadResult:
    vessel: Vessel
    success: bool
    filepath: Optional[str] = None
    size_bytes: int = 0
    elapsed_ms: float = 0
    error: Optional[str] = None
    cached: bool = False


# ── CSV reader ───────────────────────────────────────────────────────────────

def read_vessels_csv(path: str) -> list[Vessel]:
    vessels = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        # Normalize header names
        if reader.fieldnames:
            reader.fieldnames = [h.strip().lower() for h in reader.fieldnames]
        for row in reader:
            mmsi = (row.get("mmsi") or "").strip()
            name = (row.get("name") or row.get("vessel_name") or "").strip()
            if mmsi and name:
                vessels.append(Vessel(mmsi=mmsi, name=name))
    return vessels


# ── Extension from content-type ──────────────────────────────────────────────

_CT_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/avif": ".avif",
    "image/gif": ".gif",
}


def _ext_from_ct(content_type: str) -> str:
    ct = content_type.split(";")[0].strip().lower()
    return _CT_TO_EXT.get(ct, ".jpg")


# ── Single download ─────────────────────────────────────────────────────────

async def download_one(
    client: httpx.AsyncClient,
    base_url: str,
    vessel: Vessel,
    out_dir: Path,
    retries: int,
) -> DownloadResult:
    url = f"{base_url}/vessel/image"
    params = {"mmsi": vessel.mmsi, "name": vessel.name}

    last_err = ""
    for attempt in range(1, retries + 1):
        t0 = time.perf_counter()
        try:
            r = await client.get(url, params=params, timeout=30.0)
            elapsed = (time.perf_counter() - t0) * 1000

            if r.status_code == 200:
                ct = r.headers.get("content-type", "image/jpeg")
                ext = _ext_from_ct(ct)
                filename = f"{vessel.filename_stem}{ext}"
                filepath = out_dir / filename
                filepath.write_bytes(r.content)
                cached = r.headers.get("X-Cache", "") == "HIT"
                return DownloadResult(
                    vessel=vessel,
                    success=True,
                    filepath=str(filepath),
                    size_bytes=len(r.content),
                    elapsed_ms=elapsed,
                    cached=cached,
                )
            else:
                last_err = f"HTTP {r.status_code}"
        except httpx.TimeoutException:
            elapsed = (time.perf_counter() - t0) * 1000
            last_err = "TIMEOUT"
        except Exception as exc:
            elapsed = (time.perf_counter() - t0) * 1000
            last_err = str(exc)[:120]

        # Back off before retry
        if attempt < retries:
            await asyncio.sleep(1.0 * attempt)

    return DownloadResult(
        vessel=vessel,
        success=False,
        elapsed_ms=elapsed,
        error=last_err,
    )


# ── Bulk orchestrator ────────────────────────────────────────────────────────

async def bulk_download(
    vessels: list[Vessel],
    base_url: str,
    out_dir: Path,
    concurrency: int,
    retries: int,
) -> list[DownloadResult]:
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[DownloadResult] = []
    semaphore = asyncio.Semaphore(concurrency)
    total = len(vessels)
    done_count = 0
    start_time = time.perf_counter()

    async def _worker(vessel: Vessel) -> DownloadResult:
        nonlocal done_count
        async with semaphore:
            result = await download_one(client, base_url, vessel, out_dir, retries)
        done_count += 1
        icon = "OK" if result.success else "FAIL"
        elapsed_total = time.perf_counter() - start_time
        rate = done_count / elapsed_total if elapsed_total > 0 else 0
        print(
            f"  [{done_count:>{len(str(total))}}/{total}] "
            f"{icon:4s}  {vessel.name:30s}  MMSI={vessel.mmsi}  "
            f"{result.elapsed_ms:>7.0f}ms  "
            f"({rate:.1f}/s)",
        )
        return result

    async with httpx.AsyncClient(
        follow_redirects=True,
        limits=httpx.Limits(max_connections=concurrency + 10, max_keepalive_connections=concurrency),
    ) as client:
        # Health check
        print(f"\n  Checking API at {base_url} ...", end=" ", flush=True)
        try:
            r = await client.get(f"{base_url}/health", timeout=5.0)
            if r.status_code == 200:
                print(f"OK")
            else:
                print(f"WARNING: HTTP {r.status_code}")
        except Exception as exc:
            print(f"\n  Cannot reach API: {exc}")
            print("  Start the server first: uvicorn main:app --host 0.0.0.0 --port 8000")
            sys.exit(1)

        print(f"  Downloading {total} vessel images (concurrency={concurrency}, retries={retries})\n")

        tasks = [asyncio.create_task(_worker(v)) for v in vessels]
        results = await asyncio.gather(*tasks)

    return list(results)


# ── Report ───────────────────────────────────────────────────────────────────

def write_report(results: list[DownloadResult], report_path: Path):
    with open(report_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["mmsi", "name", "status", "filepath", "size_bytes", "elapsed_ms", "error"])
        for r in results:
            writer.writerow([
                r.vessel.mmsi,
                r.vessel.name,
                "OK" if r.success else "FAIL",
                r.filepath or "",
                r.size_bytes,
                f"{r.elapsed_ms:.0f}",
                r.error or "",
            ])


def print_summary(results: list[DownloadResult], elapsed_s: float):
    total = len(results)
    ok = sum(1 for r in results if r.success)
    fail = total - ok
    total_bytes = sum(r.size_bytes for r in results)

    print(f"\n  {'='*56}")
    print(f"  BULK DOWNLOAD COMPLETE")
    print(f"  {'='*56}")
    print(f"  Total vessels:   {total}")
    print(f"  Successful:      {ok}  ({ok/total*100:.1f}%)" if total else "")
    print(f"  Failed:          {fail}")
    print(f"  Total size:      {total_bytes / 1024 / 1024:.1f} MB")
    print(f"  Wall time:       {elapsed_s:.1f}s")
    print(f"  Throughput:      {total / elapsed_s:.1f} vessels/s" if elapsed_s > 0 else "")
    print(f"  {'='*56}")

    if fail > 0:
        print(f"\n  Failed vessels:")
        for r in results:
            if not r.success:
                print(f"    {r.vessel.name:30s}  MMSI={r.vessel.mmsi}  error={r.error}")
    print()


# ── CLI ──────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Bulk download vessel images via the API")
    p.add_argument("--csv", default="ships.csv", help="Path to CSV file with mmsi,name columns (default: ships.csv)")
    p.add_argument("--out", default="images", help="Output directory for images (default: images/)")
    p.add_argument("--url", default="http://127.0.0.1:8000", help="Base URL of Vessel Image API")
    p.add_argument("--concurrency", type=int, default=5, help="Max concurrent downloads (default: 5)")
    p.add_argument("--retries", type=int, default=3, help="Retries per vessel on failure (default: 3)")
    p.add_argument("--report", default="report.csv", help="Output CSV report path (default: report.csv)")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()

    vessels = read_vessels_csv(args.csv)
    if not vessels:
        print(f"  No vessels found in {args.csv}. Expected CSV with columns: mmsi, name")
        sys.exit(1)

    print(f"  Loaded {len(vessels)} vessels from {args.csv}")

    t0 = time.perf_counter()
    results = asyncio.run(
        bulk_download(
            vessels=vessels,
            base_url=args.url.rstrip("/"),
            out_dir=Path(args.out),
            concurrency=args.concurrency,
            retries=args.retries,
        )
    )
    elapsed = time.perf_counter() - t0

    report_path = Path(args.report)
    write_report(results, report_path)
    print(f"  Report written to {report_path}")

    print_summary(results, elapsed)
