"""
test_scraper.py — Google Finance scraper test harness
======================================================
One representative instrument per asset class.
Each test makes a live network call and validates the returned data shape.

Run all:           pytest tests/test_scraper.py -v
Run one class:     pytest tests/test_scraper.py::TestFX -v
Run fast (no net): pytest tests/test_scraper.py -v --collect-only
"""
import sys
import os
import re
import time

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.conftest import assert_price, assert_pct

# ── Scraper helpers (duplicated from app.py to keep tests self-contained) ──────

_DS2_PRICE_RE = re.compile(
    r'\[(-?[\d.Ee+\-]+),(-?[\d.Ee+\-]+),(-?[\d.Ee+\-]+),\d+,\d+,\d+\]'
)


def _extract_ds2(html: str) -> str | None:
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
        block = html[start:i - 1]
        m = re.search(r"key\s*:\s*['\"]([^'\"]+)['\"]", block)
        if m and m.group(1) == "ds:2":
            d = block.find("data:")
            return block[d:] if d != -1 else block
        pos = i


def scrape(ticker: str, session, scale: float = 1.0) -> dict | None:
    """
    Fetch one Google Finance quote page and return {price, change, changePct}.
    scale applies to price and change only (e.g. 0.1 for CBOE yield indices).
    Returns None when no price data is found.
    """
    url = f"https://www.google.com/finance/quote/{ticker}"
    try:
        r = session.get(url, timeout=12)
        r.raise_for_status()
    except Exception:
        return None

    blob = _extract_ds2(r.text)
    if not blob:
        return None

    m = _DS2_PRICE_RE.search(blob)
    if not m:
        return None

    price, change, pct = float(m.group(1)), float(m.group(2)), float(m.group(3))
    return {
        "price":     price * scale,
        "change":    change * scale,
        "changePct": pct,
        "ticker":    ticker,
    }


def fetch_cnbc(symbol: str, session) -> dict | None:
    """Scrape a bond yield from https://www.cnbc.com/quotes/<symbol>."""
    import re as _re
    url = f"https://www.cnbc.com/quotes/{symbol}"
    try:
        r = session.get(url, timeout=12)
        r.raise_for_status()
        m = _re.search(r'"last"\s*:\s*"?([-\d.]+)"?', r.text)
        if m:
            return {"price": float(m.group(1)), "symbol": symbol}
    except Exception:
        pass
    return None


# ── Test classes ───────────────────────────────────────────────────────────────

class TestIndices:
    """Equity index scraping — representative sample: S&P 500."""

    def test_sp500(self, gf_session):
        """S&P 500 via .INX:INDEXSP — expect a 4-5 digit level with a daily % move."""
        data = scrape(".INX:INDEXSP", gf_session)
        assert data is not None, "S&P 500: scrape returned None"
        assert_price(data["price"], "S&P 500", min_val=1_000, max_val=20_000)
        assert_pct(data["changePct"], "S&P 500")

    def test_ftse100(self, gf_session):
        """FTSE 100 via UKX:INDEXFTSE — actual index level (not ETF proxy)."""
        data = scrape("UKX:INDEXFTSE", gf_session)
        assert data is not None, "FTSE 100: scrape returned None"
        assert_price(data["price"], "FTSE 100", min_val=3_000, max_val=15_000)
        assert_pct(data["changePct"], "FTSE 100")

    def test_nikkei(self, gf_session):
        """Nikkei 225 — 5-digit level, zero-decimal display."""
        data = scrape("NI225:INDEXNIKKEI", gf_session)
        assert data is not None, "Nikkei 225: scrape returned None"
        assert_price(data["price"], "Nikkei 225", min_val=10_000, max_val=100_000)

    def test_all_configured_indices(self, gf_session, instruments):
        """All indices in instruments.yaml must return a positive price."""
        failures = []
        for entry in instruments.get("indices", []):
            ticker = entry["gf_ticker"]
            data = scrape(ticker, gf_session)
            if data is None or data["price"] <= 0:
                failures.append(f"{entry['label']} ({ticker})")
            time.sleep(0.5)
        assert not failures, "Indices returned no data: " + ", ".join(failures)


class TestVIX:
    """VIX fear gauge."""

    def test_vix_price(self, gf_session):
        """VIX should be a positive number, typically between 10 and 80."""
        data = scrape("VIX:INDEXCBOE", gf_session)
        assert data is not None, "VIX: scrape returned None"
        assert_price(data["price"], "VIX", min_val=5, max_val=90)

    def test_vix_thresholds(self, gf_session, instruments):
        """VIX thresholds from config must be ordered low < moderate < high."""
        vix_entries = instruments.get("vix", [])
        assert vix_entries, "No VIX entry in instruments.yaml"
        thresholds = vix_entries[0].get("thresholds", {})
        low, mod, high = thresholds.get("low"), thresholds.get("moderate"), thresholds.get("high")
        assert low < mod < high, f"VIX thresholds out of order: {low}, {mod}, {high}"


