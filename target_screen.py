#!/usr/bin/env python3
"""target_screen.py — under-$10 stocks with the biggest ANALYST TARGET upside,
filtered down to businesses that are actually businesses.

READ-ONLY RESEARCH. Places no orders, touches no broker, imports no
alpaca_client. Output is a reading list, not a buy list.

────────────────────────────────────────────────────────────────────────
WHY THIS SCREEN IS BUILT DEFENSIVELY (read before trusting the ranking)

Sorting by "biggest gap to the analyst target" is, mechanically, a screen
for STOCKS THAT JUST CRASHED. Targets are updated slowly and reluctantly,
so the largest upside almost always belongs to a name whose price
collapsed while the target sat still. The screen's own top line is
therefore its own worst enemy, and every gate below exists to fight it:

  1. ANALYST COUNT >= 4.  A "target" backed by one analyst at the bank
     that underwrote the IPO is not a consensus, it is a press release.
     AV gives StrongBuy/Buy/Hold/Sell/StrongSell counts — we sum them.
  2. MARKET CAP >= $200M and REVENUE >= $100M.  The owner looked at EXYN
     (2026-08-13): $22M cap, $5.8M revenue, -$15M/yr burn, a $10 target
     against a $2.82 price = "255% upside" that meant nothing. That whole
     class is excluded by these two lines.
  3. GROSS PROFIT > 0.  Sells its product for more than it costs to make.
     A shockingly effective filter at this price point.
  4. STALENESS FLAG.  If price is under half its 52-week high AND the
     upside is over 100%, the target is probably just old. Flagged loudly
     rather than silently dropped — sometimes it IS a real dislocation,
     but you should know which one you're looking at.

WHAT AN ANALYST TARGET IS WORTH: not much on its own. Published targets
are systematically optimistic, cluster around 12-month horizons that
rarely bind, and get revised toward the price rather than the reverse.
Treat a big number here as "several people who read the filings for a
living think this is mispriced" — a place to start reading, nothing more.

Usage:
    python3 target_screen.py                 # default: $1-$10
    python3 target_screen.py --min 3 --max 10
    python3 target_screen.py --all           # show flagged/failing too
"""
import argparse
import csv
import io
import json
import os
import sys
import urllib.request

from av_client import AlphaVantage

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP = os.path.join(HERE, "snapshots")

# ── gates ────────────────────────────────────────────────────────────────
MIN_DOLLAR_VOLUME = 2_000_000      # tradeable at all
MIN_MARKET_CAP    = 200_000_000    # not a shell (EXYN was $22M)
MIN_REVENUE       = 100_000_000    # a real operating business
MIN_ANALYSTS      = 4              # a consensus, not one person's opinion


def num(v, default=None):
    """AV returns 'None', '-', '' and real numbers as strings. Normalize."""
    if v in (None, "None", "-", "", "0.0000"):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def listing_universe(cfg):
    """Every active common stock on the three main US exchanges."""
    url = ("https://www.alphavantage.co/query?function=LISTING_STATUS"
           f"&apikey={cfg['alpha_vantage_api_key']}")
    txt = urllib.request.urlopen(url, timeout=90).read().decode()
    rows = list(csv.DictReader(io.StringIO(txt)))
    return [r["symbol"] for r in rows
            if r["assetType"] == "Stock"
            and r["exchange"] in ("NASDAQ", "NYSE", "AMEX")
            and r["status"] == "Active"
            and not any(c in r["symbol"] for c in "+=^.-")]


def price_universe(av, symbols):
    """Bulk-quote the whole list, 100 per request. ~25s for 7,500 names."""
    out = {}
    for i in range(0, len(symbols), 100):
        try:
            data = av.bulk_quotes(symbols[i:i + 100])
        except Exception:
            continue
        for q in data.get("data") or []:
            p, v = num(q.get("close"), 0), num(q.get("volume"), 0)
            if p and p > 0:
                out[q["symbol"]] = (p, v or 0)
    return out


def analyst_count(o):
    return sum(int(num(o.get(k), 0) or 0) for k in (
        "AnalystRatingStrongBuy", "AnalystRatingBuy", "AnalystRatingHold",
        "AnalystRatingSell", "AnalystRatingStrongSell"))


