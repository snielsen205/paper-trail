"""
bot.py — Milestone 5: the paper-trading bot. Rulebook v1.1 is the law.

PAPER MONEY ONLY. alpaca_client.py refuses live keys.

Commands:
  python3 bot.py enter            9:45 AM ET entry run (waits for 9:45 if
                                  early; refuses + logs "missed" if late)
  python3 bot.py enter --dry-run  full rehearsal: every decision made and
                                  printed, NO orders placed. Works even
                                  before Alpaca keys exist.
  python3 bot.py manage           daily upkeep: log exits, apply the 5-day
                                  time stop, reconcile vs Alpaca, check
                                  the 10% circuit breaker.
  python3 bot.py status           account + positions + recent events.
  python3 bot.py resume --reason "..."   un-pause after a circuit breaker
                                  (a deliberate human decision, logged).

Everything is logged to snapshots/bot_log.jsonl (every pick, every day,
including skips and unfilled orders — rulebook §9), and the dashboard
reads snapshots/bot_account.json, refreshed by every command here.

The daily rhythm once keys are in:
  9:45 AM ET   python3 bot.py enter
  after close  python3 bot.py manage
"""

import argparse
import glob
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from av_client import load_config, AlphaVantage
from alpaca_client import Alpaca

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP_DIR = os.path.join(HERE, "snapshots")
STATE_PATH = os.path.join(SNAP_DIR, "bot_state.json")
LOG_PATH = os.path.join(SNAP_DIR, "bot_log.jsonl")
ACCOUNT_PATH = os.path.join(SNAP_DIR, "bot_account.json")
ET = ZoneInfo("America/New_York")
DRY_RUN_EQUITY = 100000.0        # rehearsal sizing when no account exists


# ---------------- state & logging ----------------

def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"window_start_date": None, "window_start_equity": None,
            "paused": False, "pause_reason": None, "open": {}}


def save_state(state):
    os.makedirs(SNAP_DIR, exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def log_event(event, **fields):
    """Append one line to the permanent ledger. Never edited, only added.
    Important events also push to the owner's phone (rulebook §14:
    'errors alert visibly' — and fills/exits are worth knowing too)."""
    os.makedirs(SNAP_DIR, exist_ok=True)
    row = {"ts": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"),
           "date": datetime.now(ET).strftime("%Y-%m-%d"),
           "event": event, **fields}
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(row) + "\n")
    _push_for_event(row)
    return row


# ---------------- phone notifications (ntfy.sh, free, no account) ----------

_NTFY_TOPIC = None                   # cached; "" means disabled


def _topic():
    global _NTFY_TOPIC
    if _NTFY_TOPIC is None:
        try:
            from av_client import load_config as _lc
            _NTFY_TOPIC = _lc().get("notifications", {}).get("ntfy_topic", "")
        except SystemExit:
            _NTFY_TOPIC = ""
    return _NTFY_TOPIC


def _header_safe(s):
    """HTTP headers go out as latin-1, so a smart dash or curly quote in a
    TITLE raises UnicodeEncodeError and the whole push is lost. (Real case
    2026-07-31: earnings_flag flagged REPL and the em-dash in "1 earnings
    mover — analyst verdicts" killed the alert.) The message BODY is fine —
    it's sent as UTF-8 bytes. So: transliterate the usual suspects, then drop
    anything still unencodable rather than lose the notification."""
    for bad, good in (("—", "-"), ("–", "-"), ("‘", "'"),
                      ("’", "'"), ("“", '"'), ("”", '"'),
                      ("…", "..."), (" ", " ")):
        s = s.replace(bad, good)
    return s.encode("latin-1", "ignore").decode("latin-1")


def notify(title, message, priority=3, tags=""):
    """Push to the phone. Never allowed to break trading: any failure is
    printed and swallowed."""
    topic = _topic()
    if not topic:
        return
    try:
        req = urllib.request.Request(
            "https://ntfy.sh/" + topic,
            data=message.encode("utf-8"),
            headers={"Title": _header_safe(title), "Priority": str(priority),
                     "Tags": _header_safe(tags)},
            method="POST")
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:                            # noqa: BLE001
        print(f"  (phone notification failed, continuing: {e})")


