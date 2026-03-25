"""
External market data fetchers.

- CNN Fear & Greed Index  via RapidAPI
- VIX (CBOE Volatility Index) via yfinance
"""

import logging
import os
from typing import TypedDict

import requests
import yfinance as yf
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "")
FEAR_GREED_HOST = "fear-and-greed-index.p.rapidapi.com"
FEAR_GREED_URL = f"https://{FEAR_GREED_HOST}/v1/fgi"


class FearGreedData(TypedDict):
    value: int
    label: str


class MarketSnapshot(TypedDict):
    vix: float | None
    fear_greed_value: int | None
    fear_greed_label: str | None


# ---------------------------------------------------------------------------
# CNN Fear & Greed Index
# ---------------------------------------------------------------------------

def fetch_fear_greed() -> FearGreedData | None:
    """
    Fetch the current CNN Fear & Greed Index from RapidAPI.
    Returns a dict with 'value' (0-100) and 'label' (e.g. 'Extreme Greed').
    Returns None on failure.
    """
    if not RAPIDAPI_KEY:
        logger.warning("RAPIDAPI_KEY not set; skipping Fear & Greed fetch")
        return None

    headers = {
        "X-RapidAPI-Key": RAPIDAPI_KEY,
        "X-RapidAPI-Host": FEAR_GREED_HOST,
    }
    try:
        resp = requests.get(FEAR_GREED_URL, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error("Fear & Greed API error: %s", exc)
        return None

    # The API returns a nested structure; navigate to current value
    # Common response shape: {"fgi": {"now": {"value": 72, "valueText": "Greed"}}}
    try:
        fgi = data.get("fgi") or data
        now = fgi.get("now") or fgi
        value = int(now["value"])
        label = str(now.get("valueText") or now.get("label", ""))
        return FearGreedData(value=value, label=label)
    except (KeyError, TypeError, ValueError) as exc:
        logger.error("Unexpected Fear & Greed response: %s | data=%r", exc, str(data)[:300])
        return None


# ---------------------------------------------------------------------------
# VIX via yfinance
# ---------------------------------------------------------------------------

def fetch_vix() -> float | None:
    """
    Fetch the latest VIX closing price using yfinance.
    Returns a float or None on failure.
    """
    try:
        ticker = yf.Ticker("^VIX")
        hist = ticker.history(period="2d")
        if hist.empty:
            logger.error("yfinance returned empty history for ^VIX")
            return None
        return float(hist["Close"].iloc[-1])
    except Exception as exc:
        logger.error("yfinance VIX error: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Combined snapshot
# ---------------------------------------------------------------------------

def fetch_market_snapshot() -> MarketSnapshot:
    """Fetch VIX and Fear & Greed in one call."""
    fg = fetch_fear_greed()
    vix = fetch_vix()
    return MarketSnapshot(
        vix=vix,
        fear_greed_value=fg["value"] if fg else None,
        fear_greed_label=fg["label"] if fg else None,
    )
