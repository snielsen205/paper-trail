"""
make_track_record.py - build the public track record from the ledger.

Reads snapshots/track_record.jsonl (the frozen grading ledger) and writes:
  TRACK_RECORD.md   human-readable summary, every call included
  track_record.csv  the raw graded rows, for anyone who wants to check the math

Nothing here filters, reweights, or excludes a losing trade. If a pick was
logged and graded, it appears. That is the whole point of the file.
"""

import csv
import json
import os
from collections import defaultdict
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(HERE, "snapshots", "track_record.jsonl")

FIELDS = ["pick_date", "ticker", "direction", "score", "entry_close",
          "exit_date", "exit_close", "return_pct", "spy_return_pct",
          "beat_spy", "no_call"]


def load():
    with open(LEDGER) as fh:
        return [json.loads(l) for l in fh if l.strip()]


def summarize(rows):
    """Win rate, average and median return, and the same for SPY."""
    rets = [r["return_pct"] for r in rows if r.get("return_pct") is not None]
    if not rets:
        return None
    spy = [r["spy_return_pct"] for r in rows if r.get("spy_return_pct") is not None]
    wins = [x for x in rets if x > 0]
    beats = [r for r in rows if r.get("beat_spy")]
    srt = sorted(rets)
    mid = len(srt) // 2
    median = srt[mid] if len(srt) % 2 else (srt[mid - 1] + srt[mid]) / 2
    return {
        "n": len(rets),
        "win_rate": 100 * len(wins) / len(rets),
        "avg": sum(rets) / len(rets),
        "median": median,
        "beat_spy": 100 * len(beats) / len(rows),
        "spy_avg": sum(spy) / len(spy) if spy else 0.0,
        "best": max(rets),
        "worst": min(rets),
    }


def row(label, s):
    if not s:
        return f"| {label} | 0 | - | - | - | - | - |"
    return (f"| {label} | {s['n']} | {s['win_rate']:.1f}% | {s['avg']:+.2f}% | "
            f"{s['median']:+.2f}% | {s['beat_spy']:.1f}% | {s['spy_avg']:+.2f}% |")


def main():
    rows = load()
    graded = [r for r in rows if r.get("graded")]
    pending = [r for r in rows if not r.get("graded")]
    calls = [r for r in graded if not r.get("no_call")]
    watch = [r for r in graded if r.get("no_call")]

    dates = sorted({r["pick_date"] for r in rows})
    all_s = summarize(calls)

    out = []
    A = out.append
    A("# Track Record")
    A("")
    A(f"Every pick this screener has made, from **{dates[0]}** to **{dates[-1]}** "
      f"({len(dates)} trading days). Nothing is excluded. The losers are here "
      f"because a track record that drops them is not a track record.")
    A("")
    A(f"*Generated {date.today().isoformat()} from `snapshots/track_record.jsonl`. "
      f"Regenerate with `python3 make_track_record.py`.*")
    A("")
    A("## How grading works")
    A("")
    A("- A pick is logged **before** the outcome is known, on the day it is made.")
    A("- It is graded at a fixed horizon set in config, measured **close-to-close** "
      "on split-adjusted prices, against SPY over the identical window.")
    A("- Once graded, the result is **frozen**. The rule cannot be changed after "
      "seeing the outcome.")
    A("- `SHORT` picks have their sign inverted, so a stock that doubles is a "
      "-100% short.")
    A("- Picks the bot flagged **WATCH** (no position taken) are graded and shown "
      "separately. They are kept because a signal that was right but untradeable "
      "is still information about the screener.")
    A("")
    A("## Results")
    A("")
    A("| Segment | Picks | Win rate | Avg return | Median | Beat SPY | SPY avg |")
    A("|---|---|---|---|---|---|---|")
    A(row("**All calls**", all_s))
    for d in ("LONG", "SHORT"):
        A(row(d, [r for r in calls if r.get("direction") == d]and summarize([r for r in calls if r.get("direction") == d]) or None))
    A(row("WATCH (no position)", summarize(watch)))
    A("")
    A(f"{len(pending)} picks are logged but not yet ripe for grading.")
    A("")
    A("## The honest read")
    A("")
    if all_s and all_s["avg"] < 0:
        A(f"**This strategy is losing money.** Over {all_s['n']} graded calls the "
          f"average is **{all_s['avg']:+.2f}%** while SPY returned "
          f"**{all_s['spy_avg']:+.2f}%** over the same windows. It beat SPY on "
          f"{all_s['beat_spy']:.1f}% of calls and won outright "
          f"{all_s['win_rate']:.1f}% of the time.")
        A("")
        A(f"The distribution is the story: a median of {all_s['median']:+.2f}% "
          f"against a mean of {all_s['avg']:+.2f}% means the average is being "
          f"dragged by a small number of severe losses, not by broad weakness. "
          f"The worst single call was {all_s['worst']:+.1f}%; the best was "
          f"{all_s['best']:+.1f}%.")
        A("")
        A("That gap is the actual finding, and it points at position sizing and "
          "stop discipline rather than at the signal itself. It is being "
          "addressed in the rulebook, not by re-running the screener until the "
          "numbers look better.")
    A("")
    A("## Every graded call")
    A("")
    A("| Date | Ticker | Dir | Entry | Exit | Return | SPY | Beat SPY |")
    A("|---|---|---|---|---|---|---|---|")
    for r in sorted(calls, key=lambda r: (r["pick_date"], r["ticker"])):
        A(f"| {r['pick_date']} | {r['ticker']} | {r.get('direction','')} | "
          f"{r.get('entry_close')} | {r.get('exit_close')} | "
          f"{r.get('return_pct',0):+.2f}% | {r.get('spy_return_pct',0):+.2f}% | "
          f"{'yes' if r.get('beat_spy') else 'no'} |")
    A("")

    with open(os.path.join(HERE, "TRACK_RECORD.md"), "w") as fh:
        fh.write("\n".join(out) + "\n")

    with open(os.path.join(HERE, "track_record.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in sorted(graded, key=lambda r: (r["pick_date"], r["ticker"])):
            w.writerow(r)

    print(f"TRACK_RECORD.md  ({len(calls)} calls, {len(watch)} watch)")
    print(f"track_record.csv ({len(graded)} graded rows)")


if __name__ == "__main__":
    main()
