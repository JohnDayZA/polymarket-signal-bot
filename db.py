"""
SQLite database layer for Polymarket signal logging.
"""

import sqlite3
import os
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.getenv("DB_PATH", "signals.db")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create tables and migrate schema if needed."""
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS signals (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       TEXT    NOT NULL,
                market_id       TEXT    NOT NULL,
                question        TEXT    NOT NULL,
                category        TEXT    NOT NULL,
                market_price    REAL,           -- current best YES price (0-1)
                claude_prob     REAL,           -- Claude estimated probability (0-1)
                confidence      TEXT,           -- low / medium / high
                reasoning       TEXT,
                vix             REAL,
                fear_greed_value    INTEGER,
                fear_greed_label    TEXT,
                days_to_resolution  REAL,       -- days between signal timestamp and market end date
                resolved_value      REAL,       -- 1.0 = YES, 0.0 = NO, NULL = unresolved
                resolved_at         TEXT,       -- UTC ISO timestamp of resolution fetch
                was_claude_correct  INTEGER     -- 1 = correct, 0 = incorrect, NULL = unresolved
            );

            CREATE TABLE IF NOT EXISTS run_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT    NOT NULL,
                markets_processed   INTEGER,
                errors      INTEGER,
                notes       TEXT
            );
        """)

        # Migrate existing tables that pre-date the resolution columns
        existing = {row[1] for row in conn.execute("PRAGMA table_info(signals)")}
        for col, typedef in [
            ("days_to_resolution", "REAL"),
            ("resolved_value",     "REAL"),
            ("resolved_at",        "TEXT"),
            ("was_claude_correct", "INTEGER"),
        ]:
            if col not in existing:
                conn.execute(f"ALTER TABLE signals ADD COLUMN {col} {typedef}")


def log_signal(
    market_id: str,
    question: str,
    category: str,
    market_price: float | None,
    claude_prob: float | None,
    confidence: str | None,
    reasoning: str | None,
    vix: float | None,
    fear_greed_value: int | None,
    fear_greed_label: str | None,
    days_to_resolution: float | None = None,
) -> int:
    """Insert one signal row. Returns the new row id."""
    ts = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO signals
              (timestamp, market_id, question, category, market_price,
               claude_prob, confidence, reasoning, vix,
               fear_greed_value, fear_greed_label, days_to_resolution)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ts, market_id, question, category, market_price,
                claude_prob, confidence, reasoning, vix,
                fear_greed_value, fear_greed_label, days_to_resolution,
            ),
        )
        return cur.lastrowid


def log_run(markets_processed: int, errors: int, notes: str = "") -> None:
    ts = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO run_log (timestamp, markets_processed, errors, notes) VALUES (?,?,?,?)",
            (ts, markets_processed, errors, notes),
        )
