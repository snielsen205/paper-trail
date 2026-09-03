"""
analyst.py — the first "hedge fund" agent: a shadow analyst. It reads the day's
Top-10 board, writes a genuine BULL case and BEAR case for each name, renders a
VERDICT (bullish / bearish / neutral) with a confidence, and logs it to a
gradeable ledger. It places NO orders — ever. It is a second opinion that gets
graded against SPY, exactly like the screener's picks, so we can find out
whether an agent's judgment beats the market BEFORE trusting it with anything.

  python3 analyst.py run     analyze today's Top-10 -> verdicts logged
  python3 analyst.py grade   grade past verdicts at +5 trading days vs SPY
  python3 analyst.py report  print the shadow track record

THE BRAIN comes in two forms and the ledger/grading/tab are identical for both:
  - LLM brain (analyze_llm): a real Claude call reads the pick's data and writes
    the bull case, bear case, and verdict. Active only when an Anthropic API key
    is present in config.json ("anthropic" block). Pay-per-use, opt-in.
  - rule brain (analyze_v1): deterministic synthesis of the signals we already
    compute (score, direction, momentum, volatility, news sentiment, options
    flow, earnings). The automatic fallback when there's no key — or if any LLM
    call hiccups — so a scheduled run can never break on the analyst.
Either brain can DISAGREE with the mechanical bot (e.g. a strong score on a name
already up 40% -> analyst turns cautious). Every verdict is graded vs SPY at +5
trading days regardless of which brain produced it; each row records its "brain".

SHADOW ONLY. Nothing here trades. The verdict must earn its keep vs SPY first.
"""

import glob
import json
import os
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import claude_client
from av_client import load_config, AlphaVantage

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP = os.path.join(HERE, "snapshots")
LOG = os.path.join(SNAP, "analyst_log.jsonl")
ACCOUNT = os.path.join(SNAP, "analyst_account.json")
COSTS = os.path.join(SNAP, "analyst_costs.jsonl")
ET = ZoneInfo("America/New_York")

# Token usage accumulated across THIS run's LLM calls. The brain is pay-per-use,
# so every run records what it spent — an agent that can't tell you its own
# running cost is one you can't decide to keep or kill.
_RUN_USAGE = {"calls": 0, "input_tokens": 0, "output_tokens": 0}


def load_costs():
    if not os.path.exists(COSTS):
        return []
    out = []
    with open(COSTS) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


def cost_summary(cfg=None):
    """Lifetime + per-run LLM spend, read from the cost ledger."""
    rows = load_costs()
    total = sum(r.get("cost_usd", 0.0) for r in rows)
    calls = sum(r.get("calls", 0) for r in rows)
    tin = sum(r.get("input_tokens", 0) for r in rows)
    tout = sum(r.get("output_tokens", 0) for r in rows)
    model = rows[-1].get("model") if rows else (
        (cfg or {}).get("anthropic", {}).get("model"))
    return {
        "total_usd": round(total, 4),
        "runs": len(rows),
        "calls": calls,
        "input_tokens": tin,
        "output_tokens": tout,
        "avg_per_run_usd": round(total / len(rows), 4) if rows else 0.0,
        "avg_per_call_usd": round(total / calls, 4) if calls else 0.0,
        "last_run_usd": round(rows[-1].get("cost_usd", 0.0), 4) if rows else 0.0,
        "model": model,
        # A trading month is ~21 sessions; this is what the brain runs at.
        "projected_monthly_usd": round((total / len(rows)) * 21, 2) if rows else 0.0,
    }


def record_run_cost(cfg, board_date):
    """Append this run's spend to the cost ledger. No LLM calls -> no row."""
    if not _RUN_USAGE["calls"]:
        return None
    usd = claude_client.price(cfg, _RUN_USAGE["input_tokens"],
                              _RUN_USAGE["output_tokens"])
    row = {
        "ts": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"),
        "board": board_date,
        "model": (cfg.get("anthropic") or {}).get("model"),
        "calls": _RUN_USAGE["calls"],
        "input_tokens": _RUN_USAGE["input_tokens"],
        "output_tokens": _RUN_USAGE["output_tokens"],
        "cost_usd": round(usd, 6),
    }
    os.makedirs(SNAP, exist_ok=True)
    with open(COSTS, "a") as f:
        f.write(json.dumps(row) + "\n")
    return row


def latest_top10():
    files = sorted(glob.glob(os.path.join(SNAP, "top10_*.json")))
    if not files:
        return None, None
    name = os.path.basename(files[-1])
    return json.load(open(files[-1])), name.split("_")[1]


