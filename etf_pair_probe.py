#!/usr/bin/env python3
"""
etf_pair_probe.py — a PARALLEL diagnostic. It changes no rule and touches no
live path. Rulebook section 8 allows variants to "run in parallel as separate
logged strategies against the same days — never as replacements mid-test",
which is what this is: it reads the archived Top 10 snapshots and reports how
often the screener ranked the same bet twice.

The question it answers, and the only one worth acting on: how often would the
duplication have ACTUALLY reached the book — both legs at or above the score
floor on the same day? If the answer is zero, the filter is not worth a dial.

Run:  python3 etf_pair_probe.py
"""
import json, glob, os, collections

HERE = os.path.dirname(os.path.abspath(__file__))

# Inverse pairs: (bull, bear) on the same underlying. Long the bear is the
# same position as short the bull, so these collide whenever the directions
# are opposed.
INVERSE_PAIRS = [
    ("SOXL", "SOXS", "semiconductors"),
    ("TQQQ", "SQQQ", "nasdaq-100"),
    ("SPXL", "SPXS", "s&p 500"),
    ("UPRO", "SPXU", "s&p 500"),
    ("TNA",  "TZA",  "small caps"),
    ("LABU", "LABD", "biotech"),
    ("NUGT", "DUST", "gold miners"),
    ("JNUG", "JDST", "junior gold"),
    ("YINN", "YANG", "china"),
    ("FAS",  "FAZ",  "financials"),
    ("ERX",  "ERY",  "energy"),
    ("GUSH", "DRIP", "oil & gas"),
    ("BOIL", "KOLD", "natural gas"),
]

# Leveraged semis ETFs vs semis single names — a softer conflict: same factor
# exposure, not the same instrument.
SEMIS_ETF = {"SOXL": +1, "SOXS": -1}
SEMIS_NAMES = {"NVDA", "AMD", "INTC", "MU", "AVGO", "TSM", "SMCI", "ARM", "QCOM"}

SCORE_FLOOR = 80  # bot_dials score floor; both legs must clear it to matter


def signed(direction):
    """+1 long, -1 short, 0 watch/unknown."""
    d = (direction or "").upper()
    return 1 if d == "LONG" else (-1 if d == "SHORT" else 0)


def main():
    files = sorted(glob.glob(os.path.join(HERE, "snapshots", "top10_*.json")))
    if not files:
        print("No top10_*.json snapshots found.")
        return

    days = 0
    dup_days = []          # same bet expressed twice
    dup_tradeable = []     # ...and both legs cleared the score floor
    semis_conflict = []    # leveraged semis ETF opposing a semis single name

    for path in files:
        try:
            rows = json.load(open(path))
        except Exception:
            continue
        if not isinstance(rows, list) or not rows:
            continue
        days += 1
        date = os.path.basename(path)[6:16]
        by = {r.get("ticker"): r for r in rows if r.get("ticker")}

        for bull, bear, underlying in INVERSE_PAIRS:
            if bull in by and bear in by:
                sb, sr = signed(by[bull].get("direction")), signed(by[bear].get("direction"))
                if sb and sr and sb != sr:
                    # opposed directions on an inverse pair == the same bet twice
                    scores = (by[bull].get("score", 0), by[bear].get("score", 0))
                    rec = (date, bull, by[bull].get("direction"), scores[0],
                           bear, by[bear].get("direction"), scores[1], underlying)
                    dup_days.append(rec)
                    if scores[0] >= SCORE_FLOOR and scores[1] >= SCORE_FLOOR:
                        dup_tradeable.append(rec)

        for etf, lev_sign in SEMIS_ETF.items():
            if etf not in by:
                continue
            etf_bet = signed(by[etf].get("direction")) * lev_sign  # net semis view
            if not etf_bet:
                continue
            for name in SEMIS_NAMES & set(by):
                nb = signed(by[name].get("direction"))
                if nb and nb != etf_bet:
                    semis_conflict.append(
                        (date, etf, by[etf].get("direction"), by[etf].get("score", 0),
                         name, by[name].get("direction"), by[name].get("score", 0)))

    print("=" * 74)
    print(f"ETF PAIR PROBE — {days} days of Top 10 snapshots")
    print("=" * 74)
    print(f"\nScore floor for entry: {SCORE_FLOOR}\n")

    print(f"1. Same bet ranked twice (inverse pair, opposed directions): "
          f"{len(dup_days)} occurrences on {len(set(d[0] for d in dup_days))} days")
    for d in dup_days[-12:]:
        print(f"   {d[0]}  {d[1]} {d[2]} (score {d[3]})  ==  {d[4]} {d[5]} (score {d[6]})   [{d[7]}]")
    if len(dup_days) > 12:
        print(f"   ... and {len(dup_days)-12} earlier")

    print(f"\n2. ...and BOTH legs cleared the score floor (would have reached the book): "
          f"{len(dup_tradeable)}")
    for d in dup_tradeable:
        print(f"   {d[0]}  {d[1]} {d[3]} / {d[4]} {d[6]}   [{d[7]}]")
    if not dup_tradeable:
        print("   none — the duplication never got near an entry")

    print(f"\n3. Leveraged semis ETF opposing a semis single name: {len(semis_conflict)}")
    for d in semis_conflict[-8:]:
        print(f"   {d[0]}  {d[1]} {d[2]} ({d[3]})  vs  {d[4]} {d[5]} ({d[6]})")

    freq = collections.Counter(d[7] for d in dup_days)
    if freq:
        print("\n4. Which underlyings duplicate most:")
        for u, n in freq.most_common():
            print(f"   {u:<18} {n}")

    print("\n" + "-" * 74)
    if dup_tradeable:
        print("VERDICT: duplication reached tradeable scores. Worth one dial after")
        print("the window closes 2026-09-21 — a same-underlying dedupe in score.py.")
    elif dup_days:
        print("VERDICT: the screener does rank the same bet twice, but never with")
        print("both legs above the score floor. It is a display/ranking wart, not a")
        print("risk leak. Do not spend the post-window dial change on it.")
    else:
        print("VERDICT: no duplication found in the archived snapshots.")
    print("-" * 74)


if __name__ == "__main__":
    main()
