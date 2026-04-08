"""
Resolution tracker for Polymarket signals.

For every unique market_id in signals.db that is still unresolved,
fetches the current market state from the CLOB API and writes back:
  - resolved_value      (1.0 = YES, 0.0 = NO)
  - resolved_at         (UTC ISO timestamp)
  - was_claude_correct  (1 if Claude's direction matched outcome, else 0)

A market is considered resolved when:
  - closed = true  AND
  - one token has winner = true  (YES → 1.0, NO → 0.0)

Run daily:
    python resolve.py                    # process up to 200 markets
    python resolve.py --limit 500        # process up to 500 markets
    python resolve.py --dry-run          # print without writing to DB
"""

import argparse
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

CLOB_BASE = "https://clob.polymarket.com"
REQUEST_TIMEOUT = (5, 10)   # (connect timeout, read timeout) in seconds


# ---------------------------------------------------------------------------
# CLOB API helpers
# ---------------------------------------------------------------------------
# The market_id stored in signals.db is the CLOB condition_id (hex string).
# The CLOB API supports direct lookup via GET /markets/{condition_id} and
# is the authoritative source for resolution — resolved tokens have winner=true.
# (The Gamma API uses a different internal conditionId and cannot be queried
# by CLOB condition_id, so it is not used here.)

def _fetch_market(condition_id: str) -> dict | None:
    """Fetch a single market by condition_id from the CLOB API."""
    try:
        resp = requests.get(
            f"{CLOB_BASE}/markets/{condition_id}",
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.error("CLOB fetch failed for %s: %s", condition_id[:16], exc)
        return None


def _parse_outcome(market: dict) -> float | None:
    """
    Return 1.0 if the market resolved YES, 0.0 if NO, None if still open.

    CLOB resolution signals:
      - closed = true (market has stopped trading)
      - One token has winner = true
        * outcome "Yes" winner → 1.0
        * outcome "No"  winner → 0.0
    """
    if not market.get("closed", False):
        return None

    tokens = market.get("tokens") or []
    for token in tokens:
        if token.get("winner", False):
            outcome = token.get("outcome", "").strip().lower()
            if outcome == "yes":
                return 1.0
            if outcome == "no":
                return 0.0

    return None  # closed but no winner token yet (still settling)


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

def resolve(dry_run: bool = False, limit: int = 200) -> None:
    db.init_db()

    with db.get_connection() as conn:
        # One row per distinct market_id (GROUP BY deduplicates repeated signals for
        # the same market so --limit means N unique markets, not N signal rows).
        unresolved = conn.execute("""
            SELECT market_id,
                   MAX(question)    AS question,
                   MAX(claude_prob) AS claude_prob
            FROM signals
            WHERE resolved_value IS NULL
              AND market_id != ''
            GROUP BY market_id
            ORDER BY market_id
            LIMIT ?
        """, (limit,)).fetchall()

    if not unresolved:
        logger.info("No unresolved markets found.")
        return

    logger.info("Checking %d unresolved market(s) (limit=%d)...", len(unresolved), limit)

    resolved_count = 0
    skipped_count = 0
    error_count = 0
    now = datetime.now(timezone.utc).isoformat()

    for i, row in enumerate(unresolved, 1):
        if i % 50 == 0:
            logger.info(
                "  Progress: %d/%d checked (%d resolved, %d open, %d errors)",
                i, len(unresolved), resolved_count, skipped_count, error_count,
            )
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
    parser.add_argument("--limit", type=int, default=200, metavar="N",
                        help="Max distinct markets to check per run (default: 200)")
    args = parser.parse_args()
    resolve(dry_run=args.dry_run, limit=args.limit)


if __name__ == "__main__":
    main()
