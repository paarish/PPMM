# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# PPMM — Private Markets Monitor
## Claude Code Project Brief

---

## Project Overview

PPMM is a personal financial markets dashboard built for use across MacBook Pro, iPad Air and iPhone 16. It displays live (or close/delayed) market data across global equities, FX, bonds, commodities and crypto.

The project is being built in two phases:

**Phase 1 (current)** — Local network only. Hosted on a Synology NAS via Docker. Accessible on home WiFi from any device via `http://nas-ip:5000`.

**Phase 2 (later)** — External access via a Raspberry Pi on the external router. Do not build Phase 2 yet.

---

## Your Mission

Work through the following steps in order. Do not move to the next step until the current one is complete and verified. Report back when everything is done.

---

## Step 1 — Environment Setup

```bash
# Verify Python 3.11+
python3 --version

# Install test dependencies
pip install -r tests/requirements-test.txt

# Verify pytest works
pytest --version
```

---

## Step 2 — Run the Full Test Suite

```bash
pytest tests/ -v --tb=short 2>&1 | tee test_results.txt
```

Review every failure. Categorise them as:
- **A) Symbol failures** — Finnhub returning no data for a symbol (fix by updating symbols.yaml with a working alt_symbol)
- **B) Network failures** — API unreachable (check connectivity, retry once)
- **C) Code bugs** — test logic errors (fix the test or the implementation)
- **D) Expected failures** — tests that require the NAS backend (mark with `@pytest.mark.skip(reason="requires NAS backend")` and document clearly)

---

## Step 3 — Fix All Symbol Failures

For any instrument where the primary symbol returns no data:
1. Check the `alt_symbol` field in `config/symbols.yaml`
2. Test the alt_symbol via a direct Finnhub API call
3. If alt_symbol works, update `symbols.yaml` to swap it in as the primary
4. If neither works, research alternative symbols (ETF equivalents are acceptable)
5. Re-run tests until all non-NAS tests pass

### Known issues to resolve:
- **Equity indices** — OANDA CFD symbols (`OANDA:SPX500_USD` etc) may be blocked on Finnhub free tier. Use ETF fallbacks: SPY (S&P 500), QQQ (NASDAQ), EWU (FTSE), EWG (DAX), EWJ (Nikkei), EWH (Hang Seng)
- **Commodities** — OANDA symbols (`OANDA:BRENT_USD`, `OANDA:WTICO_USD`) may be blocked. Use ETF fallbacks: BNO (Brent), USO (WTI)
- **VIX** — `^VIX` may not work. Try `VIXY` or `VXX` ETF as fallback
- **UK stocks** — `BARC.L` with `.L` suffix. Test this explicitly and document result
- **FX rates** — Do NOT use Finnhub for FX. Use `open.er-api.com` (confirmed working from browser). Fallback: `@fawazahmed0/currency-api` via jsdelivr CDN
- **Bond yields (FRED + Stooq)** — These work server-side only. Mark browser-only tests as skip with reason. Keep server-side tests active

### Finnhub free tier constraints:
- 60 requests/minute — always add `time.sleep(0.3)` between calls
- FX rates: blocked (use open.er-api.com instead)
- OANDA feed symbols: likely blocked (use ETF alternatives)
- US stocks: confirmed working
- Crypto (BINANCE:BTCUSDT): confirmed working

---

## Step 4 — Build the Ticker Test Page

Create `static/ticker_test.html` — a standalone browser page for testing individual symbols before adding them to the dashboard.

### Requirements:
- Dark Bloomberg-style theme (matching the dashboard — dark background `#060d13`, blue accents `#5ab0e0`, monospace font IBM Plex Mono)
- Seven tabs: Indices, US Stocks, UK Stocks, Commodities, FX, Crypto, VIX
- Each tab shows a table of all configured symbols from `symbols.yaml` (hardcoded from config at build time)
- Each row has:
  - Instrument label
  - Symbol string
  - Data source (finnhub / open.er-api.com / fred / stooq)
  - A **TEST** button that fetches the live price
  - Price display (green if working, red if failed)
  - Change % where available
  - Status badge: ✓ WORKING / ✗ FAILED / ⚠ NO DATA
  - Raw response toggle (expandable)