def _push_for_event(row):
    """Which ledger events deserve a pocket buzz, and how they read.
    Quiet events (skips, no_room, dry runs) stay in the file only."""
    e = row["event"]
    t = row.get("ticker", "")
    r = row.get("reason", "")
    if e == "entered":
        notify(f"Entered {t}",
               f"{row['direction']} {row['qty']} sh @ {row['fill_price']}\n"
               f"stop {row['stop']} / target {row['target']} "
               f"(score {row['score']})", tags="white_check_mark")
    elif e == "partial_fill":
        notify(f"Partial fill {t}",
               f"{row['qty_filled']}/{row['qty_wanted']} sh @ "
               f"{row['fill_price']} — bracket resized", tags="warning")
    elif e == "unfilled":
        notify(f"{t} not filled", "Walked away at the deadline — "
               "logged as not tradeable today.", priority=2)
    elif e == "exit":
        res = row.get("result_pct")
        badge = "tada" if (res or 0) >= 0 else "small_red_triangle_down"
        notify(f"Exit {t}: {r}",
               (f"{res:+.1f}% @ {row.get('exit_price')}" if res is not None
                else "See dashboard for details."), tags=badge)
    elif e == "time_stop_submitted":
        notify(f"Time stop {t}",
               f"Held {row['days_held']} trading days — closing at market.",
               tags="alarm_clock")
    elif e == "paused":
        notify("CIRCUIT BREAKER — bot paused", r, priority=5,
               tags="rotating_light")
    elif e == "resumed":
        notify("Bot resumed", r, tags="arrow_forward")
    elif e == "reconcile_mismatch":
        notify(f"Reconcile MISMATCH {t}", r, priority=5, tags="warning")
    elif e == "missed_day":
        notify("Missed entry day", r, priority=4, tags="warning")
    elif e == "error":
        notify(f"Bot error {t}", r, priority=5, tags="x")


def recent_events(n=25):
    if not os.path.exists(LOG_PATH):
        return []
    with open(LOG_PATH) as f:
        lines = [l for l in f if l.strip()]
    return [json.loads(l) for l in lines[-n:]]


def write_account_snapshot(state, equity=None, cash=None, alpaca_positions=None):
    """The file the dashboard's Bot tab reads. Written by every command."""
    live = {p["symbol"]: p for p in (alpaca_positions or [])}
    positions = []
    for sym, pos in state["open"].items():
        now_price = None
        if sym in live:
            try:
                now_price = float(live[sym]["current_price"])
            except (KeyError, ValueError):
                pass
        positions.append({**pos, "ticker": sym, "now": now_price})
    with open(ACCOUNT_PATH, "w") as f:
        json.dump({
            "updated": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"),
            "equity": equity, "cash": cash,
            "window_start_equity": state["window_start_equity"],
            "window_start_date": state["window_start_date"],
            "paused": state["paused"], "pause_reason": state["pause_reason"],
            "positions": positions,
            "recent_events": recent_events(25),
        }, f, indent=2)


# ---------------- shared helpers ----------------

def px(value):
    """
    Round a price the way exchanges accept: pennies at >=$1, hundredths of a
    cent below $1.

    Precision is decided by THIS price, never by a reference price. Alpaca
    validates every order leg on its own value, so a sub-$1 entry whose 2:1
    target lands above $1 needs the TARGET in pennies even though the entry
    may carry four decimals. The old price_hint argument passed the entry as
    the reference and produced take_profit 1.1426 on LOOP (2026-08-20) and
    1.2389 on HOWL (2026-08-21); Alpaca 422'd the whole bracket both times
    ("sub-penny increment does not fulfill minimum pricing criteria") and the
    bot logged a skip, so two intended trades never happened.
    """
    return round(value, 2 if value >= 1 else 4)


def latest_top10():
    files = sorted(glob.glob(os.path.join(SNAP_DIR, "top10_*.json")))
    if not files:
        return None, None
    name = os.path.basename(files[-1])                # top10_YYYY-MM-DD_HHMM
    snap_date = name.split("_")[1]
    with open(files[-1]) as f:
        return json.load(f), snap_date


def trading_days_between(alp, start_date, end_date):
    """How many trading days have fully elapsed since entry (uses Alpaca's
    real calendar — holidays and half-days handled, rulebook §14)."""
    days = alp.calendar(start_date, end_date)
    return max(0, len(days) - 1)                      # entry day itself = 0


def get_sector(av, ticker):
    try:
        overview = av.company_overview(ticker)
        sector = (overview or {}).get("Sector") or ""
        return sector.strip().upper() or "UNKNOWN"
    except RuntimeError:
        return "UNKNOWN"


def open_risk_dollars(state):
    """Sum of what every open position would lose at its stop (§10)."""
    total = 0.0
    for pos in state["open"].values():
        total += abs(pos["fill_price"] - pos["stop"]) * pos["qty"]
    return total


