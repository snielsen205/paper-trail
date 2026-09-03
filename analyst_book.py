"""
analyst_book.py — the shadow analyst's SIMULATED BOOK. Agent #1 finally gets to
"trade," but only on paper-of-the-paper: this file places NO orders, touches NO
broker, and never imports alpaca_client. It turns the analyst's opinions into an
actual equity curve so we can see what its judgment would have EARNED, not just
how often it was directionally right.

Why this exists: `analyst.py grade` answers "did the call beat SPY at +5 days?"
— one bit per call. It can't tell you whether the wins were big and the losses
small, what the drawdown felt like, or whether sizing and a stop change the
answer. A book answers all three, and it starts accruing that record today
instead of waiting for enough graded calls to mean anything.

  python3 analyst_book.py run      rebuild the book from the verdict ledger
  python3 analyst_book.py report   print the book's performance

LONG *AND* SHORT (owner's call, 2026-07-29). BULLISH verdicts go long, BEARISH
verdicts go short. This is deliberately NOT what the momentum sleeve does — that
one is long-only because its long/short cousin was stress-tested in the lab and
rejected (t=1.09 gross, borrow fees ate it, hard-to-borrow names flipped it
negative; see momentum_sleeve.py). The difference is that this book risks
nothing: it exists precisely to find out whether the analyst's bearish calls —
which are currently ALL of its conviction, 12 BEARISH / 0 BULLISH as of
2026-07-29 — are worth anything before that question ever reaches an order.

WHAT A SIMULATED SHORT CANNOT TELL YOU (read this before believing a good
number). The sim charges borrow and honors gaps, but three things stay outside
it, and all three cut the same way — against the short:
  1. AVAILABILITY. `min_short_price` (default $5) throws out the outright
     fiction — sub-$5 movers, where borrow dries up, and sub-$1 names that
     cannot be located at any price. But above the floor it still ASSUMES every
     name was shortable, and a mover that just spiked 40% is precisely the one
     that goes hard-to-borrow that morning. A short the book "took" may not have
     been takeable.
  2. BORROW IS A GUESS. borrow_cost_annual_pct is a flat dial (default 15%/yr,
     accrued per trading day held). Genuinely hard-to-borrow movers run far
     higher — 50-200%/yr happens. If the book's shorts only work at 15%, they
     don't work.
  3. SQUEEZE TAIL. A close-based stop can't cap a name that runs 200% overnight;
     the book exits at the real bad close (it does NOT pretend to fill at the
     stop price), but daily bars still miss the worst of an intraday squeeze.
  Net: treat a profitable short book as an upper bound, not an estimate.

HOW IT FILLS (and why it can't flatter itself):
  * Entry is the verdict day's ADJUSTED CLOSE — not the 9:32 screen price the
    analyst saw. The screen price would be an intraday fill we never tested, and
    assuming it would quietly hand the book a few free percent. Close-to-close
    also makes the book agree with `analyst.py grade`, which measures the same
    window the same way.
  * Exit is the close on the hold horizon (default 5 trading days = the grading
    horizon), or the first close through the stop, whichever comes first.
  * The stop is CLOSE-based, because daily bars are all this book gets. A real
    intraday stop would have exited earlier and usually worse. Mildly optimistic
    on the long side; on the short side the gap-through behavior above is what
    keeps it honest.

REBUILT FROM SCRATCH EVERY RUN. The book is a pure function of the verdict
ledger plus price history, so there is no state file to drift or double-count.
Re-run it as often as you like; the 2026-07-22 momentum stacking incident
(re-runs piling up duplicate orders) is structurally impossible here.

All dials live in config.json -> "analyst_book" (rulebook: no hardcoded numbers).
"""

import glob
import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from av_client import load_config, AlphaVantage

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP = os.path.join(HERE, "snapshots")
VERDICTS = os.path.join(SNAP, "analyst_log.jsonl")
BOOK = os.path.join(SNAP, "fund_book.jsonl")
ACCOUNT = os.path.join(SNAP, "fund_account.json")
ET = ZoneInfo("America/New_York")

DEFAULTS = {
    "starting_equity": 100000.0,
    "position_pct": 10.0,
    "max_positions": 5,
    "min_confidence": 60,
    "hold_trading_days": 5,
    "stop_pct": 15.0,
    "max_loss_pct": None,
    "allow_short": True,
    "borrow_cost_annual_pct": 15.0,
    "min_short_price": 5.0,
    "benchmark": "SPY",
    "fill_model": "intraday",
    "min_bar_coverage_pct": 25.0,
}
TRADING_DAYS_PER_YEAR = 252
MINUTES_PER_SESSION = 390          # 9:30-16:00 ET
ENTRY_ET = (9, 45)                 # rulebook §2 — same minute bot.py works
WALKAWAY_ET = (10, 5)              # bot.py's hard deadline; unfilled after this
CACHE_DIR = os.path.join(HERE, "cache", "intraday")


