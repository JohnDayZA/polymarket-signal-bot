"""
Polymarket Signal Bot — Health Check
=====================================
Checks scheduled tasks, database health, API connectivity, recent signals,
and recent resolutions. Prints a final HEALTHY / WARNING / CRITICAL status.

Usage:
    python health_check.py
"""

import os
import subprocess
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DB_PATH = os.getenv("DB_PATH", "signals.db")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "")
CLOB_BASE = "https://clob.polymarket.com"
FEAR_GREED_HOST = "fear-and-greed-index.p.rapidapi.com"
FEAR_GREED_URL = f"https://{FEAR_GREED_HOST}/v1/fgi"
TASK_NAMES = ["PolymarketSignalBot", "PolymarketResolve"]
REQUEST_TIMEOUT = 10

# Paper trading constants
MIN_EDGE = 0.05           # minimum claude_prob vs market_price gap to take a position
MIN_MARKET_PRICE = 0.10   # exclude near-impossible markets — matches polymarket.py
MAX_MARKET_PRICE = 0.90   # exclude near-certain markets — matches polymarket.py
MAX_POSITION_PCT = 0.10   # hard cap: no more than 10% of bankroll per trade
STARTING_CAPITALS = [50.0, 500.0]

# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
SEP = "-" * 68
WIDE_SEP = "=" * 68

def header(title: str) -> None:
    print(f"\n{WIDE_SEP}")
    print(f"  {title}")
    print(WIDE_SEP)

def sub(label: str, value: str, ok: bool | None = None) -> None:
    marker = ""
    if ok is True:
        marker = "  [OK]"
    elif ok is False:
        marker = "  [FAIL]"
    print(f"  {label:<32} {value}{marker}")

def row_sep() -> None:
    print(f"  {SEP}")

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

# ---------------------------------------------------------------------------
# 1. Scheduled tasks
# ---------------------------------------------------------------------------