class TestFX:
    """FX rate scraping — BASE-QUOTE format."""

    def test_gbp_usd(self, gf_session):
        """GBP/USD should be between 1.0 and 1.6."""
        data = scrape("GBP-USD", gf_session)
        assert data is not None, "GBP/USD: scrape returned None"
        assert_price(data["price"], "GBP/USD", min_val=0.8, max_val=2.0)
        assert_pct(data["changePct"], "GBP/USD")

    def test_eur_gbp(self, gf_session):
        """EUR/GBP cross."""
        data = scrape("EUR-GBP", gf_session)
        assert data is not None, "EUR/GBP: scrape returned None"
        assert_price(data["price"], "EUR/GBP", min_val=0.6, max_val=1.2)

    def test_all_configured_fx(self, gf_session, instruments):
        """All FX pairs in instruments.yaml must return a positive rate."""
        failures = []
        for entry in instruments.get("fx", []):
            ticker = entry["gf_ticker"]
            data = scrape(ticker, gf_session)
            if data is None or data["price"] <= 0:
                failures.append(f"{entry['label']} ({ticker})")
            time.sleep(0.5)
        assert not failures, "FX pairs returned no data: " + ", ".join(failures)


class TestCommodities:
    """Energy and metals futures — W00 continuous contracts."""

    def test_brent_crude(self, gf_session):
        """Brent crude (BZW00:NYMEX) — typically 60–120 USD/bbl."""
        data = scrape("BZW00:NYMEX", gf_session)
        assert data is not None, "Brent Crude: scrape returned None"
        assert_price(data["price"], "Brent Crude", min_val=30, max_val=200)
        assert_pct(data["changePct"], "Brent Crude")

    def test_wti_crude(self, gf_session):
        """WTI crude (CLW00:NYMEX) — typically 5–10 USD below Brent."""
        data = scrape("CLW00:NYMEX", gf_session)
        assert data is not None, "WTI Crude: scrape returned None"
        assert_price(data["price"], "WTI Crude", min_val=30, max_val=200)

    def test_henry_hub(self, gf_session):
        """Henry Hub natural gas (NGW00:NYMEX) — typically 1–10 USD/MMBtu."""
        data = scrape("NGW00:NYMEX", gf_session)
        assert data is not None, "Henry Hub: scrape returned None"
        assert_price(data["price"], "Henry Hub", min_val=0.5, max_val=30)

    def test_ttf_gas(self, gf_session):
        """Dutch TTF natural gas (TTFW00:NYMEX) — EUR/MWh.
        NOTE: changePct may be large (>10%) on contract-roll day — we skip % assertion.
        """
        data = scrape("TTFW00:NYMEX", gf_session)
        assert data is not None, "TTF Gas: scrape returned None"
        assert_price(data["price"], "TTF Gas", min_val=5, max_val=500)

    def test_gold(self, gf_session):
        """Gold (GCW00:COMEX) — USD/oz."""
        data = scrape("GCW00:COMEX", gf_session)
        assert data is not None, "Gold: scrape returned None"
        assert_price(data["price"], "Gold", min_val=500, max_val=10_000)


class TestCrypto:
    """Cryptocurrency — BASE-USD format."""

    def test_bitcoin(self, gf_session):
        """Bitcoin (BTC-USD) — expect five or six digit USD price."""
        data = scrape("BTC-USD", gf_session)
        assert data is not None, "Bitcoin: scrape returned None"
        assert_price(data["price"], "Bitcoin", min_val=5_000, max_val=1_000_000)
        assert_pct(data["changePct"], "Bitcoin")


class TestStocks:
    """Individual equity scraping."""

    def test_us_stock_apple(self, gf_session):
        """Apple (AAPL:NASDAQ) — expect a three-digit USD price."""
        data = scrape("AAPL:NASDAQ", gf_session)
        assert data is not None, "Apple: scrape returned None"
        assert_price(data["price"], "Apple", min_val=50, max_val=2_000)
        assert_pct(data["changePct"], "Apple")

    def test_uk_stock_barclays(self, gf_session):
        """Barclays (BARC:LON) — quoted in pence (GBX), expect 100–600p."""
        data = scrape("BARC:LON", gf_session)
        assert data is not None, "Barclays: scrape returned None"
        assert_price(data["price"], "Barclays (GBX)", min_val=50, max_val=1_000)
        assert_pct(data["changePct"], "Barclays")


