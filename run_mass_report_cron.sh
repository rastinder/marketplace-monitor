#!/usr/bin/env bash
#
# Mass-report cron runner (NIGHT ONLY).
#
# schedule.py gates every tick: the job sleeps for 12 hours and works inside a
# 12 hour night window (nominal 20:00 open) whose wake time is randomised
# 45-80 minutes after the window opens, deterministically per calendar day.
# Sweeps are additionally spaced at least MIN_GAP_MIN apart so one night
# cannot turn into dozens of back-to-back runs.
#
# Runs mass_report.py once per permitted tick (one rotating search term per
# cycle, listings already reported are skipped via the fingerprint dedup
# table).
#
# Safety / locking:
#   * exclusive MRLOCK prevents overlapping mass-report runs
#   * the phone is a shared resource held for the WHOLE run by whichever
#     job drives it.  The 2h opencode job takes a shared MRLOCK hold for
#     its entire run, so waiting for it to end would starve this job
#     forever.  Instead, if an opencode run is active, this script stops
#     THAT run with SIGTERM (anchored pattern: only the timeout wrapper).
#     The opencode cron job itself is never stopped - it starts a fresh
#     run at its next 15-minute tick, after the phone lock frees.
#   * conversely the opencode runner takes a shared hold on MRLOCK and
#     skips its tick while we drive the phone, so the two UI drivers
#     never overlap.
#
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
LOG="$ROOT/data/mass-report.log"
OPLOCK=/tmp/marketplace-monitor-opencode.lock
MRLOCK=/tmp/marketplace-monitor-massreport.lock
OPCODE_BIN="${OPENCODE_BIN:-$HOME/.opencode/bin/opencode}"
OP_TIMEOUT_RE="^timeout --signal=TERM --kill-after=30 7200 ${OPCODE_BIN} run"

# Stop an active opencode phone-drive run so its (long) shared MRLOCK
# hold cannot starve this job.
if pgrep -f "$OP_TIMEOUT_RE" >/dev/null 2>&1; then
  pkill -TERM -f "$OP_TIMEOUT_RE" 2>/dev/null || true
  sleep 3
  # If its runner shell died earlier (orphaned run), nobody will clear
  # the window file - remove it so the opencode job's next tick starts
  # a fresh run instead of waiting out a stale 2h deadline.
  if ! pgrep -f 'run_opencode_2h\.sh' >/dev/null 2>&1; then
    rm -f "$ROOT/data/opencode-2h.window"
  fi
fi

# --- night-only gate -------------------------------------------------------
if ! python3 "$ROOT/schedule.py" >> "$LOG" 2>&1; then
  # sleeping: 12h window closed, or the randomised wake time has not arrived
  exit 0
fi

exec 9>"$MRLOCK"
flock -w 60 9 || exit 0
# the phone is ours from here: this sweep counts against the nightly spacing
python3 "$ROOT/schedule.py" --mark >> "$LOG" 2>&1
exec 8>> "$OPLOCK"
flock -w 60 -s 8 || exit 0   # opencode restarted mid-wait -> skip this tick

cd "$ROOT" || exit 1
rc=0
{
  echo "[$(date -Is)] run start"
  python3 mass_report.py --max-terms 0 --max-reports 0 || rc=$?
  echo "[$(date -Is)] exit=$rc"
} >> "$LOG" 2>&1
# propagate the real outcome (2 = preflight abort, e.g. Facebook logged out)
exit "$rc"