def check_circuit_breaker(state, equity, dials):
    """§15: 10% drawdown from window start -> pause new entries."""
    if state["window_start_equity"] is None:
        state["window_start_equity"] = equity
        state["window_start_date"] = datetime.now(ET).strftime("%Y-%m-%d")
        save_state(state)
        return False
    dd = (state["window_start_equity"] - equity) / state["window_start_equity"]
    limit = dials["drawdown_pause_pct"] / 100.0
    if dd >= limit and not state["paused"]:
        state["paused"] = True
        state["pause_reason"] = (
            f"circuit breaker: equity {equity:.0f} is down "
            f"{dd * 100:.1f}% from window start "
            f"{state['window_start_equity']:.0f}")
        save_state(state)
        log_event("paused", reason=state["pause_reason"])
        print(f"\n*** CIRCUIT BREAKER: {state['pause_reason']} ***")
        print("Existing brackets still resolve. New entries stop until you")
        print('run:  python3 bot.py resume --reason "why it is safe now"\n')
    return state["paused"]


# ---------------- ENTER ----------------

def qualify_picks(picks, config, state):
    """Rulebook §1 + §10 + §11 filters, in order, every decision logged.
    Returns the picks that survive; logs why the others didn't."""
    dials = config["bot_dials"]
    floor = dials["score_floor"]
    min_dv = config["filters"]["min_dollar_volume"]
    survivors = []
    for p in sorted(picks, key=lambda x: -x["score"]):
        t = p["ticker"]
        if p["direction"] == "WATCH":
            continue                                  # never traded, §1
        if p["score"] < floor:
            continue                                  # below the floor, §1
        if p["dollar_volume"] < min_dv:
            log_event("skip", ticker=t, reason="dollar volume under floor",
                      dollar_volume=p["dollar_volume"], score=p["score"])
            continue
        if not p.get("atr"):
            log_event("skip", ticker=t, reason="bad data: no ATR",
                      score=p["score"])
            continue
        if t in state["open"]:
            log_event("skip", ticker=t,
                      reason="already in position (never add or flip)",
                      score=p["score"])
            continue
        survivors.append(p)
    return survivors


def plan_order(p, equity, dials, quote):
    """Size and price one pick per §2-§5. Returns an order plan dict or
    (None, reason)."""
    t, direction, atr = p["ticker"], p["direction"], p["atr"]
    bid, ask = quote["bid"], quote["ask"]
    if bid <= 0 or ask <= 0 or ask < bid:
        return None, f"bad data: crossed/zero quote (bid {bid}, ask {ask})"

    pad = dials.get("entry_limit_pad_pct", 0.1) / 100.0
    if direction == "LONG":
        limit = px(ask * (1 + pad))                   # a hair above ask, §2
        side = "buy"
    else:
        limit = px(bid * (1 - pad))                   # a hair below bid, §2
        side = "sell"

    stop_dist = dials["atr_stop_multiple"] * atr      # §3
    sign = 1 if direction == "LONG" else -1
    stop = px(limit - sign * stop_dist)
    target = px(limit + sign * stop_dist * dials["reward_to_risk"])
    if stop <= 0:
        return None, "bad data: stop would be at/below zero"

    risk_dollars = equity * dials["risk_per_trade_pct"] / 100.0    # §5
    qty = int(risk_dollars / stop_dist)
    cap_qty = int(equity * dials["position_cap_pct"] / 100.0 / limit)
    qty = min(qty, cap_qty)
    if qty < 1:
        return None, "sized to zero shares (price too high for risk budget)"

    # Measurement, not rules: spread and day-move go into every log row so
    # the window-1 autopsy can ask "how much did friction and chasing
    # extended spikes actually cost?" without guessing.
    mid = (bid + ask) / 2
    spread_pct = round((ask - bid) / mid * 100, 3) if mid > 0 else None
    return {"ticker": t, "direction": direction, "side": side, "qty": qty,
            "limit": limit, "stop": stop, "target": target, "atr": atr,
            "score": p["score"], "risk_dollars": round(qty * stop_dist, 2),
            "stop_dist": stop_dist, "spread_pct": spread_pct,
            "change_pct": p.get("change_pct")}, None


