"""
track_record.py — Milestone 3: the honesty ledger.

The screener makes calls every day. This file is how we find out whether
those calls were any good — measured, not remembered. It does three things:

  log     Append today's Top 10 picks to the permanent ledger. Every pick
          is recorded, including WATCH ("no call") and the ones the bot
          would skip — an untradeable-but-right signal is a finding.

  grade   For any pick old enough (close +N trading days, N from config),
          measure its close-to-close move and SPY's move over the SAME
          window, then FREEZE the result. The grading rule never changes
          mid-window — that's what makes the track record trustworthy.

  report  Print the standing verdict: win rate, average return, and how
          the picks did versus just holding SPY.

Reference price is each pick's *closing* price on pick day (not the
intraday scan price), so the stock and SPY are compared over an identical
close-to-close window. The scan price is kept alongside, for reference.

Run it:
  python3 track_record.py log       # after a scan, log the day's picks
  python3 track_record.py grade      # fetch prices, grade what's ripe
  python3 track_record.py report     # see how the calls are doing
"""

import glob
import json
import os
import re
import sys
from datetime import datetime

from av_client import AlphaVantage, load_config

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP_DIR = os.path.join(HERE, "snapshots")
LEDGER = os.path.join(SNAP_DIR, "track_record.jsonl")
ADJ_CLOSE = "5. adjusted close"


# ---------------- ledger read/write (one JSON object per line) ----------------

