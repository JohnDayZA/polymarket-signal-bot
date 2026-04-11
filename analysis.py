"""
analysis.py — Deep signal quality analysis for signals.db.

Requires 20+ resolved markets to run meaningfully.

Sections:
  1. Calibration Analysis       (decile buckets)
  2. Accuracy by Category
  3. Accuracy by Confidence Level
  4. Accuracy by Days to Resolution
  5. Gap Size vs Outcome
  6. Kelly P&L Simulation       (Quarter Kelly, 10-90% band)
  7. Signal Quality Trend       (first half vs second half, needs 30+ days)
  8. Top 10 Signals Right Now   (largest unresolved gaps)

Output: console + analysis_report.txt
"""

import math
import os
import sqlite3
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.getenv("DB_PATH", "signals.db")
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "analysis_report.txt")

MIN_RESOLVED = 20
MIN_MARKET_PRICE = 0.10
MAX_MARKET_PRICE = 0.90
MIN_EDGE = 0.07
MAX_POSITION_PCT = 0.10
STARTING_CAPITALS = [50.0, 500.0]
KELLY_CAP = 0.25         # full Kelly capped at 25% before quartering

SEP  = "-" * 72
WIDE = "=" * 72

# ---------------------------------------------------------------------------
# Output accumulator — writes to console and file simultaneously
# ---------------------------------------------------------------------------

class Report:
    def __init__(self):
        self._lines: list[str] = []

    def __call__(self, text: str = "") -> None:
        print(text)
        self._lines.append(text)

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(self._lines) + "\n")


out = Report()


def section(title: str) -> None:
    out()
    out(WIDE)
    out(f"  {title}")
    out(WIDE)


def sep() -> None:
    out(f"  {SEP}")


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_resolved(conn: sqlite3.Connection) -> list[dict]:
    """Most recent signal row per market, resolved, with correctness."""
    rows = conn.execute("""
        SELECT s.market_id, s.question, s.category,
               s.market_price, s.claude_prob, s.confidence,
               s.days_to_resolution, s.resolved_value,
               s.was_claude_correct, s.resolved_at, s.timestamp
        FROM signals s
        WHERE s.id IN (SELECT MAX(id) FROM signals GROUP BY market_id)
          AND s.resolved_value IS NOT NULL
          AND s.was_claude_correct IS NOT NULL
          AND s.claude_prob IS NOT NULL
          AND s.market_price IS NOT NULL
    """).fetchall()
    return [dict(r) for r in rows]


def fetch_first_signals(conn: sqlite3.Connection) -> list[dict]:
    """First signal row per market — used for Kelly sim to get entry price."""
    rows = conn.execute("""
        SELECT s.market_id, s.question, s.category,
               s.market_price, s.claude_prob,
               s.resolved_value, s.resolved_at
        FROM signals s
        WHERE s.id IN (SELECT MIN(id) FROM signals GROUP BY market_id)
          AND s.resolved_value IS NOT NULL
          AND s.claude_prob IS NOT NULL
          AND s.market_price IS NOT NULL
        ORDER BY s.resolved_at ASC
    """).fetchall()
    return [dict(r) for r in rows]


