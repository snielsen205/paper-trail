"""
score.py — Milestone 2: the scoring engine.
Takes the filtered movers (the candidate pool) and interrogates each one:
  - News sentiment: is there a fresh, relevant catalyst, and which way?
  - Options flow: realtime put/call ratio — where is the options crowd?
  - ATR: how much does this thing normally move (feeds stop preview)?
Then produces a 0-100 score, a LONG/SHORT/WATCH call, and the top 10.

Rank is by SIGNAL CLARITY, not by size of move. A +77% name with no
catalyst and conflicting flow ranks below a -2% name where everything
agrees. WATCH = no confident direction = score capped, never tradeable.

Run it:  python3 score.py
"""

import csv
import io
import json
import os
from datetime import datetime, timedelta, timezone

from av_client import AlphaVantage, load_config
from scan import parse_movers

MAX_CANDIDATES = 25          # safety valve on API spend per run
WATCH_SCORE_CAP = 65         # a WATCH can never look like a setup


# ---------------- signal fetchers (each fails soft) ----------------

def get_news_signal(av, ticker):
    """Returns (catalyst_points 0-45, direction -1/0/+1, note)."""
    try:
        data = av.news_sentiment(ticker)
        feed = data.get("feed", [])
    except Exception:
        return 0, 0, "no news data"

    # Alpha Vantage stamps time_published in UTC. Comparing it against a naive
    # datetime.now() (local ET) made every article look 4-5 hours YOUNGER than
    # it was, quietly stretching the 36h window to ~40h. Proof it was wrong:
    # NVDA's newest article computed to an age of MINUS 2.3 hours. Both sides
    # are UTC-aware now.
    now = datetime.now(timezone.utc)
    fresh, sentiments = 0, []
    for item in feed:
        try:
            ts = datetime.strptime(item["time_published"][:13],
                                   "%Y%m%dT%H%M").replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            continue
        if now - ts > timedelta(hours=36):
            continue
        for s in item.get("ticker_sentiment", []):
            if s.get("ticker") == ticker:
                rel = float(s.get("relevance_score", 0))
                if rel >= 0.35:              # article is actually ABOUT this name
                    fresh += 1
                    sentiments.append(float(s.get("ticker_sentiment_score", 0)))

    if fresh == 0:
        return 0, 0, "no fresh relevant news"

    avg = sum(sentiments) / len(sentiments)
    # Volume of coverage (up to 25 pts) + strength of tone (up to 20 pts)
    points = min(fresh * 6, 25) + min(abs(avg) * 60, 20)
    direction = 1 if avg > 0.12 else (-1 if avg < -0.12 else 0)
    note = f"{fresh} fresh articles, avg sentiment {avg:+.2f}"
    return round(points), direction, note


def get_options_signal(av, ticker):
    """Returns (direction -1/0/+1, ratio or None, note)."""
    try:
        data = av.put_call_ratio(ticker)
        ratio = float(data["put_call_ratio_full_chain"])
    except Exception:
        return 0, None, "no options data"
    if ratio <= 0.65:
        return 1, ratio, f"put/call {ratio:.2f} (bullish flow)"
    if ratio >= 1.05:
        return -1, ratio, f"put/call {ratio:.2f} (bearish flow)"
    return 0, ratio, f"put/call {ratio:.2f} (neutral)"


def get_atr(av, ticker):
    """Latest daily ATR value, or None."""
    try:
        data = av.atr(ticker)
        series = data.get("Technical Analysis: ATR", {})
        latest = max(series.keys())
        return float(series[latest]["ATR"])
    except Exception:
        return None


def build_earnings_map(av):
    """Fetch the market-wide earnings calendar once and reduce it to
    {ticker -> nearest upcoming report date}. Fails soft to an empty map
    so a calendar outage never blocks a scan."""
    try:
        text = av.earnings_calendar(horizon="3month")
    except Exception:
        return {}
    mapping = {}
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        sym = (row.get("symbol") or "").strip()
        raw_date = (row.get("reportDate") or "").strip()
        if not sym or not raw_date:
            continue
        try:
            d = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            continue
        # Keep the soonest upcoming date if a symbol appears more than once.
        if sym not in mapping or d < mapping[sym]:
            mapping[sym] = d
    return mapping


def get_earnings_signal(ticker, earnings_map, config):
    """Returns (catalyst_points, note). Earnings is a *catalyst-presence*
    signal — a scheduled reason to move — not a direction (a report can beat
    or miss). Imminent earnings adds confidence; nothing otherwise."""
    scfg = config.get("scoring", {})
    window = scfg.get("earnings_catalyst_days", 7)
    pts = scfg.get("earnings_catalyst_points", 10)
    report_date = earnings_map.get(ticker)
    if not report_date:
        return 0, "no upcoming earnings"
    days = (report_date - datetime.now().date()).days
    if 0 <= days <= window:
        when = "today" if days == 0 else f"in {days}d"
        return pts, f"earnings {when} ({report_date}) - catalyst"
    return 0, f"next earnings {report_date} (outside {window}d window)"


# ---------------- scoring ----------------

def liquidity_points(dollar_volume):
    if dollar_volume >= 500e6:
        return 20
    if dollar_volume >= 100e6:
        return 15
    return 10                               # floor already guaranteed $20M+


