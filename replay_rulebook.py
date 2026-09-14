#!/usr/bin/env python3
"""
replay_rulebook.py — run the frozen bot rulebook over EVERY directional pick in
the ledger, not just the ones the live bot had room to take.

WHY THIS EXISTS. The live bot has 18 closed trades. That is too few to tell
whether the exit discipline adds value or whether a friendly tape did. The
ledger holds 224 directional calls. Resolving each one against its own minute
tape gives a large-n read on the same question this week instead of next
spring, at ~12 entries/month.

WHAT IT IS NOT. This is a PARALLEL measurement under rulebook section 8
("future variants run in parallel as separate logged strategies against the
same days — never as replacements mid-test"). It changes no dial, writes to no
live file, and reads config.json read-only. Window 1 stays frozen.

It is also NOT "what the bot would have earned." Portfolio constraints — max 5
concurrent, 5% total open risk, 2 per sector, 1% position sizing — are
deliberately NOT applied. Each pick resolves independently off its own tape, so
what this measures is PER-TRADE EXPECTANCY of the entry+bracket+time-stop rule.
Adding capital bookkeeping would shrink n back toward the live number and
answer a different question.

THE HONEST LIMIT. Fills come from the free IEX feed, which sees one venue's
slice. Thin names return a handful of minutes, and the missing minutes are
exactly the ones where a stop might have printed — sparse data does not add
noise, it HIDES stop-outs and flatters the result. resolve_intraday() therefore
EXCLUDES any name under the coverage floor rather than filling on partial data.
That exclusion is not free: it biases the surviving sample toward liquid names,
so the coverage-skip count is reported as a headline number, not a footnote.

Run:  python3 replay_rulebook.py
      python3 replay_rulebook.py --floors 60,70,80,90
      python3 replay_rulebook.py --coverage 50
"""
import argparse, json, os, sys, collections

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# Reuse the vetted machinery rather than writing a second fill model. Two fill
# models that disagree is a worse problem than no second measurement at all.
from analyst_book import minute_bars, resolve_intraday       # noqa: E402
# Imported directly and only for READ-ONLY bar fetches, matching the
# convention analyst_book.py uses: this module never submits an order.
from alpaca_client import Alpaca                             # noqa: E402


def load_cfg():
    with open(os.path.join(HERE, "config.json")) as f:
        return json.load(f)


def directional_picks(path):
    """Every LONG/SHORT call in the ledger. WATCH is excluded because the bot
    never trades it — including it would measure the screener, not the rules."""
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("direction") in ("LONG", "SHORT") and r.get("atr"):
                out.append(r)
    return out


def ret_pct(side, entry, exit_px):
    if not entry:
        return None
    return ((exit_px - entry) / entry * 100.0) if side == "LONG" \
        else ((entry - exit_px) / entry * 100.0)


def replay(picks, api, bd, max_atr_pct, hold_days, coverage_floor, floor):
    """Resolve every pick at or above `floor`. Returns (trades, skips)."""
    trades, skips = [], collections.Counter()
    eligible = [p for p in picks if (p.get("score") or 0) >= floor]

    for p in eligible:
        sym, day, side = p["ticker"], p["pick_date"], p["direction"]
        # +10 calendar days covers 5 trading days plus weekends/holidays.
        end = _add_days(day, 12)
        try:
            bars = minute_bars(api, sym, day, end)
        except Exception as e:
            skips[f"bar fetch failed: {type(e).__name__}"] += 1
            continue

        r = resolve_intraday(bars, day, side, float(p["atr"]), bd, hold_days,
                             coverage_floor, max_atr_pct)
        if "skip" in r:
            # Collapse the parameterised coverage message into one bucket.
            reason = r["skip"]
            if reason.startswith("IEX coverage"):
                reason = "IEX coverage below floor"
            elif reason.startswith("TOO VOLATILE"):
                reason = "ATR ceiling (never bot-eligible)"
            skips[reason] += 1
            continue
        if r.get("exit") is None:
            skips["still open at end of tape"] += 1
            continue

        trades.append({
            "date": day, "ticker": sym, "side": side,
            "score": p.get("score"), "entry": r["entry"], "exit": r["exit"],
            "reason": r["reason"], "coverage": r["coverage"],
            "ret": ret_pct(side, r["entry"], r["exit"]),
        })
    return trades, skips, len(eligible)


def _add_days(day, n):
    from datetime import datetime, timedelta
    return (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=n)).strftime("%Y-%m-%d")