def assess(o, price):
    """Turn one overview into a row, or a reason it was rejected."""
    cap  = num(o.get("MarketCapitalization"))
    rev  = num(o.get("RevenueTTM"))
    gp   = num(o.get("GrossProfitTTM"))
    tgt  = num(o.get("AnalystTargetPrice"))
    n    = analyst_count(o)

    if not tgt or tgt <= price:      return None, "no target above price"
    if not cap or cap < MIN_MARKET_CAP:  return None, f"market cap ${(cap or 0)/1e6:.0f}M < $200M"
    if not rev or rev < MIN_REVENUE:     return None, f"revenue ${(rev or 0)/1e6:.0f}M < $100M"
    if gp is None or gp <= 0:            return None, "no gross profit"
    if n < MIN_ANALYSTS:                 return None, f"only {n} analyst(s)"

    hi   = num(o.get("52WeekHigh"))
    ma200 = num(o.get("200DayMovingAverage"))
    eps  = num(o.get("EPS"))
    opm  = num(o.get("OperatingMarginTTM"))
    grow = num(o.get("QuarterlyRevenueGrowthYOY"))
    roe  = num(o.get("ReturnOnEquityTTM"))
    upside = (tgt / price - 1) * 100

    # quality score — how much of a "good business" case is there?
    q, why = 0, []
    if eps and eps > 0:   q += 2; why.append("profitable")
    if opm and opm > 0:   q += 2; why.append(f"op margin {opm*100:.0f}%")
    if grow and grow > 0: q += 1; why.append(f"rev +{grow*100:.0f}% YoY")
    if roe and roe > 0:   q += 1; why.append(f"ROE {roe*100:.0f}%")
    if rev >= 1e9:        q += 1; why.append("$1B+ revenue")
    if n >= 8:            q += 1; why.append(f"{n} analysts")

    flags = []
    off_high = (price / hi - 1) * 100 if hi else None
    # DATA-INTEGRITY CHECK, added after the first run. AV reports RevenueTTM in
    # the filer's LOCAL currency while labelling Currency "USD", so foreign ADRs
    # arrive with nonsense figures — LOMA showed $855B revenue against a $1.1B
    # market cap (Argentine pesos), SUPV 949x. A price/sales under ~0.12 is not
    # a bargain, it is a unit mismatch, and it silently passes the revenue gate.
    # NOTE this does NOT cleanly separate currency errors from genuinely
    # low-margin, debt-heavy US names (CYH 29x, GT 10x, FUBO 20x are all real),
    # so it FLAGS rather than drops, and says which it suspects.
    if rev and cap and rev / cap > 8:
        flags.append(f"revenue {rev/cap:.0f}x market cap — check reporting "
                     f"currency (foreign ADR?) or very thin margins")
    if hi and price < hi * 0.5 and upside > 100:
        flags.append("STALE-TARGET RISK: <half its 52w high AND >100% upside")
    if ma200 and price < ma200 * 0.75:
        flags.append("well below its 200-day trend")
    if eps is not None and eps < 0:
        flags.append("unprofitable")
    if grow is not None and grow < 0:
        flags.append("revenue shrinking")

    return {
        "ticker": o.get("Symbol"), "name": (o.get("Name") or "")[:36],
        "sector": (o.get("Sector") or "")[:22], "price": price, "target": tgt,
        "upside": upside, "analysts": n, "cap": cap, "rev": rev, "eps": eps,
        "opm": opm, "grow": grow, "roe": roe, "off_high": off_high,
        "quality": q, "why": why, "flags": flags,
    }, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=float, default=1.0)
    ap.add_argument("--max", type=float, default=10.0)
    ap.add_argument("--all", action="store_true", help="include flagged names")
    ap.add_argument("--limit", type=int, default=0, help="cap overview lookups")
    args = ap.parse_args()

    cfg = json.load(open(os.path.join(HERE, "config.json")))
    av = AlphaVantage(cfg)

    print(f"target_screen — ${args.min:.2f} to ${args.max:.2f}, "
          f"$vol>=${MIN_DOLLAR_VOLUME/1e6:.0f}M, cap>=${MIN_MARKET_CAP/1e6:.0f}M, "
          f"rev>=${MIN_REVENUE/1e6:.0f}M, >={MIN_ANALYSTS} analysts\n")

    uni = listing_universe(cfg)
    print(f"  {len(uni)} active common stocks")
    prices = price_universe(av, uni)
    print(f"  {len(prices)} priced")

    cands = sorted(t for t, (p, v) in prices.items()
                   if args.min <= p <= args.max and p * v >= MIN_DOLLAR_VOLUME)
    if args.limit:
        cands = cands[:args.limit]
    print(f"  {len(cands)} in the price + liquidity band — pulling fundamentals "
          f"(~{len(cands)/150:.0f} min, cached 24h)\n")

    rows, rejected = [], {}
    for i, t in enumerate(cands, 1):
        if i % 100 == 0:
            print(f"    ...{i}/{len(cands)}")
        try:
            o = av.company_overview(t)
        except Exception:
            continue
        if not o or not o.get("Symbol"):
            continue
        row, why = assess(o, prices[t][0])
        if row:
            rows.append(row)
        else:
            rejected[why] = rejected.get(why, 0) + 1

    clean = [r for r in rows if not r["flags"]]
    flagged = [r for r in rows if r["flags"]]
    for group, label in ((clean, "PASSED EVERY GATE, NO FLAGS"),
                         (flagged, "PASSED THE GATES BUT CARRY WARNINGS")):
        if not group or (group is flagged and not args.all):
            continue
        group.sort(key=lambda r: (-r["quality"], -r["upside"]))
        print(f"\n{'='*78}\n  {label}  ({len(group)})\n{'='*78}")
        for r in group:
            print(f"\n  {r['ticker']:6} {r['name']}")
            print(f"         ${r['price']:.2f} -> target ${r['target']:.2f}   "
                  f"= {r['upside']:+.0f}%   ({r['analysts']} analysts)")
            print(f"         {r['sector']}  ·  cap ${r['cap']/1e9:.2f}B  ·  "
                  f"rev ${r['rev']/1e9:.2f}B  ·  quality {r['quality']}/8")
            if r["why"]:
                print(f"         + {', '.join(r['why'])}")
            for f in r["flags"]:
                print(f"         ⚠ {f}")

    os.makedirs(SNAP, exist_ok=True)
    out = os.path.join(SNAP, "target_screen_latest.json")
    json.dump({"generated": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
               "band": [args.min, args.max], "clean": clean, "flagged": flagged},
              open(out, "w"), indent=1)
    print(f"\n\n  {len(clean)} clean · {len(flagged)} flagged · wrote {out}")
    print("  top rejection reasons:")
    for why, n in sorted(rejected.items(), key=lambda kv: -kv[1])[:6]:
        print(f"    {n:>4}  {why}")
    print("\n  Reminder: this is a READING LIST. Analyst targets are optimistic\n"
          "  by construction and get revised toward price, not the reverse.")


if __name__ == "__main__":
    main()