class TestBonds:
    """Government bond yield scraping — all via CNBC quote pages."""

    def test_us_10y_cnbc(self, cnbc_session):
        """US 10Y Treasury yield via CNBC US10Y."""
        data = fetch_cnbc("US10Y", cnbc_session)
        assert data is not None, "US 10Y (CNBC): fetch returned None"
        assert_price(data["price"], "US 10Y yield", min_val=0.5, max_val=10.0)

    def test_us_2y_cnbc(self, cnbc_session):
        """US 2Y Treasury yield via CNBC US2Y."""
        data = fetch_cnbc("US2Y", cnbc_session)
        assert data is not None, "US 2Y (CNBC): fetch returned None"
        assert_price(data["price"], "US 2Y yield", min_val=0.5, max_val=10.0)

    def test_uk_10y_cnbc(self, cnbc_session):
        """UK 10Y Gilt yield via CNBC UK10Y."""
        data = fetch_cnbc("UK10Y", cnbc_session)
        assert data is not None, "UK 10Y (CNBC): fetch returned None"
        assert_price(data["price"], "UK 10Y yield", min_val=0.5, max_val=10.0)

    def test_uk_5y_cnbc(self, cnbc_session):
        """UK 5Y Gilt yield via CNBC UK5Y."""
        data = fetch_cnbc("UK5Y", cnbc_session)
        assert data is not None, "UK 5Y (CNBC): fetch returned None"
        assert_price(data["price"], "UK 5Y yield", min_val=0.5, max_val=10.0)

    def test_all_configured_bonds(self, cnbc_session, instruments):
        """All bonds in instruments.yaml with source:cnbc must return a yield."""
        failures = []
        for entry in instruments.get("bonds", []):
            if entry.get("source") != "cnbc":
                continue
            sym  = entry["cnbc_symbol"]
            data = fetch_cnbc(sym, cnbc_session)
            if data is None or data["price"] <= 0:
                failures.append(f"{entry['label']} ({sym})")
            time.sleep(0.5)
        assert not failures, "CNBC bonds returned no data: " + ", ".join(failures)


class TestConfig:
    """Config file integrity — no network calls."""

    def test_instruments_file_loadable(self, instruments):
        """instruments.yaml must be non-empty and have required top-level sections."""
        required = ["indices", "vix", "fx", "commodities", "crypto", "stocks", "bonds"]
        missing = [k for k in required if k not in instruments]
        assert not missing, f"instruments.yaml missing sections: {missing}"

    def test_settings_file_loadable(self, settings):
        """settings.yaml must have a schedule.market_data interval."""
        assert "schedule" in settings, "settings.yaml missing 'schedule' key"
        interval = settings["schedule"].get("market_data")
        assert interval and interval > 0, f"schedule.market_data must be positive, got {interval}"

    def test_all_gf_instruments_have_tickers(self, instruments):
        """Every Google Finance instrument must have a gf_ticker field."""
        missing = []
        for section in ["indices", "vix", "fx", "commodities", "crypto"]:
            for entry in instruments.get(section, []):
                if not entry.get("gf_ticker"):
                    missing.append(f"{section}/{entry.get('label','?')}")
        for entry in instruments.get("stocks", {}).get("us", []):
            if not entry.get("gf_ticker"):
                missing.append(f"stocks/us/{entry.get('label','?')}")
        for entry in instruments.get("stocks", {}).get("uk", []):
            if not entry.get("gf_ticker"):
                missing.append(f"stocks/uk/{entry.get('label','?')}")
        for entry in instruments.get("bonds", []):
            src = entry.get("source", "google_finance")
            if src == "google_finance" and not entry.get("gf_ticker"):
                missing.append(f"bonds/{entry.get('label','?')}")
        assert not missing, f"Missing gf_ticker: {missing}"

    def test_cnbc_bonds_have_symbols(self, instruments):
        """Every bond entry with source: cnbc must have a cnbc_symbol."""
        missing = []
        for entry in instruments.get("bonds", []):
            if entry.get("source") == "cnbc" and not entry.get("cnbc_symbol"):
                missing.append(entry.get("label", "?"))
        assert not missing, f"CNBC bond entries missing cnbc_symbol: {missing}"

    def test_no_duplicate_labels(self, instruments):
        """Instrument labels must be unique within each section."""
        for section in ["indices", "vix", "fx", "commodities", "crypto", "bonds"]:
            labels = [e["label"] for e in instruments.get(section, [])]
            dupes = [l for l in labels if labels.count(l) > 1]
            assert not dupes, f"Duplicate labels in {section}: {set(dupes)}"
