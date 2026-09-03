"""
review.py — the learning instrument. Reads the winning and losing trades and
turns them into evidence, tested against the hypotheses we wrote down before
the window started. It CHANGES NOTHING — the rulebook is frozen for the 6-week
window; this only tells us what the data is saying, so that when the window
closes we change one dial for a reason instead of a hunch.

Two ways to see it:
  python3 review.py           — text report in the terminal
  the dashboard's "Review" tab — same analysis, live, via build_review()

Reads only (no API calls, no orders):
  snapshots/bot_log.jsonl        — movers bot: entries, exits, skips, misses
  snapshots/track_record.jsonl   — screener calls graded vs SPY at +5 days
"""

import json
import os
import statistics
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP = os.path.join(HERE, "snapshots")

# Hypotheses on trial this window (from CLAUDE.md "window-2 candidates").
# review.py exists to confirm or kill each with data, never to act on them now.
HYPOTHESES = [
    "Exhaustion: does the bot lose more on names already up big at entry?",
    "Friction: do wide-spread entries (paper-flattered) underperform?",
    "Dilution: do small-cap gainers reverse overnight on share offerings?",
    "Floor: are score>=80 winners actually cleaner than the 70-79 it skipped?",
]

MIN_TRADES = 6      # below this, cross-tabs are noise; we say so instead.


def load_jsonl(name):
    path = os.path.join(SNAP, name)
    if not os.path.exists(path):
        return []
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return rows


def compute_movers(log):
    """Closed movers trades: realized record + exhaustion/friction cross-tabs
    (joins each exit back to its entry for change_pct / spread_pct)."""
    entries = {}
    for r in log:
        if r.get("event") in ("entered", "partial_fill"):
            entries[(r.get("ticker"), r.get("date"))] = r
    exits = [r for r in log if r.get("event") == "exit"
             and r.get("result_pct") is not None]
    open_now = sum(1 for r in log if r.get("event") == "entered") - len(exits)

    if not exits:
        return {"closed": 0, "open": max(open_now, 0),
                "note": "No closed trades yet — brackets resolve into wins/"
                        "losses over time. Open-position P&L is a rumor, not "
                        "a result."}

    results = [e["result_pct"] for e in exits]
    wins = [r for r in results if r > 0]
    beat = [e for e in exits if e.get("spy_pct") is not None
            and (e["result_pct"] - e["spy_pct"]) > 0]
    out = {
        "closed": len(exits), "open": max(open_now, 0),
        "win_rate": round(len(wins) / len(exits) * 100),
        "avg_result": round(statistics.fmean(results), 1),
        "beat_spy": len(beat), "total_realized": round(sum(results), 1),
    }

    tagged = []
    for e in exits:
        ent = entries.get((e.get("ticker"), e.get("entry_date")))
        if ent and ent.get("change_pct") is not None:
            tagged.append((abs(ent["change_pct"]), e["result_pct"]))
    if len(tagged) >= MIN_TRADES:
        tagged.sort()
        half = len(tagged) // 2
        calm = round(statistics.fmean(r for _, r in tagged[:half]), 1)
        hot = round(statistics.fmean(r for _, r in tagged[-half:]), 1)
        out["exhaustion"] = {
            "calm": calm, "hot": hot,
            "verdict": ("hot names doing worse — supports the exhaustion worry"
                        if hot < calm - 1 else "no clear exhaustion effect yet"),
        }
    else:
        out["exhaustion_pending"] = f"need ~{MIN_TRADES} closed, have {len(tagged)}"
    return out


def compute_screener(track):
    """Screener call quality vs SPY, independent of whether the bot traded."""
    graded = [r for r in track if r.get("graded") and not r.get("no_call")]
    pending = [r for r in track if not r.get("graded") and not r.get("no_call")]
    if not graded:
        return {"graded": 0, "pending": len(pending),
                "note": "None graded yet — each pick needs +5 trading days. "
                        "Measures the SCREENER's judgment even on no-trade days."}
    beat = [r for r in graded if r.get("beat_spy")]
    out = {
        "graded": len(graded), "pending": len(pending),
        "beat_spy_pct": round(len(beat) / len(graded) * 100),
        "avg_return": round(statistics.fmean(r["return_pct"] for r in graded), 1),
        "avg_spy": round(statistics.fmean(r["spy_return_pct"] for r in graded), 1),
    }
    hi = [r["return_pct"] for r in graded if r["score"] >= 80]
    lo = [r["return_pct"] for r in graded if r["score"] < 80]
    if hi and lo:
        out["floor"] = {"hi": round(statistics.fmean(hi), 1),
                        "lo": round(statistics.fmean(lo), 1)}
    return out


def build_review():
    """Everything the CLI and the dashboard both render. Read-only."""
    return {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "movers": compute_movers(load_jsonl("bot_log.jsonl")),
        "screener": compute_screener(load_jsonl("track_record.jsonl")),
        "hypotheses": HYPOTHESES,
    }


def main():
    d = build_review()
    m, s = d["movers"], d["screener"]
    print("\n" + "#" * 60)
    print(f"#  MOVERS LEARNING REVIEW — {d['generated']}")
    print("#" * 60)

    print("\n" + "=" * 60 + "\n  MOVERS BOT — CLOSED TRADES\n" + "=" * 60)
    if m["closed"] == 0:
        print(f"  {m['note']}")
    else:
        print(f"  Closed {m['closed']} | win rate {m['win_rate']}% | "
              f"avg {m['avg_result']:+}% | beat SPY {m['beat_spy']}/{m['closed']}")
        if "exhaustion" in m:
            ex = m["exhaustion"]
            print(f"  Exhaustion: calm half {ex['calm']:+}% vs hot half "
                  f"{ex['hot']:+}% -> {ex['verdict']}")
        elif "exhaustion_pending" in m:
            print(f"  Exhaustion/friction test: {m['exhaustion_pending']}")

    print("\n" + "=" * 60 + "\n  SCREENER CALLS — vs SPY (+5 days)\n" + "=" * 60)
    if s["graded"] == 0:
        print(f"  {s['note']}  ({s['pending']} ripening)")
    else:
        print(f"  Graded {s['graded']} | beat SPY {s['beat_spy_pct']}% | "
              f"avg call {s['avg_return']:+}% vs SPY {s['avg_spy']:+}%")
        if "floor" in s:
            print(f"  Floor: score>=80 {s['floor']['hi']:+}% vs 70-79 "
                  f"{s['floor']['lo']:+}%")

    print("\n" + "=" * 60 + "\n  HYPOTHESES ON TRIAL (do NOT act mid-window):")
    for h in d["hypotheses"]:
        print(f"   - {h}")
    print("=" * 60)
    print("  Window FROZEN. Findings justify ONE dial change AFTER the window,")
    print("  logged with its reason. Lessons: movers_trading_lessons.md.\n")


if __name__ == "__main__":
    main()
