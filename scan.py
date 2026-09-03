"""
scan.py — Milestone 1.
Fetches the live market-wide movers feed, applies the rulebook's
liquidity filter ($20M+ dollar volume, no warrants/units), and prints
what survives. Also saves a timestamped snapshot to snapshots/ —
the first brick of the logging discipline.

Run it:  python3 scan.py
"""

import json
import os
from datetime import datetime

from av_client import AlphaVantage, load_config


def parse_movers(raw, config):
    """Flatten the three lists, compute dollar volume, apply filters."""
    filters = config["filters"]
    min_dv = filters["min_dollar_volume"]
    skip_chars = filters["skip_ticker_symbols_containing"]

    seen = {}
    sections = [
        ("top_gainers", "gainer"),
        ("top_losers", "loser"),
        ("most_actively_traded", "active"),
    ]
    for section, label in sections:
        for row in raw.get(section, []):
            t = row["ticker"]
            if t in seen:
                seen[t]["lists"].append(label)
                continue
            try:
                price = float(row["price"])
                volume = int(row["volume"])
                change_pct = float(row["change_percentage"].rstrip("%"))
            except (ValueError, KeyError):
                continue  # malformed row: rulebook says skip bad data
            seen[t] = {
                "ticker": t,
                "price": price,
                "change_pct": change_pct,
                "volume": volume,
                "dollar_volume": price * volume,
                "lists": [label],
            }

    survivors, junked = [], []
    for m in seen.values():
        if any(c in m["ticker"] for c in skip_chars):
            junked.append((m["ticker"], "warrant/unit symbol"))
        elif m["ticker"].endswith("W") and len(m["ticker"]) == 5:
            junked.append((m["ticker"], "likely warrant"))
        elif m["dollar_volume"] < min_dv:
            junked.append((m["ticker"], f"${m['dollar_volume']:,.0f} traded"))
        else:
            survivors.append(m)

    survivors.sort(key=lambda m: abs(m["change_pct"]), reverse=True)
    return survivors, junked


def main():
    config = load_config()
    av = AlphaVantage(config)

    print("\nFetching live movers from Alpha Vantage...")
    raw = av.top_gainers_losers()
    updated = raw.get("last_updated", "unknown")
    print(f"Feed last updated: {updated}\n")

    survivors, junked = parse_movers(raw, config)

    print(f"{'TICKER':<8}{'PRICE':>10}{'CHANGE':>10}{'$ VOLUME':>15}   LISTS")
    print("-" * 60)
    for m in survivors:
        dv = f"${m['dollar_volume'] / 1e6:,.0f}M"
        print(
            f"{m['ticker']:<8}{m['price']:>10.2f}{m['change_pct']:>9.1f}%"
            f"{dv:>15}   {','.join(m['lists'])}"
        )

    print(f"\n{len(survivors)} tradeable movers.")
    print(f"{len(junked)} junked by the liquidity filter "
          f"(warrants, sub-$20M names).")

    # Save the snapshot — logging starts on day one.
    here = os.path.dirname(os.path.abspath(__file__))
    snap_dir = os.path.join(here, "snapshots")
    os.makedirs(snap_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    snap_path = os.path.join(snap_dir, f"scan_{stamp}.json")
    with open(snap_path, "w") as f:
        json.dump(
            {"feed_last_updated": updated,
             "survivors": survivors,
             "junked": junked},
            f, indent=2,
        )
    print(f"Snapshot saved: snapshots/scan_{stamp}.json")
    print("\nMilestone 1 complete. Next: scoring engine (Milestone 2).")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        raise SystemExit(f"\n{e}\n")