def cmd_enter(dry_run=False, force=False):
    config = load_config()
    dials = config["bot_dials"]
    state = load_state()
    av = AlphaVantage(config)
    now = datetime.now(ET)
    today = now.strftime("%Y-%m-%d")

    picks, snap_date = latest_top10()
    if picks is None:
        sys.exit("No top10 snapshot found. Run scan.py then score.py first.")
    if snap_date != today:
        msg = (f"Latest top10 is from {snap_date}, not today ({today}). "
               f"Run scan.py + score.py this morning first.")
        if dry_run:
            print(f"NOTE ({msg}) — continuing anyway, this is a rehearsal.\n")
        else:
            sys.exit(msg)

    # -- connect (or rehearse) --
    alp = None
    if dry_run:
        try:
            alp = Alpaca(config)
        except SystemExit:
            print("Rehearsal without Alpaca keys: using snapshot prices as "
                  f"quotes and ${DRY_RUN_EQUITY:,.0f} equity.\n")
        equity = DRY_RUN_EQUITY
        if alp:
            equity = float(alp.account()["equity"])
    else:
        alp = Alpaca(config)
        account = alp.account()
        equity = float(account["equity"])

        clock = alp.clock()
        if not clock["is_open"] and not force:
            log_event("missed_day", reason="market closed at run time")
            sys.exit("Market is closed — no entry run today (logged).")

        # The minutes clock, §2: place at 9:45, give up at 10:05. The
        # deadline is fixed at 10:05 even if this run starts a few minutes
        # late — the window never slides.
        entry_t = now.replace(hour=9, minute=45, second=0, microsecond=0)
        window_end = entry_t + timedelta(minutes=dials["entry_window_minutes"])
        if now > window_end and not force:
            log_event("missed_day",
                      reason=f"enter ran at {now.strftime('%H:%M')} ET, "
                             f"after the entry window — never enter late")
            sys.exit("Past the entry window. Logged as a missed day (§14) — "
                     "the bot never enters late to catch up.")
        if now < entry_t:
            wait = (entry_t - now).total_seconds()
            print(f"Waiting {wait / 60:.1f} min until 9:45 AM ET...")
            time.sleep(wait)

        if check_circuit_breaker(state, equity, dials):
            write_account_snapshot(state, equity,
                                   float(account["cash"]), alp.positions())
            sys.exit("Bot is paused — no new entries.")

    if state["paused"] and not dry_run:
        sys.exit(f"Bot is paused ({state['pause_reason']}). "
                 'Run: python3 bot.py resume --reason "..."')

    # -- qualify + portfolio limits (§10), highest score first --
    candidates = qualify_picks(picks, config, state)
    print(f"{len(candidates)} qualifying pick(s) after the score/volume/"
          f"repeat filters.")
    max_risk = equity * dials["max_total_open_risk_pct"] / 100.0
    used_risk = open_risk_dollars(state)
    sector_count = {}
    for pos in state["open"].values():
        s = pos.get("sector", "UNKNOWN")
        sector_count[s] = sector_count.get(s, 0) + 1

    placed = []
    for p in candidates:
        t = p["ticker"]
        if len(state["open"]) + len(placed) >= dials["max_positions"]:
            log_event("no_room", ticker=t, score=p["score"],
                      reason=f"max {dials['max_positions']} positions")
            continue

        sector = get_sector(av, t)
        if sector_count.get(sector, 0) >= dials["max_per_sector"]:
            log_event("skip", ticker=t, score=p["score"],
                      reason=f"sector full ({sector}) — movers cluster")
            continue

        # -- live tradeability checks (need Alpaca) --
        if alp:
            try:
                asset = alp.asset(t)
            except RuntimeError as e:
                log_event("skip", ticker=t, reason=f"bad data: {e}")
                continue
            if not asset.get("tradable"):
                log_event("skip", ticker=t, reason="not tradable / halted")
                continue
            if p["direction"] == "SHORT" and not (
                    asset.get("shortable") and asset.get("easy_to_borrow")):
                log_event("skip", ticker=t, reason="short unavailable",
                          score=p["score"])           # §12 — this log matters
                continue
            try:
                q = alp.latest_quote(t)
                quote = {"bid": float(q.get("bp", 0)),
                         "ask": float(q.get("ap", 0))}
            except (RuntimeError, ValueError) as e:
                log_event("skip", ticker=t, reason=f"bad data: quote ({e})")
                continue
        else:
            quote = {"bid": p["price"], "ask": p["price"]}   # rehearsal proxy

        plan, reason = plan_order(p, equity, dials, quote)
        if plan is None:
            log_event("skip", ticker=t, reason=reason, score=p["score"])
            continue
        if used_risk + plan["risk_dollars"] > max_risk:
            log_event("no_room", ticker=t, score=p["score"],
                      reason=f"open-risk budget: {used_risk:.0f} used of "
                             f"{max_risk:.0f} max")
            continue

        plan["sector"] = sector
        if dry_run:
            log_event("dry_run_plan", **{k: plan[k] for k in
                      ("ticker", "direction", "qty", "limit", "stop",
                       "target", "score", "risk_dollars", "spread_pct",
                       "change_pct")})
            print(f"  WOULD PLACE  {t:<6} {plan['direction']:<5} "
                  f"{plan['qty']} sh  limit {plan['limit']}  "
                  f"stop {plan['stop']}  target {plan['target']}  "
                  f"(risking ${plan['risk_dollars']:.0f})")
        else:
            try:
                order = alp.submit_bracket(
                    t, plan["side"], plan["qty"], plan["limit"],
                    plan["stop"], plan["target"])
            except RuntimeError as e:
                log_event("skip", ticker=t, reason=f"order rejected: {e}")
                continue
            plan["order_id"] = order["id"]
            print(f"  PLACED  {t:<6} {plan['direction']:<5} {plan['qty']} sh"
                  f"  limit {plan['limit']}  stop {plan['stop']}  "
                  f"target {plan['target']}")
        used_risk += plan["risk_dollars"]
        sector_count[sector] = sector_count.get(sector, 0) + 1
        placed.append(plan)

    if dry_run:
        if not placed:
            print("  (nothing qualifies today)")
        print("\nRehearsal complete — no orders were placed.")
        write_account_snapshot(state, equity if alp else None, None,
                               alp.positions() if alp else None)
        return

    # -- the walk-away rule (§2): poll until the fixed 10:05 deadline
    # (under --force there is no real window, so allow one from now) --
    if placed:
        deadline = window_end if not force else datetime.now(ET) + timedelta(
            minutes=dials["entry_window_minutes"])
        pending = {pl["ticker"]: pl for pl in placed}
        print(f"\nWatching fills until "
              f"{deadline.strftime('%H:%M')} ET (walk-away rule)...")
        while pending and datetime.now(ET) < deadline:
            time.sleep(30)
            for t in list(pending):
                pl = pending[t]
                order = alp.get_order(pl["order_id"])
                if order["status"] == "filled":
                    fill = float(order["filled_avg_price"])
                    state["open"][t] = {
                        "entry_date": today, "direction": pl["direction"],
                        "qty": pl["qty"], "fill_price": fill,
                        "stop": pl["stop"], "target": pl["target"],
                        "score": pl["score"], "atr": pl["atr"],
                        "sector": pl["sector"], "order_id": pl["order_id"],
                    }
                    save_state(state)
                    log_event("entered", ticker=t, direction=pl["direction"],
                              qty=pl["qty"], fill_price=fill,
                              intended_limit=pl["limit"], stop=pl["stop"],
                              target=pl["target"], score=pl["score"],
                              atr=pl["atr"], sector=pl["sector"],
                              spread_pct=pl["spread_pct"],
                              change_pct=pl["change_pct"])
                    print(f"  FILLED  {t} @ {fill}")
                    del pending[t]

        # window over: cancel stragglers; keep whatever partially filled
        for t, pl in pending.items():
            order = alp.get_order(pl["order_id"])
            filled_qty = int(float(order.get("filled_qty") or 0))
            if order["status"] not in ("filled", "canceled"):
                try:
                    alp.cancel_order(pl["order_id"])
                except RuntimeError:
                    pass
            if filled_qty > 0:                        # partial, §13
                fill = float(order["filled_avg_price"])
                state["open"][t] = {
                    "entry_date": today, "direction": pl["direction"],
                    "qty": filled_qty, "fill_price": fill,
                    "stop": pl["stop"], "target": pl["target"],
                    "score": pl["score"], "atr": pl["atr"],
                    "sector": pl["sector"], "order_id": pl["order_id"],
                }
                save_state(state)
                log_event("partial_fill", ticker=t, qty_wanted=pl["qty"],
                          qty_filled=filled_qty, fill_price=fill,
                          direction=pl["direction"], score=pl["score"],
                          spread_pct=pl["spread_pct"],
                          change_pct=pl["change_pct"])
                print(f"  PARTIAL {t}: {filled_qty}/{pl['qty']} filled — "
                      f"remainder canceled")
                # Cancelling the partially-filled parent also cancelled its
                # stop/target legs, so the filled shares are now naked. Restore
                # protection with an OCO sized to the actual fill (rulebook §6).
                exit_side = "sell" if pl["direction"] == "LONG" else "buy"
                try:
                    prot = alp.submit_oco_exit(t, exit_side, filled_qty,
                                               pl["target"], pl["stop"])
                    state["open"][t]["protect_order_id"] = prot.get("id")
                    save_state(state)
                    print(f"    re-protected {t}: OCO stop {pl['stop']} / "
                          f"target {pl['target']} on {filled_qty} sh")
                except RuntimeError as e:
                    log_event("error", ticker=t,
                              reason=f"partial-fill re-protect FAILED: {e}")
                    notify("Position UNPROTECTED",
                           f"{t}: {filled_qty} sh filled but re-protect failed "
                           f"— add a stop manually.\n{e}",
                           priority=5, tags="rotating_light")
                    print(f"    *** re-protect FAILED for {t}: {e}")
            else:
                log_event("unfilled", ticker=t, direction=pl["direction"],
                          intended_limit=pl["limit"], score=pl["score"],
                          spread_pct=pl["spread_pct"],
                          change_pct=pl["change_pct"],
                          reason="not filled in the entry window — "
                                 "not tradeable today, no chasing")
                print(f"  UNFILLED {t} — canceled (data, not failure)")
    else:
        print("Nothing to place today.")

    account = alp.account()
    write_account_snapshot(state, float(account["equity"]),
                           float(account["cash"]), alp.positions())
    print(f"\nDone. {len(state['open'])} open position(s). "
          f"Equity ${float(account['equity']):,.2f}")
    notify("Entry run finished",
           f"{len(placed)} order(s) placed, {len(state['open'])} open "
           f"position(s).\nEquity ${float(account['equity']):,.2f}",
           priority=2, tags="sunrise")