def _query_task(task_name: str) -> dict:
    """Run schtasks and parse key fields for a single task."""
    result = {
        "name": task_name,
        "found": False,
        "status": "N/A",
        "last_run": "N/A",
        "last_result": "N/A",
        "next_run": "N/A",
        "last_result_ok": None,
    }
    try:
        proc = subprocess.run(
            ["schtasks", "/query", "/tn", task_name, "/fo", "LIST", "/v"],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            return result  # task not found

        result["found"] = True
        for line in proc.stdout.splitlines():
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            key = key.strip().lower()
            val = val.strip()
            if "status" in key and "logon" not in key:
                result["status"] = val
            elif "last run time" in key:
                result["last_run"] = val
            elif "last result" in key:
                result["last_result"] = val
                result["last_result_ok"] = (val.strip() == "0")
            elif "next run time" in key:
                result["next_run"] = val
    except Exception as exc:
        result["status"] = f"Error: {exc}"
    return result


def check_tasks() -> list[dict]:
    return [_query_task(name) for name in TASK_NAMES]


def print_tasks(tasks: list[dict]) -> None:
    header("1.  SCHEDULED TASKS")
    for t in tasks:
        print(f"\n  Task: {t['name']}")
        row_sep()
        if not t["found"]:
            sub("Status", "NOT FOUND in Task Scheduler", ok=False)
        else:
            sub("Status",      t["status"])
            sub("Last run",    t["last_run"])
            sub("Last result", t["last_result"] + (" (success)" if t["last_result_ok"] else " (ERROR)" if t["last_result"] != "N/A" else ""), ok=t["last_result_ok"])
            sub("Next run",    t["next_run"])

# ---------------------------------------------------------------------------
# 2. Database health
# ---------------------------------------------------------------------------

def check_db() -> dict:
    result = {
        "reachable": False,
        "total_signals": 0,
        "unique_markets": 0,
        "signals_24h": 0,
        "signals_1h": 0,
        "resolved_count": 0,
        "accuracy": None,
        "error": None,
    }
    if not os.path.exists(DB_PATH):
        result["error"] = f"File not found: {DB_PATH}"
        return result
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        cutoff_24h = (now_utc() - timedelta(hours=24)).isoformat()
        cutoff_1h  = (now_utc() - timedelta(hours=1)).isoformat()

        result["total_signals"]  = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        result["unique_markets"] = conn.execute("SELECT COUNT(DISTINCT market_id) FROM signals").fetchone()[0]
        result["signals_24h"]    = conn.execute("SELECT COUNT(*) FROM signals WHERE timestamp >= ?", (cutoff_24h,)).fetchone()[0]
        result["signals_1h"]     = conn.execute("SELECT COUNT(*) FROM signals WHERE timestamp >= ?", (cutoff_1h,)).fetchone()[0]
        result["resolved_count"] = conn.execute("SELECT COUNT(DISTINCT market_id) FROM signals WHERE resolved_value IS NOT NULL").fetchone()[0]

        acc_row = conn.execute("""
            SELECT
                SUM(was_claude_correct) AS correct,
                COUNT(was_claude_correct) AS total
            FROM signals
            WHERE was_claude_correct IS NOT NULL
        """).fetchone()
        if acc_row and acc_row["total"] and acc_row["total"] > 0:
            result["accuracy"] = acc_row["correct"] / acc_row["total"]

        conn.close()
        result["reachable"] = True
    except Exception as exc:
        result["error"] = str(exc)
    return result


def print_db(db: dict) -> None:
    header("2.  DATABASE HEALTH")
    print(f"\n  Path: {DB_PATH}")
    row_sep()
    if not db["reachable"]:
        sub("Status", f"UNREACHABLE — {db['error']}", ok=False)
        return

    sub("Status",                "Connected", ok=True)
    sub("Total signals",         f"{db['total_signals']:,}")
    sub("Unique markets tracked",f"{db['unique_markets']:,}")
    sub("Signals (last 24h)",    str(db["signals_24h"]), ok=(db["signals_24h"] > 0))
    sub("Signals (last 1h)",     str(db["signals_1h"]))
    sub("Resolved markets",      str(db["resolved_count"]))
    if db["accuracy"] is not None:
        pct = db["accuracy"] * 100
        sub("Claude accuracy",   f"{pct:.1f}%  ({int(db['accuracy'] * db['resolved_count'])}/{db['resolved_count']})")
    else:
        sub("Claude accuracy",   "N/A (no resolved markets yet)")

# ---------------------------------------------------------------------------
# 3. Paper trading P&L simulation
# ---------------------------------------------------------------------------

def _kelly_fraction(win_prob: float, win_price: float) -> float:
    """Full Kelly fraction for a binary outcome bet. Returns 0 if no edge."""
    if win_price <= 0.0 or win_price >= 1.0:
        return 0.0
    b = (1.0 - win_price) / win_price   # net profit per dollar staked on a win
    full_kelly = (win_prob * b - (1.0 - win_prob)) / b
    return max(0.0, full_kelly)


def check_paper_trading() -> dict:
    """
    Simulate paper trading on all resolved markets.
    Uses the first (earliest) signal row per market for claude_prob and market_price.
    Filters:
      - market_price must be within MIN_MARKET_PRICE..MAX_MARKET_PRICE (10-90%)
      - edge (|claude_prob - market_price|) must exceed MIN_EDGE (5%)
    Sizing: Quarter Kelly, hard-capped at MAX_POSITION_PCT of current bankroll.
    Bankroll updated sequentially in resolved_at order.
    """
    empty = {"error": None, "trades": [], "extreme_skipped": 0,
             "no_edge_skipped": 0, "null_skipped": 0, "sims": {}}
    if not os.path.exists(DB_PATH):
        return {**empty, "error": f"File not found: {DB_PATH}"}
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT market_id, question, claude_prob, market_price,
                   resolved_value, resolved_at
            FROM signals
            WHERE resolved_value IS NOT NULL
              AND id IN (SELECT MIN(id) FROM signals GROUP BY market_id)
            ORDER BY resolved_at ASC
        """).fetchall()
        conn.close()
    except Exception as exc:
        return {**empty, "error": str(exc)}

    trades = []
    null_skipped    = 0
    extreme_skipped = 0
    no_edge_skipped = 0

    for row in rows:
        cp = row["claude_prob"]
        mp = row["market_price"]
        rv = row["resolved_value"]

        if cp is None or mp is None or rv is None:
            null_skipped += 1
            continue

        # Extreme price filter — market is near-resolved; Claude's data can't compete
        if not (MIN_MARKET_PRICE <= mp <= MAX_MARKET_PRICE):
            extreme_skipped += 1
            continue

        edge = cp - mp
        if edge > MIN_EDGE:
            direction = "YES"
            win_prob  = cp
            win_price = mp
        elif edge < -MIN_EDGE:
            direction = "NO"
            win_prob  = 1.0 - cp
            win_price = 1.0 - mp
        else:
            no_edge_skipped += 1
            continue

        full_k    = _kelly_fraction(win_prob, win_price)
        quarter_k = full_k / 4.0
        odds      = (1.0 - win_price) / win_price

        won = (rv == 1.0) if direction == "YES" else (rv == 0.0)

        trades.append({
            "question":      row["question"],
            "direction":     direction,
            "claude_prob":   cp,
            "market_price":  mp,
            "quarter_kelly": quarter_k,
            "odds":          odds,
            "won":           won,
        })

    # Simulate each starting capital sequentially
    sims = {}
    for start in STARTING_CAPITALS:
        bankroll     = start
        n_positions  = 0
        n_wins       = 0
        largest_win  = 0.0
        largest_loss = 0.0

        for t in trades:
            # Quarter Kelly, hard-capped at MAX_POSITION_PCT of current bankroll
            raw_stake = t["quarter_kelly"] * bankroll
            stake     = min(raw_stake, MAX_POSITION_PCT * bankroll, bankroll)
            if stake < 0.01:
                continue

            n_positions += 1
            if t["won"]:
                profit = stake * t["odds"]
                bankroll += profit
                n_wins += 1
                if profit > largest_win:
                    largest_win = profit
            else:
                if stake > largest_loss:
                    largest_loss = stake
                bankroll -= stake

        total_pnl = bankroll - start
        sims[start] = {
            "positions":    n_positions,
            "wins":         n_wins,
            "win_rate":     (n_wins / n_positions * 100) if n_positions else 0.0,
            "total_pnl":    total_pnl,
            "return_pct":   (total_pnl / start) * 100,
            "largest_win":  largest_win,
            "largest_loss": largest_loss,
        }

    return {
        "error":          None,
        "trades":         trades,
        "null_skipped":   null_skipped,
        "extreme_skipped":extreme_skipped,
        "no_edge_skipped":no_edge_skipped,
        "sims":           sims,
    }


def print_paper_trading(pt: dict) -> None:
    header("3.  PAPER TRADING P&L  (Quarter Kelly, 10% cap, 10-90% price band)")
    if pt.get("error"):
        print(f"  Error: {pt['error']}")
        return

    trades   = pt["trades"]
    total    = len(trades) + pt["extreme_skipped"] + pt["no_edge_skipped"] + pt["null_skipped"]

    print(f"\n  Resolved markets available:  {total}")
    print(f"  Skipped (null price/prob):   {pt['null_skipped']}")
    print(f"  Skipped (extreme price):     {pt['extreme_skipped']}  (outside 10-90%)")
    print(f"  Skipped (no edge):           {pt['no_edge_skipped']}  (gap <= 5%)")
    print(f"  Positions taken:             {len(trades)}")

    if not trades:
        print("\n  No trades to simulate yet.")
        return

    for start, s in pt["sims"].items():
        pnl_sign = "+" if s["total_pnl"] >= 0 else ""
        ret_sign = "+" if s["return_pct"] >= 0 else ""
        print(f"\n  ${start:,.0f} starting capital  (max position: ${start * MAX_POSITION_PCT:.0f})")
        row_sep()
        sub("Positions taken",    str(s["positions"]))
        sub("Win rate",           f"{s['win_rate']:.1f}%  ({s['wins']}/{s['positions']})")
        sub("Total P&L",          f"{pnl_sign}${s['total_pnl']:.2f}")
        sub("Return on capital",  f"{ret_sign}{s['return_pct']:.1f}%")
        sub("Largest single win", f"${s['largest_win']:.2f}")
        sub("Largest single loss",f"${s['largest_loss']:.2f}")


# ---------------------------------------------------------------------------
# 4. API connectivity
# ---------------------------------------------------------------------------

def _ping_clob() -> tuple[bool, str]:
    try:
        t0 = time.monotonic()
        resp = requests.get(f"{CLOB_BASE}/markets", params={"active": "true", "limit": 1}, timeout=REQUEST_TIMEOUT)
        ms = int((time.monotonic() - t0) * 1000)
        resp.raise_for_status()
        return True, f"HTTP {resp.status_code}  ({ms} ms)"
    except Exception as exc:
        return False, str(exc)[:80]


def _ping_anthropic() -> tuple[bool, str]:
    if not ANTHROPIC_API_KEY:
        return False, "ANTHROPIC_API_KEY not set"
    try:
        import anthropic
        t0 = time.monotonic()
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        models = client.models.list()
        ms = int((time.monotonic() - t0) * 1000)
        count = len(list(models)) if hasattr(models, "__iter__") else "?"
        return True, f"{count} models listed  ({ms} ms)"
    except Exception as exc:
        return False, str(exc)[:80]


def _ping_rapidapi() -> tuple[bool, str]:
    if not RAPIDAPI_KEY:
        return False, "RAPIDAPI_KEY not set"
    try:
        t0 = time.monotonic()
        resp = requests.get(
            FEAR_GREED_URL,
            headers={"X-RapidAPI-Key": RAPIDAPI_KEY, "X-RapidAPI-Host": FEAR_GREED_HOST},
            timeout=REQUEST_TIMEOUT,
        )
        ms = int((time.monotonic() - t0) * 1000)
        resp.raise_for_status()
        data = resp.json()
        fgi = data.get("fgi") or data
        now_fgi = fgi.get("now") or fgi
        val = now_fgi.get("value", "?")
        label = now_fgi.get("valueText") or now_fgi.get("label", "?")
        return True, f"HTTP {resp.status_code}  FGI={val} ({label})  ({ms} ms)"
    except Exception as exc:
        return False, str(exc)[:80]


def check_apis() -> dict:
    clob_ok, clob_msg       = _ping_clob()
    anthro_ok, anthro_msg   = _ping_anthropic()
    rapid_ok, rapid_msg     = _ping_rapidapi()
    return {
        "clob":     (clob_ok, clob_msg),
        "anthropic":(anthro_ok, anthro_msg),
        "rapidapi": (rapid_ok, rapid_msg),
    }


def print_apis(apis: dict) -> None:
    header("4.  API CONNECTIVITY")
    print()
    clob_ok, clob_msg     = apis["clob"]
    anthro_ok, anthro_msg = apis["anthropic"]
    rapid_ok, rapid_msg   = apis["rapidapi"]
    sub("Polymarket CLOB",  clob_msg,   ok=clob_ok)
    sub("Anthropic API",    anthro_msg, ok=anthro_ok)
    sub("RapidAPI Fear&Greed", rapid_msg, ok=rapid_ok)

# ---------------------------------------------------------------------------
# 4. Latest signals
# ---------------------------------------------------------------------------

def check_latest_signals() -> list[dict]:
    if not os.path.exists(DB_PATH):
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT timestamp, question, category, claude_prob, market_price
            FROM signals
            ORDER BY id DESC
            LIMIT 5
        """).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def print_latest_signals(signals: list[dict]) -> None:
    header("5.  LATEST SIGNALS  (5 most recent)")
    if not signals:
        print("  No signals found.")
        return

    col_w = (19, 60, 17, 10, 12)
    hdr = f"  {'Timestamp':<{col_w[0]}}  {'Question':<{col_w[1]}}  {'Category':<{col_w[2]}}  {'Claude':>{col_w[3]}}  {'Mkt Price':>{col_w[4]}}"
    print()
    print(hdr)
    print(f"  {SEP}")
    for s in signals:
        ts   = (s["timestamp"] or "")[:19].replace("T", " ")
        q    = (s["question"] or "")[:60]
        cat  = (s["category"] or "")[:17]
        cp   = f"{s['claude_prob']:.3f}" if s["claude_prob"] is not None else "  N/A"
        mp   = f"{s['market_price']:.3f}" if s["market_price"] is not None else "  N/A"
        print(f"  {ts:<{col_w[0]}}  {q:<{col_w[1]}}  {cat:<{col_w[2]}}  {cp:>{col_w[3]}}  {mp:>{col_w[4]}}")

