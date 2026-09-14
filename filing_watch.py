"""
filing_watch.py — wait for ONE specific EDGAR filing to appear, then push.

edgar_watch.py scans a whole universe for deal signals. This is the opposite:
a short list of "tell me the moment THIS company files THIS form," for events
with a known date-shaped answer and a real consequence.

First target is the USA Rare Earth resale registration. USAR filed two S-3s on
2026-09-04 covering 126,476,950 shares issued in the Serra Verde merger, of
which 53,150,925 carry NO contractual lock-up. Those shares cannot be sold
until the SEC declares the registration effective, and EDGAR publishes that as
a form type "EFFECT". As of 2026-09-08 no EFFECT has posted. This issuer's own
history puts the lag at a median of 16 days (their last S-3 took 23), so the
notice most likely lands ~Sep 20-27 — but one of their registrations took 128
days, so the point is to watch rather than guess.

The day it posts is the day ~4.2 days of average volume becomes sellable.

READ-ONLY. Places no orders, touches no ledger, shares no state with bot.py.
Fail-soft: any network or parse error prints and exits 0, so a launchd job
can never turn into a pager storm.

Run:
  python3 filing_watch.py --list     # show targets, no network
  python3 filing_watch.py            # check; push on a new hit
  python3 filing_watch.py --test     # prove the phone push works
  python3 filing_watch.py --quiet    # check, print, don't push
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
TARGETS = os.path.join(HERE, "filing_watchlist.json")
SEEN = os.path.join(HERE, "snapshots", "filing_watch_seen.json")
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"


def contact():
    """SEC requires a real contact string in the User-Agent."""
    try:
        cfg = json.load(open(os.path.join(HERE, "config.json")))
        return cfg.get("sec_contact") or "soren.nielsen0517@outlook.com"
    except Exception:
        return "soren.nielsen0517@outlook.com"


def push(title, message, priority=4, tags="rotating_light"):
    """Reuse the movers ntfy topic. Never allowed to raise."""
    try:
        cfg = json.load(open(os.path.join(HERE, "config.json")))
        topic = (cfg.get("notifications") or {}).get("ntfy_topic")
        if not topic:
            print("  (no ntfy topic configured; skipping push)")
            return
        req = urllib.request.Request(
            "https://ntfy.sh/" + topic, data=message.encode("utf-8"),
            headers={"Title": title, "Priority": str(priority), "Tags": tags},
            method="POST")
        urllib.request.urlopen(req, timeout=5).read()
        print("  pushed to phone.")
    except Exception as e:                                    # noqa: BLE001
        print(f"  (phone push failed, continuing: {e})")


def load_targets():
    with open(TARGETS) as fh:
        return json.load(fh)


def load_seen():
    try:
        return json.load(open(SEEN))
    except Exception:
        return {}


def save_seen(seen):
    os.makedirs(os.path.dirname(SEEN), exist_ok=True)
    tmp = SEEN + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(seen, fh, indent=2)
    os.replace(tmp, SEEN)


def fetch(cik):
    url = SUBMISSIONS.format(cik=str(cik).zfill(10))
    req = urllib.request.Request(url, headers={"User-Agent": contact()})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:                                # noqa: BLE001
            if attempt == 2:
                print(f"  EDGAR fetch failed for CIK {cik}: {e}")
                return None
            time.sleep(2 * (attempt + 1))


def check(t, seen, quiet):
    key = f"{t['cik']}:{','.join(t['forms'])}:{t['since']}"
    if seen.get(key):
        print(f"  {t['ticker']}: already reported on {seen[key]} — nothing to do.")
        return False
    d = fetch(t["cik"])
    if not d:
        return False
    r = d["filings"]["recent"]
    hits = [(dt, f, acc) for dt, f, acc
            in zip(r["filingDate"], r["form"], r["accessionNumber"])
            if f in t["forms"] and dt >= t["since"]]
    if not hits:
        days = (date.today() - date.fromisoformat(t["since"])).days
        print(f"  {t['ticker']}: no {'/'.join(t['forms'])} yet "
              f"({days} days since {t['since']}).")
        return False

    dt, f, acc = sorted(hits)[0]
    url = (f"https://www.sec.gov/Archives/edgar/data/"
           f"{int(t['cik'])}/{acc.replace('-', '')}/")
    print(f"  *** {t['ticker']}: {f} FILED {dt} ***\n  {url}")
    if not quiet:
        push(f"{t['ticker']}: {f} filed",
             f"{t['name']}\n\nFiled {dt}\n{url}\n\n{t.get('note','')}")
    seen[key] = dt
    return True


def main():
    p = argparse.ArgumentParser(description="watch for one specific EDGAR form")
    p.add_argument("--list", action="store_true", help="show targets, no network")
    p.add_argument("--test", action="store_true", help="send a test push")
    p.add_argument("--quiet", action="store_true", help="check but don't push")
    a = p.parse_args()

    if a.test:
        push("filing_watch test", "If you see this, the watcher can reach your "
             "phone.", priority=3, tags="white_check_mark")
        return

    targets = load_targets()
    if a.list:
        for t in targets:
            print(f"  {t['ticker']:6} CIK {t['cik']}  forms={'/'.join(t['forms'])}"
                  f"  since {t['since']}\n    {t['name']}")
        return

    print(f"filing_watch — {date.today().isoformat()}")
    seen = load_seen()
    if any(check(t, seen, a.quiet) for t in list(targets)):
        save_seen(seen)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:                                    # noqa: BLE001
        print(f"filing_watch failed soft: {e}")
        sys.exit(0)
