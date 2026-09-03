#!/bin/bash
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# evening_run.sh — after-close upkeep, fired by launchd (com.movers.evening)
# at 4:15 PM ET on weekdays. This is the half of the daily cycle that was
# MISSING: without it, brackets that resolved during the day never got logged,
# the 5-day time-stop never fired, and screener picks never graded.
#
#   bot.py manage         -> log the day's stop/target exits, reconcile the
#                            movers ledger vs Alpaca, check the circuit breaker
#   track_record.py grade -> grade any picks that reached +5 trading days vs SPY
#
# Both are read/log-only and idempotent (safe to re-run; they never place new
# entries), so no time-window guard is needed — weekday scheduling + the fact
# that they no-op on quiet days is enough. Logs to snapshots/cron_evening.log.

cd $HERE || exit 1
PY=/Library/Frameworks/Python.framework/Versions/3.14/bin/python3
LOG=snapshots/cron_evening.log

say() { echo "[$(TZ=America/New_York date '+%Y-%m-%d %H:%M:%S ET')] $*" | tee -a "$LOG"; }

say "--- evening run fired ---"
caffeinate -i -s -w $$ &

say "running bot.py manage (exits + reconcile + circuit breaker)"
$PY bot.py manage >>"$LOG" 2>&1 || say "manage failed (non-fatal)"

say "running track_record.py grade (grade picks at +5 trading days)"
$PY track_record.py grade >>"$LOG" 2>&1 || say "grade failed (non-fatal)"

say "running analyst.py grade (grade shadow verdicts vs SPY)"
$PY analyst.py grade >>"$LOG" 2>&1 || say "analyst grade failed (non-fatal)"

# Rebuild the book AFTER grading so the day's closes are in: this is what
# fires the 5-day time-stops and marks open positions. Rebuilt from scratch
# every time (pure function of the verdict ledger + prices), so re-running it
# can never double-count.
say "rebuilding analyst_book.py (simulated book — exits + marks, no orders)"
$PY analyst_book.py run >>"$LOG" 2>&1 || say "analyst_book failed (non-fatal)"

say "--- evening run complete ---"