def dials(cfg):
    d = dict(DEFAULTS)
    d.update({k: v for k, v in (cfg.get("analyst_book") or {}).items()
              if not k.startswith("_")})
    return d


def load_verdicts():
    if not os.path.exists(VERDICTS):
        return []
    out = []
    with open(VERDICTS) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


# ---------------- price history ----------------

def series(av, ticker):
    """{date: adjusted_close} for one symbol, or None if AV can't serve it.
    Fail-soft on purpose: one dead ticker must not kill the whole rebuild."""
    try:
        raw = av.daily_adjusted(ticker).get("Time Series (Daily)", {})
    except (RuntimeError, KeyError, ValueError):
        return None
    out = {}
    for day, bar in raw.items():
        try:
            out[day] = float(bar["5. adjusted close"])
        except (KeyError, ValueError):
            pass
    return out or None


# ---------------- position math ----------------
# A LONG spends `cost` cash and is worth shares*price.
# A SHORT reserves `cost` as margin; its value is that margin plus the gain
# (entry - price) * shares, minus borrow accrued so far. Both therefore have a
# single "value" the equity curve can sum, and return_pct is return on the
# capital the book committed either way.

def borrow_accrued(p, days, rate_annual):
    if p["side"] != "SHORT" or days <= 0:
        return 0.0
    notional = p["shares"] * p["entry_price"]
    return notional * (rate_annual / 100.0) * (days / TRADING_DAYS_PER_YEAR)


def position_value(p, px, days, rate_annual):
    if p["side"] == "LONG":
        return p["shares"] * px
    gain = (p["entry_price"] - px) * p["shares"]
    return p["cost"] + gain - borrow_accrued(p, days, rate_annual)


def hit_stop(p, px, stop_pct):
    if p["side"] == "LONG":
        return px <= p["entry_price"] * (1 - stop_pct / 100.0)
    return px >= p["entry_price"] * (1 + stop_pct / 100.0)


# ---------------- intraday fill model ----------------
# Mirrors what bot.py actually does, instead of assuming a close-to-close fill:
#   * entry is a MARKETABLE LIMIT at 9:45 ET, padded entry_limit_pad_pct above
#     the ask / below the bid (rulebook §2). Minute bars carry no quotes, so the
#     9:45 bar's close stands in for the touch price — the same fill≈limit
#     approximation bot.py documents for its own brackets.
#   * protection is an ATR BRACKET off the frozen bot_dials (atr_stop_multiple,
#     reward_to_risk), using the ATR the screener already computed that morning.
#   * the position is then walked MINUTE BY MINUTE until a leg triggers or the
#     time stop lands — so a stop that would have fired intraday on day 2 fires
#     on day 2, instead of being invisible until a daily close.
#
# THE DATA LIMIT THAT SHAPES ALL OF THIS: the free Alpaca feed is IEX only,
# which sees a single venue's slice of the tape. Liquid names come back with a
# full 390 bars/day; thin movers come back with a handful (JEM 6, AMIX 3, DFNS 2
# out of 31 minutes when probed on 2026-07-24). Missing minutes are the minutes
# in which a stop might have printed — so sparse data does not add noise, it
# systematically HIDES stop-outs and flatters the book. Names under
# min_bar_coverage_pct are therefore EXCLUDED, not filled on partial data.


def et_naive(ts):
    """Alpaca bar timestamps are RFC-3339 UTC; return naive ET datetime."""
    return (datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
            .replace(tzinfo=ZoneInfo("UTC")).astimezone(ET).replace(tzinfo=None))