def load_ledger():
    if not os.path.exists(LOG):
        return []
    out = []
    with open(LOG) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


# ---------------- the analyst's brain ----------
# analyze()      -> dispatcher: LLM if a key is configured, else rule-based.
# analyze_v1()   -> the rule-based brain (also the fail-soft fallback).
# analyze_llm()  -> the Claude-powered brain.


def analyze(p, cfg=None):
    """Pick the brain and return (verdict_dict, brain_used).

    Uses the LLM brain when an Anthropic key is configured AND anthropic.brain
    isn't set to "rule"; otherwise the rule-based brain. ALWAYS fail-soft: any
    LLM error falls back to rule-based, so a scheduled run never dies on the
    analyst. The returned dict has the same shape from either brain."""
    cfg = cfg or {}
    brain_pref = (cfg.get("anthropic") or {}).get("brain", "llm")
    if brain_pref != "rule" and claude_client.have_key(cfg):
        try:
            return analyze_llm(p, cfg), "llm"
        except claude_client.ClaudeError as e:
            print(f"    (LLM brain unavailable on {p.get('ticker')}: {e} — using rule-based)")
    return analyze_v1(p), "rule"


def analyze_v1(p):
    """Weigh the evidence into a bull case, a bear case, and a verdict.
    Returns a dict. Deterministic and explainable — a real analyst weighing
    the same signals. Also the automatic fallback when the LLM brain is off."""
    why = p.get("why", "")
    chg = p.get("change_pct", 0.0)
    score = p.get("score", 0)
    atr_pct = (p.get("atr_pct_of_price") or 0) * 100

    m = re.search(r"avg sentiment ([+-]?\d+\.?\d*)", why)
    sentiment = float(m.group(1)) if m else None
    bullish_flow = "bullish flow" in why
    bearish_flow = "bearish flow" in why
    has_earnings = "earnings" in why and "catalyst" in why
    conflict = "CONFLICT" in why
    too_volatile = "TOO VOLATILE" in why

    bull, bear, lean = [], [], 0
    if score >= 80:
        bull.append(f"high-conviction screen score ({score})"); lean += 2
    elif score >= 65:
        bull.append(f"solid screen score ({score})"); lean += 1
    if p.get("direction") == "LONG":
        bull.append("mechanical read leans long"); lean += 1
    elif p.get("direction") == "SHORT":
        bear.append("mechanical read leans short"); lean -= 1
    if sentiment is not None and sentiment >= 0.2:
        bull.append(f"positive news tone ({sentiment:+.2f})"); lean += 1
    elif sentiment is not None and sentiment <= -0.1:
        bear.append(f"negative news tone ({sentiment:+.2f})"); lean -= 1
    if bullish_flow:
        bull.append("options flow bullish"); lean += 1
    if bearish_flow:
        bear.append("options flow bearish"); lean -= 1
    if has_earnings:
        bull.append("scheduled earnings catalyst"); lean += 1
    if abs(chg) >= 30:
        bear.append(f"already moved {chg:+.0f}% — exhaustion risk"); lean -= 2
    elif 3 <= chg < 30:
        bull.append(f"healthy momentum ({chg:+.0f}%)")
    if atr_pct >= 15:
        bear.append(f"very volatile (ATR {atr_pct:.0f}% of price)"); lean -= 1
    if conflict:
        bear.append("news and options disagree"); lean -= 1
    if too_volatile:
        bear.append("flagged too volatile to bracket"); lean -= 1
    if not bull:
        bull.append("no clear bullish evidence")
    if not bear:
        bear.append("no clear bearish evidence")

    if lean >= 2:
        verdict = "BULLISH"
    elif lean <= -2:
        verdict = "BEARISH"
    else:
        verdict = "NEUTRAL"
    confidence = min(95, 45 + abs(lean) * 11)

    mech = p.get("direction", "WATCH")
    agrees = ((verdict == "BULLISH" and mech == "LONG")
              or (verdict == "BEARISH" and mech == "SHORT")
              or (verdict == "NEUTRAL" and mech == "WATCH"))
    lead = bull[0] if verdict == "BULLISH" else bear[0] if verdict == "BEARISH" else "signals are mixed"
    thesis = f"{verdict.title()} ({confidence}%): {lead}."
    return {"verdict": verdict, "confidence": confidence, "lean": lean,
            "bull": bull, "bear": bear, "thesis": thesis,
            "agrees_with_bot": agrees}


# ---------------- the LLM brain (Claude) ----------

