"""
quality_screen.py — "quality on sale": surface GOOD companies trading near their
52-week low, and flag the ones that are cheap for a BAD reason (value traps).

It is a RESEARCH SCREEN, not a trader. It places no orders and recommends
nothing. It surfaces candidates with the hard numbers that are the actual
reasons to look closer — so you research them yourself. Read the metrics, not
the label.

THE WHOLE POINT (per the lab): a 52-week low is meaningless on its own; what
matters is WHY it's down. A great business marked down for a temporary reason is
a sale. A deteriorating one is a value trap — cheap because it should be. This
screen separates them with fundamentals, not vibes:

  ON SALE   — near its 52-week low AND profitable (ROE/margins) AND not
              egregiously overvalued (P/E). A good business, marked down.
  TRAP-RISK — near a low but WEAK fundamentals (no earnings, thin/negative
              margins, low ROE). Cheap for a reason. NOT a candidate.
  PRICEY    — good business but not cheap / not near a low. Watchlist.

Universe: quality_universe.txt (editable; deliberately NOT the S&P 500).
Data: Alpha Vantage COMPANY_OVERVIEW (fundamentals + 52wk range, cached 24h) +
one bulk-quotes call for current prices. Reuses the movers app's av_client.

HONEST CAVEAT: quality + value is a real, evidence-backed factor (unlike the
technical indicators we killed in the lab). But it is SLOW and regime-dependent
— value can lag growth for years — and nothing here is a proven edge until we
backtest it. This finds candidates to research; it does not tell you to buy.

Run:  python3 quality_screen.py
"""

import os
import sys

from av_client import AlphaVantage, load_config

HERE = os.path.dirname(os.path.abspath(__file__))

# --- dials (edit freely; these are loose sanity gates, not a strategy) ---
NEAR_LOW_MAX_PCT = 25.0   # "near the low" = within the bottom 25% of its 52wk range
ROE_MIN = 0.12            # quality: return on equity >= 12%
MARGIN_MIN = 0.0          # quality: positive profit margin (must actually make money)
PE_MAX = 35.0             # value sanity: not egregiously expensive (P/E)


def num(x):
    """AV returns fundamentals as strings; missing shows as 'None'/'-'/''."""
    if x in (None, "None", "-", ""):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load_universe():
    path = os.path.join(HERE, "quality_universe.txt")
    if not os.path.exists(path):
        sys.exit("No quality_universe.txt — create it (one ticker per line).")
    out = []
    for line in open(path):
        t = line.split("#")[0].strip().upper()
        if t:
            out.append(t)
    return out


def prices_for(av, tickers):
    """Current price per symbol via bulk quotes (100/call)."""
    px = {}
    for i in range(0, len(tickers), 100):
        batch = tickers[i:i + 100]
        for row in av.bulk_quotes(batch).get("data", []):
            p = num(row.get("close"))
            if row.get("symbol") and p:
                px[row["symbol"]] = p
    return px


def assess(t, price, ov):
    """Return a row dict with the metrics + a verdict, or None if unusable."""
    low, high = num(ov.get("52WeekLow")), num(ov.get("52WeekHigh"))
    roe = num(ov.get("ReturnOnEquityTTM"))
    pm = num(ov.get("ProfitMargin"))
    pe = num(ov.get("PERatio"))
    pb = num(ov.get("PriceToBookRatio"))
    peg = num(ov.get("PEGRatio"))
    if price is None or low is None or high is None or high <= low:
        return None
    range_pos = (price - low) / (high - low) * 100          # 0 = at low, 100 = at high
    near_low = range_pos <= NEAR_LOW_MAX_PCT
    has_fund = roe is not None and pe is not None
    # quality = actually profitable, decent returns on equity
    quality_ok = (has_fund and roe >= ROE_MIN and pe > 0
                  and (pm is None or pm > MARGIN_MIN))
    value_ok = pe is not None and 0 < pe <= PE_MAX

    if not has_fund:
        verdict = "NO-DATA"
    elif near_low and quality_ok and value_ok:
        verdict = "ON-SALE"
    elif near_low and not quality_ok:
        verdict = "TRAP-RISK"
    elif near_low and quality_ok and not value_ok:
        verdict = "PRICEY"
    elif quality_ok:
        verdict = "QUALITY"          # good business, not near a low
    else:
        verdict = "—"
    return {
        "ticker": t, "name": (ov.get("Name") or "")[:26],
        "sector": (ov.get("Sector") or "").title()[:12],
        "price": price, "range_pos": range_pos,
        "off_low": (price / low - 1) * 100,
        "mcap_b": (num(ov.get("MarketCapitalization")) or 0) / 1e9,
        "roe": roe, "pm": pm, "pe": pe, "pb": pb, "peg": peg,
        "verdict": verdict,
    }


def fmt_pct(x):
    return f"{x*100:>5.0f}%" if x is not None else "   -- "


def fmt(x, w=6):
    return f"{x:>{w}.1f}" if x is not None else " " * (w - 2) + "--"


def line(r):
    return (f"  {r['ticker']:<5} {r['name']:<26} {r['sector']:<12}"
            f" ${r['price']:>7.2f} {r['range_pos']:>4.0f}%low "
            f" ROE{fmt_pct(r['roe'])} mgn{fmt_pct(r['pm'])}"
            f" P/E{fmt(r['pe'],5)} P/B{fmt(r['pb'],5)} PEG{fmt(r['peg'],4)}")


def main():
    cfg = load_config()
    av = AlphaVantage(cfg)
    universe = load_universe()
    print("=" * 96)
    print(f"  QUALITY ON SALE — {len(universe)} names (research screen; places no orders, recommends nothing)")
    print(f"  gates: near-low <= {NEAR_LOW_MAX_PCT:.0f}% up its 52wk range | ROE >= {ROE_MIN*100:.0f}% | positive margin | P/E <= {PE_MAX:.0f}")
    print("=" * 96)
    print("  fetching prices + fundamentals (first run pulls each overview; cached 24h after) ...", flush=True)

    px = prices_for(av, universe)
    rows = []
    for t in universe:
        try:
            ov = av.company_overview(t)
        except RuntimeError:
            continue
        if not ov or not ov.get("Symbol"):
            continue
        r = assess(t, px.get(t), ov)
        if r:
            rows.append(r)

    onsale = sorted([r for r in rows if r["verdict"] == "ON-SALE"],
                    key=lambda r: (-(r["roe"] or 0), r["range_pos"]))
    traps = sorted([r for r in rows if r["verdict"] == "TRAP-RISK"],
                   key=lambda r: r["range_pos"])
    pricey = [r for r in rows if r["verdict"] == "PRICEY"]

    print(f"\n  ON SALE — good businesses trading near their 52-wk low ({len(onsale)}). "
          "Best ROE first; research these, don't just buy them.")
    if onsale:
        for r in onsale:
            print(line(r))
    else:
        print("    (none right now — quality names aren't near their lows in this universe)")

    if traps:
        print(f"\n  TRAP-RISK — near a low but weak fundamentals ({len(traps)}). "
              "Cheap for a reason; NOT candidates.")
        for r in traps:
            print(line(r))

    print(f"\n  ({len(pricey)} good businesses not near a low / not cheap — watchlist. "
          f"{sum(1 for r in rows if r['verdict']=='NO-DATA')} had no usable fundamentals.)")
    print("\n  Reminder: quality+value is real but SLOW and unproven here until we backtest it.")
    print("  This is a list to research — not a buy list.")
    print("=" * 96 + "\n")


if __name__ == "__main__":
    main()
