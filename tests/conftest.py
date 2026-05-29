"""
PPMM Test Configuration
=======================
Shared fixtures and helpers for all test modules.

Run all tests:         pytest tests/ -v
Run the scraper suite: pytest tests/test_scraper.py -v
Run with live output:  pytest tests/ -v -s
"""
import os
import time

import pytest
import requests
import yaml

# ── Paths ──────────────────────────────────────────────────────────────────────
import json

ROOT_DIR         = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTRUMENTS_PATH = os.path.join(ROOT_DIR, "config", "instruments.yaml")
SETTINGS_PATH    = os.path.join(ROOT_DIR, "config", "settings.yaml")
COOKIES_PATH     = os.path.join(ROOT_DIR, "config", "gf_cookies.json")


# ── Config fixtures ────────────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def instruments():
    """Load instruments.yaml once for the entire test session."""
    with open(INSTRUMENTS_PATH) as f:
        return yaml.safe_load(f) or {}


@pytest.fixture(scope="session")
def settings():
    """Load settings.yaml once for the entire test session."""
    with open(SETTINGS_PATH) as f:
        return yaml.safe_load(f) or {}


@pytest.fixture(scope="session")
def config(instruments, settings):
    """Merged config (instruments + settings) — matches what app.py uses."""
    return {**instruments, **settings}


# ── Google Finance session fixture ─────────────────────────────────────────────
@pytest.fixture(scope="session")
def gf_session():
    """Requests session with GDPR consent bypass cookies read from config/gf_cookies.json."""
    try:
        with open(COOKIES_PATH) as f:
            cookies = json.load(f)
    except Exception as e:
        pytest.skip(f"Cannot read {COOKIES_PATH}: {e}")
    s = requests.Session()
    for name, value in cookies.items():
        if not name.startswith("_") and name != "last_refreshed":
            s.cookies.set(name, value, domain=".google.com")
    s.headers.update({
        "User-Agent":      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "en-GB,en;q=0.9",
    })
    return s


# ── FRED session fixture ───────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def fred_session():
    s = requests.Session()
    s.headers["User-Agent"] = "PPMM-Test/2.0"
    return s


# ── Rate-limit guard ───────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def rate_limit():
    """Brief pause after every test to avoid hammering external APIs."""
    yield
    time.sleep(0.5)


# ── Shared assertion helpers ───────────────────────────────────────────────────
def assert_price(value, label: str, min_val: float = 0.0, max_val: float = 1_000_000):
    assert value is not None,               f"{label}: price is None"
    assert isinstance(value, (int, float)), f"{label}: price is not a number — got {type(value)}"
    assert value > min_val,                 f"{label}: price {value} ≤ {min_val}"
    assert value < max_val,                 f"{label}: price {value} ≥ {max_val} (suspiciously large)"


def assert_pct(value, label: str, max_abs: float = 50.0):
    """Daily % change should exist and not be absurdly large (>±50%)."""
    assert value is not None,               f"{label}: changePct is None"
    assert isinstance(value, (int, float)), f"{label}: changePct is not a number"
    assert abs(value) <= max_abs,           f"{label}: changePct {value:+.2f}% looks wrong (>{max_abs}%)"