def summarize(trades):
    if not trades:
        return None
    rets = [t["ret"] for t in trades if t["ret"] is not None]
    n = len(rets)
    wins = [r for r in rets if r > 0]
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1) if n > 1 else 0.0
    sd = var ** 0.5
    t_stat = (mean / (sd / (n ** 0.5))) if sd > 0 and n > 1 else 0.0
    srt = sorted(rets)
    median = srt[n // 2] if n % 2 else (srt[n // 2 - 1] + srt[n // 2]) / 2
    return {
        "n": n, "win_rate": len(wins) / n * 100, "mean": mean,
        "median": median, "sd": sd, "t": t_stat,
        "best": max(rets), "worst": min(rets),
        "reasons": collections.Counter(t["reason"] for t in trades),
        "avg_coverage": sum(t["coverage"] for t in trades) / n,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--floors", default=None,
                    help="comma-separated score floors to sweep (default: the "
                         "frozen bot floor only)")
    ap.add_argument("--coverage", type=float, default=None,
                    help="override the IEX coverage floor %% (default: from config)")
    ap.add_argument("--json", metavar="PATH", help="also write per-trade rows here")
    args = ap.parse_args()

    cfg = load_cfg()
    bd = cfg["bot_dials"]
    ab = cfg.get("analyst_book", {})
    max_atr_pct = cfg["scoring"]["max_atr_pct_of_price"]
    hold_days = bd.get("time_stop_days", 5)
    coverage_floor = args.coverage if args.coverage is not None \
        else ab.get("min_bar_coverage_pct", 25.0)
    live_floor = bd.get("score_floor", 80)

    picks = directional_picks(os.path.join(HERE, "snapshots", "track_record.jsonl"))
    api = Alpaca(cfg)

    floors = [int(x) for x in args.floors.split(",")] if args.floors else [live_floor]

    print("=" * 78)
    print("RULEBOOK REPLAY — frozen dials, every directional pick in the ledger")
    print("=" * 78)
    print(f"\n  ledger directional picks : {len(picks)}   (WATCH excluded)")
    print(f"  dials                    : stop {bd.get('atr_stop_multiple')}xATR, "
          f"{bd.get('reward_to_risk')}:1 target, {hold_days}-day time stop")
    print(f"  ATR ceiling              : {max_atr_pct*100:.0f}% of price")
    print(f"  IEX coverage floor       : {coverage_floor:.0f}% of 390 min")
    print(f"  live bot score floor     : {live_floor}")
    print("\n  Per-trade expectancy only — no portfolio caps, no position sizing.\n")

    all_rows = {}
    for floor in floors:
        trades, skips, n_elig = replay(picks, api, bd, max_atr_pct, hold_days,
                                       coverage_floor, floor)
        s = summarize(trades)
        all_rows[floor] = trades
        tag = "  <- live bot floor" if floor == live_floor else ""
        print("-" * 78)
        print(f"SCORE FLOOR {floor}{tag}")
        print(f"  eligible picks: {n_elig}   resolved: {len(trades)}   "
              f"skipped: {sum(skips.values())}")
        if skips:
            for reason, c in skips.most_common():
                print(f"     {c:>4}  {reason}")
        if not s:
            print("  no resolvable trades at this floor\n")
            continue
        print(f"\n  n = {s['n']}   win rate {s['win_rate']:.1f}%   "
              f"avg {s['mean']:+.2f}%   median {s['median']:+.2f}%")
        print(f"  sd {s['sd']:.2f}   t = {s['t']:+.2f}   "
              f"best {s['best']:+.1f}%   worst {s['worst']:+.1f}%")
        print(f"  avg IEX coverage of resolved trades: {s['avg_coverage']:.0f}%")
        print(f"  exits: " + ", ".join(f"{k} {v}" for k, v in s["reasons"].most_common()))
        print()

    print("=" * 78)
    print("READING THIS")
    print("=" * 78)
    print("""
  MEAN vs MEDIAN is the whole story. A positive mean on a flat or negative
  median means a few outsized winners are carrying it — the same fat-tail
  artifact that pead_size.py and news-first both produced. A rule set that
  only works through its tail is one bad fill away from not working.

  The coverage skips are NOT neutral. Every excluded name is one the IEX feed
  could not see properly, and thin names are where the violent adverse moves
  live. The surviving sample leans liquid, so treat the result as an UPPER
  bound on what the rules would do across the full pick list.

  This is per-trade expectancy, not a P&L. It cannot be compared directly to
  the live bot's +6.53%, which is capital-weighted under position caps.
""")

    if args.json:
        rows = [r for f in all_rows for r in all_rows[f]]
        with open(args.json, "w") as f:
            json.dump(rows, f, indent=2)
        print(f"  per-trade rows written to {args.json}\n")


if __name__ == "__main__":
    main()
