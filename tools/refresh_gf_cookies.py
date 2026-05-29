#!/usr/bin/env python3
"""
refresh_gf_cookies.py
=====================
Renews the Google Finance GDPR consent cookies stored in config/gf_cookies.json.

Run this when:
  - /api/health returns  "cookies_valid": false
  - The dashboard shows stale data and the backend log says "consent page"
  - You want to proactively refresh before cookies expire

Usage:
  python3 tools/refresh_gf_cookies.py          # auto-refresh
  python3 tools/refresh_gf_cookies.py --check  # check only, don't write

How it works:
  1. Makes a request to Google Finance with no cookies
  2. Follows the GDPR consent redirect
  3. Submits the "Accept all" form automatically
  4. Captures the fresh SOCS + CONSENT cookies from the response
  5. Writes them to config/gf_cookies.json (volume-mounted on NAS, no rebuild needed)

The backend reloads cookies on the next fetch cycle automatically.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

import requests

ROOT_DIR     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COOKIES_PATH = os.path.join(ROOT_DIR, "config", "gf_cookies.json")

PROBE_URL = "https://www.google.com/finance/quote/AAPL:NASDAQ"
HEADERS   = {
    "User-Agent":      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _check_cookies(cookies: dict) -> bool:
    """Return True if the given cookies bypass the consent page."""
    s = requests.Session()
    for name, value in cookies.items():
        s.cookies.set(name, value, domain=".google.com")
    s.headers.update(HEADERS)
    try:
        r = s.get(PROBE_URL, timeout=12, allow_redirects=True)
        if "consent.google.com" in r.url:
            return False
        # Also verify we can find a price blob
        if "AF_initDataCallback" in r.text:
            return True
        return False
    except Exception as e:
        print(f"  ERROR during check: {e}")
        return False


def _load_existing() -> dict:
    try:
        with open(COOKIES_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"  WARNING: could not read existing cookies: {e}")
        return {}


def _save_cookies(consent: str, socs: str) -> None:
    existing = _load_existing()
    existing.update({
        "CONSENT":       consent,
        "SOCS":          socs,
        "last_refreshed": datetime.now(timezone.utc).isoformat(),
    })
    with open(COOKIES_PATH, "w") as f:
        json.dump(existing, f, indent=2)
    print(f"  Saved to {COOKIES_PATH}")


def _extract_form_fields(html: str) -> dict:
    """Extract all <input type=hidden> fields from the consent form."""
    fields = {}
    for m in re.finditer(r'<input[^>]+name=["\']([^"\']+)["\'][^>]+value=["\']([^"\']*)["\']', html):
        fields[m.group(1)] = m.group(2)
    for m in re.finditer(r'<input[^>]+value=["\']([^"\']*)["\'][^>]+name=["\']([^"\']+)["\']', html):
        fields[m.group(2)] = m.group(1)
    return fields


def _extract_form_action(html: str) -> str | None:
    m = re.search(r'<form[^>]+action=["\']([^"\']+)["\']', html)
    return m.group(1) if m else None


def refresh() -> bool:
    """
    Attempt automated cookie refresh via consent form submission.
    Returns True on success.
    """
    print("Step 1: Probing Google Finance without cookies…")
    s = requests.Session()
    s.headers.update(HEADERS)

    try:
        r = s.get(PROBE_URL, timeout=12, allow_redirects=True)
    except Exception as e:
        print(f"  Network error: {e}")
        return False

    if "consent.google.com" not in r.url:
        # Cookies in session (from redirect chain) may already be set
        socs    = s.cookies.get("SOCS",    domain=".google.com") or ""
        consent = s.cookies.get("CONSENT", domain=".google.com") or ""
        if socs:
            print("  No consent redirect — Google didn't challenge us.")
            print(f"  SOCS:    {socs[:40]}…")
            print(f"  CONSENT: {consent}")
            _save_cookies(consent or "YES+GB.en+V14+BX", socs)
            return True
        print("  No consent page and no SOCS cookie. Existing cookies may still be valid.")
        return False

    consent_url = r.url
    print(f"  Redirected to consent page: {consent_url[:80]}…")

    print("Step 2: Parsing consent form…")
    form_action = _extract_form_action(r.text)
    form_fields = _extract_form_fields(r.text)

    if not form_action:
        # Try alternate pattern — some consent pages use a JSON-driven UI
        print("  Could not find form action — trying alternate accept endpoint…")
        return _try_alternate_accept(consent_url, s)

    # Resolve relative action URL
    if form_action.startswith("/"):
        from urllib.parse import urlparse
        parsed = urlparse(consent_url)
        form_action = f"{parsed.scheme}://{parsed.netloc}{form_action}"

    print(f"  Form action: {form_action}")
    print(f"  Fields found: {list(form_fields.keys())}")

    print("Step 3: Submitting consent acceptance…")
    # Override / add the button value that indicates "Accept all"
    form_fields.setdefault("set_eom", "true")

    try:
        post_r = s.post(form_action, data=form_fields, timeout=12, allow_redirects=True)
    except Exception as e:
        print(f"  POST failed: {e}")
        return False

    print(f"  POST response: {post_r.status_code} → {post_r.url[:80]}")

    socs    = s.cookies.get("SOCS",    domain=".google.com") or ""
    consent = s.cookies.get("CONSENT", domain=".google.com") or ""

    if not socs:
        # Check response cookies directly
        for c in post_r.cookies:
            if c.name == "SOCS":
                socs = c.value
            if c.name == "CONSENT":
                consent = c.value

    if not socs:
        print("  No SOCS cookie in response — automated acceptance may have failed.")
        return _try_alternate_accept(consent_url, s)

    print(f"  Got SOCS:    {socs[:40]}…")
    print(f"  Got CONSENT: {consent}")

    print("Step 4: Verifying new cookies against Google Finance…")
    if _check_cookies({"CONSENT": consent, "SOCS": socs}):
        print("  Verification passed.")
        _save_cookies(consent, socs)
        return True
    else:
        print("  Verification failed — cookies obtained but GF still redirecting.")
        return False


def _try_alternate_accept(consent_url: str, session: requests.Session) -> bool:
    """
    Some Google consent pages use a button that calls a JS endpoint.
    Try the known API accept path as a fallback.
    """
    from urllib.parse import urlparse, parse_qs
    parsed  = urlparse(consent_url)
    params  = parse_qs(parsed.query)
    gl      = params.get("gl", ["GB"])[0]
    pc      = params.get("pc", ["finance"])[0]
    hl      = params.get("hl", ["en"])[0]
    cont    = params.get("continue", [PROBE_URL])[0]

    accept_url = "https://consent.google.com/save"
    payload = {
        "gl":       gl,
        "m":        "0",
        "app":      "0",
        "pc":       pc,
        "continue": cont,
        "x":        "6",
        "bl":       params.get("bl", ["boq_identityfrontenduiserver"])[0],
        "hl":       hl,
        "src":      "2",
        "set_eom":  "true",
    }
    try:
        r = session.post(accept_url, data=payload, timeout=12, allow_redirects=True)
        socs    = session.cookies.get("SOCS",    domain=".google.com") or ""
        consent = session.cookies.get("CONSENT", domain=".google.com") or ""
        if socs and _check_cookies({"CONSENT": consent, "SOCS": socs}):
            print(f"  Alternate accept succeeded.")
            _save_cookies(consent, socs)
            return True
    except Exception as e:
        print(f"  Alternate accept error: {e}")

    print()
    print("  Automated refresh failed. Manual steps:")
    print("  1. Open https://www.google.com/finance in your browser (UK/EU region)")
    print("  2. Accept cookies when prompted")
    print("  3. Open DevTools → Application → Cookies → google.com")
    print("  4. Copy the values of CONSENT and SOCS")
    print(f"  5. Edit {COOKIES_PATH} and update those two fields")
    print("  6. The backend will pick them up on the next fetch cycle automatically")
    return False


def main():
    parser = argparse.ArgumentParser(description="Refresh Google Finance GDPR cookies")
    parser.add_argument("--check", action="store_true", help="Check current cookies, don't refresh")
    args = parser.parse_args()

    existing = _load_existing()
    current_cookies = {k: v for k, v in existing.items()
                       if k not in ("last_refreshed", "_note")}

    if args.check:
        print("Checking existing cookies…")
        if current_cookies and _check_cookies(current_cookies):
            last = existing.get("last_refreshed", "unknown")
            print(f"  OK — cookies are valid (last refreshed: {last})")
            sys.exit(0)
        else:
            print("  FAIL — cookies are expired or missing")
            sys.exit(1)

    # Check first — no-op if already valid
    if current_cookies:
        print("Checking existing cookies first…")
        if _check_cookies(current_cookies):
            last = existing.get("last_refreshed", "unknown")
            print(f"  Still valid (last refreshed: {last}) — no refresh needed.")
            sys.exit(0)
        print("  Expired — refreshing now.")
    else:
        print("No existing cookies — fetching fresh ones.")

    success = refresh()

    if success:
        print()
        print("Done. The backend will use the new cookies on its next fetch cycle.")
        print("To apply immediately without waiting, restart the container:")
        print("  docker compose -f backend/docker-compose.yml restart")
        sys.exit(0)
    else:
        print()
        print("Refresh failed. See manual instructions above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
