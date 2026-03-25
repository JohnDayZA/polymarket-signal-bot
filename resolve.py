"""
Resolution tracker for Polymarket signals.

For every unique market_id in signals.db that is still unresolved,
fetches the current market state from the Gamma API and writes back:
  - resolved_value      (1.0 = YES, 0.0 = NO)
  - resolved_at         (UTC ISO timestamp)
  - was_claude_correct  (1 if Claude's direction matched outcome, else 0)

A market is considered resolved when:
  - closed = true  AND
  - outcomePrices is ["1", "0"] (YES wins) or ["0", "1"] (NO wins)

Run daily:
    python resolve.py
    python resolve.py --dry-run   # print without writing
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

import db

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[
        logging.StreamHandler(stream=open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False)),
        logging.FileHandler("resolve.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("resolve")

GAMMA_BASE = "https://gamma-api.polymarket.com"
REQUEST_TIMEOUT = 10


# ---------------------------------------------------------------------------
# Gamma API helpers
# ---------------------------------------------------------------------------

def _fetch_market(condition_id: str) -> dict | None:
    """Fetch a single market by conditionId from the Gamma API."""
    try:
        resp = requests.get(
            f"{GAMMA_BASE}/markets",
            params={"conditionId": condition_id},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        # Gamma returns a list; grab the first match
        if isinstance(data, list) and data:
            return data[0]
        if isinstance(data, dict):
            return data
        return None
    except Exception as exc:
        logger.error("Gamma fetch failed for %s: %s", condition_id[:16], exc)
        return None


def _parse_outcome(market: dict) -> float | None:
    """
    Return 1.0 if the market resolved YES, 0.0 if NO, None if still open.
    Uses outcomePrices: a fully-resolved YES market has prices ["1", "0"].
    Also respects the closed flag — unresolved markets stay open.
    """
    if not market.get("closed", False):
        return None

    raw = market.get("outcomePrices")
    if not raw:
        return None
    try:
        if isinstance(raw, str):
            raw = json.loads(raw)
        yes_price = float(raw[0])
        no_price = float(raw[1])
    except (ValueError, TypeError, IndexError):
        return None

    if yes_price == 1.0 and no_price == 0.0:
        return 1.0
    if yes_price == 0.0 and no_price == 1.0:
        return 0.0
    return None  # still trading / not fully settled


def _was_correct(claude_prob: float | None, resolved_value: float) -> int | None:
    """
    Return 1 if Claude's direction matched the outcome, 0 if not, None if no prob.
    Rule: claude_prob > 0.5 → predicted YES; < 0.5 → predicted NO; == 0.5 → abstain.
    """
    if claude_prob is None:
        return None
    if claude_prob > 0.5:
        return 1 if resolved_value == 1.0 else 0
    if claude_prob < 0.5:
        return 1 if resolved_value == 0.0 else 0
    return None  # exactly 0.5 — no directional call


# ---------------------------------------------------------------------------
# Main resolution loop
# ---------------------------------------------------------------------------

def resolve(dry_run: bool = False) -> None:
    db.init_db()

    with db.get_connection() as conn:
        # Distinct markets that have no resolved_value yet and have a real condition_id
        unresolved = conn.execute("""
            SELECT DISTINCT market_id,
                   question,
                   claude_prob
            FROM signals
            WHERE resolved_value IS NULL
              AND market_id != ''
            ORDER BY market_id
        """).fetchall()

    if not unresolved:
        logger.info("No unresolved markets found.")
        return

    logger.info("Checking %d unresolved market(s)...", len(unresolved))

    resolved_count = 0
    skipped_count = 0
    error_count = 0
    now = datetime.now(timezone.utc).isoformat()

    for row in unresolved:
        market_id = row["market_id"]
        question = row["question"]
        claude_prob = row["claude_prob"]

        market = _fetch_market(market_id)
        if market is None:
            error_count += 1
            continue

        outcome = _parse_outcome(market)
        if outcome is None:
            logger.debug("  Still open: %s", question[:70])
            skipped_count += 1
            continue

        correct = _was_correct(claude_prob, outcome)
        outcome_label = "YES" if outcome == 1.0 else "NO"
        correct_label = {1: "CORRECT", 0: "WRONG", None: "N/A"}[correct]

        logger.info(
            "  RESOLVED %s | Claude=%.3f | correct=%s | %s",
            outcome_label,
            claude_prob if claude_prob is not None else -1,
            correct_label,
            question[:70],
        )

        if not dry_run:
            with db.get_connection() as conn:
                conn.execute(
                    """
                    UPDATE signals
                    SET resolved_value = ?,
                        resolved_at = ?,
                        was_claude_correct = ?
                    WHERE market_id = ?
                      AND resolved_value IS NULL
                    """,
                    (outcome, now, correct, market_id),
                )

        resolved_count += 1

    logger.info(
        "Done: %d resolved, %d still open, %d errors%s",
        resolved_count,
        skipped_count,
        error_count,
        " [DRY RUN — no writes]" if dry_run else "",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket resolution tracker")
    parser.add_argument("--dry-run", action="store_true", help="Print results without writing to DB")
    args = parser.parse_args()
    resolve(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
