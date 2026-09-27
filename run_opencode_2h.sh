#!/usr/bin/env bash
#
# 2-hour opencode monitoring job runner (read-only research, local files only).
#
# Cron ticks every 15 min:
#   * window file alive            -> another run is in progress, exit 0
#   * window expired               -> clear it and start a fresh 2h window
#   * no window                    -> start a fresh 2h window
# The runner holds an exclusive lock for the whole 2h run; the mass-report
# runner takes a shared hold on this lock and skips its tick while we run,
# and we take a shared hold on theirs so the two never drive the phone at
# the same time.  There is intentionally NO permanent stop file: the job
# restarts every time its window ends (cron job does not stop).
#
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
WINDOW="$ROOT/data/opencode-2h.window"
LOCK=/tmp/marketplace-monitor-opencode.lock
MRLOCK=/tmp/marketplace-monitor-massreport.lock
LOG="$ROOT/data/opencode-2h.log"
NOW=$(date +%s)

if [ -f "$WINDOW" ]; then
  DEADLINE=$(tr -dc '0-9' < "$WINDOW")
  if [ -n "$DEADLINE" ] && [ "$NOW" -lt "$DEADLINE" ]; then
    exit 0   # window still running
  fi
  rm -f "$WINDOW"   # expired -> fall through and start a new window
fi

exec 9>"$LOCK"
flock -n 9 || exit 0
exec 8>> "$MRLOCK"
flock -s -n 8 || exit 0   # mass-report holds the phone -> skip this tick

printf '%s\n' "$((NOW + 7200))" > "$WINDOW"
# Absolute path: cron's PATH (/usr/bin:/bin) does not include ~/.opencode/bin
# Model must match the litellm provider entry in opencode.json (served on :8083)
timeout --signal=TERM --kill-after=30 7200 "${OPENCODE_BIN:-$HOME/.opencode/bin/opencode}" run --auto --dir "$ROOT" --model "${OPENCODE_MODEL:-mimo-v26-9b-mtp}" "$(cat "$ROOT/opencode_2h_prompt.txt")" >> "$LOG" 2>&1
STATUS=$?
printf '\n[%s] opencode_exit=%s\n' "$(date -Is)" "$STATUS" >> "$LOG"
rm -f "$WINDOW"
exit "$STATUS"