def score_candidate(av, m, config, earnings_map):
    news_pts, news_dir, news_note = get_news_signal(av, m["ticker"])
    opt_dir, opt_ratio, opt_note = get_options_signal(av, m["ticker"])
    earn_pts, earn_note = get_earnings_signal(m["ticker"], earnings_map, config)
    atr = get_atr(av, m["ticker"])

    # Direction: news and options must not contradict each other.
    dirs = [d for d in (news_dir, opt_dir) if d != 0]
    if not dirs:
        direction, dir_pts = "WATCH", 0
    elif all(d == 1 for d in dirs):
        direction = "LONG"
        dir_pts = 20 + (15 if len(dirs) == 2 else 0)   # both agreeing beats one
    elif all(d == -1 for d in dirs):
        direction = "SHORT"
        dir_pts = 20 + (15 if len(dirs) == 2 else 0)
    else:
        direction, dir_pts = "WATCH", 0                # signals conflict

    # Volatility ceiling (M3): a name whose ATR is a huge fraction of its
    # price can't be bracketed sanely — the 1.75xATR stop lands near zero and
    # the target is a fantasy. Force it to WATCH so the bot never touches it.
    ceiling = config.get("scoring", {}).get("max_atr_pct_of_price", 0.18)
    atr_pct = (atr / m["price"]) if (atr and m["price"]) else None
    too_volatile = atr_pct is not None and atr_pct > ceiling
    if too_volatile:
        direction, dir_pts = "WATCH", 0

    score = news_pts + earn_pts + dir_pts + liquidity_points(m["dollar_volume"])
    score = min(score, 100)                            # keep the scale honest
    if direction == "WATCH":
        score = min(score, WATCH_SCORE_CAP)

    # Rulebook stop/target preview (bridges to the bot later). Not computed
    # for WATCH names — including volatility-capped ones, on purpose.
    stop = target = None
    if atr and direction in ("LONG", "SHORT"):
        dials = config["bot_dials"]
        risk = dials["atr_stop_multiple"] * atr
        sign = 1 if direction == "LONG" else -1
        stop = m["price"] - sign * risk
        target = m["price"] + sign * risk * dials["reward_to_risk"]

    why = [news_note, opt_note]
    if earn_pts > 0:
        why.append(earn_note)
    if news_dir != 0 and opt_dir != 0 and news_dir != opt_dir:
        why.append("CONFLICT: news and options disagree -> no call")
    if too_volatile:
        why.append(
            f"TOO VOLATILE: ATR {atr_pct * 100:.0f}% of price "
            f"(ceiling {ceiling * 100:.0f}%) -> not tradeable"
        )
    return {
        **m,
        "score": int(score),
        "direction": direction,
        "atr": atr,
        "atr_pct_of_price": round(atr_pct, 4) if atr_pct is not None else None,
        "stop": stop,
        "target": target,
        "why": " | ".join(why),
    }


def main():
    config = load_config()
    av = AlphaVantage(config)

    print("\nStep 1: fetching candidate pool (live movers + junk filter)...")
    raw = av.top_gainers_losers()
    survivors, junked = parse_movers(raw, config)
    pool = survivors[:MAX_CANDIDATES]
    print(f"  {len(pool)} candidates ({len(junked)} junked). "
          f"Feed updated {raw.get('last_updated', '?')}")

    print("\nStep 2: loading the earnings calendar (one cached call)...")
    earnings_map = build_earnings_map(av)
    print(f"  {len(earnings_map)} companies with upcoming report dates."
          if earnings_map else "  (calendar unavailable - scoring without it)")

    print(f"\nStep 3: scoring each candidate "
          f"(~3 API calls each, rate-limited)...")
    scored = []
    for i, m in enumerate(pool, 1):
        print(f"  [{i}/{len(pool)}] {m['ticker']}...")
        scored.append(score_candidate(av, m, config, earnings_map))

    scored.sort(key=lambda x: x["score"], reverse=True)
    top10 = scored[:10]
    floor = config["bot_dials"]["score_floor"]

    print(f"\n{'#':<3}{'TICKER':<8}{'DIR':<7}{'SCORE':>5}"
          f"{'PRICE':>10}{'CHANGE':>9}   STOP/TARGET (rulebook preview)")
    print("-" * 78)
    for i, s in enumerate(top10, 1):
        st = (f"{s['stop']:.2f} / {s['target']:.2f}"
              if s["stop"] else "--")
        flag = " <- bot-eligible" if (
            s["score"] >= floor and s["direction"] != "WATCH") else ""
        print(f"{i:<3}{s['ticker']:<8}{s['direction']:<7}{s['score']:>5}"
              f"{s['price']:>10.2f}{s['change_pct']:>8.1f}%   {st}{flag}")
        print(f"   why: {s['why']}")

    eligible = [s for s in top10
                if s["score"] >= floor and s["direction"] != "WATCH"]
    print(f"\n{len(eligible)} name(s) at or above the {floor} floor with a "
          f"direction -> these are what the bot would trade.")

    here = os.path.dirname(os.path.abspath(__file__))
    snap_dir = os.path.join(here, "snapshots")
    os.makedirs(snap_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    path = os.path.join(snap_dir, f"top10_{stamp}.json")
    with open(path, "w") as f:
        json.dump(top10, f, indent=2)
    print(f"Top-10 snapshot saved: snapshots/top10_{stamp}.json")
    print("\nTo log today's picks for grading:  python3 track_record.py log")


if __name__ == "__main__":
    main()
