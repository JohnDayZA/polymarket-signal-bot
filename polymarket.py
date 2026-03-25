"""
Polymarket CLOB API client.

Fetches active binary markets in the Politics and Crypto categories.
API docs: https://docs.polymarket.com/#get-markets
"""

import json
import logging
from datetime import datetime, timedelta, timezone

import requests

logger = logging.getLogger(__name__)

CLOB_BASE = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"

# Polymarket tag slugs for the categories we care about
TARGET_TAGS = {"politics", "crypto", "cryptocurrency", "political"}

# Number of markets to fetch per page
PAGE_SIZE = 100
MAX_PAGES = 3
REQUEST_TIMEOUT = 10
MIN_VOLUME_USDC = 100.0

# Markets whose questions contain any of these phrases (case-insensitive) are excluded.
# Rule: only block markets where the resolution trigger is itself a fictional or
# entertainment product. Open-ended real-world backstops (wars, elections, scientific
# milestones) are valid — Claude can reason about them. Only fictional products create
# zombie markets with no real resolution path.
QUESTION_BLOCKLIST = [
    "before gta vi",  # fictional game used as a resolution backstop
    "before gta 6",   # alternate phrasing
]

# Markets priced near 50/50 with low volume are likely abandoned with no real price discovery.
STALE_PRICE_LO = 0.45
STALE_PRICE_HI = 0.55
STALE_MAX_VOLUME = 1000.0