def minute_bars(api, sym, start_day, end_day):
    """Minute bars for one symbol over a date range, cached per symbol+range.
    Past sessions never change, so a rebuild re-reads the cache instead of
    re-billing the API — this is what keeps the nightly rebuild cheap."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{sym}_{start_day}_{end_day}.json")
    today = datetime.now(ET).strftime("%Y-%m-%d")
    if os.path.exists(path) and end_day < today:
        try:
            cached = json.load(open(path))
            # JSON has no tuple type, so "hm" round-trips as a LIST. Every
            # downstream comparison is against tuples (ENTRY_ET/WALKAWAY_ET),
            # and tuple <= list raises TypeError — so normalize on the way in.
            for r in cached:
                if isinstance(r.get("hm"), list):
                    r["hm"] = tuple(r["hm"])
            return cached
        except (json.JSONDecodeError, OSError):
            pass
    start = f"{start_day}T13:00:00Z"
    end = (datetime.strptime(end_day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%dT01:00:00Z")
    try:
        bars = api.bars(sym, "1Min", start, end)
    except Exception:
        return []
    rows = []
    for b in bars:
        try:
            dt = et_naive(b["t"])
        except (KeyError, ValueError):
            continue
        if not (9, 30) <= (dt.hour, dt.minute) <= (16, 0):
            continue           # regular hours only, like the bot
        rows.append({"day": dt.strftime("%Y-%m-%d"), "hm": (dt.hour, dt.minute),
                     "o": b.get("o"), "h": b.get("h"), "l": b.get("l"), "c": b.get("c")})
    if end_day < today:
        try:
            json.dump(rows, open(path, "w"))
        except OSError:
            pass
    return rows


def load_atr_map():
    """{(date, ticker): atr} from the morning top-10 snapshots. The bracket has
    to use the SAME ATR the screener saw that morning, not one recomputed later
    — otherwise the sim is bracketing on information the bot never had."""
    out = {}
    for path in glob.glob(os.path.join(SNAP, "top10_*.json")):
        day = os.path.basename(path).split("_")[1]
        try:
            for p in json.load(open(path)):
                if p.get("atr"):
                    out[(day, p["ticker"])] = float(p["atr"])
        except (json.JSONDecodeError, OSError, KeyError, ValueError):
            continue
    return out


def resolve_intraday(bars, day, side, atr, bd, hold_days, coverage_floor,
                     max_atr_pct, max_loss_pct=None):
    """Fill one verdict the way bot.py would, then walk the bracket.

    Returns a dict describing the outcome, or {"skip": reason}. Self-contained:
    a position's bracket resolves off its own tape, independent of the book's
    other positions, so this can run before any capital bookkeeping."""
    if not bars:
        return {"skip": "no intraday data (IEX)"}

    days = sorted({b["day"] for b in bars if b["day"] >= day})[:hold_days + 1]
    if not days or days[0] != day:
        return {"skip": "no intraday data on the verdict date"}

    # Coverage gate — see the note above. Judge it on the ENTRY session, the one
    # that decides whether this name is really tradeable on this feed.
    entry_day_bars = [b for b in bars if b["day"] == day]
    coverage = len(entry_day_bars) / MINUTES_PER_SESSION * 100
    if coverage < coverage_floor:
        return {"skip": f"IEX coverage {coverage:.0f}% < {coverage_floor:.0f}% "
                        f"({len(entry_day_bars)}/{MINUTES_PER_SESSION} min) — "
                        f"too thin to see a stop"}

    # --- entry: marketable limit at 9:45, walking away at 10:05 (bot.py) ---
    window = [b for b in entry_day_bars if ENTRY_ET <= b["hm"] <= WALKAWAY_ET]
    if not window:
        return {"skip": "no bar in the 9:45-10:05 entry window — unfilled"}
    ref = window[0]["c"]
    if not ref or ref <= 0:
        return {"skip": "no usable price at 9:45"}
    pad = bd.get("entry_limit_pad_pct", 0.1) / 100.0
    entry = ref * (1 + pad) if side == "LONG" else ref * (1 - pad)

    # VOLATILITY CEILING (scoring.max_atr_pct_of_price, M3). The screener forces
    # any name whose ATR exceeds this share of price to WATCH — it is never
    # bot-eligible, because an ATR bracket on it is nonsense. A bot-faithful
    # book has to honour the same rule: without it, CAPR (ATR 37% of a $6.59
    # price) got a SHORT target at -$1.86, a price that cannot be reached.
    if entry > 0 and atr / entry > max_atr_pct:
        return {"skip": f"TOO VOLATILE: ATR {atr / entry * 100:.0f}% of price "
                        f"> {max_atr_pct * 100:.0f}% ceiling — never bot-eligible"}

    # --- bracket, off the frozen movers dials ---
    stop_dist = atr * bd.get("atr_stop_multiple", 1.75)
    rr = bd.get("reward_to_risk", 2.0)
    # TARGET is always computed off the FULL ATR distance, before any loss cap.
    # The cap is a risk rule, not a new strategy: tightening the stop must not
    # quietly drag the profit target in with it, or we'd be measuring a
    # different system and couldn't compare the result to the uncapped book.
    tgt_dist = stop_dist * rr
    # LOSS CAP (analyst_book.max_loss_pct). Owner's 2026-08-10 call: no single
    # trade may be designed to lose more than this share of its cost. Applied as
    # whichever stop is TIGHTER — if the ATR stop is already inside the cap it
    # governs and nothing changes. This is a cap, not a replacement.
    if max_loss_pct:
        stop_dist = min(stop_dist, entry * float(max_loss_pct) / 100.0)
    if side == "LONG":
        stop, target = entry - stop_dist, entry + tgt_dist
    else:
        stop, target = entry + stop_dist, entry - tgt_dist
    if stop <= 0 or target <= 0:
        return {"skip": "ATR bracket puts a leg at or below zero — unreachable"}

    # --- walk the tape ---
    entry_hm = window[0]["hm"]
    for b in bars:
        if b["day"] not in days:
            continue
        if b["day"] == day and b["hm"] <= entry_hm:
            continue
        hi, lo, op = b.get("h"), b.get("l"), b.get("o")
        if hi is None or lo is None:
            continue
        if side == "LONG":
            hit_stop_now, hit_target_now = lo <= stop, hi >= target
        else:
            hit_stop_now, hit_target_now = hi >= stop, lo <= target
        # Both legs inside one minute: assume the STOP filled first. We can't
        # see the path within a bar, and crediting the target would be the
        # single easiest way to manufacture a win that never happened.
        if hit_stop_now:
            # A stop order becomes a market order: it fills AT the stop, or at
            # the open if the bar gapped straight through it.
            px = op if (op is not None and
                        ((side == "LONG" and op < stop) or
                         (side == "SHORT" and op > stop))) else stop
            return {"entry": entry, "stop": stop, "target": target,
                    "exit": px, "exit_day": b["day"], "exit_hm": b["hm"],
                    "reason": "stop", "coverage": coverage}
        if hit_target_now:
            return {"entry": entry, "stop": stop, "target": target,
                    "exit": target, "exit_day": b["day"], "exit_hm": b["hm"],
                    "reason": "target", "coverage": coverage}

    # --- neither leg fired: time stop at the next open (rulebook §7) ---
    if len(days) > hold_days:
        last_day = days[hold_days]
        opens = [b for b in bars if b["day"] == last_day]
        if opens:
            return {"entry": entry, "stop": stop, "target": target,
                    "exit": opens[0]["o"] or opens[0]["c"], "exit_day": last_day,
                    "exit_hm": opens[0]["hm"], "reason": "time stop",
                    "coverage": coverage}
    live = [b for b in bars if b["day"] == days[-1]]
    return {"entry": entry, "stop": stop, "target": target, "exit": None,
            "last": (live[-1]["c"] if live else ref), "last_day": days[-1],
            "reason": None, "coverage": coverage}


# ---------------- the simulation ----------------

def select_actionable(d, verdicts):
    """Which verdicts are calls the book may act on. Shared by both fill
    models so they can never disagree about WHAT was traded — only about how
    it filled."""
    actionable, skipped = [], []
    for v in verdicts:
        if v.get("verdict") == "BULLISH":
            side = "LONG"
        elif v.get("verdict") == "BEARISH":
            if not d["allow_short"]:
                continue
            side = "SHORT"
        else:
            continue              # NEUTRAL is not a call
        if (v.get("confidence") or 0) < d["min_confidence"]:
            skipped.append({"date": v["date"], "ticker": v["ticker"], "side": side,
                            "reason": f"confidence {v.get('confidence')} < {d['min_confidence']}"})
            continue
        actionable.append({"date": v["date"], "ticker": v["ticker"], "side": side,
                           "confidence": v.get("confidence"), "brain": v.get("brain"),
                           "thesis": v.get("thesis", "")})
    return actionable, skipped


def build_book_intraday(cfg, verdicts):
    """The bot-faithful book: 9:45 marketable limit + ATR bracket, walked minute
    by minute. Same signature and same outputs as build_book, so everything
    downstream (stats, snapshot, dashboard) is unchanged."""
    from alpaca_client import Alpaca      # imported HERE, and only for READ-ONLY
    d = dials(cfg)                        # bar data. This file submits nothing.
    bd = cfg.get("bot_dials", {})
    max_atr_pct = cfg.get("scoring", {}).get("max_atr_pct_of_price", 0.18)
    av = AlphaVantage(cfg)
    api = Alpaca(cfg)
    rate = d["borrow_cost_annual_pct"]
    hold = d["hold_trading_days"]
    today = datetime.now(ET).strftime("%Y-%m-%d")

    bench = series(av, d["benchmark"])
    if not bench:
        raise RuntimeError(f"no {d['benchmark']} price history — cannot build the book")

    actionable, skipped = select_actionable(d, verdicts)
    if not verdicts:
        return [], [], skipped
    start = min(v["date"] for v in verdicts)
    calendar = sorted(day for day in bench if start <= day <= today)
    if not calendar:
        return [], [], skipped

    atr_map = load_atr_map()

    # 1) Resolve every candidate against its own tape, before any bookkeeping.
    plans = {}
    for a in actionable:
        key = (a["date"], a["ticker"])
        atr = atr_map.get(key)
        if not atr:
            skipped.append({**{k: a[k] for k in ("date", "ticker", "side")},
                            "reason": "no ATR in that morning's top-10 snapshot"})
            continue
        idx = calendar.index(a["date"]) if a["date"] in calendar else None
        if idx is None:
            skipped.append({**{k: a[k] for k in ("date", "ticker", "side")},
                            "reason": "verdict date is not a trading day"})
            continue
        end_day = calendar[min(idx + hold, len(calendar) - 1)]
        bars = minute_bars(api, a["ticker"], a["date"], end_day)
        r = resolve_intraday(bars, a["date"], a["side"], atr, bd, hold,
                             d["min_bar_coverage_pct"], max_atr_pct,
                             d.get("max_loss_pct"))
        if "skip" in r:
            skipped.append({**{k: a[k] for k in ("date", "ticker", "side")},
                            "reason": r["skip"]})
            continue
        plans[key] = (a, r)

    # 2) Capital + slot bookkeeping, day by day (exits before entries).
    cash = float(d["starting_equity"])
    open_pos, trades, curve = [], [], []
    by_day = {}
    for (day, _t), (a, r) in plans.items():
        by_day.setdefault(day, []).append((a, r))

    def day_index(day):
        return calendar.index(day) if day in calendar else len(calendar) - 1

    def last_close(p, day):
        return p["marks"].get(day, p["last_price"])

    for i, day in enumerate(calendar):
        still_open = []
        for p in open_pos:
            r = p["_plan"]
            if r["exit"] is not None and r["exit_day"] <= day:
                held = day_index(r["exit_day"]) - p["open_index"]
                borrow = round(borrow_accrued(p, held, rate), 2)
                value = position_value(p, r["exit"], held, rate)
                cash += value
                p["exit_date"] = r["exit_day"]
                p["exit_time"] = f"{r['exit_hm'][0]:02d}:{r['exit_hm'][1]:02d}"
                p["exit_price"] = round(r["exit"], 4)
                p["exit_reason"] = r["reason"]
                p["borrow_cost"] = borrow
                p["pnl"] = round(value - p["cost"], 2)
                p["return_pct"] = round(p["pnl"] / p["cost"] * 100, 2)
                p["price_return_pct"] = round((r["exit"] / p["entry_price"] - 1) * 100, 2)
                b0, b1 = bench.get(p["open_date"]), bench.get(r["exit_day"])
                p["spy_return_pct"] = round((b1 / b0 - 1) * 100, 2) if b0 and b1 else None
                if p["spy_return_pct"] is None:
                    p["beat_spy"] = None
                elif p["side"] == "LONG":
                    p["beat_spy"] = p["price_return_pct"] > p["spy_return_pct"]
                else:
                    p["beat_spy"] = p["price_return_pct"] < p["spy_return_pct"]
                p["open"] = False
            else:
                still_open.append(p)
        open_pos = still_open

        equity_now = cash + sum(position_value(p, p["last_price"],
                                               i - p["open_index"], rate)
                                for p in open_pos)
        for a, r in sorted(by_day.get(day, []), key=lambda x: -(x[0]["confidence"] or 0)):
            t = a["ticker"]
            if len(open_pos) >= d["max_positions"]:
                skipped.append({"date": day, "ticker": t, "side": a["side"],
                                "reason": f"book full ({d['max_positions']} positions)"})
                continue
            if any(p["ticker"] == t for p in open_pos):
                skipped.append({"date": day, "ticker": t, "side": a["side"],
                                "reason": "already held"})
                continue
            entry = r["entry"]
            if a["side"] == "SHORT" and entry < d["min_short_price"]:
                skipped.append({"date": day, "ticker": t, "side": "SHORT",
                                "reason": f"unborrowable: ${entry:.4f} < ${d['min_short_price']:.2f} floor"})
                continue
            alloc = min(equity_now * d["position_pct"] / 100.0, cash)
            if alloc <= 0:
                skipped.append({"date": day, "ticker": t, "side": a["side"],
                                "reason": "no cash / margin"})
                continue
            cash -= alloc
            pos = {"ticker": t, "side": a["side"], "open_date": day,
                   "open_time": f"{ENTRY_ET[0]:02d}:{ENTRY_ET[1]:02d}",
                   "open_index": i, "entry_price": round(entry, 4),
                   "stop_price": round(r["stop"], 4), "target_price": round(r["target"], 4),
                   "shares": round(alloc / entry, 6), "cost": round(alloc, 2),
                   "last_price": entry, "confidence": a["confidence"],
                   "brain": a["brain"], "thesis": a["thesis"], "open": True,
                   "coverage_pct": round(r["coverage"], 1), "fill_model": "intraday",
                   "exit_date": None, "exit_time": None, "exit_price": None,
                   "exit_reason": None, "borrow_cost": 0.0, "pnl": None,
                   "return_pct": None, "price_return_pct": None,
                   "spy_return_pct": None, "beat_spy": None, "marks": {},
                   "_plan": r}
            open_pos.append(pos)
            trades.append(pos)

        for p in open_pos:
            if p["_plan"]["exit"] is None:
                p["last_price"] = p["_plan"].get("last") or p["last_price"]
        curve.append({"date": day,
                      "equity": round(cash + sum(position_value(p, p["last_price"],
                                                                i - p["open_index"], rate)
                                                 for p in open_pos), 2),
                      "benchmark": bench.get(day)})

    last_i = len(calendar) - 1
    for p in open_pos:
        days_held = last_i - p["open_index"]
        value = position_value(p, p["last_price"], days_held, rate)
        p["borrow_cost"] = round(borrow_accrued(p, days_held, rate), 2)
        p["pnl"] = round(value - p["cost"], 2)
        p["return_pct"] = round(p["pnl"] / p["cost"] * 100, 2)
        p["price_return_pct"] = round((p["last_price"] / p["entry_price"] - 1) * 100, 2)
        p["last_price"] = round(p["last_price"], 4)
    for t in trades:
        t.pop("_plan", None)
        t.pop("marks", None)

    return trades, curve, skipped


def build_book(cfg, verdicts):
    """Replay the verdict ledger day by day into a book of simulated trades.

    Returns (trades, equity_curve, skipped). Deterministic: same ledger + same
    price history always produces the same book."""
    d = dials(cfg)
    av = AlphaVantage(cfg)
    today = datetime.now(ET).strftime("%Y-%m-%d")
    rate = d["borrow_cost_annual_pct"]

    bench = series(av, d["benchmark"])
    if not bench:
        raise RuntimeError(f"no {d['benchmark']} price history — cannot build the book")

    actionable, skipped = select_actionable(d, verdicts)

    if not verdicts:
        return [], [], skipped

    # Trading calendar = the benchmark's own trading days. Inception is the day
    # the analyst first spoke.
    start = min(v["date"] for v in verdicts)
    calendar = sorted(day for day in bench if start <= day <= today)
    if not calendar:
        return [], [], skipped

    prices = {}
    for a in actionable:
        t = a["ticker"]
        if t not in prices:
            prices[t] = series(av, t)
        if not prices[t] or a["date"] not in prices[t]:
            skipped.append({"date": a["date"], "ticker": t, "side": a["side"],
                            "reason": "no adjusted close for the verdict date"})

    by_day = {}
    for a in actionable:
        by_day.setdefault(a["date"], []).append(a)

    cash = float(d["starting_equity"])
    open_pos, trades, curve = [], [], []

    def mark(day, i):
        held = 0.0
        for p in open_pos:
            px = prices[p["ticker"]].get(day, p["last_price"])
            p["last_price"] = px
            held += position_value(p, px, i - p["open_index"], rate)
        return cash + held

    for i, day in enumerate(calendar):
        # 1) EXITS first — a position that dies today can't fund today's entry.
        #    (Deliberate: same-day recycling of capital is a free-money
        #    assumption the book hasn't earned.)
        still_open = []
        for p in open_pos:
            px = prices[p["ticker"]].get(day)
            if px is None:
                still_open.append(p)
                continue
            held_days = i - p["open_index"]
            reason = None
            if hit_stop(p, px, d["stop_pct"]):
                # NOTE: we exit at px, the real close that breached — NOT at the
                # stop price. A short that gaps through its stop eats the whole
                # gap, which is exactly the risk worth measuring.
                reason = "stop"
            elif held_days >= d["hold_trading_days"]:
                reason = "time stop"
            if reason:
                borrow = round(borrow_accrued(p, held_days, rate), 2)
                value = position_value(p, px, held_days, rate)
                cash += value
                p["exit_date"], p["exit_price"] = day, round(px, 4)
                p["exit_reason"] = reason
                p["borrow_cost"] = borrow
                p["pnl"] = round(value - p["cost"], 2)
                p["return_pct"] = round(p["pnl"] / p["cost"] * 100, 2)
                p["price_return_pct"] = round((px / p["entry_price"] - 1) * 100, 2)
                b0, b1 = bench.get(p["open_date"]), bench.get(day)
                p["spy_return_pct"] = round((b1 / b0 - 1) * 100, 2) if b0 and b1 else None
                # Same convention as analyst.py grade: a long "wins" if the stock
                # beat SPY, a short wins if it lagged SPY.
                if p["spy_return_pct"] is None:
                    p["beat_spy"] = None
                elif p["side"] == "LONG":
                    p["beat_spy"] = p["price_return_pct"] > p["spy_return_pct"]
                else:
                    p["beat_spy"] = p["price_return_pct"] < p["spy_return_pct"]
                p["open"] = False
            else:
                still_open.append(p)
        open_pos = still_open

        # 2) ENTRIES — sized off the book's equity as of this morning.
        equity_now = mark(day, i)
        for a in sorted(by_day.get(day, []), key=lambda x: -(x["confidence"] or 0)):
            t = a["ticker"]
            if not prices.get(t) or day not in prices[t]:
                continue
            if len(open_pos) >= d["max_positions"]:
                skipped.append({"date": day, "ticker": t, "side": a["side"],
                                "reason": f"book full ({d['max_positions']} positions)"})
                continue
            if any(p["ticker"] == t for p in open_pos):
                skipped.append({"date": day, "ticker": t, "side": a["side"],
                                "reason": "already held"})
                continue
            px = prices[t][day]
            # BORROWABILITY FLOOR. A short below this price is fiction: sub-$5
            # names are where borrow dries up entirely and sub-$1 names simply
            # cannot be located, whatever you'd pay. Counting them as winners
            # would invent profit the book could never have taken. The first
            # build had no floor and 4 of 9 shorts were sub-$1 penny movers.
            if a["side"] == "SHORT" and px < d["min_short_price"]:
                skipped.append({"date": day, "ticker": t, "side": "SHORT",
                                "reason": f"unborrowable: ${px:.4f} < ${d['min_short_price']:.2f} floor"})
                continue
            alloc = min(equity_now * d["position_pct"] / 100.0, cash)
            if alloc <= 0 or px <= 0:
                skipped.append({"date": day, "ticker": t, "side": a["side"],
                                "reason": "no cash / margin"})
                continue
            cash -= alloc                 # long: spent. short: reserved as margin.
            pos = {"ticker": t, "side": a["side"], "open_date": day,
                   "open_index": i, "entry_price": round(px, 4),
                   "shares": round(alloc / px, 6), "cost": round(alloc, 2),
                   "last_price": px, "confidence": a["confidence"],
                   "brain": a["brain"], "thesis": a["thesis"], "open": True,
                   "exit_date": None, "exit_price": None, "exit_reason": None,
                   "borrow_cost": 0.0, "pnl": None, "return_pct": None,
                   "price_return_pct": None, "spy_return_pct": None,
                   "beat_spy": None}
            open_pos.append(pos)
            trades.append(pos)

        curve.append({"date": day, "equity": round(mark(day, i), 2),
                      "benchmark": bench.get(day)})

    # Live marks for whatever is still open on the last day.
    last_i = len(calendar) - 1
    last = calendar[last_i]
    for p in open_pos:
        px = prices[p["ticker"]].get(last, p["last_price"])
        days = last_i - p["open_index"]
        value = position_value(p, px, days, rate)
        p["last_price"] = round(px, 4)
        p["borrow_cost"] = round(borrow_accrued(p, days, rate), 2)
        p["pnl"] = round(value - p["cost"], 2)
        p["return_pct"] = round(p["pnl"] / p["cost"] * 100, 2)
        p["price_return_pct"] = round((px / p["entry_price"] - 1) * 100, 2)

    return trades, curve, skipped


# ---------------- stats ----------------

def stats(cfg, trades, curve):
    d = dials(cfg)
    start_eq = float(d["starting_equity"])
    closed = [t for t in trades if not t["open"]]
    open_t = [t for t in trades if t["open"]]
    equity = curve[-1]["equity"] if curve else start_eq

    wins = [t for t in closed if (t["pnl"] or 0) > 0]
    losses = [t for t in closed if (t["pnl"] or 0) <= 0]
    beat = [t for t in closed if t.get("beat_spy")]
    graded = [t for t in closed if t.get("beat_spy") is not None]

    peak, max_dd = start_eq, 0.0
    for row in curve:
        peak = max(peak, row["equity"])
        max_dd = min(max_dd, (row["equity"] / peak - 1) * 100)

    bench_ret = None
    if curve and curve[0].get("benchmark") and curve[-1].get("benchmark"):
        bench_ret = round((curve[-1]["benchmark"] / curve[0]["benchmark"] - 1) * 100, 2)
    total_ret = round((equity / start_eq - 1) * 100, 2)

    def side_stat(side):
        s_closed = [t for t in closed if t["side"] == side]
        s_open = [t for t in trades if t["open"] and t["side"] == side]
        return {
            "closed": len(s_closed), "open": len(s_open),
            "pnl": round(sum(t["pnl"] or 0 for t in s_closed), 2),
            "win_rate_pct": (round(sum(1 for t in s_closed if (t["pnl"] or 0) > 0)
                                   / len(s_closed) * 100) if s_closed else None),
        }

    return {
        "starting_equity": start_eq,
        "equity": round(equity, 2),
        "total_return_pct": total_ret,
        "benchmark_return_pct": bench_ret,
        "excess_vs_benchmark_pct": (round(total_ret - bench_ret, 2)
                                    if bench_ret is not None else None),
        "realized_pnl": round(sum(t["pnl"] or 0 for t in closed), 2),
        "open_pnl": round(sum(t["pnl"] or 0 for t in open_t), 2),
        "borrow_paid": round(sum(t.get("borrow_cost") or 0 for t in trades), 2),
        "trades": len(trades),
        "closed": len(closed),
        "open_positions": len(open_t),
        "win_rate_pct": round(len(wins) / len(closed) * 100) if closed else None,
        "beat_spy_pct": round(len(beat) / len(graded) * 100) if graded else None,
        "avg_win_pct": (round(sum(t["return_pct"] for t in wins) / len(wins), 2)
                        if wins else None),
        "avg_loss_pct": (round(sum(t["return_pct"] for t in losses) / len(losses), 2)
                         if losses else None),
        "max_drawdown_pct": round(max_dd, 2),
        "inception": curve[0]["date"] if curve else None,
        "long": side_stat("LONG"),
        "short": side_stat("SHORT"),
        "shorts_enabled": d["allow_short"],
        "borrow_cost_annual_pct": d["borrow_cost_annual_pct"],
        "fill_model": d.get("fill_model", "intraday"),
        "exit_reasons": {r: sum(1 for t in closed if t.get("exit_reason") == r)
                         for r in ("target", "stop", "time stop")},
    }


def write_outputs(cfg, trades, curve, skipped, s):
    with open(BOOK, "w") as f:
        for t in sorted(trades, key=lambda x: (x["open_date"], x["ticker"])):
            f.write(json.dumps(t) + "\n")
    json.dump({
        "updated": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"),
        "simulated": True,
        "dials": dials(cfg),
        "stats": s,
        "positions": sorted([t for t in trades if t["open"]],
                            key=lambda x: x["open_date"], reverse=True),
        "closed": sorted([t for t in trades if not t["open"]],
                         key=lambda x: x["exit_date"] or "", reverse=True)[:40],
        "curve": curve[-120:],
        "skipped": skipped[-40:],
    }, open(ACCOUNT, "w"), indent=2)


# ---------------- commands ----------------

def cmd_run():
    cfg = load_config()
    d = dials(cfg)
    verdicts = load_verdicts()
    if not verdicts:
        sys.exit("No analyst verdicts yet — run `python3 analyst.py run` first.")

    bullish = sum(1 for v in verdicts if v.get("verdict") == "BULLISH")
    bearish = sum(1 for v in verdicts if v.get("verdict") == "BEARISH")

    model = d.get("fill_model", "intraday")
    print("\n  ANALYST BOOK — simulated, no broker, no orders")
    print(f"  fill model: {model}"
          + ("  (9:45 marketable limit + ATR bracket, walked minute by minute)"
             if model == "intraday" else "  (close-to-close)"))
    print(f"  long + short: {d['allow_short']}  ·  borrow {d['borrow_cost_annual_pct']}%/yr"
          f"  ·  {len(verdicts)} verdicts ({bullish} bullish, {bearish} bearish)")
    print("-" * 66)

    if model == "intraday":
        trades, curve, skipped = build_book_intraday(cfg, verdicts)
    else:
        trades, curve, skipped = build_book(cfg, verdicts)
    for t in trades:
        t.setdefault("fill_model", "close")
    s = stats(cfg, trades, curve)
    write_outputs(cfg, trades, curve, skipped, s)

    if not trades:
        print("  No positions opened.")
        if skipped:
            print(f"  {len(skipped)} actionable verdict(s) skipped — see snapshots/fund_account.json")
    else:
        for t in sorted(trades, key=lambda x: (x["open_date"], x["ticker"])):
            tag = f"{t['side']:<5}"
            if t["open"]:
                print(f"  OPEN   {tag} {t['ticker']:<6} in {t['open_date']} @ {t['entry_price']:<9.4f} "
                      f"now {t['last_price']:<9.4f} {t['return_pct']:+7.2f}%")
            else:
                flag = "beat SPY" if t.get("beat_spy") else "missed"
                print(f"  CLOSED {tag} {t['ticker']:<6} {t['open_date']} -> {t['exit_date']} "
                      f"{t['return_pct']:+7.2f}% ({t['exit_reason']}, {flag})")
    print(f"\n  Equity ${s['equity']:,.2f} ({s['total_return_pct']:+.2f}%)"
          + (f" vs {d['benchmark']} {s['benchmark_return_pct']:+.2f}%"
             if s["benchmark_return_pct"] is not None else ""))
    if s["short"]["closed"] or s["short"]["open"]:
        print(f"  Shorts: {s['short']['closed']} closed / {s['short']['open']} open, "
              f"P&L ${s['short']['pnl']:,.2f}, borrow paid ${s['borrow_paid']:,.2f}")
        print("  Remember: the sim assumes every short was BORROWABLE at "
              f"{d['borrow_cost_annual_pct']}%/yr. Movers often aren't. Upper bound, not an estimate.")
    print("  Wrote snapshots/fund_account.json + fund_book.jsonl (simulated — no orders placed).")


def cmd_report():
    if not os.path.exists(ACCOUNT):
        sys.exit("No book yet — run `python3 analyst_book.py run` first.")
    print(json.dumps(json.load(open(ACCOUNT)).get("stats", {}), indent=2))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    {"run": cmd_run, "report": cmd_report}.get(cmd, lambda: print(__doc__))()
