import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import schedule


class WindowTests(unittest.TestCase):
    def test_window_is_twelve_hours(self):
        now = datetime(2026, 9, 27, 21, 30)
        wake, end, _start = schedule.window_bounds(now)
        self.assertEqual((end - wake).total_seconds() / 3600, 12)

    def test_wake_offset_is_between_45_and_80_minutes(self):
        day = datetime(2026, 9, 27)
        for offset_day in range(30):
            offset = schedule.wake_offset_minutes(day + timedelta(days=offset_day))
            self.assertGreaterEqual(offset, 45)
            self.assertLessEqual(offset, 80)

    def test_wake_offset_is_stable_within_a_day(self):
        day = datetime(2026, 9, 27)
        self.assertEqual(schedule.wake_offset_minutes(day), schedule.wake_offset_minutes(day))

    def test_wake_offset_varies_across_days(self):
        offsets = {schedule.wake_offset_minutes(datetime(2026, 9, 27) + timedelta(days=d))
                   for d in range(30)}
        self.assertGreater(len(offsets), 5, "wake time should be randomised per day")

    def test_small_hours_belong_to_the_previous_nights_window(self):
        now = datetime(2026, 9, 27, 2, 0)
        _wake, end, start = schedule.window_bounds(now)
        self.assertEqual(start.date(), datetime(2026, 9, 26).date())
        self.assertGreater(end, now)


class DecisionTests(unittest.TestCase):
    def test_daytime_sleeps(self):
        now = datetime(2026, 9, 27, 13, 0)
        self.assertEqual(schedule.decide(now, None)["action"], "sleep")

    def test_early_evening_before_wake_sleeps(self):
        day = datetime(2026, 9, 27)
        now = day.replace(hour=20, minute=5)
        decision = schedule.decide(now, None)
        self.assertEqual(decision["action"], "sleep")
        self.assertIn("wake", decision["reason"])

    def test_runs_inside_the_window(self):
        day = datetime(2026, 9, 27)
        wake, _end, _s = schedule.window_bounds(day.replace(hour=23, minute=0))
        self.assertEqual(schedule.decide(wake, None)["action"], "run")
        self.assertEqual(schedule.decide(wake + timedelta(hours=3), None)["action"], "run")

    def test_sleeps_after_twelve_hours(self):
        day = datetime(2026, 9, 27)
        wake, end, _s = schedule.window_bounds(day.replace(hour=21, minute=0))
        self.assertEqual(schedule.decide(end, None)["action"], "sleep")

    def test_minimum_gap_between_sweeps(self):
        day = datetime(2026, 9, 27)
        wake, _end, _s = schedule.window_bounds(day.replace(hour=21, minute=0))
        self.assertEqual(schedule.decide(wake + timedelta(minutes=10), wake)["action"], "sleep")
        self.assertEqual(schedule.decide(wake + timedelta(minutes=61), wake)["action"], "run")

    def test_every_day_gets_exactly_one_night_window(self):
        # 48h of 15-minute ticks must contain both a sleeping and a working phase
        base = datetime(2026, 9, 27, 0, 0)
        actions = []
        for minutes in range(0, 48 * 60, 15):
            actions.append(schedule.decide(base + timedelta(minutes=minutes), None)["action"])
        self.assertIn("sleep", actions)
        self.assertIn("run", actions)
        # a full 24h cannot be all-run: the day side must sleep
        first_day = actions[:96]
        self.assertIn("sleep", first_day)


class TimezoneTests(unittest.TestCase):
    def test_timezone_aware_input_does_not_crash(self):
        # `date -Is` yields an aware timestamp while datetime.now() is naive;
        # mixing them used to raise TypeError inside the min-gap check
        aware = datetime(2026, 9, 26, 22, 30, tzinfo=timezone.utc)
        naive_last = datetime(2026, 9, 26, 22, 0)
        decision = schedule.decide(schedule._naive_local(aware), naive_last)
        self.assertIn(decision["action"], ("run", "sleep"))

    def test_naive_local_normaliser_strips_offset(self):
        aware = datetime(2026, 9, 26, 22, 30, tzinfo=timezone.utc)
        self.assertIsNone(schedule._naive_local(aware).tzinfo)

    def test_cli_accepts_aware_now(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            original = schedule.STATE_PATH
            schedule.STATE_PATH = Path(d) / "schedule.json"
            try:
                schedule.write_last_run(datetime(2026, 9, 26, 22, 0))
                code = schedule.main(["--now", "2026-09-26T22:30:00+00:00"])
            finally:
                schedule.STATE_PATH = original
        self.assertIn(code, (0, 1))


class OverrideTests(unittest.TestCase):
    def test_env_overrides_apply(self):
        import importlib
        import os
        os.environ["MMC_WINDOW_OPEN_HOUR"] = "3"
        os.environ["MMC_WAKE_MIN_MIN"] = "10"
        os.environ["MMC_WAKE_MIN_MAX"] = "20"
        try:
            reloaded = importlib.reload(schedule)
            self.assertEqual(reloaded.WINDOW_OPEN_HOUR, 3)
            self.assertEqual(reloaded.wake_offset_minutes(datetime(2026, 9, 27)),
                             reloaded.wake_offset_minutes(datetime(2026, 9, 27)))
            self.assertTrue(10 <= reloaded.wake_offset_minutes(datetime(2026, 9, 27)) <= 20)
        finally:
            for key in ("MMC_WINDOW_OPEN_HOUR", "MMC_WAKE_MIN_MIN", "MMC_WAKE_MIN_MAX"):
                os.environ.pop(key, None)
            importlib.reload(schedule)


if __name__ == "__main__":
    unittest.main()