def _get_all_markets_clob() -> list[dict]:
    """
    Page through the CLOB /markets endpoint and return all active markets.
    Each market dict includes tokens (YES/NO) with current prices.
    """
    markets: list[dict] = []
    next_cursor = ""
    page = 0

    while page < MAX_PAGES:
        params: dict = {"active": "true", "closed": "false", "limit": PAGE_SIZE}
        if next_cursor:
            params["next_cursor"] = next_cursor

        try:
            resp = requests.get(f"{CLOB_BASE}/markets", params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.error("CLOB /markets request failed: %s", exc)
            break

        batch = data.get("data", [])
        markets.extend(batch)
        page += 1
        logger.info("CLOB page %d/%d: got %d markets (total: %d)", page, MAX_PAGES, len(batch), len(markets))

        next_cursor = data.get("next_cursor", "")
        if not next_cursor or next_cursor == "LTE=":
            break

    return markets


def _get_gamma_markets_by_tag(tag: str) -> list[dict]:
    """
    Use the Gamma Markets API to fetch markets by tag/category.
    Returns raw market dicts including condition_id and question.
    """
    markets: list[dict] = []
    offset = 0
    page = 0

    while page < MAX_PAGES:
        params = {
            "active": "true",
            "closed": "false",
            "tag": tag,
            "limit": PAGE_SIZE,
            "offset": offset,
        }
        try:
            resp = requests.get(f"{GAMMA_BASE}/markets", params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            batch = resp.json()
        except Exception as exc:
            logger.error("Gamma /markets tag=%s request failed: %s", tag, exc)
            break

        if not batch:
            break

        markets.extend(batch)
        page += 1
        logger.info("Gamma tag=%-10s page %d/%d: got %d markets (total: %d)", tag, page, MAX_PAGES, len(batch), len(markets))

        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    return markets


def _best_yes_price(market: dict) -> float | None:
    """
    Extract the best YES token price from a CLOB market dict.
    Returns a float in [0, 1] or None.
    """
    tokens = market.get("tokens", [])
    for token in tokens:
        if token.get("outcome", "").upper() == "YES":
            price = token.get("price")
            if price is not None:
                try:
                    return float(price)
                except (TypeError, ValueError):
                    pass
    return None


MIN_DAYS_TO_RESOLUTION = 7  # exclude markets resolving within this many days


def _is_expired_or_imminent(market: dict) -> bool:
    """
    Return True if the market has already expired OR resolves within
    MIN_DAYS_TO_RESOLUTION days. Imminent markets have little signal value —
    prices are already converging to the outcome.
    """
    end_raw = market.get("endDate") or market.get("end_date_iso") or market.get("endDateIso")
    if not end_raw:
        return False
    try:
        end_raw = str(end_raw).rstrip("Z")
        if "T" in end_raw:
            end_dt = datetime.fromisoformat(end_raw).replace(tzinfo=timezone.utc)
        else:
            end_dt = datetime.fromisoformat(end_raw + "T23:59:59").replace(tzinfo=timezone.utc)
        cutoff = datetime.now(timezone.utc) + timedelta(days=MIN_DAYS_TO_RESOLUTION)
        return end_dt < cutoff
    except (ValueError, TypeError):
        return False


def _has_sufficient_volume(market: dict) -> bool:
    """Return True if the market's total volume >= MIN_VOLUME_USDC."""
    for key in ("volume", "volumeNum", "volumeClob"):
        val = market.get(key)
        if val is not None:
            try:
                return float(val) >= MIN_VOLUME_USDC
            except (TypeError, ValueError):
                pass
    return False  # exclude if no volume data available


def _is_blocklisted(question: str) -> bool:
    """Return True if the question contains any phrase from QUESTION_BLOCKLIST."""
    q = question.lower()
    return any(phrase in q for phrase in QUESTION_BLOCKLIST)


def _is_stale_coinflip(market: dict, price: float | None) -> bool:
    """
    Return True if the market looks like an abandoned coin-flip:
    YES price near 50/50 AND volume below STALE_MAX_VOLUME.
    """
    if price is None:
        return False
    if not (STALE_PRICE_LO <= price <= STALE_PRICE_HI):
        return False
    for key in ("volume", "volumeNum", "volumeClob"):
        val = market.get(key)
        if val is not None:
            try:
                return float(val) < STALE_MAX_VOLUME
            except (TypeError, ValueError):
                pass
    return False


def _category_from_tags(tags: list) -> str:
    """Map a list of tag dicts/strings to a clean category label."""
    labels = set()
    for t in tags:
        slug = (t.get("slug", "") if isinstance(t, dict) else str(t)).lower()
        if "crypto" in slug or "cryptocurrency" in slug:
            labels.add("Crypto")
        elif "politi" in slug:
            labels.add("Politics")
    return "/".join(sorted(labels)) if labels else "Other"


def fetch_target_markets() -> list[dict]:
    """
    Return a deduplicated list of active Politics and Crypto markets.

    Each item is a dict with:
        market_id   : str  (condition_id / CLOB market id)
        question    : str
        category    : str  ("Politics", "Crypto", or "Politics/Crypto")
        market_price: float | None  (best YES price, 0-1)
    """
    # Strategy: pull from Gamma (has tag metadata) then match prices from CLOB
    seen_ids: set[str] = set()
    results: list[dict] = []

    expired_count = 0
    low_volume_count = 0
    blocklisted_count = 0
    stale_coinflip_count = 0

    for tag in ("politics", "crypto"):
        raw = _get_gamma_markets_by_tag(tag)
        for m in raw:
            cid = m.get("conditionId") or m.get("condition_id") or m.get("id", "")
            if not cid or cid in seen_ids:
                continue

            if _is_expired_or_imminent(m):
                expired_count += 1
                continue

            if not _has_sufficient_volume(m):
                low_volume_count += 1
                continue

            question = m.get("question", "")
            if _is_blocklisted(question):
                blocklisted_count += 1
                continue

            # Parse price before stale-coinflip check (needs the value)
            price: float | None = None
            outcomes_prices_raw = m.get("outcomePrices")
            try:
                if outcomes_prices_raw:
                    if isinstance(outcomes_prices_raw, str):
                        outcomes_prices_raw = json.loads(outcomes_prices_raw)
                    price = float(outcomes_prices_raw[0])
            except (TypeError, ValueError, IndexError):
                price = None

            if _is_stale_coinflip(m, price):
                stale_coinflip_count += 1
                continue

            seen_ids.add(cid)

            tags_raw = m.get("tags") or []
            category = _category_from_tags(tags_raw) or tag.capitalize()

            results.append(
                {
                    "market_id": cid,
                    "question": question,
                    "category": category,
                    "market_price": price,
                }
            )

    logger.info(
        "Filtered out: %d expired/imminent (<%dd), %d low-volume (<$%.0f), %d blocklisted, %d stale coin-flips (<$%.0f vol @ 45-55%%)",
        expired_count, MIN_DAYS_TO_RESOLUTION, low_volume_count, MIN_VOLUME_USDC,
        blocklisted_count, stale_coinflip_count, STALE_MAX_VOLUME,
    )

    # If Gamma returned nothing, fall back to CLOB with tag filtering
    if not results:
        logger.warning("Gamma returned no markets; falling back to CLOB pagination")
        clob_markets = _get_all_markets_clob()
        for m in clob_markets:
            tags_raw = m.get("tags") or []
            tag_slugs = {
                (t.get("slug", "") if isinstance(t, dict) else str(t)).lower()
                for t in tags_raw
            }
            if not (tag_slugs & TARGET_TAGS):
                continue
            cid = m.get("condition_id") or m.get("id", "")
            if not cid or cid in seen_ids:
                continue
            seen_ids.add(cid)
            results.append(
                {
                    "market_id": cid,
                    "question": m.get("question", ""),
                    "category": _category_from_tags(tags_raw),
                    "market_price": _best_yes_price(m),
                }
            )

    logger.info("Total target markets found: %d", len(results))
    return results