# ---------------------------------------------------------------------------
# 5. Latest resolved
# ---------------------------------------------------------------------------

def check_latest_resolved() -> list[dict]:
    if not os.path.exists(DB_PATH):
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT question, resolved_at, resolved_value, claude_prob, was_claude_correct
            FROM signals
            WHERE resolved_value IS NOT NULL
            GROUP BY market_id
            ORDER BY resolved_at DESC
            LIMIT 5
        """).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def print_latest_resolved(resolved: list[dict]) -> None:
    header("6.  LATEST RESOLVED  (5 most recent)")
    if not resolved:
        print("  No resolved markets yet.")
        return

    col_w = (19, 60, 8, 10, 10)
    hdr = f"  {'Resolved At':<{col_w[0]}}  {'Question':<{col_w[1]}}  {'Outcome':<{col_w[2]}}  {'Claude':>{col_w[3]}}  {'Correct?':>{col_w[4]}}"
    print()
    print(hdr)
    print(f"  {SEP}")
    for r in resolved:
        ts      = (r["resolved_at"] or "")[:19].replace("T", " ")
        q       = (r["question"] or "")[:60]
        outcome = "YES" if r["resolved_value"] == 1.0 else "NO"
        cp      = f"{r['claude_prob']:.3f}" if r["claude_prob"] is not None else "   N/A"
        correct_raw = r["was_claude_correct"]
        if correct_raw == 1:
            correct = "CORRECT"
        elif correct_raw == 0:
            correct = "WRONG"
        else:
            correct = "N/A"
        print(f"  {ts:<{col_w[0]}}  {q:<{col_w[1]}}  {outcome:<{col_w[2]}}  {cp:>{col_w[3]}}  {correct:>{col_w[4]}}")

# ---------------------------------------------------------------------------
# Overall status
# ---------------------------------------------------------------------------

def compute_status(tasks: list[dict], db_result: dict, apis: dict) -> tuple[str, list[str]]:
    """
    HEALTHY  — all APIs reachable, DB has signals in last 24h, both tasks last-result = 0
    WARNING  — minor issues: signals in 24h but not 1h, or one API slow/degraded, or a task had a non-zero exit
    CRITICAL — DB unreachable, no signals in 24h, or core APIs down
    """
    issues: list[str] = []
    critical = False
    warning  = False

    # DB
    if not db_result["reachable"]:
        issues.append("CRITICAL: Database unreachable")
        critical = True
    elif db_result["signals_24h"] == 0:
        issues.append("CRITICAL: No signals written in the last 24 hours")
        critical = True
    elif db_result["signals_1h"] == 0:
        issues.append("WARNING: No signals written in the last 1 hour")
        warning = True

    # APIs
    clob_ok, _   = apis["clob"]
    anthro_ok, _ = apis["anthropic"]
    rapid_ok, _  = apis["rapidapi"]

    if not clob_ok:
        issues.append("CRITICAL: Polymarket CLOB API unreachable")
        critical = True
    if not anthro_ok:
        issues.append("CRITICAL: Anthropic API unreachable")
        critical = True
    if not rapid_ok:
        issues.append("WARNING: RapidAPI Fear & Greed unreachable (non-fatal)")
        warning = True

    # Tasks
    for t in tasks:
        if not t["found"]:
            issues.append(f"WARNING: Task '{t['name']}' not found in Task Scheduler")
            warning = True
        elif t["last_result_ok"] is False:
            issues.append(f"WARNING: Task '{t['name']}' last run returned exit code {t['last_result']}")
            warning = True

    if critical:
        return "CRITICAL", issues
    if warning:
        return "WARNING", issues
    return "HEALTHY", issues


def print_status(status: str, issues: list[str]) -> None:
    print(f"\n{WIDE_SEP}")
    label = {
        "HEALTHY":  "  OVERALL STATUS:  HEALTHY",
        "WARNING":  "  OVERALL STATUS:  WARNING",
        "CRITICAL": "  OVERALL STATUS:  CRITICAL",
    }.get(status, f"  OVERALL STATUS:  {status}")
    print(label)
    if issues:
        print()
        for issue in issues:
            print(f"    * {issue}")
    print(WIDE_SEP)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print(f"\n{WIDE_SEP}")
    print(f"  Polymarket Signal Bot - Health Check")
    print(f"  {now_utc().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{WIDE_SEP}")

    tasks    = check_tasks()
    db_result= check_db()
    pt       = check_paper_trading()
    apis     = check_apis()
    signals  = check_latest_signals()
    resolved = check_latest_resolved()

    print_tasks(tasks)
    print_db(db_result)
    print_paper_trading(pt)
    print_apis(apis)
    print_latest_signals(signals)
    print_latest_resolved(resolved)

    status, issues = compute_status(tasks, db_result, apis)
    print_status(status, issues)
    print()

    sys.exit(0 if status == "HEALTHY" else 1 if status == "WARNING" else 2)


if __name__ == "__main__":
    main()
