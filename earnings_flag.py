"""
earnings_flag.py — pocket nudge for earnings-driven analyst verdicts.

After the analyst renders its daily verdicts (analyst.py run), this pushes a
phone alert (via the same ntfy notifier the bot uses) for any verdict on a name
whose earnings are the likely reason it's moving — i.e. it reported in the last
few days or reports within the week (from the cached AV earnings calendar). So
on a heavy earnings morning you get one buzz listing the graded verdicts, and
can open the Hedge Fund tab for the full bull/bear.

READ-ONLY. Places no orders. Fail-soft (a bad push can never break anything).
No earnings-name verdicts -> it stays silent. Wired into morning_run.sh after
the analyst run.
"""

import csv
import glob
import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP = os.path.join(HERE, "snapshots")
CACHE = os.path.join(HERE, "cache")
ET = ZoneInfo("America/New_York")

PAST_DAYS = 4      # catch names that just reported and are gapping now
AHEAD_DAYS = 7     # and names about to report


def earnings_window():
    """symbol -> reportDate for names reporting within [today-4, today+7]."""
    files = glob.glob(os.path.join(CACHE, "*EARNINGS_CALENDAR*.csv"))
    if not files:
        return {}
    today = datetime.now(ET).date()
    lo, hi = today - timedelta(days=PAST_DAYS), today + timedelta(days=AHEAD_DAYS)
    out = {}
    with open(max(files, key=os.path.getmtime)) as f:
        for r in csv.DictReader(f):
            sym = (r.get("symbol") or "").strip()
            raw = (r.get("reportDate") or "").strip()
            if not sym or not raw:
                continue
            try:
                d = datetime.strptime(raw, "%Y-%m-%d").date()
            except ValueError:
                continue
            if lo <= d <= hi:
                out[sym] = raw
    return out


def main():
    path = os.path.join(SNAP, "analyst_account.json")
    if not os.path.exists(path):
        print("earnings_flag: no analyst_account.json yet — nothing to flag.")
        return
    try:
        acct = json.load(open(path))
    except (json.JSONDecodeError, OSError):
        print("earnings_flag: could not read analyst_account.json.")
        return
    verdicts = acct.get("verdicts", [])
    if not verdicts:
        print("earnings_flag: no verdicts logged yet.")
        return

    board_date = verdicts[0].get("date")          # write_account sorts newest first
    today_verdicts = [v for v in verdicts if v.get("date") == board_date]
    earn = earnings_window()
    hits = [v for v in today_verdicts if v.get("ticker") in earn]
    if not hits:
        print(f"earnings_flag: no earnings-name verdicts on the {board_date} board.")
        return

    lines = []
    for v in hits:
        tk = v.get("ticker", "")
        brain = " AI" if v.get("brain") == "llm" else ""
        thesis = (v.get("thesis") or "")[:48]
        lines.append(f"{tk} {v.get('verdict')} {v.get('confidence')}%{brain} "
                     f"(reports {earn[tk]}) — {thesis}")
    msg = "\n".join(lines) + "\n\nOpen the Hedge Fund tab for the full bull/bear."
    title = f"{len(hits)} earnings mover{'s' if len(hits) != 1 else ''} — analyst verdicts"

    try:
        from bot import notify
        notify(title, msg, priority=4, tags="bar_chart")
    except Exception as e:                            # noqa: BLE001
        print(f"earnings_flag: notify failed (non-fatal): {e}")
    print(f"earnings_flag: flagged {len(hits)} — " + ", ".join(v.get("ticker", "") for v in hits))


if __name__ == "__main__":
    main()