def load_ledger():
    if not os.path.exists(LEDGER):
        return []
    rows = []
    with open(LEDGER) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def save_ledger(rows):
    os.makedirs(SNAP_DIR, exist_ok=True)
    with open(LEDGER, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


# ---------------- LOG ----------------

def latest_top10_snapshot():
    files = sorted(glob.glob(os.path.join(SNAP_DIR, "top10_*.json")))
    return files[-1] if files else None


def pick_date_from_name(path):
    """top10_2026-07-17_1848.json -> '2026-07-17'."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(path))
    return m.group(1) if m else datetime.now().strftime("%Y-%m-%d")


def cmd_log(args):
    snap = args[0] if args else latest_top10_snapshot()
    if not snap or not os.path.exists(snap):
        raise SystemExit(
            "No top-10 snapshot to log.\n"
            "Fix: run `python3 score.py` first, then `python3 track_record.py log`."
        )
    with open(snap) as f:
        picks = json.load(f)
    pick_date = pick_date_from_name(snap)

    ledger = load_ledger()
    already = {(r["pick_date"], r["ticker"]) for r in ledger}

    added, skipped = 0, 0
    for p in picks:
        key = (pick_date, p["ticker"])
        if key in already:
            skipped += 1
            continue
        ledger.append({
            "pick_date": pick_date,
            "ticker": p["ticker"],
            "direction": p["direction"],
            "score": p["score"],
            "scan_price": p["price"],
            "atr": p.get("atr"),
            "stop": p.get("stop"),
            "target": p.get("target"),
            "why": p.get("why", ""),
            "no_call": p["direction"] == "WATCH",
            # filled in at grade time, then frozen:
            "graded": False,
            "entry_close": None,
            "exit_date": None,
            "exit_close": None,
            "return_pct": None,       # direction-adjusted (SHORT inverts sign)
            "spy_return_pct": None,
            "beat_spy": None,
            "grade_note": None,
        })
        added += 1

    save_ledger(ledger)
    print(f"\nLogged {added} pick(s) for {pick_date} "
          f"({skipped} already in the ledger, left alone).")
    print(f"Ledger now holds {len(ledger)} pick(s) total.")
    tradeable = sum(1 for p in picks if p["direction"] != "WATCH")
    print(f"Of today's {len(picks)}: {tradeable} directional, "
          f"{len(picks) - tradeable} WATCH (logged as 'no call').")
    print("\nGrade them once 5 trading days have passed: "
          "python3 track_record.py grade")


# ---------------- GRADE ----------------

def adj_close_series(av, ticker):
    """{'YYYY-MM-DD': adjusted_close_float} or None on failure."""
    try:
        data = av.daily_adjusted(ticker)
        series = data.get("Time Series (Daily)", {})
        out = {}
        for d, bar in series.items():
            try:
                out[d] = float(bar[ADJ_CLOSE])
            except (KeyError, ValueError):
                continue
        return out or None
    except Exception:
        return None


def cmd_grade(args):
    config = load_config()
    av = AlphaVantage(config)
    tcfg = config.get("track_record", {})
    n_days = tcfg.get("grade_after_trading_days", 5)
    benchmark = tcfg.get("benchmark", "SPY")

    ledger = load_ledger()
    todo = [r for r in ledger if not r.get("graded")]
    if not todo:
        print("\nNothing to grade — every logged pick is already graded.")
        return

    print(f"\nGrading {len(todo)} ungraded pick(s), "
          f"window = close +{n_days} trading days vs {benchmark}.")

    # The benchmark defines the market calendar. One fetch, reused for all.
    spy = adj_close_series(av, benchmark)
    if not spy:
        raise SystemExit(f"Could not load {benchmark} prices — cannot grade. "
                         "Try again shortly.")
    spy_dates = sorted(spy.keys())              # ascending trading days

    graded_now = 0
    ticker_cache = {}
    for r in todo:
        pd = r["pick_date"]
        if pd not in spy_dates:
            r["grade_note"] = f"{pd} not a {benchmark} trading day"
            continue
        idx = spy_dates.index(pd)
        exit_idx = idx + n_days
        if exit_idx >= len(spy_dates):
            r["grade_note"] = (f"pending: {n_days} trading days not yet "
                               f"elapsed since {pd}")
            continue                            # too soon — leave ungraded
        exit_date = spy_dates[exit_idx]

        series = ticker_cache.get(r["ticker"])
        if series is None:
            series = adj_close_series(av, r["ticker"])
            ticker_cache[r["ticker"]] = series or {}
        if not series or pd not in series or exit_date not in series:
            r["grade_note"] = "missing price data for stock on entry/exit day"
            continue

        entry_c = series[pd]
        exit_c = series[exit_date]
        raw = (exit_c - entry_c) / entry_c
        directional = raw if r["direction"] != "SHORT" else -raw

        spy_ret = (spy[exit_date] - spy[pd]) / spy[pd]

        r["entry_close"] = round(entry_c, 4)
        r["exit_date"] = exit_date
        r["exit_close"] = round(exit_c, 4)
        r["return_pct"] = round(directional * 100, 2)
        r["spy_return_pct"] = round(spy_ret * 100, 2)
        r["beat_spy"] = None if r["no_call"] else (directional > spy_ret)
        r["grade_note"] = "graded" + (" (no call - excluded from stats)"
                                      if r["no_call"] else "")
        r["graded"] = True
        graded_now += 1

    save_ledger(ledger)
    pending = sum(1 for r in ledger
                  if not r.get("graded") and r.get("grade_note", "").startswith("pending"))
    print(f"\nGraded {graded_now} pick(s) this run. {pending} still pending "
          f"(not yet {n_days} trading days old).")
    print("See the verdict: python3 track_record.py report")


# ---------------- REPORT ----------------

def _avg(nums):
    return sum(nums) / len(nums) if nums else 0.0


def cmd_report(args):
    ledger = load_ledger()
    if not ledger:
        print("\nThe ledger is empty. Log some picks first: "
              "python3 track_record.py log")
        return

    graded = [r for r in ledger if r.get("graded")]
    pending = [r for r in ledger if not r.get("graded")]
    scored = [r for r in graded if not r["no_call"]]   # tradeable calls only

    print("\n=== TRACK RECORD ===")
    print(f"Ledger: {len(ledger)} picks | {len(graded)} graded | "
          f"{len(pending)} pending")

    if graded:
        print(f"\n{'DATE':<12}{'TICKER':<8}{'DIR':<7}{'SCORE':>5}"
              f"{'RET%':>8}{'SPY%':>8}{'ALPHA':>8}  RESULT")
        print("-" * 70)
        for r in sorted(graded, key=lambda x: (x["pick_date"], -x["score"])):
            if r["no_call"]:
                verdict = "no call"
            elif r["beat_spy"]:
                verdict = "beat SPY"
            else:
                verdict = "lagged SPY"
            ret = r["return_pct"]
            spy = r["spy_return_pct"]
            alpha = (ret - spy) if (ret is not None and spy is not None) else None
            print(f"{r['pick_date']:<12}{r['ticker']:<8}{r['direction']:<7}"
                  f"{r['score']:>5}"
                  f"{ret:>7.1f}%{spy:>7.1f}%"
                  f"{(alpha if alpha is not None else 0):>7.1f}%  {verdict}")

    if scored:
        rets = [r["return_pct"] for r in scored]
        spys = [r["spy_return_pct"] for r in scored]
        wins = sum(1 for x in rets if x > 0)
        beat = sum(1 for r in scored if r["beat_spy"])
        print("\n--- Verdict (tradeable calls only, WATCH excluded) ---")
        print(f"Graded calls:      {len(scored)}")
        print(f"Win rate:          {wins}/{len(scored)} "
              f"({100 * wins / len(scored):.0f}%)   "
              f"[rulebook's honest bar at 2:1 is ~40%]")
        print(f"Avg pick return:   {_avg(rets):+.2f}%")
        print(f"Avg SPY return:    {_avg(spys):+.2f}%")
        print(f"Avg alpha vs SPY:  {_avg(rets) - _avg(spys):+.2f}%")
        print(f"Beat SPY:          {beat}/{len(scored)} "
              f"({100 * beat / len(scored):.0f}%)")
    elif graded:
        print("\nNo tradeable (LONG/SHORT) calls graded yet — "
              "only WATCH 'no call' picks so far.")

    if pending:
        print(f"\n{len(pending)} pick(s) still ripening (need "
              "+5 trading days). Re-run `grade` after they age.")


# ---------------- entry point ----------------

USAGE = (
    "\nUsage:\n"
    "  python3 track_record.py log [snapshot.json]   log today's picks\n"
    "  python3 track_record.py grade                  grade what's ripe\n"
    "  python3 track_record.py report                 show the verdict\n"
)

COMMANDS = {"log": cmd_log, "grade": cmd_grade, "report": cmd_report}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        raise SystemExit(USAGE)
    COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        raise SystemExit(f"\n{e}\n")