def fetch_unresolved(conn: sqlite3.Connection) -> list[dict]:
    """Most recent signal per unresolved market, in the 10-90% band."""
    rows = conn.execute("""
        SELECT s.market_id, s.question, s.category,
               s.market_price, s.claude_prob, s.confidence,
               s.days_to_resolution, s.timestamp
        FROM signals s
        WHERE s.id IN (SELECT MAX(id) FROM signals GROUP BY market_id)
          AND s.resolved_value IS NULL
          AND s.claude_prob IS NOT NULL
          AND s.market_price BETWEEN ? AND ?
    """, (MIN_MARKET_PRICE, MAX_MARKET_PRICE)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------

def kelly_fraction(win_prob: float, win_price: float) -> float:
    """Quarter Kelly, capped at KELLY_CAP. Returns 0 if no edge."""
    if win_price <= 0.0 or win_price >= 1.0:
        return 0.0
    b = (1.0 - win_price) / win_price
    full_k = (win_prob * b - (1.0 - win_prob)) / b
    full_k = max(0.0, min(full_k, KELLY_CAP))
    return full_k / 4.0


def pct(v: float | None, decimals: int = 1) -> str:
    if v is None:
        return "  N/A"
    return f"{v * 100:.{decimals}f}%"


def signed(v: float, prefix: str = "$") -> str:
    sign = "+" if v >= 0 else ""
    return f"{sign}{prefix}{v:.2f}"


# ---------------------------------------------------------------------------
# Section 1 — Calibration Analysis
# ---------------------------------------------------------------------------

def section_calibration(resolved: list[dict]) -> None:
    section("1.  CALIBRATION ANALYSIS  (Claude prob decile vs actual YES rate)")

    deciles = [(i / 10, (i + 1) / 10) for i in range(10)]
    header_fmt = f"  {'Decile':<14}  {'Count':>6}  {'Pred (mid)':>10}  {'Actual YES':>10}  {'Calib Err':>10}  Bar"
    out()
    out(header_fmt)
    sep()

    worst_error = 0.0
    worst_decile = ""

    for lo, hi in deciles:
        bucket = [r for r in resolved if lo <= r["claude_prob"] < hi]
        # Top bucket is inclusive of 1.0
        if hi == 1.0:
            bucket = [r for r in resolved if lo <= r["claude_prob"] <= hi]

        count = len(bucket)
        mid = (lo + hi) / 2.0
        if count == 0:
            label = f"{int(lo*100):2d}-{int(hi*100):2d}%"
            out(f"  {label:<14}  {'0':>6}  {'—':>10}  {'—':>10}  {'—':>10}")
            continue

        yes_count = sum(1 for r in bucket if r["resolved_value"] == 1.0)
        actual_rate = yes_count / count
        error = mid - actual_rate   # positive = overconfident YES, negative = underconfident

        # Simple ASCII bar for error
        bar_len = min(20, int(abs(error) * 100))
        bar = (">" if error > 0 else "<") * bar_len

        label = f"{int(lo*100):2d}-{int(hi*100):2d}%"
        out(f"  {label:<14}  {count:>6}  {pct(mid):>10}  {pct(actual_rate):>10}  {pct(error, 1):>10}  {bar}")

        if abs(error) > abs(worst_error):
            worst_error = error
            worst_decile = label

    sep()
    if worst_decile:
        direction = "overconfident (Claude says YES more than markets resolve YES)" if worst_error > 0 else "underconfident (Claude says YES less than markets resolve YES)"
        out(f"  Largest calibration error: {worst_decile}  ({pct(worst_error)})  — {direction}")

    # Overall Brier score
    brier = sum((r["claude_prob"] - r["resolved_value"]) ** 2 for r in resolved) / len(resolved)
    out(f"  Brier score: {brier:.4f}  (lower is better; 0.25 = random, 0.0 = perfect)")


# ---------------------------------------------------------------------------
# Section 2 — Accuracy by Category
# ---------------------------------------------------------------------------

def _kelly_pnl_per_trade(r: dict) -> float | None:
    """Compute quarter-Kelly P&L as a fraction of bankroll for one trade."""
    cp = r["claude_prob"]
    mp = r["market_price"]
    rv = r["resolved_value"]
    if None in (cp, mp, rv):
        return None
    if not (MIN_MARKET_PRICE <= mp <= MAX_MARKET_PRICE):
        return None
    edge = cp - mp
    if edge > MIN_EDGE:
        win_prob, win_price, won = cp, mp, (rv == 1.0)
    elif edge < -MIN_EDGE:
        win_prob, win_price, won = 1.0 - cp, 1.0 - mp, (rv == 0.0)
    else:
        return None
    fk = kelly_fraction(win_prob, win_price)
    if fk <= 0:
        return None
    odds = (1.0 - win_price) / win_price
    return fk * odds if won else -fk


def section_by_category(resolved: list[dict]) -> None:
    section("2.  ACCURACY BY CATEGORY")

    cats: dict[str, list[dict]] = {}
    for r in resolved:
        cats.setdefault(r["category"] or "Other", []).append(r)

    col = (18, 8, 9, 9, 10, 12)
    hdr = (f"  {'Category':<{col[0]}}  {'Total':>{col[1]}}  "
           f"{'Correct':>{col[2]}}  {'Accuracy':>{col[3]}}  "
           f"{'Avg Gap':>{col[4]}}  {'Avg Kelly PnL':>{col[5]}}")
    out()
    out(hdr)
    sep()

    # Sort by accuracy desc, then total desc
    rows_out = []
    for cat, items in cats.items():
        total = len(items)
        correct = sum(1 for r in items if r["was_claude_correct"] == 1)
        accuracy = correct / total if total else 0.0
        gaps = [abs(r["claude_prob"] - r["market_price"]) for r in items
                if r["claude_prob"] is not None and r["market_price"] is not None]
        avg_gap = sum(gaps) / len(gaps) if gaps else 0.0
        pnls = [p for r in items if (p := _kelly_pnl_per_trade(r)) is not None]
        avg_pnl = sum(pnls) / len(pnls) if pnls else None
        rows_out.append((cat, total, correct, accuracy, avg_gap, avg_pnl))

    rows_out.sort(key=lambda x: (-x[3], -x[1]))

    for cat, total, correct, accuracy, avg_gap, avg_pnl in rows_out:
        pnl_str = f"{avg_pnl*100:+.1f}%" if avg_pnl is not None else "   N/A"
        out(f"  {cat:<{col[0]}}  {total:>{col[1]}}  {correct:>{col[2]}}  "
            f"  {pct(accuracy):>{col[3]}}  {pct(avg_gap):>{col[4]}}  {pnl_str:>{col[5]}}")

    sep()
    overall_acc = sum(1 for r in resolved if r["was_claude_correct"] == 1) / len(resolved)
    out(f"  Overall accuracy across all categories: {pct(overall_acc)}")


# ---------------------------------------------------------------------------
# Section 3 — Accuracy by Confidence Level
# ---------------------------------------------------------------------------

def section_by_confidence(resolved: list[dict]) -> None:
    section("3.  ACCURACY BY CONFIDENCE LEVEL")

    levels = ["high", "medium", "low"]
    col = (10, 8, 9, 9)
    hdr = (f"  {'Confidence':<{col[0]}}  {'Count':>{col[1]}}  "
           f"{'Correct':>{col[2]}}  {'Accuracy':>{col[3]}}")
    out()
    out(hdr)
    sep()

    accs: dict[str, float | None] = {}
    for lvl in levels:
        items = [r for r in resolved if (r.get("confidence") or "").lower() == lvl]
        if not items:
            out(f"  {lvl.capitalize():<{col[0]}}  {'0':>{col[1]}}  {'—':>{col[2]}}  {'—':>{col[3]}}")
            accs[lvl] = None
            continue
        correct = sum(1 for r in items if r["was_claude_correct"] == 1)
        accuracy = correct / len(items)
        accs[lvl] = accuracy
        out(f"  {lvl.capitalize():<{col[0]}}  {len(items):>{col[1]}}  {correct:>{col[2]}}  {pct(accuracy):>{col[3]}}")

    sep()
    h, m, l_ = accs.get("high"), accs.get("medium"), accs.get("low")
    if h is not None and m is not None:
        if h > m:
            out(f"  High confidence IS more accurate than medium ({pct(h)} vs {pct(m)}) — confidence is well-calibrated.")
        else:
            out(f"  High confidence is NOT more accurate than medium ({pct(h)} vs {pct(m)}) — confidence labels may be unreliable.")
    else:
        out("  Insufficient data across confidence levels to compare.")


# ---------------------------------------------------------------------------
# Section 4 — Accuracy by Days to Resolution
# ---------------------------------------------------------------------------

def section_by_duration(resolved: list[dict]) -> None:
    section("4.  ACCURACY BY DAYS TO RESOLUTION")

    buckets = [
        ("3-7 days",   3,  7),
        ("7-14 days",  7,  14),
        ("14-30 days", 14, 30),
        ("30-60 days", 30, 60),
    ]

    col = (14, 8, 9, 9, 10)
    hdr = (f"  {'Duration':<{col[0]}}  {'Count':>{col[1]}}  "
           f"{'Correct':>{col[2]}}  {'Accuracy':>{col[3]}}  {'Avg Gap':>{col[4]}}")
    out()
    out(hdr)
    sep()

    for label, lo, hi in buckets:
        items = [r for r in resolved
                 if r.get("days_to_resolution") is not None
                 and lo <= r["days_to_resolution"] < hi]
        if not items:
            out(f"  {label:<{col[0]}}  {'0':>{col[1]}}  {'—':>{col[2]}}  {'—':>{col[3]}}  {'—':>{col[4]}}")
            continue
        correct = sum(1 for r in items if r["was_claude_correct"] == 1)
        accuracy = correct / len(items)
        gaps = [abs(r["claude_prob"] - r["market_price"]) for r in items]
        avg_gap = sum(gaps) / len(gaps)
        out(f"  {label:<{col[0]}}  {len(items):>{col[1]}}  {correct:>{col[2]}}  "
            f"  {pct(accuracy):>{col[3]}}  {pct(avg_gap):>{col[4]}}")

    no_duration = [r for r in resolved if r.get("days_to_resolution") is None]
    sep()
    out(f"  Markets with no days_to_resolution recorded: {len(no_duration)}")


# ---------------------------------------------------------------------------
# Section 5 — Gap Size vs Outcome
# ---------------------------------------------------------------------------

def section_gap_vs_outcome(resolved: list[dict]) -> None:
    section("5.  GAP SIZE vs OUTCOME  (does bigger gap = better edge?)")

    buckets = [
        ("5-10%",  0.05, 0.10),
        ("10-20%", 0.10, 0.20),
        ("20-30%", 0.20, 0.30),
        ("30%+",   0.30, 1.01),
    ]

    col = (10, 8, 9, 9, 9)
    hdr = (f"  {'Gap Band':<{col[0]}}  {'Count':>{col[1]}}  "
           f"{'Correct':>{col[2]}}  {'Accuracy':>{col[3]}}  {'Avg Gap':>{col[4]}}")
    out()
    out(hdr)
    sep()

    in_band = [r for r in resolved
               if r["claude_prob"] is not None and r["market_price"] is not None]

    for label, lo, hi in buckets:
        items = [r for r in in_band if lo <= abs(r["claude_prob"] - r["market_price"]) < hi]
        if not items:
            out(f"  {label:<{col[0]}}  {'0':>{col[1]}}  {'—':>{col[2]}}  {'—':>{col[3]}}  {'—':>{col[4]}}")
            continue
        correct = sum(1 for r in items if r["was_claude_correct"] == 1)
        accuracy = correct / len(items)
        avg_gap = sum(abs(r["claude_prob"] - r["market_price"]) for r in items) / len(items)
        out(f"  {label:<{col[0]}}  {len(items):>{col[1]}}  {correct:>{col[2]}}  "
            f"  {pct(accuracy):>{col[3]}}  {pct(avg_gap):>{col[4]}}")

    no_edge = [r for r in in_band if abs(r["claude_prob"] - r["market_price"]) < MIN_EDGE]
    sep()
    out(f"  Markets with gap < 5% (no edge, excluded): {len(no_edge)}")


# ---------------------------------------------------------------------------
# Section 6 — Kelly P&L Simulation
# ---------------------------------------------------------------------------

def section_kelly(first_signals: list[dict]) -> None:
    section("6.  KELLY P&L SIMULATION  (Quarter Kelly, 10-90% band, 10% position cap)")

    trades = []
    null_skip = extreme_skip = no_edge_skip = 0

    for r in first_signals:
        cp, mp, rv = r["claude_prob"], r["market_price"], r["resolved_value"]
        if None in (cp, mp, rv):
            null_skip += 1
            continue
        if not (MIN_MARKET_PRICE <= mp <= MAX_MARKET_PRICE):
            extreme_skip += 1
            continue
        edge = cp - mp
        if edge > MIN_EDGE:
            win_prob, win_price, won = cp, mp, (rv == 1.0)
            direction = "YES"
        elif edge < -MIN_EDGE:
            win_prob, win_price, won = 1.0 - cp, 1.0 - mp, (rv == 0.0)
            direction = "NO"
        else:
            no_edge_skip += 1
            continue

        fk = kelly_fraction(win_prob, win_price)
        if fk <= 0:
            no_edge_skip += 1
            continue
        odds = (1.0 - win_price) / win_price
        trades.append({
            "question":  r["question"],
            "direction": direction,
            "claude_prob": cp,
            "market_price": mp,
            "quarter_kelly": fk,
            "odds": odds,
            "won": won,
        })

    total_candidates = len(first_signals)
    out()
    out(f"  Resolved market candidates:    {total_candidates}")
    out(f"  Skipped (null data):           {null_skip}")
    out(f"  Skipped (extreme price):       {extreme_skip}  (outside 10-90%)")
    out(f"  Skipped (gap <= 5%):           {no_edge_skip}")
    out(f"  Positions simulated:           {len(trades)}")

    if not trades:
        out()
        out("  No qualifying trades to simulate.")
        return

    for start in STARTING_CAPITALS:
        bankroll = start
        returns = []            # fractional returns per trade for Sharpe
        best_trade = {"pnl": -999.0, "q": ""}
        worst_trade = {"pnl": 999.0, "q": ""}

        for t in trades:
            raw_stake = t["quarter_kelly"] * bankroll
            stake = min(raw_stake, MAX_POSITION_PCT * bankroll, bankroll)
            if stake < 0.01:
                continue
            if t["won"]:
                profit = stake * t["odds"]
                bankroll += profit
                trade_return = profit / stake
                if profit > best_trade["pnl"]:
                    best_trade = {"pnl": profit, "q": t["question"]}
            else:
                bankroll -= stake
                trade_return = -1.0
                if -stake < worst_trade["pnl"]:
                    worst_trade = {"pnl": -stake, "q": t["question"]}
            returns.append(trade_return)

        total_pnl = bankroll - start
        ret_pct = total_pnl / start * 100
        n = len(returns)

        out()
        out(f"  ${start:,.0f} starting capital  (max position: ${start * MAX_POSITION_PCT:.0f})")
        sep()
        out(f"  {'Total P&L':<28} {signed(total_pnl)}")
        out(f"  {'Return on capital':<28} {'+' if ret_pct >= 0 else ''}{ret_pct:.1f}%")
        out(f"  {'Final bankroll':<28} ${bankroll:.2f}")

        if best_trade["pnl"] > -999:
            out(f"  {'Best trade':<28} +${best_trade['pnl']:.2f}  |  {best_trade['q'][:45]}")
        if worst_trade["pnl"] < 999:
            out(f"  {'Worst trade':<28} ${worst_trade['pnl']:.2f}  |  {worst_trade['q'][:45]}")

        if n >= 5:
            mean_r = sum(returns) / n
            variance = sum((x - mean_r) ** 2 for x in returns) / n
            std_r = math.sqrt(variance) if variance > 0 else 0.0
            sharpe = (mean_r / std_r * math.sqrt(n)) if std_r > 0 else None
            if sharpe is not None:
                out(f"  {'Sharpe ratio (simplified)':<28} {sharpe:.2f}  (>1.0 is good; uses per-trade returns)")
        else:
            out(f"  {'Sharpe ratio':<28} N/A (need 5+ trades)")


# ---------------------------------------------------------------------------
# Section 7 — Signal Quality Trend
# ---------------------------------------------------------------------------

def section_trend(resolved: list[dict]) -> None:
    section("7.  SIGNAL QUALITY TREND  (first half vs second half)")

    # Need date range
    timestamped = [r for r in resolved if r.get("timestamp")]
    if not timestamped:
        out()
        out("  No timestamp data available.")
        return

    timestamped.sort(key=lambda r: r["timestamp"])
    first_ts = timestamped[0]["timestamp"][:10]
    last_ts  = timestamped[-1]["timestamp"][:10]

    try:
        t0 = datetime.fromisoformat(timestamped[0]["timestamp"][:19]).replace(tzinfo=timezone.utc)
        t1 = datetime.fromisoformat(timestamped[-1]["timestamp"][:19]).replace(tzinfo=timezone.utc)
        span_days = (t1 - t0).total_seconds() / 86400
    except Exception:
        span_days = 0

    out()
    out(f"  Data span: {first_ts}  to  {last_ts}  ({span_days:.0f} days)")

    if span_days < 30:
        out(f"  Insufficient history for trend analysis (need 30+ days, have {span_days:.0f}).")
        return

    mid_idx = len(timestamped) // 2
    first_half  = timestamped[:mid_idx]
    second_half = timestamped[mid_idx:]

    def half_stats(items: list[dict]) -> tuple[int, int, float, str, str]:
        total = len(items)
        correct = sum(1 for r in items if r["was_claude_correct"] == 1)
        accuracy = correct / total if total else 0.0
        start = items[0]["timestamp"][:10] if items else "?"
        end   = items[-1]["timestamp"][:10] if items else "?"
        return total, correct, accuracy, start, end

    t1, c1, a1, s1, e1 = half_stats(first_half)
    t2, c2, a2, s2, e2 = half_stats(second_half)

    col = (24, 8, 9, 9)
    out()
    out(f"  {'Period':<{col[0]}}  {'Markets':>{col[1]}}  {'Correct':>{col[2]}}  {'Accuracy':>{col[3]}}")
    sep()
    out(f"  {f'First half  ({s1} - {e1})':<{col[0]}}  {t1:>{col[1]}}  {c1:>{col[2]}}  {pct(a1):>{col[3]}}")
    out(f"  {f'Second half ({s2} - {e2})':<{col[0]}}  {t2:>{col[1]}}  {c2:>{col[2]}}  {pct(a2):>{col[3]}}")
    sep()

    delta = a2 - a1
    if abs(delta) < 0.02:
        out(f"  Signal quality is STABLE  (delta: {'+' if delta >= 0 else ''}{delta*100:.1f}pp)")
    elif delta > 0:
        out(f"  Signal quality is IMPROVING  (delta: +{delta*100:.1f}pp)  — model is getting better over time.")
    else:
        out(f"  Signal quality is DECLINING  (delta: {delta*100:.1f}pp)  — investigate market selection or category drift.")


# ---------------------------------------------------------------------------
# Section 8 — Top 10 Signals Right Now
# ---------------------------------------------------------------------------

def section_top_signals(unresolved: list[dict]) -> None:
    section("8.  TOP 10 SIGNALS RIGHT NOW  (largest unresolved gaps, 10-90% band)")

    with_gap = []
    for r in unresolved:
        cp, mp = r["claude_prob"], r["market_price"]
        if cp is None or mp is None:
            continue
        gap = cp - mp
        with_gap.append({**r, "gap": gap})

    with_gap.sort(key=lambda r: abs(r["gap"]), reverse=True)
    top10 = with_gap[:10]

    if not top10:
        out()
        out("  No unresolved markets with sufficient data found.")
        return

    out()
    col = (52, 12, 10, 10, 8, 10, 8)
    hdr = (f"  {'Question':<{col[0]}}  {'Category':<{col[1]}}  "
           f"{'Claude':>{col[2]}}  {'Mkt':>{col[3]}}  {'Gap':>{col[4]}}  "
           f"{'Days Left':>{col[5]}}  {'Conf':>{col[6]}}")
    out(hdr)
    sep()

    for r in top10:
        q   = (r["question"] or "")[:col[0]]
        cat = (r["category"] or "")[:col[1]]
        cp  = pct(r["claude_prob"])
        mp  = pct(r["market_price"])
        gap = r["gap"]
        gap_str = f"{'+' if gap >= 0 else ''}{gap*100:.1f}%"
        d   = f"{r['days_to_resolution']:.0f}d" if r.get("days_to_resolution") is not None else "  ?"
        conf = (r.get("confidence") or "?")[:col[6]].capitalize()
        out(f"  {q:<{col[0]}}  {cat:<{col[1]}}  {cp:>{col[2]}}  {mp:>{col[3]}}  "
            f"{gap_str:>{col[4]}}  {d:>{col[5]}}  {conf:>{col[6]}}")

    sep()
    bullish = sum(1 for r in with_gap if r["gap"] > MIN_EDGE)
    bearish = sum(1 for r in with_gap if r["gap"] < -MIN_EDGE)
    neutral = len(with_gap) - bullish - bearish
    out(f"  Total unresolved in band: {len(with_gap)}  "
        f"|  Bullish (Claude > Mkt +5%): {bullish}  "
        f"|  Bearish: {bearish}  "
        f"|  No edge: {neutral}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if not os.path.exists(DB_PATH):
        print(f"ERROR: DB not found at {DB_PATH}")
        sys.exit(1)

    conn = connect()
    resolved    = fetch_resolved(conn)
    first_sigs  = fetch_first_signals(conn)
    unresolved  = fetch_unresolved(conn)
    conn.close()

    n_resolved = len(resolved)
    if n_resolved < MIN_RESOLVED:
        print(f"Insufficient resolved markets for analysis — need {MIN_RESOLVED}, have {n_resolved}")
        sys.exit(0)

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    out(WIDE)
    out(f"  Polymarket Signal Bot — Deep Analysis")
    out(f"  Generated: {now_str}")
    out(f"  Database:  {DB_PATH}")
    out(f"  Resolved markets analysed: {n_resolved}")
    out(WIDE)

    section_calibration(resolved)
    section_by_category(resolved)
    section_by_confidence(resolved)
    section_by_duration(resolved)
    section_gap_vs_outcome(resolved)
    section_kelly(first_sigs)
    section_trend(resolved)
    section_top_signals(unresolved)

    out()
    out(WIDE)
    out(f"  Analysis complete.  Report saved to: {OUT_PATH}")
    out(WIDE)
    out()

    out.save(OUT_PATH)


if __name__ == "__main__":
    main()
