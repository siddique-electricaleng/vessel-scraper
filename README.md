# Vessel Image Scraper API

REST API that fetches vessel/ship photos from maritime tracking sites using MMSI and vessel name. Returns raw image binary for easy serving. Available in **Python (FastAPI)** and **Next.js (TypeScript)** implementations.

---

## Quick Start

### Python (FastAPI) — runs on `main` branch

```bash
# 1. Install
pip install -r requirements.txt

# 2. Start server (port 8000)
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4

# 3. Test
curl "http://127.0.0.1:8000/vessel/image?mmsi=311052100&name=ANASTASIA+K" -o ship.jpg
```

### Next.js (TypeScript) — runs on `nextjs-api` branch

```bash
# 1. Install
cd nextjs-api
npm install

# 2. Start server (port 3000)
npm run dev

# 3. Test
curl "http://127.0.0.1:3000/api/vessel/image?mmsi=311052100&name=ANASTASIA+K" -o ship.jpg
```

---

## API Endpoints

Both implementations expose the same endpoints. Only the base URL and path prefix differ.

| Endpoint | Python URL | Next.js URL | Method | Description |
|---|---|---|---|---|
| Vessel Image | `/vessel/image?mmsi=X&name=Y` | `/api/vessel/image?mmsi=X&name=Y` | GET | Returns raw image bytes |
| Health Check | `/health` | `/api/health` | GET | Cache stats and status |
| Debug | `/vessel/debug?mmsi=X&name=Y` | `/api/vessel/debug?mmsi=X&name=Y` | GET | Per-scraper diagnostic info |
| Clear Cache | `/cache` | `/api/cache` | DELETE | Flush the image cache |
| Swagger Docs | `/docs` | N/A | GET | Interactive API docs (Python only) |

### Query Parameters

| Parameter | Required | Description | Example |
|---|---|---|---|
| `mmsi` | Yes | 7-15 digit MMSI number | `311052100` |
| `name` | Yes | Vessel name | `ANASTASIA K` |

### Response Headers

| Header | Values | Meaning |
|---|---|---|
| `X-Cache` | `HIT` / `MISS` | Whether result came from cache |
| `X-Vessel-Name` | string | Echoes the vessel name |
| `X-Vessel-MMSI` | string | Echoes the MMSI |
| `Cache-Control` | `public, max-age=3600` | Browser/CDN can cache for 1 hour |

---

## CLI Scripts

### Stress Test (target: 3 requests/second)

**Python:**
```bash
python stress_test.py --rps 3 --duration 30
python stress_test.py --rps 5 --duration 60 --url http://server:8000
```

**Next.js:**
```bash
node scripts/stress_test.mjs --rps 3 --duration 30
node scripts/stress_test.mjs --rps 5 --duration 60 --url http://server:3000
```

| Flag | Default | Description |
|---|---|---|
| `--rps` | `3` | Target requests per second |
| `--duration` | `30` | Test duration in seconds |
| `--warmup` | `3` | Warm-up pause after cache priming |
| `--url` | `http://127.0.0.1:8000` (py) / `:3000` (js) | API base URL |

### Bulk Download from CSV

Prepare a CSV file with `mmsi` and `name` columns:

```csv
mmsi,name
311052100,ANASTASIA K
235113366,QUEEN MARY 2
636092799,MSC OSCAR
```

**Python:**
```bash
python bulk_download.py --csv ships.csv --out images/ --concurrency 2 --delay 2
```

**Next.js:**
```bash
node scripts/bulk_download.mjs --csv ships.csv --out images/ --concurrency 2 --delay 2
```

| Flag | Default | Description |
|---|---|---|
| `--csv` | `ships.csv` | Path to input CSV file |
| `--out` | `images/` | Output directory for downloaded images |
| `--concurrency` | `2` | Max parallel downloads |
| `--delay` | `1` | Seconds between dispatching requests |
| `--retries` | `3` | Retry attempts per failed vessel |
| `--report` | `report.csv` | Output CSV with success/fail log |
| `--url` | `http://127.0.0.1:8000` (py) / `:3000` (js) | API base URL |

Output images are saved as `{VESSEL_NAME}_{MMSI}.{ext}` (e.g. `ANASTASIA_K_311052100.jpg`).

A `report.csv` is generated with the status of every vessel:

```csv
mmsi,name,status,filepath,size_bytes,elapsed_ms,error
311052100,ANASTASIA K,OK,images/ANASTASIA_K_311052100.png,34985,2,
229976000,EVER GIVEN,FAIL,,0,5000,HTTP 404
```

---

## Rate Limiting

Maritime sites (VesselFinder, MarineTraffic, etc.) will block your IP if you hit them too fast.

**Built-in protections:**
- `SourceThrottle` enforces a 2-second minimum gap between requests to the same source
- Bulk download staggers requests with `--delay`
- Failed requests retry with exponential backoff

**Recommended settings for large jobs (1000+ vessels):**
```bash
# Conservative — won't get blocked
python bulk_download.py --csv vessels.csv --concurrency 2 --delay 3

# Moderate — faster but riskier
python bulk_download.py --csv vessels.csv --concurrency 3 --delay 1
```

**If you get blocked:** Wait 1-2 hours for the ban to lift. The rate limiter will prevent it from happening again.

**Performance vs. safety:**
- Cache HITs serve instantly at 3+ req/s (stress test proven)
- Cache MISSES are throttled to avoid bans
- First bulk download is slow; subsequent requests are fast from cache

---

## Architecture

```
Python version (main branch)
├── main.py              FastAPI app, endpoints, caching, image download
├── scrapers.py          Per-source scraping logic + rate limiter
├── stress_test.py       3 req/s stress tester
├── bulk_download.py     CSV bulk image downloader
├── requirements.txt     Python dependencies
└── ships.csv            Sample vessel list

Next.js version (nextjs-api branch)
└── nextjs-api/
    ├── app/api/
    │   ├── vessel/image/route.ts   GET /api/vessel/image
    │   ├── vessel/debug/route.ts   GET /api/vessel/debug
    │   ├── health/route.ts         GET /api/health
    │   └── cache/route.ts          DELETE /api/cache
    ├── lib/
    │   ├── scrapers.ts             VesselFinder, MarineTraffic, etc.
    │   ├── throttle.ts             Per-source rate limiter
    │   ├── cache.ts                In-memory TTL cache (node-cache)
    │   ├── extract.ts              HTML image URL extractor
    │   ├── resolve.ts              Orchestrates scrapers + download
    │   └── constants.ts            Trusted hosts, headers
    └── scripts/
        ├── stress_test.mjs         Stress tester
        └── bulk_download.mjs       CSV bulk downloader
```

---

## Image Sources

The scraper tries these sources in order (all run concurrently, first success wins):

1. **MarineTraffic** — `marinetraffic.com`
2. **VesselFinder** — `vesselfinder.com` (most reliable, extracts IMO for direct CDN URL)
3. **FleetMon** — `fleetmon.com`
4. **VesselTracker** — `vesseltracker.com`
5. **ShipSpotting** — `shipspotting.com`
6. **MyShipTracking** — `myshiptracking.com`

---

## Example Vessels for Testing

| Vessel | MMSI | Status |
|---|---|---|
| ANASTASIA K | `311052100` | Works |
| QUEEN MARY 2 | `235113366` | Works |
| HARMONY OF THE SEAS | `310627000` | Works |
| MSC OSCAR | `636092799` | Works |
