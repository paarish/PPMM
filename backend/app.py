"""
PPMM Flask Backend — Google Finance edition
All market data scraped from finance.google.com (no API keys required).
UK bond yields fall back to FRED CSV where no GF page exists.

Endpoints:
  GET /           → ppmm.html dashboard
  GET /api/data   → latest market_data.json
  GET /api/health → status, last_fetch, stale flag, symbol count
  GET /api/config → merged instruments + settings as JSON
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import requests
import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ppmm")

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR         = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTRUMENTS_PATH = os.path.join(BASE_DIR, "config", "instruments.yaml")
SETTINGS_PATH    = os.path.join(BASE_DIR, "config", "settings.yaml")
COOKIES_PATH     = os.path.join(BASE_DIR, "config", "gf_cookies.json")
STATIC_DIR       = os.path.join(BASE_DIR, "static")
DATA_DIR         = os.path.join(BASE_DIR, "data")
DATA_FILE        = os.path.join(DATA_DIR, "market_data.json")

os.makedirs(DATA_DIR, exist_ok=True)

# ── Config ────────────────────────────────────────────────────────────────────
def load_config() -> dict:
    """Merge instruments.yaml + settings.yaml into a single config dict."""
    with open(INSTRUMENTS_PATH) as f:
        instruments = yaml.safe_load(f) or {}
    with open(SETTINGS_PATH) as f:
        settings = yaml.safe_load(f) or {}
    return {**instruments, **settings}

# ── Google Finance session ────────────────────────────────────────────────────

GF_RATE_DELAY = 0.5   # seconds between requests

_DS2_PRICE_RE = re.compile(
    r'\[(-?[\d.Ee+\-]+),(-?[\d.Ee+\-]+),(-?[\d.Ee+\-]+),\d+,\d+,\d+\]'
)

# Consent-redirect detection: if the final URL contains this host the cookies
# have expired and all scrapes will silently return None.
_CONSENT_HOST = "consent.google.com"

# Module-level flag — set True when a consent redirect is detected.
_cookies_invalid = False


def _load_gf_cookies() -> dict:
    """Load GDPR bypass cookies from config/gf_cookies.json."""
    try:
        with open(COOKIES_PATH) as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if not k.startswith("_")}
    except Exception as e:
        log.error("Could not read %s: %s — scraping will fail", COOKIES_PATH, e)
        return {}


def _build_gf_session() -> requests.Session:
    """Create a requests.Session with current GF consent cookies."""
    cookies = _load_gf_cookies()
    s = requests.Session()
    for name, value in cookies.items():
        if name not in ("last_refreshed",):
            s.cookies.set(name, value, domain=".google.com")
    s.headers.update({
        "User-Agent":      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "en-GB,en;q=0.9",
    })
    return s


GF_SESSION = _build_gf_session()


def reload_gf_cookies():
    """Re-read gf_cookies.json and replace the live session cookies.
    Called automatically after tools/refresh_gf_cookies.py writes new values.
    """
    global GF_SESSION, _cookies_invalid
    GF_SESSION = _build_gf_session()
    _cookies_invalid = False
    log.info("GF session cookies reloaded from %s", COOKIES_PATH)


def _extract_ds2_block(html: str) -> str | None:
    pos = 0
    while True:
        idx = html.find("AF_initDataCallback(", pos)
        if idx == -1:
            return None
        start = idx + len("AF_initDataCallback(")
        depth, i = 1, start
        while i < len(html) and depth:
            c = html[i]
            if   c == "(": depth += 1
            elif c == ")": depth -= 1
            i += 1
        block = html[start : i - 1]
        m = re.search(r"key\s*:\s*['\"]([^'\"]+)['\"]", block)
        if m and m.group(1) == "ds:2":
            d = block.find("data:")
            return block[d:] if d != -1 else block
        pos = i


def scrape_gf(ticker: str, timeout: int = 12) -> dict | None:
    """
    Fetch one Google Finance quote page.
    Returns {price, change, changePct} or None.
    Sets _cookies_invalid=True if a consent redirect is detected.
    """
    global _cookies_invalid
    url = f"https://www.google.com/finance/quote/{ticker}"
    try:
        r = GF_SESSION.get(url, timeout=timeout)
        r.raise_for_status()
    except Exception as e:
        log.warning("GF fetch %s: %s", ticker, e)
        return None

    if _CONSENT_HOST in r.url:
        if not _cookies_invalid:
            log.critical(
                "Google Finance is serving a consent page — GF cookies have expired. "
                "Run tools/refresh_gf_cookies.py to renew."
            )
            _cookies_invalid = True
        return None

    blob = _extract_ds2_block(r.text)
    if not blob:
        log.warning("GF %s: ds:2 block not found", ticker)
        return None

    m = _DS2_PRICE_RE.search(blob)
    if not m:
        log.warning("GF %s: no price found in ds:2", ticker)
        return None

    return {
        "price":     float(m.group(1)),
        "change":    float(m.group(2)),
        "changePct": float(m.group(3)),
    }


def fetch_gf_entry(entry: dict) -> dict | None:
    """Fetch one instrument entry. Applies optional scale (e.g. 0.01 for GBX→GBP)."""
    ticker = entry.get("gf_ticker")
    if not ticker:
        return None

    data = scrape_gf(ticker)
    time.sleep(GF_RATE_DELAY)
    if data is None:
        return None

    scale = entry.get("scale", 1.0)
    if scale != 1.0:
        data["price"]  *= scale
        data["change"] *= scale

    data["symbol_used"] = ticker
    return data


# ── FRED fallback ─────────────────────────────────────────────────────────────

FRED_SESSION = requests.Session()
FRED_SESSION.headers["User-Agent"] = "PPMM-Backend/2.0"


def fetch_fred(series_id: str, timeout: int = 15) -> dict | None:
    try:
        r = FRED_SESSION.get(
            "https://fred.stlouisfed.org/graph/fredgraph.csv",
            params={"id": series_id},
            timeout=timeout,
        )
        r.raise_for_status()
        lines = [
            l for l in r.text.strip().split("\n")
            if not l.startswith("DATE") and not l.startswith("observation_date")
        ]
        for line in reversed(lines):
            parts = line.split(",")
            if len(parts) >= 2 and parts[1].strip() not in ("", "."):
                try:
                    return {
                        "price":     float(parts[1].strip()),
                        "change":    None,
                        "changePct": None,
                        "symbol_used": series_id,
                        "source":    "fred",
                    }
                except ValueError:
                    continue
    except Exception as e:
        log.warning("FRED %s: %s", series_id, e)
    return None


# ── Fetch cycle ───────────────────────────────────────────────────────────────

def fetch_market_data():
    """Full market data fetch cycle. Writes to data/market_data.json."""
    log.info("Starting market data fetch…")
    cfg = load_config()
    now = datetime.now(timezone.utc)

    payload = {
        "timestamp":   now.isoformat(),
        "indices":     {},
        "vix":         {},
        "fx":          {},
        "commodities": {},
        "crypto":      {},
        "bonds":       {},
        "stocks":      {"us": {}, "uk": {}},
    }

    for entry in cfg.get("indices", []):
        data = fetch_gf_entry(entry)
        if data:
            payload["indices"][entry["label"]] = data
        else:
            log.warning("Index %s: no data", entry["label"])

    for entry in cfg.get("vix", []):
        data = fetch_gf_entry(entry)
        if data:
            payload["vix"][entry["label"]] = data

    for entry in cfg.get("fx", []):
        data = fetch_gf_entry(entry)
        if data:
            payload["fx"][entry["label"]] = data

    for entry in cfg.get("commodities", []):
        data = fetch_gf_entry(entry)
        if data:
            payload["commodities"][entry["label"]] = data

    for entry in cfg.get("crypto", []):
        data = fetch_gf_entry(entry)
        if data:
            payload["crypto"][entry["label"]] = data

    for entry in cfg.get("stocks", {}).get("us", []):
        data = fetch_gf_entry(entry)
        if data:
            payload["stocks"]["us"][entry["label"]] = data

    for entry in cfg.get("stocks", {}).get("uk", []):
        data = fetch_gf_entry(entry)
        if data:
            payload["stocks"]["uk"][entry["label"]] = data

    for entry in cfg.get("bonds", []):
        label  = entry["label"]
        source = entry.get("source", "google_finance")
        data   = fetch_fred(entry["series_id"]) if source == "fred" else fetch_gf_entry(entry)
        if data:
            payload["bonds"][label] = data
        else:
            log.warning("Bond %s: no data (source=%s)", label, source)

    with open(DATA_FILE, "w") as f:
        json.dump(payload, f, indent=2)

    count = sum(len(v) if isinstance(v, dict) else 1
                for k, v in payload.items() if k != "timestamp")
    log.info("Fetch complete. %d items written.", count)


# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder=STATIC_DIR)
CORS(app)


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "ppmm.html")


@app.route("/api/data")
def api_data():
    if not os.path.exists(DATA_FILE):
        return jsonify({
            "status":  "loading",
            "message": "First fetch in progress — retry in a few seconds",
        }), 202
    with open(DATA_FILE) as f:
        return app.response_class(response=f.read(), status=200, mimetype="application/json")


@app.route("/api/health")
def api_health():
    cfg      = load_config()
    interval = cfg.get("schedule", {}).get("market_data", 300)
    last     = None
    status   = "loading"
    stale    = False

    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE) as f:
                d    = json.load(f)
                last = d.get("timestamp")
                status = "ok"
            if last:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
                stale = age > (2 * interval)
        except Exception:
            status = "error"

    symbols = sum([
        len(cfg.get("indices", [])),
        len(cfg.get("vix", [])),
        len(cfg.get("fx", [])),
        len(cfg.get("commodities", [])),
        len(cfg.get("crypto", [])),
        len(cfg.get("stocks", {}).get("us", [])),
        len(cfg.get("stocks", {}).get("uk", [])),
        len(cfg.get("bonds", [])),
    ])

    return jsonify({
        "status":          status,
        "last_fetch":      last,
        "stale":           stale,
        "cookies_valid":   not _cookies_invalid,
        "symbols_loaded":  symbols,
        "fetch_interval":  interval,
    })


@app.route("/api/config")
def api_config():
    cfg = load_config()
    return jsonify({k: v for k, v in cfg.items() if k != "api"})


# ── Scheduler ─────────────────────────────────────────────────────────────────

def start_scheduler():
    cfg      = load_config()
    interval = cfg.get("schedule", {}).get("market_data", 300)

    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        fetch_market_data, "interval", seconds=interval,
        id="market_data", next_run_time=datetime.now(),
    )
    scheduler.start()
    log.info("Scheduler started — fetching every %ds", interval)
    return scheduler


# ── Entry point ───────────────────────────────────────────────────────────────

# Start scheduler when the module is imported (covers both gunicorn and direct run).
# Guard against double-start when Flask's reloader forks a child process.
if not os.environ.get("WERKZEUG_RUN_MAIN"):
    _scheduler = start_scheduler()

if __name__ == "__main__":
    log.info("PPMM backend starting on http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
