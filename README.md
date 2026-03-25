# Polymarket Signal Validation Bot

A data-collection and signal-validation system that fetches active Polymarket prediction markets, asks Claude to estimate outcome probabilities, enriches each signal with macro context (VIX, CNN Fear & Greed Index), and logs everything to a local SQLite database. No trading or order execution.

---

## Purpose

- **Signal collection** — hourly snapshots of Claude's probability estimates vs. live Polymarket prices across Politics and Crypto markets.
- **Resolution tracking** — daily check of resolved markets, writing back actual outcomes and whether Claude's directional call was correct.
- **Backtesting** — offline evaluation of Claude signal accuracy and Kelly-sized P&L on historical resolved markets.

---

## File Structure

```
polymarket-signal-bot/
├── main.py            # Hourly runner — fetches markets, calls Claude, logs to DB
├── resolve.py         # Daily runner — checks resolved markets, writes back outcomes
├── backtest.py        # Offline backtest — accuracy + Kelly P&L on resolved history
├── polymarket.py      # Polymarket Gamma + CLOB API client, market filters
├── claude_signals.py  # Claude API wrapper — returns {probability, confidence, reasoning}
├── market_data.py     # VIX via yfinance; CNN Fear & Greed Index via RapidAPI
├── db.py              # SQLite schema, init, log_signal(), log_run()
├── .env               # Your secrets (not committed)
├── .env.example       # Template — copy to .env and fill in keys
├── requirements.txt   # Python dependencies
├── signals.db         # SQLite database (created on first run)
├── bot.log            # Rolling log from main.py
├── resolve.log        # Rolling log from resolve.py
└── backtest.log       # Rolling log from backtest.py
```

### Module responsibilities

| File | Responsibility |
|---|---|
| `main.py` | Orchestrates a full run: snapshot → markets → Claude signals → DB write |
| `resolve.py` | Queries unresolved market IDs, hits Gamma API, writes `resolved_value` / `was_claude_correct` |
| `backtest.py` | Fetches resolved history from Gamma, fetches pre-resolution prices from CLOB, runs Claude, reports accuracy + profit factor |
| `polymarket.py` | Gamma Markets API pagination with dedup, expiry, volume, blocklist, and stale coin-flip filters |
| `claude_signals.py` | Single Claude API call per market; enforces JSON-only output schema |
| `market_data.py` | `fetch_vix()` via yfinance; `fetch_fear_greed()` via RapidAPI |
| `db.py` | `init_db()` (creates + migrates schema), `log_signal()`, `log_run()` |

---

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in your API keys (see below)
python main.py         # first run initialises signals.db
```

---

## Running the scripts

### `main.py` — signal collection

```bash
python main.py                        # full run, all markets up to MAX_MARKETS env var
python main.py --max-markets 50       # process at most 50 markets
python main.py --dry-run              # fetch + Claude calls, skip DB writes
python main.py --dry-run --max-markets 5   # quick smoke test
```

Outputs one row to `signals` per market and one row to `run_log` per invocation.

### `resolve.py` — resolution tracker

```bash
python resolve.py            # check all unresolved market_ids, write back outcomes
python resolve.py --dry-run  # print results without writing to DB
```

A market is considered resolved when `closed = true` AND `outcomePrices` has fully settled to `["1","0"]` (YES) or `["0","1"]` (NO) in the Gamma API.

Correctness rule:
- `claude_prob > 0.5` → predicted YES
- `claude_prob < 0.5` → predicted NO
- `claude_prob == 0.5` → abstain (`was_claude_correct = NULL`)

### `backtest.py` — historical backtest

```bash
python backtest.py                                  # defaults: 90 days, gap>=0.15, 100 markets, $500 min vol
python backtest.py --days 60                        # shorter look-back
python backtest.py --gap 0.10                       # lower gap threshold
python backtest.py --max-markets 200 --min-volume 1000
python backtest.py --dry-run                        # list markets without calling Claude
```

Reports overall accuracy, accuracy by category, and Kelly-sized P&L for signals above the gap threshold.

---

## Environment variables (`.env`)

Copy `.env.example` to `.env` and populate:

| Variable | Required | Description | Where to get it |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Claude API key | [console.anthropic.com](https://console.anthropic.com) → API Keys |
| `RAPIDAPI_KEY` | Yes | RapidAPI key for Fear & Greed Index | [rapidapi.com](https://rapidapi.com) → subscribe to **Fear and Greed Index** API |
| `DB_PATH` | No | SQLite file path (default: `signals.db`) | Set to any local path |
| `CLAUDE_MODEL` | No | Claude model ID (default: `claude-sonnet-4-6`) | See [Anthropic docs](https://docs.anthropic.com) |
| `MAX_MARKETS` | No | Max markets per run (default: unlimited) | Set to an integer, e.g. `50` |

---

## SQLite schema (`signals.db`)

### `signals` table

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER | Auto-increment primary key |
| `timestamp` | TEXT | UTC ISO timestamp of signal collection |
| `market_id` | TEXT | Polymarket condition ID |
| `question` | TEXT | Market question |
| `category` | TEXT | Politics / Crypto / Other |
| `market_price` | REAL | Best YES price at collection time (0–1) |
| `claude_prob` | REAL | Claude estimated probability (0–1) |
| `confidence` | TEXT | `low` / `medium` / `high` |
| `reasoning` | TEXT | Claude's 1–3 sentence explanation |
| `vix` | REAL | VIX close at time of run |
| `fear_greed_value` | INTEGER | CNN Fear & Greed value (0–100) |
| `fear_greed_label` | TEXT | e.g. `Extreme Fear`, `Greed` |
| `resolved_value` | REAL | `1.0` = YES, `0.0` = NO, `NULL` = pending |
| `resolved_at` | TEXT | UTC timestamp when resolution was fetched |
| `was_claude_correct` | INTEGER | `1` correct, `0` wrong, `NULL` abstain |

### `run_log` table

Tracks each invocation of `main.py`: timestamp, markets processed, error count, notes.

---

## Scheduled tasks

Two recurring jobs are registered in the active Claude Code session:

| Job ID | Schedule | Command |
|---|---|---|
| `06ae32a1` | Every hour at :13 | `python main.py --max-markets 50` |
| `4ba3f063` | Daily at 08:23 | `python resolve.py` |

> **Note:** These are session-only — they stop when Claude Code exits and auto-expire after 7 days. For persistent scheduling use Windows Task Scheduler:

```powershell
# Hourly signal collection
schtasks /create /tn "PolymarketBot" /tr "python C:\claude\polymarket-signal-bot\main.py --max-markets 50" /sc hourly /mo 1 /st 00:13

