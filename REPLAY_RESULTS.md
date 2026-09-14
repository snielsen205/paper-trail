# Rulebook replay — 2026-09-14

**The exits do not rescue a bad signal. The score floor is what works, and
lowering it to get more trades would destroy the result.**

Run `python3 replay_rulebook.py --floors 0,50,60,70,80,90` to reproduce.

## Why this was run

The live bot has 18 closed trades — too few to separate exit discipline from a
friendly tape, and at ~12 entries/month n=100 is seven months away. The ledger
holds 224 directional calls. Resolving each against its own minute tape under
the frozen dials gives a large-n read now. This is a parallel measurement under
rulebook section 8; no dial was changed and window 1 stayed frozen.

## The result

Per-trade expectancy, frozen dials (1.75xATR stop, 2:1 target, 5-day time stop):

| score floor | n | win rate | avg | **median** | t | worst |
|---|---|---|---|---|---|---|
| 0 (all calls) | 184 | 39.1% | **−0.72%** | −2.72% | −0.48 | −89.1% |
| 50 | 92 | 43.5% | **−1.13%** | −3.33% | −0.46 | −89.1% |
| 60 | 74 | 43.2% | **−1.71%** | −3.51% | −0.63 | −89.1% |
| 70 | 53 | 41.5% | **−4.76%** | −5.32% | −1.55 | −89.1% |
| **80 (live)** | **30** | **56.7%** | **+2.58%** | **+0.30%** | **+0.76** | −23.2% |
| 90 | 20 | 65.0% | **+3.26%** | +0.86% | +0.89 | −23.2% |

### 1. Brackets alone are not the edge

Run the full rule set — entry, ATR bracket, time stop, everything — over all
184 resolvable calls and it returns **−0.72% per trade**. The claim that
mechanical exits turn a losing signal into a positive result is **false at
scale**. It is true only inside the top score bucket.

### 2. The score floor of 80 is doing the work

There is a sharp discontinuity between floor 70 (−4.76%) and floor 80
(+2.58%) — a 7-point swing from removing 23 picks. The 70–79 band is the worst
segment on the board, which matches the ledger's own bucket analysis (−6.8%).
The screener's raw picks lose money held blind at every score, but **score ≥ 80
plus a bracket** is the one combination that is positive here.

### 3. It still is not significant

At the live floor: **n=30, t=+0.76**. At floor 90: n=20, t=+0.89. Neither is
close to 2. And the median at floor 80 is **+0.30%** — essentially flat, with
the +2.58% mean carried by a tail (best +56.2%). A rule set that works only
through its tail is one bad fill from not working.

### 4. The live record is flattered by selection

The bot took 18 of the 33 eligible picks; portfolio caps decided which. Same
floor, same rules, same window:

| | per-trade avg |
|---|---|
| live bot, 18 trades | **+6.10%** |
| replay, all 30 eligible | **+2.58%** |

The live number is **2.4x** the rule set's expectancy on the full eligible
list. The bot did not just follow the rules — it got the better half of them.
That gap is the clearest evidence yet that +6.53% overstates what these rules
produce.

## What this closes

**Lowering the score floor to accumulate trades faster is measured and dead.**
It looked like the obvious way to reach n=100 before next spring. It would take
per-trade expectancy from +2.58% to −1.71% (floor 60) or −4.76% (floor 70). The
extra trades are losers. Do not run that variant.

## Limits, stated plainly

- **Per-trade expectancy, not P&L.** No position sizing, no portfolio caps, no
  capital bookkeeping. Not comparable to the live +6.53%.
- **Fills are simulated** off the free IEX feed at a 9:45 marketable limit.
  Nine calls were excluded for coverage below the floor rather than filled on
  partial data — thin names are where adverse moves hide, so the surviving
  sample leans liquid and this is an **upper bound**.
- **30 calls were still open** at the end of the tape and are excluded.
- Same window as the live record, so it is not out-of-sample. It answers
  "what do these rules do across all the picks," not "do they work in 2027."
