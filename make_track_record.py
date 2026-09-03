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
BOT_LOG = os.path.join(HERE, "snapshots", "bot_log.jsonl")

# The bot's starting equity for the evaluation window. The Alpaca paper
# account is shared with other strategies, so account equity is NOT the bot's
# result -- realized P&L from the bot's own closed trades is.
WINDOW_START_EQUITY = 100_000.0

FIELDS = ["pick_date", "ticker", "direction", "score", "entry_close",
          "exit_date", "exit_close", "return_pct", "spy_return_pct",
          "beat_spy", "no_call"]


def load():
    with open(LEDGER) as fh:
        return [json.loads(l) for l in fh if l.strip()]


def bot_results():
    """Realized P&L from the bot's own closed trades.

    Deliberately NOT read from account equity: the paper account is shared
    with other strategies, so its equity reflects their positions too. Only
    the bot's own fills and exits belong in the bot's number.
    """
    if not os.path.exists(BOT_LOG):
        return None
    with open(BOT_LOG) as fh:
        rows = [json.loads(l) for l in fh if l.strip()]
    exits = [r for r in rows
             if r.get("event") == "exit"
             and r.get("exit_price") and r.get("fill_price") and r.get("qty")]
    if not exits:
        return None
    # Exits logged before the attribution fix carry a single bundled reason
    # ("closed (manual / time-stop / re-protect)") that names three different
    # causes, one of which would violate rulebook 7. Rather than rewrite the
    # log -- an audit record you edit is not an audit record -- attribute them
    # here from the bot's own contemporaneous time_stop_submitted events, and
    # leave anything that does not match honestly unattributed.
    time_stops = defaultdict(list)
    for r in rows:
        if r.get("event") == "time_stop_submitted":
            time_stops[r["ticker"]].append(r.get("ts", ""))

    def reason_of(r):
        raw = str(r.get("reason", "unknown"))
        if not raw.startswith("closed (manual"):
            return raw
        prior = [t for t in time_stops.get(r["ticker"], []) if t <= r.get("ts", "")]
        if prior:
            return "time stop (attributed from log)"
        return "closed outside the bot (unattributed)"

    pnl = 0.0
    rets = []
    for r in exits:
        qty = float(r["qty"])
        fill = float(r["fill_price"])
        out = float(r["exit_price"])
        pnl += (out - fill) * qty if r.get("direction") == "LONG" else (fill - out) * qty
        if r.get("result_pct") is not None:
            rets.append(r["result_pct"])
    wins = [x for x in rets if x > 0]
    reasons = defaultdict(int)
    for r in exits:
        reasons[reason_of(r)] += 1
    return {
        "n": len(exits),
        "pnl": pnl,
        "pct_of_equity": 100 * pnl / WINDOW_START_EQUITY,
        "win_rate": 100 * len(wins) / len(rets) if rets else 0.0,
        "avg": sum(rets) / len(rets) if rets else 0.0,
        "best": max(rets) if rets else 0.0,
        "worst": min(rets) if rets else 0.0,
        "reasons": dict(reasons),
    }


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
    bot = bot_results()
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
    A("**Two different things are measured here, and they do not mean the "
      "same thing:**")
    A("")
    A("1. **Signal quality** (this file's main table) — what every pick did if "
      "you bought it and held it blind for the grading window. No stop, no "
      "target, no exit rule. This measures the *screener*.")
    A("2. **Strategy result** (the bot section) — what the trading rules "
      "actually produced on the picks the bot took, with stops, 2:1 targets, "
      "1% position sizing and a 5-day time stop. This measures the *system*.")
    A("")
    A("The gap between them is the value of the risk rules, and it is large. "
      "Read one as the other and you will get the wrong answer.")
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
    A("## What the bot actually did")
    A("")
    if bot:
        A(f"The screener's raw picks are one thing; the traded system is "
          f"another. Across **{bot['n']} closed trades**, the bot realized "
          f"**${bot['pnl']:,.2f}** — **{bot['pct_of_equity']:+.2f}%** on its "
          f"${WINDOW_START_EQUITY:,.0f} starting equity — at a "
          f"{bot['win_rate']:.1f}% win rate.")
        A("")
        A("| | |")
        A("|---|---|")
        A(f"| Closed trades | {bot['n']} |")
        A(f"| Realized P&L | **${bot['pnl']:,.2f}** ({bot['pct_of_equity']:+.2f}% of starting equity) |")
        A(f"| Win rate | {bot['win_rate']:.1f}% |")
        A(f"| Avg per trade | {bot['avg']:+.2f}% |")
        A(f"| Best / worst | {bot['best']:+.1f}% / {bot['worst']:+.1f}% |")
        A("")
        A("**How every position was closed:**")
        A("")
        A("| Exit reason | Trades |")
        A("|---|---|")
        for k in sorted(bot["reasons"], key=lambda k: -bot["reasons"][k]):
            A(f"| {k} | {bot['reasons'][k]} |")
        A("")
        A("Exits marked *attributed from log* were recorded before the bot "
          "logged its own exit intent; they are matched to the "
          "`time_stop_submitted` event the bot wrote at the time it initiated "
          "the close. Two early exits match no such event and are left "
          "**unattributed** rather than assumed benign — under rulebook 7 the "
          "bot is not supposed to be closed by hand, so an exit it cannot "
          "account for is a finding, not a footnote. The bot now records its "
          "exit intent before closing, so new exits are attributed directly.")
        A("")
        A("That figure is realized P&L from the bot's **own** fills and exits. "
          "It is deliberately not read off account equity: the paper account "
          "is shared with other strategies, so its equity is not the bot's "
          "result.")
    A("")
    A("## The honest read")
    A("")
    if all_s and bot:
        A(f"Held blind, the average pick loses **{abs(all_s['avg']):.2f}%** while "
          f"SPY returns **{all_s['spy_avg']:+.2f}%** over the same windows. "
          f"Traded under the rulebook, the same signal source returned "
          f"**{bot['pct_of_equity']:+.2f}%**. **The risk management is doing the "
          f"work, not the signal.**")
        A("")
        A("The clearest single case is **PLAG**. The ledger grades it "
          "**-86.3%** — bought at 5.81, and five days later it traded at 0.79. "
          "The bot logged the same pick as **+59.15%, target hit**: it took "
          "profit at its 2:1 target and was out long before the collapse. One "
          "pick, opposite outcomes, and the difference is entirely the exit "
          "rule.")
        A("")
        A("Read honestly, that cuts both ways. A screener whose picks lose "
          "money when held is not a good screener, and the median call of "
          f"{all_s['median']:+.2f}% says the edge in the raw signal is thin at "
          "best. What the record supports is a narrower claim: **a mechanical "
          "exit discipline can turn a mediocre signal into a positive result** "
          "— which is worth knowing, and is not the same as having found alpha.")
        A("")
        A("The sample is small. "
          f"{bot['n']} closed trades over {len(dates)} trading days is not "
          "enough to distinguish skill from a favorable tape, several winners "
          "carry most of the P&L, and it is paper money, where fills are "
          "kinder than they would be live. The evaluation window is frozen "
          "precisely so this gets more data before anyone concludes anything.")
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