# Daily resolution check
schtasks /create /tn "PolymarketResolve" /tr "python C:\claude\polymarket-signal-bot\resolve.py" /sc daily /st 08:23
```

---

## Filter settings

All filters are applied in `polymarket.py` before any Claude API calls are made.

### `QUESTION_BLOCKLIST`

Markets whose questions contain these phrases (case-insensitive) are excluded. The rule: **only block markets where the resolution trigger is a fictional entertainment product**, creating zombie markets with no real resolution path. Real-world events (wars, elections, scientific milestones) are valid and not blocked.

```python
QUESTION_BLOCKLIST = [
    "before gta vi",  # fictional game used as a resolution backstop
    "before gta 6",   # alternate phrasing
]
```

### Other filter constants

| Constant | Value | Effect |
|---|---|---|
| `MIN_DAYS_TO_RESOLUTION` | `7` | Excludes markets resolving within 7 days — prices are already converging |
| `MIN_VOLUME_USDC` | `100.0` | Excludes markets with lifetime volume below $100 |
| `STALE_PRICE_LO / HI` | `0.45 / 0.55` | Together with `STALE_MAX_VOLUME`: excludes coin-flip priced markets |
| `STALE_MAX_VOLUME` | `1000.0` | Markets priced 45–55% with volume below $1000 are excluded as abandoned |
| `MAX_PAGES` | `3` | Max pages fetched per tag from Gamma API (300 markets max per tag) |

### `backtest.py` extended blocklist

`backtest.py` adds an additional `BACKTEST_EXTRA_BLOCKLIST` of regex patterns filtering sports game results, over/unders, and high-frequency price-flip markets (e.g. "Bitcoin Up or Down") that are unsuitable for Claude signal evaluation.

---

## Useful queries

```sql
-- Top 10 gaps in the latest run
SELECT question, claude_prob, market_price,
       ROUND(claude_prob - market_price, 4) AS gap
FROM signals
WHERE id >= (SELECT MAX(id) - 49 FROM signals)
  AND claude_prob IS NOT NULL AND market_price IS NOT NULL
ORDER BY ABS(claude_prob - market_price) DESC LIMIT 10;

-- Claude accuracy on resolved markets
SELECT
    COUNT(*) FILTER (WHERE was_claude_correct = 1) AS correct,
    COUNT(*) FILTER (WHERE was_claude_correct = 0) AS wrong,
    ROUND(AVG(was_claude_correct) * 100, 1) || '%' AS accuracy
FROM signals WHERE resolved_value IS NOT NULL;

-- Run history
SELECT timestamp, markets_processed, errors FROM run_log ORDER BY timestamp DESC LIMIT 10;
```
