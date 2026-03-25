"""
Polymarket Signal Backtest
==========================
Fetches resolved markets from the last N days, runs Claude probability
estimates against each question, then reports:
  - Overall directional accuracy
  - Accuracy by category
  - Profit factor assuming Kelly-sized positions on signals with
    abs(claude_prob - market_price) > gap_threshold

Usage:
    python backtest.py
    python backtest.py --days 60 --gap 0.10 --max-markets 100 --min-volume 1000
    python backtest.py --dry-run   # skip Claude calls, print market list only
"""

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

import claude_signals
from polymarket import QUESTION_BLOCKLIST, MIN_VOLUME_USDC

# Additional phrases filtered specifically for backtest quality.
# These are high-frequency / sports / micro-window markets where Claude
# has no meaningful edge and that pollute accuracy/P&L stats.
BACKTEST_EXTRA_BLOCKLIST = [
    "up or down",           # Bitcoin/ETH 5-minute price flip markets
    "o/u ",                 # sports over/under
    "over/under",
    " vs. ",                # sports head-to-head game results
    " vs ",
    "spread:",              # sports spread markets
    "map handicap",         # esports
    "match winner",
    "moneyline",
    "will.*win on ",        # "Will X FC win on <date>"
    "t20 series",
    "BO3",                  # best-of-3 esports
    "will.*score",
    "total goals",
    "correct score",
]

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[
        logging.StreamHandler(
            stream=open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False)
        ),
        logging.FileHandler("backtest.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("backtest")

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
REQUEST_TIMEOUT = 10
KELLY_MAX_FRACTION = 0.25       # cap Kelly bet at 25% of bankroll
RESOLUTION_THRESHOLD = 0.97     # outcomePrices must be >= this to count as resolved


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class MarketRecord:
    market_id: str
    condition_id: str
    question: str
    category: str
    end_date: datetime
    outcome: float              # 1.0 = YES, 0.0 = NO
    market_price: float | None  # pre-resolution YES price
    volume: float
    clob_token_id: str          # YES token id


@dataclass
class SignalRecord:
    market: MarketRecord
    claude_prob: float
    confidence: str
    reasoning: str
    gap: float                  # claude_prob - market_price
    direction: str              # "YES" | "NO" | "SKIP"
    kelly_fraction: float
    pnl: float                  # realised P&L as fraction of bankroll
    correct: bool | None        # None if no directional call


# ---------------------------------------------------------------------------
# Market fetching
# ---------------------------------------------------------------------------

def _parse_outcome(market: dict) -> float | None:
    """
    Return 1.0 (YES), 0.0 (NO), or None (unresolved / ambiguous).
    Handles exact 0/1 strings and near-settled float values.
    """
    raw = market.get("outcomePrices")
    if not raw:
        return None
    try:
        prices = json.loads(raw) if isinstance(raw, str) else raw
        yes_p = float(prices[0])
        no_p = float(prices[1])
    except (ValueError, TypeError, IndexError):
        return None

    if yes_p >= RESOLUTION_THRESHOLD and no_p <= (1 - RESOLUTION_THRESHOLD):
        return 1.0
    if no_p >= RESOLUTION_THRESHOLD and yes_p <= (1 - RESOLUTION_THRESHOLD):
        return 0.0
    return None


def _parse_end_date(market: dict) -> datetime | None:
    raw = market.get("endDate") or market.get("endDateIso")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).rstrip("Z")).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _category_from_tags(tags: list) -> str:
    labels = set()
    for t in tags:
        slug = (t.get("slug", "") if isinstance(t, dict) else str(t)).lower()
        if "crypto" in slug or "cryptocurrency" in slug:
            labels.add("Crypto")
        elif "politi" in slug:
            labels.add("Politics")
    return "/".join(sorted(labels)) if labels else "Other"


def _is_blocklisted(question: str) -> bool:
    import re
    q = question.lower()
    if any(phrase in q for phrase in QUESTION_BLOCKLIST):
        return True
    # Check extra backtest-specific patterns (supports simple regex via re.search)
    for pattern in BACKTEST_EXTRA_BLOCKLIST:
        try:
            if re.search(pattern, q):
                return True
        except re.error:
            if pattern in q:
                return True
    return False


