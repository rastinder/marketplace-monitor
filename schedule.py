"""Night-only scheduling for the mass-report sweep.

The job sleeps for 12 hours and is allowed to work for a 12 hour window that
opens at night. The wake time is randomised 45-80 minutes after the nominal
window open, deterministically per calendar day, so the many 15-minute cron
ticks of that day all agree on the same wake time (and a restart does not
re-roll the dice mid-window).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import os

# Overridable for tuning and testing (env > default).
WINDOW_OPEN_HOUR = int(os.environ.get("MMC_WINDOW_OPEN_HOUR", "20"))   # nominal night start
WINDOW_HOURS = int(os.environ.get("MMC_WINDOW_HOURS", "12"))          # work window length
WAKE_MIN_MIN = int(os.environ.get("MMC_WAKE_MIN_MIN", "45"))          # wake offset: 45 ...
WAKE_MIN_MAX = int(os.environ.get("MMC_WAKE_MIN_MAX", "80"))          # ... to 80 minutes
MIN_GAP_MIN = int(os.environ.get("MMC_MIN_GAP_MIN", "60"))           # spacing between sweeps


def _naive_local(when: datetime) -> datetime:
    """Local naive time.

    `datetime.now()` is naive while `date -Is`/ISO strings from cron are
    tz-aware; comparing the two raises TypeError, so everything is normalised.
    """
    return when.astimezone().replace(tzinfo=None) if when.tzinfo else when


def _day_seed(day: datetime) -> int:
    """Stable per calendar day (no randomness that changes on restart)."""
    key = f"marketplace-monitor/{day.date().isoformat()}"
    return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16)


def wake_offset_minutes(day: datetime) -> int:
    span = WAKE_MIN_MAX - WAKE_MIN_MIN + 1
    return WAKE_MIN_MIN + (_day_seed(day) % span)


def window_bounds(now: datetime) -> tuple[datetime, datetime, datetime]:
    """Return (wake_at, window_end, window_start_of_that_night).

    The window is anchored to the night it belongs to: before WAKE_OPEN_HOUR we
    are still in the window that opened the previous evening.
    """
    if now.hour < WINDOW_OPEN_HOUR:
        anchor_day = (now - timedelta(days=1))
    else:
        anchor_day = now
    start = anchor_day.replace(hour=WINDOW_OPEN_HOUR, minute=0, second=0, microsecond=0)
    wake = start + timedelta(minutes=wake_offset_minutes(anchor_day))
    return wake, wake + timedelta(hours=WINDOW_HOURS), start


def decide(now: datetime, last_run: datetime | None, min_gap_min: int = MIN_GAP_MIN) -> dict:
    wake, end, start = window_bounds(now)
    if now < wake:
        return {"action": "sleep", "reason": "before the randomised wake time",
                "wake_at": wake.isoformat(), "window_ends": end.isoformat()}
    if now >= end:
        return {"action": "sleep", "reason": "12h work window finished",
                "wake_at": wake.isoformat(), "window_ends": end.isoformat()}
    if last_run and (now - last_run) < timedelta(minutes=min_gap_min):
        ready = last_run + timedelta(minutes=min_gap_min)
        return {"action": "sleep", "reason": f"min gap {min_gap_min}m not elapsed",
                "next_allowed": ready.isoformat(), "window_ends": end.isoformat()}
    return {"action": "run", "reason": "inside the night work window",
            "wake_at": wake.isoformat(), "window_ends": end.isoformat()}


STATE_PATH = Path(__file__).resolve().parent / "data" / "schedule.json"


def read_last_run() -> datetime | None:
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        raw_dt = datetime.fromisoformat(raw["last_run"])
    except (OSError, ValueError, KeyError):
        return None
    return _naive_local(raw_dt) if raw_dt else None


def write_last_run(when: datetime) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps({"last_run": when.isoformat()}), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="night-only schedule gate")
    parser.add_argument("--now", help="ISO timestamp (default: now)")
    parser.add_argument("--mark", action="store_true",
                        help="record that a sweep actually started (call after the lock is held)")
    args = parser.parse_args(argv)
    now = _naive_local(datetime.fromisoformat(args.now)) if args.now else datetime.now()
    if args.mark:
        write_last_run(now)
        print(json.dumps({"action": "marked", "last_run": now.isoformat()}))
        return 0
    decision = decide(now, read_last_run())
    decision["now"] = now.isoformat()
    print(json.dumps(decision))
    return 0 if decision["action"] == "run" else 1


if __name__ == "__main__":
    raise SystemExit(main())