# ---------------- MANAGE ----------------

def spy_move_pct(av, start_date, end_date):
    """SPY close-to-close over the holding window (§9). Fail-soft to None."""
    try:
        data = av.daily_adjusted("SPY")
        series = data.get("Time Series (Daily)", {})
        days = sorted(d for d in series if start_date <= d <= end_date)
        if len(days) < 2:
            return None
        a = float(series[days[0]]["5. adjusted close"])
        b = float(series[days[-1]]["5. adjusted close"])
        return round((b - a) / a * 100, 2)
    except (RuntimeError, KeyError, ValueError):
        return None


def attribute_exit(pos, closing_order):
    """Name the actual cause of an exit that did not come from a bracket leg.

    "closed (manual / time-stop / re-protect)" was three different causes in
    one string, and one of them -- a manual close -- would violate rulebook 7.
    A log that cannot tell them apart cannot show the rule was followed, so
    each is now named:

      * the bot recorded its own intent before closing (time stop)  -> trusted
      * a re-protect OCO leg filled: limit = target, stop = stop     -> inferred
      * a market order nobody on the bot's side submitted            -> flagged

    The last case is deliberately called out rather than absorbed into a
    benign-sounding label. An unattributed exit is a finding.
    """
    intent = pos.get("exit_intent")
    if intent:
        return intent

    otype = (closing_order.get("type") or "").lower()
    if otype == "limit":
        return "target hit (re-protect OCO)"
    if otype in ("stop", "stop_limit", "trailing_stop"):
        return "stop hit (re-protect OCO)"
    return "closed outside the bot (unattributed)"


