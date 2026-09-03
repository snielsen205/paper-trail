#!/bin/bash
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# morning_run.sh — the Movers morning sequence, fired by launchd at 9:31 AM
# ET on weekdays (com.movers.morning). Runs independently of any Claude
# session; the Mac just has to be awake.
#
# Guards, in order, so an automated run can never misbehave:
#   1. Time window: only scans if it's really 09:31-09:43 ET. If launchd
#      fired late (Mac was asleep at 9:31 and woke later), it logs a missed
#      day instead of scanning stale mid-morning data.
#   2. Market open: skips holidays (Alpaca clock says closed) with no log —
#      a holiday is a non-trading day, not a missed day.
# Everything it does is appended to snapshots/cron_morning.log.
#
# Pass "selftest" to run the guards and print decisions WITHOUT scanning,
# trading, or logging — used to verify the wiring safely.

cd $HERE || exit 1
PY=/Library/Frameworks/Python.framework/Versions/3.14/bin/python3
LOG=snapshots/cron_morning.log
MODE="${1:-run}"

say() { echo "[$(TZ=America/New_York date '+%Y-%m-%d %H:%M:%S ET')] $*" | tee -a "$LOG"; }

say "--- scheduled morning run fired (mode=$MODE) ---"

# 1) Time-of-day guard. Mac is on ET, so local time == ET. 10# forces
#    base-10 so a leading zero isn't read as octal.
HM=$((10#$(TZ=America/New_York date '+%H%M')))
if [ "$HM" -lt 931 ] || [ "$HM" -gt 943 ]; then
  say "outside the 09:31-09:43 scan window (now $HM). Mac was likely asleep at the open."
  if [ "$MODE" = "selftest" ]; then
    say "selftest: WOULD log a missed day (idempotent) and exit."
  else
    say "logging missed day (not scanning/trading late, per rulebook 14):"
    $PY bot.py mark-missed --reason "Scheduled run fired at $(TZ=America/New_York date '+%H:%M') ET, outside the 9:31 entry window (Mac likely asleep at the open). Not entering late." 2>&1 | tee -a "$LOG"
  fi
  exit 0
fi

# 2) Market-open guard (holidays). If the check errors, we skip rather than
#    risk trading on bad data — a safe failure.
if $PY -c "import sys; from av_client import load_config; from alpaca_client import Alpaca; sys.exit(0 if Alpaca(load_config()).clock().get('is_open') else 7)" 2>>"$LOG"; then
  say "market is open."
else
  say "market not open (holiday, or clock check failed) — skipping cleanly, no scan, no trade."
  exit 0
fi

if [ "$MODE" = "selftest" ]; then
  say "selftest: in-window and market open — WOULD run scan -> score -> track_record log -> bot enter. Stopping here (no side effects)."
  exit 0
fi

# Hold the Mac awake for the whole run. bot.py enter sleeps internally from
# 9:31 to 9:45, then polls to 10:05 — an idle-sleep in that gap would freeze
# it. `-w $$` ties the wake-lock to THIS script: it releases automatically
# when the run ends. No effect if the Mac is on battery + lid closed.
caffeinate -i -s -w $$ &
say "wake-lock held for the duration of the run (caffeinate -w $$)"

# 3) The real sequence. bot.py enter waits until 9:45 internally, then works
#    the entry window to 10:05, so this script runs ~9:32 through ~10:05.
say "running scan.py"
$PY scan.py >>"$LOG" 2>&1 || { say "scan.py failed — aborting morning run."; exit 1; }
say "running score.py"
$PY score.py >>"$LOG" 2>&1 || { say "score.py failed — aborting morning run."; exit 1; }
say "running track_record.py log"
$PY track_record.py log >>"$LOG" 2>&1
say "running analyst.py run (shadow verdicts, no orders)"
$PY analyst.py run >>"$LOG" 2>&1 || say "analyst run failed (non-fatal)"
say "rebuilding analyst_book.py (simulated book — no orders, no broker)"
$PY analyst_book.py run >>"$LOG" 2>&1 || say "analyst_book failed (non-fatal)"
say "running earnings_flag.py (phone nudge for earnings-mover verdicts)"
$PY earnings_flag.py >>"$LOG" 2>&1 || say "earnings_flag failed (non-fatal)"
say "running bot.py enter (waits for 9:45, works window to 10:05)"
$PY bot.py enter >>"$LOG" 2>&1
# Market-open manage: execute any due 5-day time-stops AT THE OPEN (rulebook
# 7 — a time-stopped position exits at the next open, which needs the market
# open) and log exits that resolved overnight. Evening run does end-of-day.
say "running bot.py manage (time-stops + exit logging, market open)"
$PY bot.py manage >>"$LOG" 2>&1 || say "manage failed (non-fatal)"
# Read-only: refresh the momentum sleeve's dashboard snapshot (live P&L,
# no orders). Keeps the Momentum tab current between monthly rebalances.
say "refreshing momentum snapshot (read-only)"
$PY momentum_sleeve.py --refresh >>"$LOG" 2>&1 || say "momentum refresh failed (non-fatal)"
say "refreshing macro snapshot (read-only)"
$PY macro_sleeve.py --refresh >>"$LOG" 2>&1 || say "macro refresh failed (non-fatal)"
# Read-only SEC filing monitor: 13D control-intent stakes, tender/merger forms,
# "strategic alternatives" 8-Ks, and Form 15/25 delistings on the quality
# universe. Public filings only, no orders, deduped so it buzzes once per
# filing. Catches pre-market 8-Ks the same day; after-close ones next morning.
say "running edgar_watch.py (read-only SEC filing monitor)"
$PY edgar_watch.py >>"$LOG" 2>&1 || say "edgar_watch failed (non-fatal)"
say "--- morning run complete ---"
