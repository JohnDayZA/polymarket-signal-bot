# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Phase 1 of a prediction market trading system. Pure data collection — no live trading or order execution. The goal is to accumulate 100 resolved markets with >55% Claude directional accuracy before advancing to Phase 2 (live execution). When Phase 2 arrives, always use GTC limit orders.

## Common commands

```bash
# Signal collection (also runs hourly via Windows Task Scheduler: PolymarketSignalBot)
python main.py --max-markets 100
python main.py --dry-run --max-markets 5     # smoke test without DB writes

# Resolution tracking (also runs daily 9:47 AM via PolymarketResolve task)
python resolve.py
python resolve.py --dry-run

# Backtest on historical resolved markets
python backtest.py --days 90 --gap 0.15 --max-markets 100
python backtest.py --dry-run                 # list markets only, skip Claude calls

# System status
python health_check.py

# Excel export (most recent signal per market, 10-90% band, sorted by gap)
python export_signals.py
```

No build step, no test suite. Install deps with `pip install -r requirements.txt`. Copy `.env.example` to `.env` and populate keys.

## Architecture

**Data flow:** `polymarket.py` fetches and filters markets → `main.py` calls `claude_signals.py` per market + `market_data.py` once per run → `db.py` writes to `signals.db`. `resolve.py` runs independently to write back outcomes.

**Key constants** (defined in `polymarket.py` unless noted):
- `MIN_MARKET_PRICE = 0.10`, `MAX_MARKET_PRICE = 0.90` — extreme-price filter applied both in market fetch and paper trading
- `LONGSHOT_FLOOR = 0.03` — hard floor before the 10/90 band filter
- `MIN_DAYS_TO_RESOLUTION = 3`, `MAX_DAYS_TO_RESOLUTION = 60` — time-window filter
- `MAX_PAGES = 5` — Gamma API pagination limit per tag (5 pages × 100 = 500 markets max)

**Filter chain** (`polymarket.py → fetch_target_markets`): expired/imminent → low volume → blocklisted → stale coin-flip → below LONGSHOT_FLOOR → outside MIN/MAX_MARKET_PRICE. All filters run before any Claude API call.

**Category classifier** (`_category_from_question`): priority-ordered keyword matching using word-boundary regex (`_wbp`). Order: Crypto → Politics → Finance → Commodities → Sports → Tech → Impossibilities → Other. When adding keywords, respect this priority order and use `_wbp()` for word-boundary safety.

**DB schema note:** `signals.db` stores one row per market per collection run (not one per market). The most-recent-row-per-market pattern is `WHERE id IN (SELECT MAX(id) FROM signals GROUP BY market_id)` — never use `SELECT col, MAX(timestamp) ... GROUP BY market_id` as SQLite does not guarantee non-aggregated columns come from the MAX row.

**Resolution:** `resolve.py` uses the CLOB API (`clob.polymarket.com/markets/{condition_id}`) — not Gamma — because `market_id` in `signals.db` is a CLOB condition_id. A market is resolved when `closed=true` AND one token has `winner=true`.

**Information gap flag:** `information_gap=1` is set in `main.py` when `abs(claude_prob - market_price) > 0.50` AND market_price is outside 0.15–0.85. This signals likely training-cutoff mismatch, not a real edge.

**Paper trading** (in `health_check.py`): Quarter Kelly sizing, capped at 10% of bankroll. Skips markets outside 10-90% price band, null claude_prob, or gap < MIN_EDGE (0.05).

## Critical rules

- **Never delete `signals.db`** — it is the primary research artifact.
- **Always commit and push after changes.**
- `DB_PATH` in `.env` must be an absolute path (e.g. `C:\claude\polymarket-signal-bot\signals.db`). Windows Task Scheduler runs from an unpredictable working directory; relative paths write to `System32`.
- When adding new columns to `signals`, add both to the `CREATE TABLE` statement and the migration list in `db.init_db()`.
- `openpyxl` is not in `requirements.txt` — it was installed separately. Add it if dependencies are ever reinstalled.

## Environment variables

| Variable | Default | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Required |
| `RAPIDAPI_KEY` | — | Required (Fear & Greed Index) |
| `DB_PATH` | `signals.db` | Use absolute path |
| `CLAUDE_MODEL` | `claude-sonnet-4-6` | Overrides model in `claude_signals.py` |
| `MAX_MARKETS` | unlimited | Per-run cap; also settable via `--max-markets` |

## Windows Task Scheduler

Two persistent tasks (survive Claude Code session):
- **PolymarketSignalBot** — hourly, runs `python main.py --max-markets 100`
- **PolymarketResolve** — daily 9:47 AM, runs `python resolve.py`

To recreate if lost, use `Register-ScheduledTask` with `WorkingDirectory` set — `Set-ScheduledTask` fails with credential errors on this machine.
