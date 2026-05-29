#!/usr/bin/env python3
"""
test_cnbc_bonds.py
==================
Probes CNBC for bond yield data before committing to a scraping strategy.

Tests three approaches in order:
  1. CNBC quote REST API  (used internally by cnbc.com)
  2. CNBC chart time-series API
  3. HTML scrape of the quote page

Usage:
  python3 tools/test_cnbc_bonds.py
"""

import json
import re
import sys
import time

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": "https://www.cnbc.com/",
}

SYMBOLS = {
    "US 2Y":  "US2Y",
    "US 5Y":  "US5Y",
    "US 10Y": "US10Y",
    "US 30Y": "US30Y",
    "UK 2Y":  "UK2Y",
    "UK 5Y":  "UK5Y",
    "UK 10Y": "UK10Y",
}

s = requests.Session()
s.headers.update(HEADERS)


# ── Approach 1: CNBC quote REST API ──────────────────────────────────────────

def try_quote_api(symbols: list[str]) -> dict:
    """
    CNBC's internal quote service, used by the website's JS.
    Returns {symbol: price} for any that succeed.
    """
    url = "https://quote.cnbc.com/quote-html-webservice/restservices/cff/owcs/pageConfig/quotes/"
    params = {
        "symbols":       "|".join(symbols),
        "requestMethod": "itv",
        "noform":        "1",
        "partnerId":     "2",
        "fund":          "1",
        "exthrs":        "1",
        "output":        "json",
        "events":        "1",
    }
    try:
        r = s.get(url, params=params, timeout=12)
        r.raise_for_status()
        data = r.json()
        results = {}
        # Response is nested: FormattedQuoteResult > FormattedQuote > []
        quotes = (data.get("FormattedQuoteResult", {})
                      .get("FormattedQuote", []))
        if isinstance(quotes, dict):
            quotes = [quotes]
        for q in quotes:
            sym   = q.get("symbol", "")
            price = q.get("last") or q.get("last_yield") or q.get("FundamentalData", {}).get("yield")
            if sym and price is not None:
                try:
                    results[sym] = float(str(price).replace(",", ""))
                except ValueError:
                    pass
        return results
    except Exception as e:
        print(f"  Quote API error: {e}")
        return {}


# ── Approach 2: CNBC chart / time-series API ─────────────────────────────────

def try_chart_api(symbol: str) -> float | None:
    """
    CNBC's chart data endpoint — returns OHLC JSON, last close = latest yield.
    """
    url = f"https://ts-api.cnbc.com/harmony/app/charts/time_series/{symbol}/D/1month/json"
    try:
        r = s.get(url, timeout=12)
        r.raise_for_status()
        data = r.json()
        bars = data.get("barData", {}).get("priceBars", [])
        if bars:
            return float(bars[-1].get("close", 0))
    except Exception as e:
        print(f"  Chart API error for {symbol}: {e}")
    return None


# ── Approach 3: HTML scrape ───────────────────────────────────────────────────

def try_html_scrape(symbol: str) -> float | None:
    """
    Scrape the CNBC quote page for the last price.
    Looks for the price in __NEXT_DATA__ JSON blob or meta tags.
    """
    url = f"https://www.cnbc.com/quotes/{symbol}"
    try:
        r = s.get(url, timeout=12)
        r.raise_for_status()

        # Try __NEXT_DATA__ (Next.js page data)
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>', r.text, re.DOTALL)
        if m:
            nd = json.loads(m.group(1))
            # Navigate to quote data — path varies by page version
            try:
                quote = nd["props"]["pageProps"]["quote"]
                last = quote.get("last") or quote.get("last_yield")
                if last is not None:
                    return float(str(last).replace(",", ""))
            except (KeyError, TypeError):
                pass

        # Fallback: look for a price pattern near common class names
        m = re.search(r'"last"\s*:\s*"?([\d.]+)"?', r.text)
        if m:
            return float(m.group(1))

    except Exception as e:
        print(f"  HTML scrape error for {symbol}: {e}")
    return None


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("CNBC Bond Yield Probe")
    print("=" * 60)

    # --- Approach 1: bulk quote API ---
    print("\n[1] CNBC Quote REST API (bulk)")
    syms = list(SYMBOLS.values())
    results = try_quote_api(syms)
    if results:
        for label, sym in SYMBOLS.items():
            val = results.get(sym)
            print(f"  {label:10s} ({sym:6s}): {val if val is not None else 'NOT FOUND'}")
    else:
        print("  No results from quote API.")

    # --- Approach 2: chart API per symbol ---
    print("\n[2] CNBC Chart Time-Series API (per symbol, 1-month daily)")
    chart_results = {}
    for label, sym in SYMBOLS.items():
        val = try_chart_api(sym)
        chart_results[sym] = val
        print(f"  {label:10s} ({sym:6s}): {val if val is not None else 'NOT FOUND'}")
        time.sleep(0.3)

    # --- Approach 3: HTML scrape (only for symbols not yet found) ---
    html_results = {}
    missing = [sym for sym in SYMBOLS.values()
               if results.get(sym) is None and chart_results.get(sym) is None]
    if missing:
        print(f"\n[3] HTML scrape (fallback for {len(missing)} missing symbols)")
        for label, sym in SYMBOLS.items():
            if sym in missing:
                val = try_html_scrape(sym)
                html_results[sym] = val
                print(f"  {label:10s} ({sym:6s}): {val if val is not None else 'NOT FOUND'}")
                time.sleep(0.5)
    else:
        print("\n[3] HTML scrape — skipped (all symbols found above)")

    # --- Summary ---
    print("\n" + "=" * 60)
    print("SUMMARY — best value per symbol")
    print("=" * 60)
    all_found = True
    for label, sym in SYMBOLS.items():
        val = results.get(sym) or chart_results.get(sym) or html_results.get(sym)
        status = f"{val:.3f}%" if val is not None else "FAILED"
        print(f"  {label:10s} ({sym:6s}): {status}")
        if val is None:
            all_found = False

    print()
    if all_found:
        print("All symbols resolved. Safe to integrate into backend.")
    else:
        print("Some symbols failed. Check output above for details.")

    sys.exit(0 if all_found else 1)


if __name__ == "__main__":
    main()
