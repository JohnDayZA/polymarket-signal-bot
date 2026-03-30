"""
Polymarket Signal Validation Bot
=================================
Data collection only — no trading or order execution.

Run:
    python main.py [--max-markets N] [--dry-run]

For each active Politics/Crypto market on Polymarket:
  1. Fetch markets from the CLOB API
  2. Ask Claude to estimate YES probability + confidence + reasoning
  3. Fetch CNN Fear & Greed Index (RapidAPI) and VIX (yfinance)
  4. Log everything to SQLite

All API keys are loaded from .env (see .env.example).
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

# ---- Logging setup --------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[
        logging.StreamHandler(stream=open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False)),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")

# ---- Project modules -------------------------------------------------------
import db
import polymarket
import claude_signals
import market_data


# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Polymarket Signal Validation Bot")
    parser.add_argument(
        "--max-markets",
        type=int,
        default=None,
        help="Limit the number of markets processed (overrides MAX_MARKETS env var)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and log to console only; do not write to the database",
    )
    return parser.parse_args()


def resolve_max_markets(cli_value: int | None) -> int | None:
    if cli_value is not None:
        return cli_value
    env_val = os.getenv("MAX_MARKETS", "").strip()
    if env_val.isdigit():
        return int(env_val)
    return None


def run(max_markets: int | None, dry_run: bool) -> None:
    logger.info("=== Polymarket Signal Bot starting ===")

    if not dry_run:
        db.init_db()
        logger.info("Database initialised at %s", os.getenv("DB_PATH", "signals.db"))

    # 1. Fetch external market context once per run (shared across all markets)
    logger.info("Fetching market snapshot (VIX + Fear & Greed)...")
    snapshot = market_data.fetch_market_snapshot()
    logger.info(
        "Snapshot -> VIX=%.2f | Fear&Greed=%s (%s)",
        snapshot["vix"] or 0.0,
        snapshot["fear_greed_value"],
        snapshot["fear_greed_label"],
    )

    # 2. Fetch target markets
    logger.info("Fetching active Politics/Crypto markets from Polymarket...")
    markets = polymarket.fetch_target_markets()

    if not markets:
        logger.warning("No markets returned. Check network or API availability.")
        if not dry_run:
            db.log_run(markets_processed=0, errors=0, notes="No markets returned")
        return

    if max_markets:
        markets = markets[:max_markets]

    logger.info("Processing %d market(s)...", len(markets))

    processed = 0
    errors = 0

    for i, mkt in enumerate(markets, start=1):
        mid = mkt["market_id"]
        question = mkt["question"]
        category = mkt["category"]
        price = mkt["market_price"]

        # Calculate days until resolution from end_date
        days_to_resolution: float | None = None
        end_date_raw = mkt.get("end_date")
        if end_date_raw:
            try:
                end_raw = str(end_date_raw).rstrip("Z")
                if "T" in end_raw:
                    end_dt = datetime.fromisoformat(end_raw).replace(tzinfo=timezone.utc)
                else:
                    end_dt = datetime.fromisoformat(end_raw + "T23:59:59").replace(tzinfo=timezone.utc)
                days_to_resolution = (end_dt - datetime.now(timezone.utc)).total_seconds() / 86400
            except (ValueError, TypeError):
                pass

        logger.info(
            "[%d/%d] %s | price=%.3f | %s",
            i, len(markets),
            question[:80],
            price if price is not None else 0.0,
            category,
        )

        # 3. Ask Claude for a signal
        signal = claude_signals.estimate_signal(
            question=question,
            category=category,
            market_price=price,
        )

        if signal is None:
            logger.warning("  -> Claude signal failed, logging with nulls")
            errors += 1
        else:
            logger.info(
                "  -> prob=%.3f | confidence=%s | %s",
                signal["probability"],
                signal["confidence"],
                signal["reasoning"][:120],
            )

        if dry_run:
            logger.info("  [dry-run] Skipping DB write")
        else:
            db.log_signal(
                market_id=mid,
                question=question,
                category=category,
                market_price=price,
                claude_prob=signal["probability"] if signal else None,
                confidence=signal["confidence"] if signal else None,
                reasoning=signal["reasoning"] if signal else None,
                vix=snapshot["vix"],
                fear_greed_value=snapshot["fear_greed_value"],
                fear_greed_label=snapshot["fear_greed_label"],
                days_to_resolution=days_to_resolution,
            )

        processed += 1

    # 4. Log the run summary
    if not dry_run:
        db.log_run(
            markets_processed=processed,
            errors=errors,
            notes=f"max_markets={max_markets}",
        )

    logger.info(
        "=== Run complete: %d processed, %d errors ===",
        processed,
        errors,
    )


def main() -> None:
    args = parse_args()
    max_markets = resolve_max_markets(args.max_markets)
    run(max_markets=max_markets, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