def cmd_manage():
    config = load_config()
    dials = config["bot_dials"]
    state = load_state()
    av = AlphaVantage(config)
    alp = Alpaca(config)
    today = datetime.now(ET).strftime("%Y-%m-%d")

    live_positions = {p["symbol"]: p for p in alp.positions()}

    # 1) positions we thought were open but Alpaca no longer holds -> the
    #    bracket resolved. Find which leg filled and log the exit (§9).
    for t in list(state["open"]):
        if t in live_positions:
            continue
        pos = state["open"][t]
        exit_price, reason = None, "unknown (see reconciliation)"
        try:
            parent = alp.get_order(pos["order_id"])
            for leg in parent.get("legs") or []:
                if leg.get("status") == "filled":
                    exit_price = float(leg["filled_avg_price"])
                    reason = ("target hit" if leg["type"] == "limit"
                              else "stop hit")
        except RuntimeError:
            pass
        # Fallback: the position left the account some OTHER way than the
        # original bracket legs — a re-protect OCO, a time-stop close_position,
        # or a manual sell. Find the actual closing fill so the exit gets a
        # real price and CONTRIBUTES TO REALIZED P&L (the dashboard only counts
        # exits that have an exit_price).
        if exit_price is None:
            try:
                want = "sell" if pos["direction"] == "LONG" else "buy"
                closed = alp.list_orders(status="closed", limit=200)
                fills = [o for o in closed
                         if o.get("symbol") == t and o.get("side") == want
                         and o.get("filled_avg_price")
                         and float(o.get("filled_qty") or 0) > 0]
                if fills:
                    fills.sort(key=lambda o: (o.get("filled_at")
                               or o.get("submitted_at") or ""), reverse=True)
                    closer = fills[0]
                    exit_price = float(closer["filled_avg_price"])
                    if reason.startswith("unknown"):
                        reason = attribute_exit(pos, closer)
            except RuntimeError:
                pass
        result = None
        if exit_price:
            sign = 1 if pos["direction"] == "LONG" else -1
            result = round(sign * (exit_price - pos["fill_price"])
                           / pos["fill_price"] * 100, 2)
        log_event("exit", ticker=t, reason=reason, exit_price=exit_price,
                  result_pct=result, direction=pos["direction"],
                  qty=pos["qty"], fill_price=pos["fill_price"],
                  entry_date=pos["entry_date"], score=pos["score"],
                  spy_pct=spy_move_pct(av, pos["entry_date"], today))
        print(f"  EXIT  {t}: {reason}"
              + (f" @ {exit_price} ({result:+.1f}%)" if exit_price else ""))
        del state["open"][t]
        save_state(state)

    # 2) the days clock (§7): 5 trading days without stop/target -> out.
    for t in list(state["open"]):
        pos = state["open"][t]
        held = trading_days_between(alp, pos["entry_date"], today)
        if held >= dials["time_stop_days"]:
            clock = alp.clock()
            if not clock["is_open"]:
                print(f"  {t}: time stop due ({held} trading days) — market "
                      f"closed now; run manage after the next open to exit.")
                continue
            # The protective legs HOLD the shares (held_for_orders), so a
            # close_position on a still-bracketed position is rejected 403
            # "insufficient qty available". Since brackets became GTC the legs
            # no longer expire overnight, so this must be done explicitly:
            # release the shares first, THEN close. Cancel is safe — we are
            # exiting anyway, and the position is closed at market immediately
            # after (a failed close is logged loudly below, not left silent).
            released = 0
            try:
                for o in alp.list_orders(status="open", limit=500):
                    if o.get("symbol") != t:
                        continue
                    try:
                        alp.cancel_order(o["id"])
                        released += 1
                    except RuntimeError:
                        pass
                if released:
                    time.sleep(1)      # let the cancels settle before selling
            except RuntimeError as e:
                print(f"  ({t}: could not list/cancel protective orders: {e})")
            try:
                alp.close_position(t)
                # Remember WHY this position is being closed. The exit itself
                # is logged on the next manage run, by which point the only
                # evidence left is a closing fill that looks identical to a
                # manual sell. Recording the intent here is what lets the
                # exit be attributed instead of guessed (rulebook 7, 9).
                pos["exit_intent"] = "time stop"
                save_state(state)
                log_event("time_stop_submitted", ticker=t, days_held=held,
                          direction=pos["direction"], qty=pos["qty"],
                          protective_orders_cancelled=released)
                print(f"  TIME STOP {t}: held {held} trading days — cancelled "
                      f"{released} protective order(s), closing at market "
                      f"(exit logged on next manage run)")
            except RuntimeError as e:
                log_event("error", ticker=t,
                          reason=f"time-stop close failed: {e}")
                notify("TIME STOP FAILED", f"{t}: could not close.\n{e}",
                       priority=5, tags="rotating_light")
                print(f"  ERROR closing {t}: {e}")

    # 3) reconciliation (§14): loud, never silently fixed. The paper account
    # is SHARED with the momentum + macro sleeves, so "Alpaca holds a symbol
    # the movers bot didn't log" is NORMAL for those — reconcile only what the
    # bot actually owns, and treat known other-sleeve holdings as expected.
    try:
        from momentum_sleeve import UNIVERSE as _MOM_UNIVERSE
        other_sleeves = set(_MOM_UNIVERSE)
    except Exception:
        other_sleeves = set()
    other_sleeves |= set(config.get("reconcile_ignore_symbols", []))

    # (a) every position the bot tracks must still match Alpaca's quantity
    for sym, pos in state["open"].items():
        live = live_positions.get(sym)
        if live is None:
            continue                      # a vanished one is handled above (exit sweep)
        want = pos["qty"] * (1 if pos["direction"] == "LONG" else -1)
        if int(float(live["qty"])) != want:
            log_event("reconcile_mismatch", ticker=sym,
                      reason="quantity differs from the bot's log",
                      alpaca_qty=live["qty"], bot_qty=pos["qty"])
            print(f"  *** MISMATCH on {sym}: Alpaca qty {live['qty']} "
                  f"vs bot log {pos['qty']}")
    # (b) a stray that is neither ours nor a known sleeve holding -> alert once
    for sym, live in live_positions.items():
        if sym in state["open"] or sym in other_sleeves:
            continue
        log_event("reconcile_mismatch", ticker=sym,
                  reason="Alpaca holds a position no sleeve logged",
                  alpaca_qty=live["qty"])
        print(f"  *** UNEXPECTED: Alpaca holds {sym} x{live['qty']} that no "
              f"sleeve recorded. Investigate (or add to "
              f"reconcile_ignore_symbols if it's a known sleeve).")

    # 4) circuit breaker + snapshot for the dashboard
    account = alp.account()
    equity = float(account["equity"])
    check_circuit_breaker(state, equity, dials)
    write_account_snapshot(state, equity, float(account["cash"]),
                           alp.positions())
    print(f"\nManage done. {len(state['open'])} open, "
          f"equity ${equity:,.2f}"
          + (" — PAUSED" if state["paused"] else ""))
    notify("Manage done",
           f"{len(state['open'])} open position(s), "
           f"equity ${equity:,.2f}"
           + (" — PAUSED" if state["paused"] else ""),
           priority=2, tags="bar_chart")