- A **RUN ALL** button per tab that tests all symbols with 300ms delay between calls
- A **★ WINNERS** button that filters to only working symbols
- A custom symbol input field — user can type any symbol, select endpoint type, click TEST
- A summary bar showing total / working / failed / no data counts
- All confirmed working symbols highlighted in green
- Page must work standalone (no build step, no npm, plain HTML/CSS/JS)
- FX tab uses open.er-api.com directly (not Finnhub)
- Bond yields tab notes that FRED/Stooq require server-side fetching

---

## Step 5 — Build the Flask Backend

Create `backend/app.py` — a single Flask application that:

### Data fetching (APScheduler background jobs):
- Fetches all market data every 5 minutes (configurable via symbols.yaml `schedule.market_data`)
- Fetches news every 30 minutes (configurable via symbols.yaml `schedule.news`)
- Fetches FX rates every 60 minutes (configurable via symbols.yaml `schedule.fx_rates`)
- Reads all symbols from `config/symbols.yaml` — no hardcoded symbols in app.py
- Writes cached data to `data/market_data.json` and `data/news.json`
- Handles all API errors gracefully — never crashes on a bad response
- Respects Finnhub rate limits (300ms between calls)
- Uses primary symbol first, falls back to alt_symbol if primary returns no data
- For FX: uses open.er-api.com primary, fawazahmed0 fallback
- For bonds: uses FRED for US yields, Stooq for UK gilts

### Flask API endpoints:
```
GET /              → serves static/ppmm.html
GET /test          → serves static/ticker_test.html  
GET /api/data      → returns latest market_data.json as JSON
GET /api/news      → returns latest news.json as JSON
GET /api/health    → returns {"status": "ok", "last_fetch": "<timestamp>", "symbols_loaded": <count>}
GET /api/config    → returns the symbols config (for the frontend to read)
```

### CORS:
- Enable CORS for all routes (flask-cors) — required for browser access from any device on the local network

### Error handling:
- If data.json doesn't exist yet (first startup), return `{"status": "loading", "message": "First fetch in progress"}` with HTTP 202
- Log all fetch errors with timestamp to console
- Never return partial data — either return the full cached payload or the loading response

### Data format — market_data.json:
```json
{
  "timestamp": "2025-01-15T10:30:00",
  "next_fetch": "2025-01-15T10:35:00",
  "indices":    {"S&P 500": {"price": 5800.50, "change": 12.3, "changePct": 0.21}},
  "vix":        {"VIX":     {"price": 18.5,    "change": -0.3, "changePct": -1.6}},
  "fx":         {"GBP/USD": {"price": 1.2650,  "source": "open.er-api.com"}},
  "commodities":{"Brent Crude": {"price": 82.50, "change": 0.5, "changePct": 0.61}},
  "crypto":     {"Bitcoin":    {"price": 95000,  "change": 500, "changePct": 0.53}},
  "bonds":      {"US 10Y":     {"price": 4.35,   "source": "fred"}},
  "stocks":     {
    "us": {"Apple": {"price": 225.50, "change": 1.2, "changePct": 0.54}},
    "uk": {"Barclays": {"price": 2.45, "change": 0.02, "changePct": 0.82}}
  }
}
```

---

## Step 6 — Build the Docker Setup

### `backend/Dockerfile`:
- Base image: `python:3.11-slim`
- Install requirements
- Copy app.py, config/, static/
- Create `/app/data/` directory
- Expose port 5000
- Run with gunicorn (1 worker, 4 threads)

