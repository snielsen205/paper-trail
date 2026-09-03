# Movers

A daily equities research system: it scans the market for large intraday
movers, filters and scores them, hands the survivors to a rules-based paper
trading bot, and grades every call it ever made against SPY.

The point of the project is not the scanner. It is the **honesty ledger** —
every pick is logged before the outcome is known and graded by a rule that
cannot be changed after the fact. The results are published in
**[TRACK_RECORD.md](TRACK_RECORD.md)**, including the losing calls.

> Paper money only. The bot refuses live Alpaca keys by design. Nothing here
> is investment advice.

---

## Current standing

As of 2026-09-02, across 145 graded calls over 32 trading days:

| | |
|---|---|
| Win rate | 44.8% |
| Average return | **-6.19%** |
| SPY over the same windows | +0.72% |
| Beat SPY | 42.1% of calls |
| Median return | -1.48% |

**The strategy is currently losing to SPY.** The gap between the -1.48%
median and the -6.19% mean says the damage is concentrated in a few severe
losses rather than spread across the book — which points at position sizing
and stop behavior, not at the signal. That is the open question the next
evaluation window is designed to answer. Full numbers and every individual
call are in [TRACK_RECORD.md](TRACK_RECORD.md); the raw graded rows are in
[track_record.csv](track_record.csv) if you want to check the arithmetic.

---

## Architecture

The system is a pipeline of small, single-purpose scripts that hand each
other JSON on disk. Nothing holds state in memory between stages, so any
stage can be re-run or inspected in isolation.

```
Alpha Vantage ──► scan.py ──────► snapshots/scan_YYYY-MM-DD.json
                     │                    │
                (junk filter)             ▼
                                    score.py ────► Top 10 + reasons
                                          │
              ┌───────────────────────────┼───────────────────────────┐
              ▼                           ▼                           ▼
      track_record.py              bot.py (Alpaca paper)        dashboard.py
    log → grade → report        bracket orders, risk rules       Flask UI
              │                           │
              ▼                           ▼
      the honesty ledger            snapshots/bot_log.jsonl
```

### The data layer
- **`av_client.py`** — everything that touches Alpha Vantage goes through
  here: rate limiting against the plan's requests/minute, on-disk response
  caching, retries with backoff, and a single place where a malformed
  response becomes a clean failure instead of a bad trade.
- **`alpaca_client.py`** — order submission, account state, market calendar,
  and shortability checks. Refuses any key that is not a paper key.
- **`claude_client.py`** — the LLM calls, isolated so cost and prompt changes
  are auditable in one file.

### The screener
- **`scan.py`** — pulls the day's movers and filters the junk: price floors,
  dollar-volume floors, and structural exclusions. Saves a dated snapshot.
- **`score.py`** — scores each survivor into a Top 10 with a written reason
  per pick and a LONG / SHORT / WATCH direction.
- **`quality_screen.py`**, **`earnings_flag.py`** — supporting filters
  (fundamental quality, earnings-date proximity).

### The analyst
- **`analyst.py`**, **`analyst_book.py`** — the LLM layer that reads news and
  filings around a candidate and writes the case for and against it. Costs
  are logged per call in `snapshots/analyst_costs.jsonl`, because an analyst
  you can't budget is an analyst you can't run.

### The takeover monitor
- **`edgar_watch.py`** — polls SEC EDGAR for the filing types that precede
  deals and control changes (SC 13D, SC 14D9, DEFM14A, 8-K item 1.01) and
  flags them against a watchlist. Independent of the movers pipeline.

### The bot
- **`bot.py`** — the execution engine. Implements
  [bot-rulebook-v1.1.md](bot-rulebook-v1.1.md) mechanically: it has no
  discretion and no manual override.

### The ledger
- **`track_record.py`** — `log` writes the day's picks before anything is
  known; `grade` measures each ripe pick close-to-close on split-adjusted
  prices against SPY over the identical window and **freezes** the result;
  `report` prints the standing verdict.
- **`make_track_record.py`** — regenerates the published `TRACK_RECORD.md`
  and `track_record.csv` from that ledger.

### Other sleeves
- **`momentum_sleeve.py`**, **`macro_sleeve.py`** — separate monthly and
  macro strategies logged alongside the movers book, never blended into it.

---

## The risk rules

These live in [bot-rulebook-v1.1.md](bot-rulebook-v1.1.md) and are enforced
in code, not by judgment. The ones that matter:

**Per trade**
- Risk exactly **1% of equity** per trade. Share count is derived from the
  stop distance, so every trade risks the same dollars regardless of the
  stock's price or volatility.
- Stop = **1.75 × ATR(14)** from the actual fill, not a fixed percentage — a
  flat % stop is too loose on calm names and gets noise-stopped on wild ones.
- Target = **2 × stop distance**. At 2:1, the strategy is profitable at a
  ~40% win rate. That is the bar it is held to.
- Entry is always a **limit order inside a bracket** — entry, stop, and
  target submit together, so a position is never in the market unprotected.
- **Walk-away rule:** unfilled after 20 minutes, the order is cancelled and
  the pick is logged as untradeable. The bot never chases.
- **Time stop:** flat after 5 trading days if neither stop nor target hit.

**Across the book**
- Max 5 concurrent positions, max **5% total open risk**, max 2 per sector.
- Never adds to a position, never averages down, never flips a position early.
- **Circuit breaker:** a 10% drawdown from the window's start pauses new
  entries and requires a logged human decision to resume.

**What makes the test valid**
- The rule set is **frozen for a 6-week evaluation window**. No mid-stream
  tweaks. Afterwards, at most **one dial** changes, and the reason is
  recorded in the config changelog.
- Unfilled, skipped, halted, and unborrowable picks are all logged. "The
  signal was right but untradeable" is a finding, not a footnote.
- The bot's log is **reconciled against the broker's account state** after
  every close. Mismatches are flagged loudly, never silently corrected.

---

## Running it

Requires Python 3.9+, an Alpha Vantage key, and (for the bot) free Alpaca
**paper** keys.

```bash
cp config.example.json config.json   # then add your keys
python3 scan.py                      # fetch + filter today's movers
python3 score.py                     # score them -> Top 10
python3 track_record.py log          # log the day's picks for grading
python3 bot.py enter --dry-run       # rehearse the bot with no orders
python3 dashboard.py                 # http://localhost:5052
```

Later, once picks are ripe:

```bash
python3 track_record.py grade
python3 make_track_record.py         # regenerate the published record
```

`config.json` holds every dial and every key. It is gitignored and must
never be committed.

The morning sequence runs unattended via macOS `launchd` at 9:31 AM ET; if
the machine was asleep the day is logged as **missed** rather than traded
late, because a late entry is a different strategy than the one being tested.

---

## Status

Milestones 1-5 complete: scanner, scorer, ledger, dashboard, paper bot.
The first evaluation window is running. `polybot/` is a retired
Polymarket news-latency experiment, kept on disk with its 24-trade ledger
for reference and no longer wired into the app.