# ---------------- STATUS / RESUME ----------------

def cmd_status():
    config = load_config()
    state = load_state()
    try:
        alp = Alpaca(config)
    except SystemExit as e:
        print(e)
        write_account_snapshot(state)
        return
    account = alp.account()
    equity, cash = float(account["equity"]), float(account["cash"])
    print(f"Paper equity  ${equity:,.2f}")
    print(f"Cash          ${cash:,.2f}")
    if state["window_start_equity"]:
        chg = (equity - state["window_start_equity"]) / \
              state["window_start_equity"] * 100
        print(f"Window        {chg:+.2f}% since {state['window_start_date']}")
    if state["paused"]:
        print(f"PAUSED: {state['pause_reason']}")
    print(f"\n{len(state['open'])} open position(s):")
    for t, p in state["open"].items():
        print(f"  {t:<6} {p['direction']:<5} {p['qty']} sh @ "
              f"{p['fill_price']}  stop {p['stop']}  target {p['target']}  "
              f"since {p['entry_date']}")
    print("\nRecent events:")
    for e in recent_events(10):
        extra = e.get("ticker") or ""
        reason = e.get("reason") or ""
        print(f"  {e['ts']}  {e['event']:<20} {extra:<6} {reason}")
    write_account_snapshot(state, equity, cash, alp.positions())