# REBALANCED 2026-07-30. The original wording leaned bearish by construction —
# it called a bullish read "cheerleading", named only exhaustion as a risk, and
# offered NEUTRAL as an explicit way out. Result: across 7/27-7/30 the LLM brain
# produced 0 BULLISH out of 29 verdicts, while the rule brain found 3-5 bullish
# names a day on the IDENTICAL boards. That is a biased instrument, not a bearish
# market. The rewrite keeps every exhaustion warning and adds the missing
# symmetry; it does not make the analyst optimistic, it stops the prompt from
# picking the answer. Safe to change now because ZERO verdicts had graded yet —
# no track record was invalidated.
LLM_SYSTEM = (
    "You are a disciplined equity analyst for a PAPER shadow book. Every name "
    "you see is a top DAILY MOVER — it already made a large move today — so your "
    "job is to judge what happens NEXT: weigh genuine continuation against "
    "exhaustion and mean-reversion. Both are real, both are common, and which one "
    "applies depends on the evidence in front of you. You never place trades.\n\n"
    "HOW YOU ARE SCORED, precisely: your verdict is measured against SPY at +5 "
    "trading days. BULLISH wins only if the stock BEATS SPY; BEARISH wins only if "
    "it LAGS SPY. This is a RELATIVE call, not a directional one — a stock that "
    "rises 2% while SPY rises 4% makes a BEARISH call correct, and a stock that "
    "falls 1% while SPY falls 3% makes a BULLISH call correct.\n\n"
    "CALIBRATION — read this as carefully as you read the data:\n"
    "* A big move is not by itself a reason to fade. Movers with a real catalyst "
    "(earnings, guidance, a contract, an approval, a sector repricing) frequently "
    "keep outperforming for days. Movers with no identifiable catalyst, a thin "
    "float, or a low-quality spike are the ones that typically give it back.\n"
    "* Reflexive bearishness on every mover is a bias, not discipline — and so is "
    "cheerleading. Both produce a worthless record.\n"
    "* NEUTRAL is for evidence that is genuinely balanced, not a way to avoid "
    "committing. A verdict that is always NEUTRAL teaches nothing and scores "
    "nothing. If the evidence leans, say so.\n"
    "* Put your uncertainty in the confidence number, not in refusing to call: a "
    "weak lean is a call at 55-65, not a NEUTRAL.\n\n"
    "Be honest and specific: cite the actual numbers you were given, and name the "
    "one fact that would change your mind.\n\n"
    "Respond with ONLY a JSON object, no markdown fences and no prose around it:\n"
    '{"verdict": "BULLISH|BEARISH|NEUTRAL", "confidence": <integer 0-100>, '
    '"bull": ["short point", ...], "bear": ["short point", ...], '
    '"thesis": "one sentence, <=160 chars"}'
)


def _build_user(p):
    """Hand the model the same facts the rule brain sees — plus the raw board
    'why' string, which already summarizes news tone, options flow, earnings."""
    atr_pct = (p.get("atr_pct_of_price") or 0) * 100
    facts = {
        "ticker": p.get("ticker"),
        "price": p.get("price"),
        "change_today_pct": p.get("change_pct"),
        "screen_score_0_100": p.get("score"),
        "mechanical_direction": p.get("direction"),
        "atr_pct_of_price": round(atr_pct, 1),
        "screener_notes": p.get("why", ""),
    }
    return (
        "Today's mover to judge (all figures are as-of the screen):\n"
        + json.dumps(facts, indent=2)
        + "\n\nWrite the bull case, the bear case, a verdict, a confidence, and a "
        "one-sentence thesis. JSON only."
    )


def _parse_verdict(raw):
    """Defensively pull the JSON object out of the model's reply and validate
    it. Uses raw_decode, which parses the FIRST complete JSON object and
    ignores any trailing prose/extra line the model sometimes adds — that stray
    trailing data used to force a needless fallback to the rule brain. Raises
    ClaudeError only on genuinely malformed output so the caller falls back."""
    i = raw.find("{")
    if i == -1:
        raise claude_client.ClaudeError("no JSON object in reply")
    body = raw[i:]
    try:
        obj, _ = json.JSONDecoder().raw_decode(body)
    except json.JSONDecodeError as e:
        # Second chance: strip trailing commas before a closing brace/bracket.
        # Strict JSON forbids them, models emit them occasionally, and losing a
        # whole verdict to one stray comma is a waste of a paid call (SOXL,
        # 2026-07-30). Same spirit as the raw_decode hardening on 7/27.
        try:
            obj, _ = json.JSONDecoder().raw_decode(
                re.sub(r",(\s*[}\]])", r"\1", body))
        except json.JSONDecodeError:
            raise claude_client.ClaudeError(f"bad JSON: {e}")

    v = str(obj.get("verdict", "")).upper().strip()
    if v not in ("BULLISH", "BEARISH", "NEUTRAL"):
        raise claude_client.ClaudeError(f"bad verdict {v!r}")
    try:
        conf = max(0, min(100, int(round(float(obj.get("confidence", 0))))))
    except (TypeError, ValueError):
        raise claude_client.ClaudeError("bad confidence")

    def clean(x):
        if isinstance(x, str):
            x = [x]
        return [str(i).strip() for i in (x or []) if str(i).strip()]

    bull = clean(obj.get("bull")) or ["no clear bullish evidence"]
    bear = clean(obj.get("bear")) or ["no clear bearish evidence"]
    thesis = str(obj.get("thesis", "")).strip() or f"{v.title()} ({conf}%)."
    return {"verdict": v, "confidence": conf, "bull": bull, "bear": bear,
            "thesis": thesis}


