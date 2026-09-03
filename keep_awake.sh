#!/bin/bash
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# keep_awake.sh — companion to the Movers trader. Holds the Mac awake through
# the morning trading window so it can't idle-sleep right when the bot needs
# to run. Fired by launchd (com.movers.awake) at 9:15 AM ET on weekdays;
# runs until 10:10 AM ET, then releases.
#
# IMPORTANT limit: this prevents *idle* sleep on an already-awake Mac. It
# canNOT wake a Mac that is already asleep (only `sudo pmset repeat wake`
# does that), and it can't beat a closed laptop lid on battery.

PY=/Library/Frameworks/Python.framework/Versions/3.14/bin/python3
LOG=$HERE/snapshots/cron_morning.log
SECS=$($PY -c "from datetime import datetime; from zoneinfo import ZoneInfo; now=datetime.now(ZoneInfo('America/New_York')); tgt=now.replace(hour=10,minute=10,second=0,microsecond=0); s=int((tgt-now).total_seconds()); print(s if s>0 else 0)")

echo "[$(TZ=America/New_York date '+%Y-%m-%d %H:%M:%S ET')] keep_awake: holding wake-lock ${SECS}s (until 10:10 ET)" >> "$LOG"
[ "$SECS" -gt 0 ] && exec caffeinate -i -s -t "$SECS"
