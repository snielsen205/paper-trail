#!/bin/bash
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# movers_news_run.sh — the SECOND EDGAR read of the morning.
#
# The 9:31 run scans the STANDING watchlist (edgar_watchlist.txt, 112 names):
# "did anything happen to the companies I follow?"
#
# This one scans TODAY'S MOVERS — the tickers on the morning top-10 board:
# "why did this name I have never heard of just move 200%?"
#
# Nobody pre-lists the small cap that is about to announce a merger. CLRO went
# +183% before the 9:31 scan even ran and was on no watchlist anywhere; its
# 8-K said "definitive agreement." This run finds that sentence in a minute
# instead of an hour.
#
# IT DOES NOT GET YOU IN EARLY. That move is over before the scan starts — the
# volume-spike study (918 events) found no reliable multi-day drift after this
# kind of event. This is for UNDERSTANDING what happened, not for chasing it.
#
# Scheduled 10:15 AM ET weekdays (com.movers.news) — safely after the morning
# run's hard 10:05 walk-away deadline, so it can never delay a trading decision.
# Read-only: no orders, no ledger writes. Logs to snapshots/cron_movers_news.log.
#
# Pass "selftest" to check the guards without scanning.

cd $HERE || exit 1
PY=/Library/Frameworks/Python.framework/Versions/3.14/bin/python3
LOG=snapshots/cron_movers_news.log
MODE="${1:-run}"

say() { echo "[$(TZ=America/New_York date '+%Y-%m-%d %H:%M:%S ET')] $*" | tee -a "$LOG"; }

say "--- movers news check fired (mode=$MODE) ---"

# Only meaningful if this morning actually produced a board. A stale board would
# re-scan yesterday's movers and report nothing new — harmless but pointless.
TODAY=$(TZ=America/New_York date '+%Y-%m-%d')
if ! ls snapshots/top10_"$TODAY"_*.json >/dev/null 2>&1; then
  say "no top10 board for $TODAY (holiday, or the morning run did not complete) — skipping."
  exit 0
fi

if [ "$MODE" = "selftest" ]; then
  say "selftest: board for $TODAY exists — WOULD scan its tickers. Stopping (no network)."
  exit 0
fi

say "scanning today's movers for filings (read-only)"
$PY edgar_watch.py --movers --days 3 >>"$LOG" 2>&1 || say "movers news scan failed (non-fatal)"
say "--- movers news check complete ---"
