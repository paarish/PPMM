# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PPMM (Private Markets Monitor) is a personal financial markets dashboard hosted on a Synology NAS (DS716+, DSM 7.1.1) via Docker. Accessible on home WiFi from MacBook Pro, iPad Air, and iPhone 16 at `http://<nas-ip>:8000`.

## Architecture

```
ppmm/
├── backend/
│   ├── app.py              # Flask + APScheduler backend (single file)
│   ├── Dockerfile          # python:3.11-slim, gunicorn
│   ├── docker-compose.yml  # port 8000:5000, volume mounts
│   └── requirements.txt
├── config/                 # volume-mounted — edit without rebuild
│   ├── instruments.yaml    # all instrument definitions (add symbols here)
│   ├── settings.yaml       # schedule interval (default 300s)
│   └── gf_cookies.json     # GDPR bypass cookies for Google Finance
├── static/                 # volume-mounted — edit without rebuild
│   └── ppmm.html           # single-file React-less dashboard
├── data/                   # volume-mounted, gitignored
│   └── market_data.json    # written by backend fetch cycle
├── tests/
│   ├── conftest.py         # fixtures: gf_session (reads gf_cookies.json), fred_session
│   └── test_scraper.py     # 27 tests across all asset classes
└── tools/
    └── refresh_gf_cookies.py  # GDPR cookie refresh utility
```

## Data Flow

1. APScheduler triggers `fetch_market_data()` every 300s (configurable in `settings.yaml`)
2. Backend scrapes Google Finance HTML, extracts prices from `AF_initDataCallback` / `ds:2` JSON blob
3. US 2Y and UK 10Y bond yields fall back to FRED CSV (no GF page exists for these)
4. Results written to `data/market_data.json`
5. Frontend polls `/api/data` on page load and on the user-configured refresh interval
6. Frontend fetches `/api/config` once at startup to build instrument display lists dynamically

## Key Technical Details

- **GF scraping**: `AF_initDataCallback` → balanced-paren traversal → find `key: "ds:2"` block → regex `\[(-?[\d.Ee+\-]+),(-?[\d.Ee+\-]+),(-?[\d.Ee+\-]+),\d+,\d+,\d+\]`
- **GDPR cookies**: `CONSENT` + `SOCS` set on `.google.com` domain. Stored in `config/gf_cookies.json` (volume-mounted). Run `tools/refresh_gf_cookies.py` to renew.
- **Cookie expiry detection**: consent redirect to `consent.google.com` sets `_cookies_invalid = True`. Visible as `"cookies_valid": false` in `/api/health`.
- **GBX→GBP**: LSE stocks quoted in pence — use `scale: 0.01` in `instruments.yaml`
- **CBOE yield indices**: store value × 10 (TNX=44.55 → 4.455%) — use `scale: 0.1`
- **W00 suffix**: nearest-month continuous contract (CLW00, BZW00, etc.)
- **Staleness**: `age > 2 × fetch_interval` → `"stale": true` in `/api/health`; amber dot on dashboard
- **Scheduler under gunicorn**: started at module import time (not inside `if __name__ == "__main__"`)

## Common Commands

```bash
# Run tests (from repo root)
pytest tests/ -v --tb=short

# Run a single test class
pytest tests/test_scraper.py::TestStocks -v

# Local Docker build and smoke test
docker compose -f backend/docker-compose.yml up --build -d
curl http://localhost:8000/api/health
docker compose -f backend/docker-compose.yml down

# Check/refresh GF cookies
python3 tools/refresh_gf_cookies.py --check
python3 tools/refresh_gf_cookies.py
```

## NAS Deployment

```bash
# Deploy update (on NAS, requires sudo)
cd /volume1/docker/ppmm
git pull

# If app.py, Dockerfile, or requirements changed:
sudo docker-compose -f backend/docker-compose.yml up -d --build

# Config or static changes only (no rebuild):
sudo docker-compose -f backend/docker-compose.yml restart ppmm

# Logs
sudo docker-compose -f backend/docker-compose.yml logs --tail=50 ppmm
```

## Adding Instruments

Edit `config/instruments.yaml` — no rebuild needed, just restart the container. The frontend reads `/api/config` at startup and renders whatever is in the config dynamically.

Google Finance ticker formats:
- Indices: `TICKER:EXCHANGE` (e.g. `.INX:INDEXSP`)
- FX: `BASE-QUOTE` (e.g. `GBP-USD`)
- Futures: `CODEW00:EXCHANGE` (e.g. `CLW00:NYMEX`)
- Stocks: `TICKER:EXCHANGE` (e.g. `AAPL:NASDAQ`, `BARC:LON`)
- Crypto: `BASE-USD` (e.g. `BTC-USD`)
- CBOE yields: `CODE:INDEXCBOE` (e.g. `TNX:INDEXCBOE`)

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /` | Dashboard (`static/ppmm.html`) |
| `GET /api/data` | Latest `market_data.json` (202 if first fetch in progress) |
| `GET /api/health` | `{status, last_fetch, stale, cookies_valid, symbols_loaded, fetch_interval}` |
| `GET /api/config` | Merged instruments + settings (used by frontend to build display lists) |

## Known Constraints

- FRED (US 2Y, UK 10Y) times out from Mac Docker Desktop — works correctly on NAS
- TTF Gas (`TTFW00:NYMEX`) shows extreme % change on contract-roll day — price is correct
- NAS uses docker-compose v1 (`docker-compose` with hyphen, not `docker compose`)
- NAS requires `sudo` to run docker-compose commands