def analyze_llm(p, cfg):
    """Claude reads the pick and returns a verdict in the same shape analyze_v1
    produces. Raises ClaudeError on any failure (the dispatcher handles it).
    Token usage is accumulated into _RUN_USAGE so the run can price itself —
    counted even if parsing then fails, because the call was still billed."""
    raw, usage = claude_client.complete(cfg, LLM_SYSTEM, _build_user(p),
                                        max_tokens=900)
    _RUN_USAGE["calls"] += 1
    _RUN_USAGE["input_tokens"] += usage.get("input_tokens", 0)
    _RUN_USAGE["output_tokens"] += usage.get("output_tokens", 0)
    o = _parse_verdict(raw)
    v, conf = o["verdict"], o["confidence"]
    sign = 1 if v == "BULLISH" else -1 if v == "BEARISH" else 0
    lean = sign * max(1, round((conf - 45) / 11)) if sign else 0  # cosmetic; grading ignores it
    mech = p.get("direction", "WATCH")
    agrees = ((v == "BULLISH" and mech == "LONG")
              or (v == "BEARISH" and mech == "SHORT")
              or (v == "NEUTRAL" and mech == "WATCH"))
    return {"verdict": v, "confidence": conf, "lean": lean,
            "bull": o["bull"], "bear": o["bear"], "thesis": o["thesis"],
            "agrees_with_bot": agrees}


# ---------------- commands ----------------

def cmd_run():
    cfg = load_config()
    picks, snap_date = latest_top10()
    if picks is None:
        sys.exit("No top10 snapshot yet — run scan.py + score.py first.")
    today = datetime.now(ET).strftime("%Y-%m-%d")
    if snap_date != today:
        print(f"NOTE: latest board is {snap_date}, not today. Analyzing it anyway.")

    brain_pref = (cfg.get("anthropic") or {}).get("brain", "llm")
    using_llm = brain_pref != "rule" and claude_client.have_key(cfg)
    brain_label = "Claude LLM" if using_llm else "rule-based"

    already = {(r["date"], r["ticker"]) for r in load_ledger()}
    written = 0
    print(f"\n  ANALYST — {snap_date} board  (brain: {brain_label})\n" + "-" * 60)
    with open(LOG, "a") as f:
        for p in picks:
            t = p["ticker"]
            if (snap_date, t) in already:
                continue
            a, brain = analyze(p, cfg)
            row = {"date": snap_date, "ts": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"),
                   "ticker": t, "entry_price": p.get("price"),
                   "mech_dir": p.get("direction"), "mech_score": p.get("score"),
                   "brain": brain,
                   "graded": False, "return_pct": None, "spy_return_pct": None,
                   "beat_spy": None, **a}
            f.write(json.dumps(row) + "\n")
            written += 1
            tag = "=bot" if a["agrees_with_bot"] else "!=bot"
            mark = "AI" if brain == "llm" else "··"
            print(f"  {mark} {t:<6} {a['verdict']:<8} {a['confidence']:>3}%  {tag:<5} {a['thesis'][:56]}")
    print(f"\n  {written} verdict(s) logged (shadow — no orders).")
    run = record_run_cost(cfg, snap_date)
    if run:
        c = cost_summary(cfg)
        print(f"  LLM cost this run: ${run['cost_usd']:.4f} "
              f"({run['calls']} calls, {run['input_tokens']:,} in / "
              f"{run['output_tokens']:,} out)")
        print(f"  Lifetime: ${c['total_usd']:.2f} over {c['runs']} run(s) "
              f"— ~${c['projected_monthly_usd']:.2f}/month at this rate")
    write_account(cfg)


