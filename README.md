# paper-trail

> ### +$6,527.92 realized — **+6.53%** on $100,000, across 18 closed trades at a 55.6% win rate.
> Rules-based paper bot, 38 trading days. Every call logged *before* the outcome was known.

[![realized P&L +6.53%](https://img.shields.io/badge/realized%20P%26L-%2B6.53%25-2ea44f?style=for-the-badge)](TRACK_RECORD.md)
[![win rate 55.6%](https://img.shields.io/badge/win%20rate-55.6%25-0969da?style=for-the-badge)](TRACK_RECORD.md)
[![18 closed trades](https://img.shields.io/badge/closed%20trades-18-0969da?style=for-the-badge)](TRACK_RECORD.md)
[![paper account](https://img.shields.io/badge/account-paper-6e7781?style=for-the-badge)](#the-result)

A daily equities research system: it scans the market for large intraday
movers, filters and scores them, hands the survivors to a rules-based paper
trading bot, and grades every call it ever made against SPY.

The name is the thesis. It paper-trades, and it leaves a paper trail — an
auditable record of every call, written down before the outcome was known,
graded by a rule that cannot be changed after the fact.

---

## The result

Across **18 closed trades** over 38 trading days, the bot's own fills and
exits produced:

| | |
|---|---|
| **Realized P&L** | **+$6,527.92** |
| **Return on $100,000 starting equity** | **+6.53%** |
| Win rate | 55.6% |
| Average per trade | +6.10% |
| Best / worst trade | +59.1% / −20.4% |

Those are realized numbers from the bot's **own** fills and exits, not account
equity — the paper account is shared with other strategies, so its equity is
not the bot's result. Fills are Alpaca paper fills; the bot refuses live keys
by design.

### What produced it

The interesting part is where the money came from. Run the *same* picks
bought and held blind — no stop, no target, no exit rule — and they lose:

| | Raw signal, held blind | Same signal, traded under the rulebook |
|---|---|---|
| Sample | 183 graded calls | 18 closed trades |
| Average return | **−5.85%** | **+6.10%** |
| Win rate | 43.2% | 55.6% |
| SPY over the same windows | +0.50% | — |

**The risk management is doing the work, not the signal.**

That is a more useful finding than "the screener picks winners" would have
been. Exit discipline is portable to any signal source; a lucky screener is
not.

The clearest single case is **PLAG**. The ledger grades it **−86.3%** —
bought at 5.81, and five days later it traded at 0.79. The bot logged the
same pick as **+59.15%, target hit**: it took its 2:1 target and was out long
before the collapse. One pick, opposite outcomes, entirely because of the
exit rule.

**The bounds on this.** 18 closed trades is a small sample, several winners
carry most of the P&L, and paper fills are kinder than live ones. The
headline moved from +6.89% to +6.53% the day the 18th trade closed red —
which is what a ledger you cannot edit after the fact does to you. Every
individual call, including every loser, is published in
**[TRACK_RECORD.md](TRACK_RECORD.md)**, and the raw graded rows are in
[track_record.csv](track_record.csv) if you want to check the arithmetic. A
track record that drops its losers is not a track record.

> Paper money only. The bot refuses live Alpaca keys by design. Nothing here
> is investment advice.

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
**paper** keys. There are **no third-party dependencies** — the whole system
runs on the standard library, so there is nothing to `pip install`.

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

`snapshots/` ships the two ledgers the track record is built from —
`track_record.jsonl` (every pick, graded and frozen) and `bot_log.jsonl`
(every fill, exit and skip) — plus one scan and one Top 10 as format samples.
The daily raw dumps are not committed; they regenerate on every run.

The morning sequence runs unattended via macOS `launchd` at 9:31 AM ET; if
the machine was asleep the day is logged as **missed** rather than traded
late, because a late entry is a different strategy than the one being tested.

---

## Status

Milestones 1-5 complete: scanner, scorer, ledger, dashboard, paper bot.
The first evaluation window is running.