def cmd_resume(reason):
    if not reason or len(reason.strip()) < 10:
        sys.exit("Resuming needs a real written reason (this is the audit "
                 'trail): python3 bot.py resume --reason "..."')
    state = load_state()
    if not state["paused"]:
        sys.exit("Bot is not paused.")
    state["paused"] = False
    state["pause_reason"] = None
    # New evaluation window starts at resume — drawdown resets deliberately.
    state["window_start_equity"] = None
    state["window_start_date"] = None
    save_state(state)
    log_event("resumed", reason=reason)
    print("Resumed. A fresh evaluation window starts on the next run.")


def cmd_mark_missed(reason):
    """Record today as a missed entry day — but only once. Idempotent so an
    automated scheduler that fires late (Mac asleep at 9:31) can't create
    duplicate misses or clobber a day that actually traded."""
    today = datetime.now(ET).strftime("%Y-%m-%d")
    acted = {"entered", "missed_day", "unfilled", "partial_fill"}
    for e in recent_events(200):
        if e.get("date") == today and e.get("event") in acted:
            print(f"{today} already has a '{e['event']}' event — "
                  f"not logging a duplicate missed day.")
            return
    log_event("missed_day", reason=reason or "no reason given")
    print(f"Logged missed_day for {today}.")


# ---------------- entry point ----------------

def cmd_test_alert():
    if not _topic():
        sys.exit("No ntfy_topic in config.json (notifications section) — "
                 "notifications are off.")
    notify("Movers test", "If you can read this on your phone, "
           "notifications work. The bot will buzz you on fills, exits, "
           "and anything that needs eyes.", tags="wave")
    print(f"Test notification sent to ntfy.sh/{_topic()}")
    print("Nothing on the phone? Check the ntfy app is subscribed to "
          "exactly that topic name.")


def main():
    ap = argparse.ArgumentParser(description="Movers paper bot (rulebook v1.1)")
    ap.add_argument("command",
                    choices=["enter", "manage", "status", "resume",
                             "test-alert", "mark-missed"])
    ap.add_argument("--dry-run", action="store_true",
                    help="rehearse enter: decide everything, place nothing")
    ap.add_argument("--force", action="store_true",
                    help="bypass the 9:45 window gate (testing only)")
    ap.add_argument("--reason", default="", help="required for resume")
    args = ap.parse_args()

    if args.command == "enter":
        cmd_enter(dry_run=args.dry_run, force=args.force)
    elif args.command == "manage":
        cmd_manage()
    elif args.command == "status":
        cmd_status()
    elif args.command == "resume":
        cmd_resume(args.reason)
    elif args.command == "test-alert":
        cmd_test_alert()
    elif args.command == "mark-missed":
        cmd_mark_missed(args.reason)


if __name__ == "__main__":
    main()