def spy_and_stock(av, ticker, start, end):
    def series(sym):
        try:
            d = av.daily_adjusted(sym).get("Time Series (Daily)", {})
            days = sorted(x for x in d if start <= x <= end)
            if len(days) < 2:
                return None
            a = float(d[days[0]]["5. adjusted close"])
            b = float(d[days[-1]]["5. adjusted close"])
            return (b - a) / a * 100
        except (RuntimeError, KeyError, ValueError):
            return None
    return series(ticker), series("SPY")


def trading_days_since(date_str, n_needed=5):
    """Rough: calendar-day proxy is not enough; use business-day count."""
    from datetime import date
    y, m, d = map(int, date_str.split("-"))
    start = date(y, m, d)
    today = datetime.now(ET).date()
    bdays = sum(1 for i in range((today - start).days)
                if (start.__class__.fromordinal(start.toordinal() + i + 1)).weekday() < 5)
    return bdays


def cmd_grade():
    cfg = load_config()
    av = AlphaVantage(cfg)
    ledger = load_ledger()
    n_days = cfg.get("track_record", {}).get("grade_after_trading_days", 5)
    today = datetime.now(ET).strftime("%Y-%m-%d")
    graded_now = 0
    for r in ledger:
        if r.get("graded") or r.get("verdict") == "NEUTRAL":
            continue
        if trading_days_since(r["date"]) < n_days:
            continue
        stock_ret, spy_ret = spy_and_stock(av, r["ticker"], r["date"], today)
        if stock_ret is None or spy_ret is None:
            continue
        # bullish "wins" if the stock beat SPY; bearish if it lagged SPY
        excess = stock_ret - spy_ret
        directional = excess if r["verdict"] == "BULLISH" else -excess
        r["return_pct"] = round(stock_ret, 2)
        r["spy_return_pct"] = round(spy_ret, 2)
        r["beat_spy"] = directional > 0
        r["graded"] = True
        r["graded_date"] = today
        graded_now += 1
    with open(LOG, "w") as f:
        for r in ledger:
            f.write(json.dumps(r) + "\n")
    print(f"Graded {graded_now} verdict(s) this run.")
    write_account(cfg)


def stats(ledger):
    graded = [r for r in ledger if r.get("graded")]
    calls = [r for r in ledger if r.get("verdict") != "NEUTRAL"]
    beat = [r for r in graded if r.get("beat_spy")]
    return {
        "total": len(ledger), "calls": len(calls),
        "neutral": sum(1 for r in ledger if r.get("verdict") == "NEUTRAL"),
        "graded": len(graded),
        "pending": sum(1 for r in calls if not r.get("graded")),
        "beat_spy_pct": round(len(beat) / len(graded) * 100) if graded else None,
        "avg_excess": round(sum((r["return_pct"] - r["spy_return_pct"]) *
                                (1 if r["verdict"] == "BULLISH" else -1)
                                for r in graded) / len(graded), 2) if graded else None,
    }


def write_account(cfg=None):
    ledger = load_ledger()
    recent = sorted(ledger, key=lambda r: (r["date"], r.get("ts", "")), reverse=True)[:40]
    json.dump({
        "updated": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"),
        "stats": stats(ledger), "verdicts": recent,
        "cost": cost_summary(cfg),
    }, open(ACCOUNT, "w"), indent=2)


def cmd_report():
    s = stats(load_ledger())
    print(json.dumps(s, indent=2))


def cmd_cost():
    """What the LLM brain has spent. Pair this with the beat-SPY number: an
    agent is worth keeping only if what it costs buys something measurable."""
    cfg = load_config()
    c = cost_summary(cfg)
    if not c["runs"]:
        print("No LLM runs recorded yet (rule-based brain is free).")
        return
    s = stats(load_ledger())
    print(f"\n  ANALYST LLM COST — model {c['model']}\n" + "-" * 52)
    print(f"    lifetime          ${c['total_usd']:.2f}")
    print(f"    runs / calls      {c['runs']} / {c['calls']}")
    print(f"    tokens            {c['input_tokens']:,} in / {c['output_tokens']:,} out")
    print(f"    per run           ${c['avg_per_run_usd']:.4f}")
    print(f"    per verdict       ${c['avg_per_call_usd']:.4f}")
    print(f"    projected monthly ${c['projected_monthly_usd']:.2f}  (~21 sessions)")
    print("-" * 52)
    print(f"    graded so far     {s['graded']} | beat SPY "
          f"{s['beat_spy_pct'] if s['beat_spy_pct'] is not None else '—'}%")
    print("    Judge the spend against that number, not against how good the "
          "write-ups read.\n")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    {"run": cmd_run, "grade": cmd_grade, "report": cmd_report,
     "cost": cmd_cost}.get(cmd, lambda: print(__doc__))()