### `backend/docker-compose.yml`:
```yaml
version: "3.8"
services:
  ppmm:
    build: .
    container_name: ppmm
    restart: unless-stopped
    ports:
      - "5000:5000"
    volumes:
      - ./data:/app/data          # persists cached data across restarts
      - ./config:/app/config      # allows editing symbols.yaml without rebuild
      - ./static:/app/static      # allows updating frontend without rebuild
    environment:
      - FINNHUB_API_KEY=d7fim8pr01qpjqqkv5d0d7fim8pr01qpjqqkv5dg
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:5000/api/health"]
      interval: 60s
      timeout: 10s
      retries: 3
```

### `backend/requirements.txt`:
```
flask==3.0.3
flask-cors==4.0.1
requests==2.31.0
apscheduler==3.10.4
gunicorn==22.0.0
pyyaml==6.0.1
```

---

## Step 7 — Update the Dashboard Frontend

Update `static/ppmm.html` (the existing React dashboard) to:
- Fetch data from `/api/data` instead of calling Finnhub directly
- Fetch news from `/api/news`
- Show a "Connecting to server…" state if `/api/data` returns 202
- Auto-retry every 10 seconds if server returns loading status
- Remove all direct API calls (Finnhub, open.er-api.com etc) from the frontend
- Remove the Finnhub API key from the frontend entirely
- Keep all existing UI features: drag-and-drop panels, device toggle, localStorage persistence, intraday chart modal (charts still call Finnhub directly — that's acceptable), refresh controls

---

## Step 8 — Final Verification

Run the complete test suite one more time:
```bash
pytest tests/ -v --tb=short 2>&1 | tee final_test_results.txt
```

Then do a local smoke test:
```bash
cd backend
pip install -r requirements.txt
python app.py &
sleep 10
curl http://localhost:5000/api/health
curl http://localhost:5000/api/data | python3 -m json.tool | head -40
```

Expected: health returns `{"status": "ok"}`, data returns a JSON payload with all sections populated.

---

## Final Project Structure

When complete the project should look like this:

```
ppmm/
├── README.md
├── pytest.ini
├── test_results.txt          ← generated by Step 2
├── final_test_results.txt    ← generated by Step 8
├── config/
│   └── symbols.yaml          ← updated with confirmed working symbols
├── tests/
│   ├── conftest.py
│   ├── requirements-test.txt
│   ├── test_config.py
│   ├── test_finnhub.py
│   ├── test_fx.py
│   ├── test_bonds.py
│   ├── test_news.py
│   └── test_integration.py
├── backend/
│   ├── app.py
│   ├── Dockerfile
│   ├── docker-compose.yml
│   └── requirements.txt
├── static/
│   ├── ppmm.html             ← updated dashboard (calls /api/data)
│   └── ticker_test.html      ← symbol tester page
└── data/                     ← created by Docker, gitignored
    ├── market_data.json
    └── news.json
```

---

## Constraints and Principles

- **No hardcoded symbols in Python code** — always read from symbols.yaml
- **No API keys in frontend code** — keys live in docker-compose.yml env vars only
- **Graceful degradation** — if one instrument fails to fetch, log it and continue. Never crash.
- **Rate limiting** — always 300ms between Finnhub calls. Never burst.
- **Delayed quotes are fine** — close prices are acceptable. No need for WebSocket streaming.
- **Phase 2 is out of scope** — do not add DDNS, Let's Encrypt, nginx reverse proxy or external access config. That is a separate phase.
- **Keep it simple** — no Redis, no database, no message queue. JSON file cache is sufficient.
- **Test first** — if you add a new data source or symbol format, write a test for it before using it in app.py

---

## When You Are Done

Report back with:
1. Full test results summary (how many pass/fail/skip and why)
2. Which symbols ended up being used for each instrument (primary vs fallback)
3. Any instruments that could not be sourced and why
4. Instructions for running the Docker container on the Synology NAS
5. Any decisions made that deviate from this brief and why

Do not proceed to Phase 2 (external access) — that will be a separate session.