def fetch_resolved_markets(
    days: int,
    min_volume: float,
    max_markets: int | None,
) -> list[MarketRecord]:
    """
    Page through Gamma for closed markets in the last `days` days,
    across Politics and Crypto tags. Returns deduplicated MarketRecord list.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    seen: set[str] = set()
    records: list[MarketRecord] = []

    for tag in ("politics", "crypto"):
        offset = 0
        page = 0
        while True:
            params = {
                "closed": "true",
                "tag": tag,
                "limit": 100,
                "offset": offset,
                "end_date_min": cutoff_str,
                "end_date_max": now_str,
                "order": "volume",
                "ascending": "false",
            }
            try:
                resp = requests.get(f"{GAMMA_BASE}/markets", params=params, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                batch = resp.json()
            except Exception as exc:
                logger.error("Gamma fetch error (tag=%s page=%d): %s", tag, page, exc)
                break

            if not batch:
                break

            page += 1
            added = 0
            for m in batch:
                cid = m.get("conditionId") or m.get("condition_id") or m.get("id", "")
                if not cid or cid in seen:
                    continue

                outcome = _parse_outcome(m)
                if outcome is None:
                    continue

                end_dt = _parse_end_date(m)
                if end_dt is None:
                    continue

                question = m.get("question", "").strip()
                if not question or _is_blocklisted(question):
                    continue

                vol = 0.0
                for key in ("volume", "volumeNum", "volumeClob"):
                    try:
                        vol = float(m.get(key) or 0)
                        if vol > 0:
                            break
                    except (TypeError, ValueError):
                        pass
                if vol < min_volume:
                    continue

                token_ids_raw = m.get("clobTokenIds", "[]")
                try:
                    token_ids = json.loads(token_ids_raw) if isinstance(token_ids_raw, str) else token_ids_raw
                    clob_token_id = token_ids[0] if token_ids else ""
                except (ValueError, IndexError):
                    clob_token_id = ""

                tags_raw = m.get("tags") or []
                category = _category_from_tags(tags_raw) or tag.capitalize()

                seen.add(cid)
                records.append(MarketRecord(
                    market_id=m.get("id", cid),
                    condition_id=cid,
                    question=question,
                    category=category,
                    end_date=end_dt,
                    outcome=outcome,
                    market_price=None,  # filled later
                    volume=vol,
                    clob_token_id=clob_token_id,
                ))
                added += 1

            logger.info(
                "Gamma tag=%-10s page %d: %d/%d batch → %d new records (total: %d)",
                tag, page, added, len(batch), added, len(records),
            )

            if len(batch) < 100:
                break
            if max_markets and len(records) >= max_markets * 3:
                break  # fetch extra to account for price-fetch failures
            offset += 100

    # Sort by volume descending and cap
    records.sort(key=lambda r: r.volume, reverse=True)
    if max_markets:
        records = records[:max_markets]

    logger.info("Resolved markets after dedup + filters: %d", len(records))
    return records


# ---------------------------------------------------------------------------
# Pre-resolution price fetch
# ---------------------------------------------------------------------------

def fetch_pre_resolution_price(record: MarketRecord) -> float | None:
    """
    Fetches the last YES price from CLOB history in the 48h window
    before the market's endDate. Returns None if unavailable.
    """
    if not record.clob_token_id:
        return None

    end_ts = int(record.end_date.timestamp())
    start_ts = int((record.end_date - timedelta(hours=48)).timestamp())

    try:
        resp = requests.get(
            f"{CLOB_BASE}/prices-history",
            params={
                "market": record.clob_token_id,
                "startTs": start_ts,
                "endTs": end_ts,
                "fidelity": 60,
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        history = resp.json().get("history", [])
    except Exception as exc:
        logger.debug("CLOB price history failed for %s: %s", record.condition_id[:12], exc)
        return None

    if not history:
        return None

    # Take the last price point before resolution
    try:
        last = history[-1]
        price = float(last.get("p", last.get("price", 0)))
        # Sanity check: ignore if already settled (>= threshold)
        if price >= RESOLUTION_THRESHOLD or price <= (1 - RESOLUTION_THRESHOLD):
            # Try second-to-last
            if len(history) >= 2:
                price = float(history[-2].get("p", history[-2].get("price", 0)))
            else:
                return None
        return price if 0.01 <= price <= 0.99 else None
    except (TypeError, ValueError, KeyError):
        return None


# ---------------------------------------------------------------------------
# Kelly criterion
# ---------------------------------------------------------------------------

def kelly_fraction(claude_prob: float, market_price: float, direction: str) -> float:
    """
    Full Kelly fraction for a binary prediction market position.
    direction: "YES" (buying YES token) or "NO" (buying NO token)
    Capped at KELLY_MAX_FRACTION.
    """
    try:
        if direction == "YES":
            # b = net odds on YES = (1 - p_market) / p_market
            b = (1.0 - market_price) / market_price
            f = (claude_prob * (1.0 + b) - 1.0) / b
        else:
            # Buying NO token at price (1 - market_price)
            p_no_market = 1.0 - market_price
            p_no_claude = 1.0 - claude_prob
            b = market_price / p_no_market
            f = (p_no_claude * (1.0 + b) - 1.0) / b
    except ZeroDivisionError:
        return 0.0

    return max(0.0, min(KELLY_MAX_FRACTION, f))


def realised_pnl(fraction: float, market_price: float, direction: str, outcome: float) -> float:
    """Return P&L as a fraction of bankroll for one bet."""
    if fraction <= 0:
        return 0.0
    if direction == "YES":
        return fraction * (1.0 - market_price) / market_price if outcome == 1.0 else -fraction
    else:  # NO
        return fraction * market_price / (1.0 - market_price) if outcome == 0.0 else -fraction


# ---------------------------------------------------------------------------
# Backtest runner
# ---------------------------------------------------------------------------

def run_backtest(
    days: int,
    gap_threshold: float,
    max_markets: int | None,
    min_volume: float,
    dry_run: bool,
) -> list[SignalRecord]:
    logger.info("=== Backtest: last %d days | gap>=%.2f | min_vol=$%.0f ===", days, gap_threshold, min_volume)

    markets = fetch_resolved_markets(days, min_volume, max_markets)
    if not markets:
        logger.warning("No resolved markets found.")
        return []

    # Fetch pre-resolution prices
    logger.info("Fetching pre-resolution prices from CLOB...")
    price_hits = 0
    for rec in markets:
        rec.market_price = fetch_pre_resolution_price(rec)
        if rec.market_price is not None:
            price_hits += 1
    logger.info("Pre-resolution price available for %d/%d markets", price_hits, len(markets))

    if dry_run:
        logger.info("[dry-run] Skipping Claude calls. Market list:")
        for r in markets:
            logger.info("  [%s] outcome=%.0f price=%s vol=%.0f | %s",
                r.category, r.outcome,
                f"{r.market_price:.3f}" if r.market_price else "N/A",
                r.volume, r.question[:70])
        return []

    # Run Claude signals
    results: list[SignalRecord] = []
    logger.info("Running Claude estimates on %d markets...", len(markets))

    for i, rec in enumerate(markets, 1):
        logger.info("[%d/%d] %s", i, len(markets), rec.question[:80])

        signal = claude_signals.estimate_signal(
            question=rec.question,
            category=rec.category,
            market_price=rec.market_price,
        )
        if signal is None:
            logger.warning("  Claude failed, skipping")
            continue

        prob = signal["probability"]
        price = rec.market_price

        if price is None:
            direction = "SKIP"
            gap = prob - 0.5  # vs neutral baseline
            kf = 0.0
            pnl = 0.0
        else:
            gap = prob - price
            if abs(gap) >= gap_threshold:
                direction = "YES" if gap > 0 else "NO"
                kf = kelly_fraction(prob, price, direction)
                pnl = realised_pnl(kf, price, direction, rec.outcome)
            else:
                direction = "SKIP"
                kf = 0.0
                pnl = 0.0

        correct: bool | None = None
        if prob > 0.5:
            correct = rec.outcome == 1.0
        elif prob < 0.5:
            correct = rec.outcome == 0.0

        outcome_label = "YES" if rec.outcome == 1.0 else "NO"
        logger.info(
            "  claude=%.3f | mkt=%s | gap=%+.3f | dir=%-4s | kf=%.3f | pnl=%+.4f | outcome=%s | correct=%s",
            prob,
            f"{price:.3f}" if price else " N/A",
            gap,
            direction,
            kf,
            pnl,
            outcome_label,
            str(correct),
        )

        results.append(SignalRecord(
            market=rec,
            claude_prob=prob,
            confidence=signal["confidence"],
            reasoning=signal["reasoning"],
            gap=gap,
            direction=direction,
            kelly_fraction=kf,
            pnl=pnl,
            correct=correct,
        ))

        # Small delay to respect API rate limits
        time.sleep(0.5)

    return results


# ---------------------------------------------------------------------------
# Summary statistics
# ---------------------------------------------------------------------------

def print_summary(results: list[SignalRecord], gap_threshold: float) -> None:
    if not results:
        print("\nNo results to summarise.")
        return

    # --- Overall accuracy ---
    directional = [r for r in results if r.correct is not None]
    correct_count = sum(1 for r in directional if r.correct)
    accuracy = correct_count / len(directional) if directional else 0.0

    # --- Accuracy by category ---
    cats: dict[str, list[SignalRecord]] = {}
    for r in directional:
        cats.setdefault(r.market.category, []).append(r)

    # --- Kelly P&L stats ---
    traded = [r for r in results if r.direction != "SKIP"]
    wins = [r for r in traded if r.pnl > 0]
    losses = [r for r in traded if r.pnl < 0]
    total_profit = sum(r.pnl for r in wins)
    total_loss = abs(sum(r.pnl for r in losses))
    profit_factor = total_profit / total_loss if total_loss > 0 else float("inf")
    cumulative_pnl = sum(r.pnl for r in traded)

    sep = "=" * 65

    print(f"\n{sep}")
    print("  BACKTEST SUMMARY")
    print(sep)
    print(f"  Markets evaluated      : {len(results)}")
    print(f"  Directional signals    : {len(directional)}")
    print(f"  Overall accuracy       : {correct_count}/{len(directional)} = {accuracy:.1%}")
    print()

    print("  Accuracy by category:")
    for cat, recs in sorted(cats.items()):
        c = sum(1 for r in recs if r.correct)
        print(f"    {cat:<20} {c}/{len(recs)} = {c/len(recs):.1%}")
    print()

    print(f"  Kelly positions (gap >= {gap_threshold:.2f}):")
    print(f"    Signals traded         : {len(traded)}")
    print(f"    Wins / Losses          : {len(wins)} / {len(losses)}")
    print(f"    Total profit (units)   : {total_profit:+.4f}")
    print(f"    Total loss   (units)   : {-total_loss:+.4f}")
    print(f"    Cumulative P&L         : {cumulative_pnl:+.4f}")
    print(f"    Profit factor          : {profit_factor:.3f}")

    if traded:
        avg_kf = sum(r.kelly_fraction for r in traded) / len(traded)
        print(f"    Avg Kelly fraction     : {avg_kf:.3f}")

    print()

    if traded:
        print("  Top 5 best trades:")
        for r in sorted(traded, key=lambda x: x.pnl, reverse=True)[:5]:
            outcome_label = "YES" if r.market.outcome == 1.0 else "NO "
            print(f"    {r.pnl:+.4f} | dir={r.direction} out={outcome_label} | {r.market.question[:55]}")

        print()
        print("  Top 5 worst trades:")
        for r in sorted(traded, key=lambda x: x.pnl)[:5]:
            outcome_label = "YES" if r.market.outcome == 1.0 else "NO "
            print(f"    {r.pnl:+.4f} | dir={r.direction} out={outcome_label} | {r.market.question[:55]}")

    print(sep)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Polymarket signal backtest")
    p.add_argument("--days",        type=int,   default=90,    help="Look-back window in days (default: 90)")
    p.add_argument("--gap",         type=float, default=0.15,  help="Min |claude_prob - market_price| to trade (default: 0.15)")
    p.add_argument("--max-markets", type=int,   default=100,   help="Max resolved markets to evaluate (default: 100)")
    p.add_argument("--min-volume",  type=float, default=500.0, help="Min market volume in USDC (default: 500)")
    p.add_argument("--dry-run",     action="store_true",       help="List markets without calling Claude")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    results = run_backtest(
        days=args.days,
        gap_threshold=args.gap,
        max_markets=args.max_markets,
        min_volume=args.min_volume,
        dry_run=args.dry_run,
    )
    if results:
        print_summary(results, gap_threshold=args.gap)


if __name__ == "__main__":
    main()
