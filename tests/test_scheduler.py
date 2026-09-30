from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import time
import unittest
from unittest import mock

from src.agent.core import scheduler

_HAS_TZSET = hasattr(time, "tzset")


def _mk(y, mo, d, h, mi, s=0):
    """Build a local-time timestamp the same way scheduler.py does."""
    return time.mktime((y, mo, d, h, mi, s, 0, 0, -1))


class SchedulerTestCase(unittest.TestCase):
    """Base fixture: isolates the schedules file and resets module globals."""

    def setUp(self) -> None:
        self._tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp_dir, ignore_errors=True)
        self.path = os.path.join(self._tmp_dir, "sub", "schedules.json")
        patcher = mock.patch.object(scheduler, "SCHEDULES_FILE", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

        scheduler._inflight.clear()
        self.addCleanup(scheduler._inflight.clear)
        scheduler._started = False
        self.addCleanup(self._reset_started)

        self._orig_tz = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        if _HAS_TZSET:
            time.tzset()
        self.addCleanup(self._restore_tz)

    def _reset_started(self) -> None:
        scheduler._started = False

    def _restore_tz(self) -> None:
        if self._orig_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._orig_tz
        if _HAS_TZSET:
            time.tzset()

    def _set_tz(self, tz: str) -> None:
        os.environ["TZ"] = tz
        if _HAS_TZSET:
            time.tzset()


class ParseSpecTests(SchedulerTestCase):
    def test_daily_single_time(self) -> None:
        parsed, n, err = scheduler.parse_spec(["daily", "09:00"])
        self.assertIsNone(err)
        self.assertEqual(n, 2)
        self.assertEqual(parsed["kind"], "daily")
        self.assertEqual(parsed["times"], [(9, 0)])
        self.assertEqual(parsed["dows"], set(range(7)))
        self.assertEqual(parsed["text"], "daily 09:00")

    def test_daily_comma_time_list_sorted(self) -> None:
        parsed, _n, err = scheduler.parse_spec(["daily", "18:00,09:00"])
        self.assertIsNone(err)
        self.assertEqual(parsed["times"], [(9, 0), (18, 0)])

    def test_case_insensitive_kind(self) -> None:
        parsed, _n, err = scheduler.parse_spec(["DAILY", "09:00"])
        self.assertIsNone(err)
        self.assertEqual(parsed["kind"], "daily")

    def test_weekdays(self) -> None:
        parsed, _n, err = scheduler.parse_spec(["weekdays", "08:30"])
        self.assertIsNone(err)
        self.assertEqual(parsed["dows"], {0, 1, 2, 3, 4})

    def test_weekly_multi_day(self) -> None:
        parsed, n, err = scheduler.parse_spec(["weekly", "mon,thu", "09:00"])
        self.assertIsNone(err)
        self.assertEqual(n, 3)
        self.assertEqual(parsed["dows"], {0, 3})

    def test_weekly_case_insensitive(self) -> None:
        parsed, _n, err = scheduler.parse_spec(["weekly", "MON,THU", "09:00"])
        self.assertIsNone(err)
        self.assertEqual(parsed["dows"], {0, 3})

    def test_hourly_colon_form(self) -> None:
        parsed, _n, err = scheduler.parse_spec(["hourly", ":15"])
        self.assertIsNone(err)
        self.assertEqual(parsed["minutes"], 15)

    def test_hourly_bare_form(self) -> None:
        parsed, _n, err = scheduler.parse_spec(["hourly", "15"])
        self.assertIsNone(err)
        self.assertEqual(parsed["minutes"], 15)

    def test_every_minutes(self) -> None:
        parsed, _n, err = scheduler.parse_spec(["every", "30m"])
        self.assertIsNone(err)
        self.assertEqual(parsed["minutes"], 30)

    def test_every_hours(self) -> None:
        parsed, _n, err = scheduler.parse_spec(["every", "2h"])
        self.assertIsNone(err)
        self.assertEqual(parsed["minutes"], 120)

    def test_at_form(self) -> None:
        parsed, n, err = scheduler.parse_spec(["at", "2099-01-01", "00:00"])
        self.assertIsNone(err)
        self.assertEqual(n, 3)
        self.assertEqual(parsed["kind"], "at")
        self.assertAlmostEqual(parsed["ts"], _mk(2099, 1, 1, 0, 0), delta=1)

    def test_rejects_every_below_minimum(self) -> None:
        _parsed, _n, err = scheduler.parse_spec(["every", "5m"])
        self.assertIsNotNone(err)

    def test_rejects_every_above_max_hours(self) -> None:
        _parsed, _n, err = scheduler.parse_spec(["every", "25h"])
        self.assertIsNotNone(err)

    def test_rejects_daily_hour_24(self) -> None:
        _parsed, _n, err = scheduler.parse_spec(["daily", "24:00"])
        self.assertIsNotNone(err)

    def test_rejects_hourly_minute_60(self) -> None:
        _parsed, _n, err = scheduler.parse_spec(["hourly", ":60"])
        self.assertIsNotNone(err)

    def test_rejects_unknown_weekday(self) -> None:
        _parsed, _n, err = scheduler.parse_spec(["weekly", "xyz", "09:00"])
        self.assertIsNotNone(err)

    def test_split_spec_prompt_round_trip(self) -> None:
        parsed, prompt, err = scheduler.split_spec_prompt("daily 09:00 good morning")
        self.assertIsNone(err)
        self.assertEqual(parsed["kind"], "daily")
        self.assertEqual(prompt, "good morning")

    def test_split_spec_prompt_missing_prompt(self) -> None:
        _parsed, _prompt, err = scheduler.split_spec_prompt("daily 09:00")
        self.assertIsNotNone(err)

    def test_split_spec_prompt_too_long(self) -> None:
        long_prompt = "x" * (scheduler._MAX_PROMPT_CHARS + 1)
        _parsed, _prompt, err = scheduler.split_spec_prompt("daily 09:00 " + long_prompt)
        self.assertIsNotNone(err)


class NextRunTests(SchedulerTestCase):
    def test_daily_before_first_time(self) -> None:
        parsed, _n, _err = scheduler.parse_spec(["daily", "09:00,18:00"])
        after = _mk(2025, 6, 2, 7, 0)
        self.assertEqual(scheduler.next_run(parsed, after), _mk(2025, 6, 2, 9, 0))

    def test_daily_between_times(self) -> None:
        parsed, _n, _err = scheduler.parse_spec(["daily", "09:00,18:00"])
        after = _mk(2025, 6, 2, 12, 0)
        self.assertEqual(scheduler.next_run(parsed, after), _mk(2025, 6, 2, 18, 0))

    def test_daily_after_last_time_rolls_to_next_day(self) -> None:
        parsed, _n, _err = scheduler.parse_spec(["daily", "09:00,18:00"])
        after = _mk(2025, 6, 2, 19, 0)
        self.assertEqual(scheduler.next_run(parsed, after), _mk(2025, 6, 3, 9, 0))

    def test_weekdays_friday_rolls_to_monday(self) -> None:
        # 2025-06-06 is a Friday.
        parsed, _n, _err = scheduler.parse_spec(["weekdays", "08:30"])
        after = _mk(2025, 6, 6, 9, 0)
        self.assertEqual(scheduler.next_run(parsed, after), _mk(2025, 6, 9, 8, 30))

    def test_weekly_multi_day(self) -> None:
        # 2025-06-03 is a Tuesday; weekly mon,thu should land on Thursday.
        parsed, _n, _err = scheduler.parse_spec(["weekly", "mon,thu", "09:00"])
        after = _mk(2025, 6, 3, 10, 0)
        self.assertEqual(scheduler.next_run(parsed, after), _mk(2025, 6, 5, 9, 0))

    def test_every_30m_aligned_to_half_hour(self) -> None:
        parsed, _n, _err = scheduler.parse_spec(["every", "30m"])
        after = _mk(2025, 6, 2, 0, 5)
        self.assertEqual(scheduler.next_run(parsed, after), _mk(2025, 6, 2, 0, 30))
        after2 = _mk(2025, 6, 2, 0, 35)
        self.assertEqual(scheduler.next_run(parsed, after2), _mk(2025, 6, 2, 1, 0))

    def test_every_45m_restarts_at_midnight(self) -> None:
        parsed, _n, _err = scheduler.parse_spec(["every", "45m"])
        after = _mk(2025, 6, 2, 23, 50)
        self.assertEqual(scheduler.next_run(parsed, after), _mk(2025, 6, 3, 0, 0))

    def test_hourly(self) -> None:
        parsed, _n, _err = scheduler.parse_spec(["hourly", ":15"])
        after = _mk(2025, 6, 2, 10, 20)
        self.assertEqual(scheduler.next_run(parsed, after), _mk(2025, 6, 2, 11, 15))

    def test_at_spent_returns_none(self) -> None:
        parsed, _n, _err = scheduler.parse_spec(["at", "2020-01-01", "00:00"])
        after = _mk(2025, 6, 2, 0, 0)
        self.assertIsNone(scheduler.next_run(parsed, after))

    def test_strictly_after_boundary(self) -> None:
        parsed, _n, _err = scheduler.parse_spec(["hourly", ":15"])
        exact = _mk(2025, 6, 2, 10, 15)
        self.assertEqual(scheduler.next_run(parsed, exact), _mk(2025, 6, 2, 11, 15))


@unittest.skipUnless(_HAS_TZSET, "time.tzset() not available on this platform")
class DstTests(SchedulerTestCase):
    def test_spring_forward_daily_fires_once(self) -> None:
        self._set_tz("America/Los_Angeles")
        # 2024-03-10: US spring-forward (02:00 -> 03:00).
        parsed, _n, _err = scheduler.parse_spec(["daily", "02:30"])
        before = _mk(2024, 3, 9, 12, 0)
        first = scheduler.next_run(parsed, before)
        self.assertIsNotNone(first)
        second = scheduler.next_run(parsed, first)
        self.assertIsNotNone(second)
        self.assertGreater(second, first)
        # Occurrences must strictly increase across the transition, never
        # repeat or go backward.
        self.assertGreater(second - first, 3600 * 20)

    def test_fall_back_daily_fires_once(self) -> None:
        self._set_tz("America/Los_Angeles")
        # 2024-11-03: US fall-back (02:00 -> 01:00).
        parsed, _n, _err = scheduler.parse_spec(["daily", "01:30"])
        before = _mk(2024, 11, 2, 12, 0)
        first = scheduler.next_run(parsed, before)
        second = scheduler.next_run(parsed, first)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertGreater(second, first)


class CrudTests(SchedulerTestCase):
    def test_add_success_message_prefix(self) -> None:
        ok, message = scheduler.add("daily 09:00", "good morning", now=_mk(2025, 6, 2, 0, 0))
        self.assertTrue(ok)
        self.assertTrue(message.startswith("OK: "))

    def test_add_name_defaults_to_prompt_prefix(self) -> None:
        scheduler.add("daily 09:00", "a" * 60, now=_mk(2025, 6, 2, 0, 0))
        entries = scheduler.list_entries()
        self.assertEqual(entries[0]["name"], ("a" * 60)[:40])

    def test_add_explicit_name(self) -> None:
        scheduler.add("daily 09:00", "prompt", name="My schedule", now=_mk(2025, 6, 2, 0, 0))
        entries = scheduler.list_entries()
        self.assertEqual(entries[0]["name"], "My schedule")

    def test_add_positional_name_argument_order(self) -> None:
        """gemini.py's manage_schedule tool calls scheduler.add(spec, prompt,
        name) positionally — name must be the 3rd positional parameter."""
        ok, _message = scheduler.add("daily 09:00", "prompt", "Positional name")
        self.assertTrue(ok)
        entries = scheduler.list_entries()
        self.assertEqual(entries[0]["name"], "Positional name")

    def test_add_rejects_at_in_the_past(self) -> None:
        ok, message = scheduler.add("at 2020-01-01 00:00", "prompt", now=_mk(2025, 6, 2, 0, 0))
        self.assertFalse(ok)
        self.assertIn("future", message)

    def test_add_rejects_empty_prompt(self) -> None:
        ok, _message = scheduler.add("daily 09:00", "   ")
        self.assertFalse(ok)

    def test_add_rejects_prompt_too_long(self) -> None:
        ok, _message = scheduler.add("daily 09:00", "x" * (scheduler._MAX_PROMPT_CHARS + 1))
        self.assertFalse(ok)

    def test_max_schedules_cap(self) -> None:
        now = _mk(2025, 6, 2, 0, 0)
        for i in range(scheduler._MAX_SCHEDULES):
            ok, _msg = scheduler.add("daily 09:00", "prompt {}".format(i), now=now)
            self.assertTrue(ok)
        ok, message = scheduler.add("daily 09:00", "overflow", now=now)
        self.assertFalse(ok)
        self.assertIn("limit", message.lower())

    def test_ids_never_reused_after_remove(self) -> None:
        now = _mk(2025, 6, 2, 0, 0)
        scheduler.add("daily 09:00", "A", now=now)
        scheduler.add("daily 09:00", "B", now=now)
        scheduler.remove(1)
        scheduler.add("daily 09:00", "C", now=now)
        ids = sorted(e["id"] for e in scheduler.list_entries())
        self.assertEqual(ids, [2, 3])

    def test_update_prompt_only_keeps_next_run_at(self) -> None:
        now = _mk(2025, 6, 2, 0, 0)
        scheduler.add("daily 09:00", "old prompt", now=now)
        before = scheduler.list_entries()[0]["next_run_at"]
        scheduler.update_schedule(1, prompt="new prompt", now=now)
        after = scheduler.list_entries()[0]
        self.assertEqual(after["next_run_at"], before)
        self.assertEqual(after["prompt"], "new prompt")

    def test_update_spec_recomputes_next_run_at(self) -> None:
        now = _mk(2025, 6, 2, 0, 0)
        scheduler.add("daily 09:00", "prompt", now=now)
        before = scheduler.list_entries()[0]["next_run_at"]
        ok, _msg = scheduler.update_schedule(1, spec="daily 20:00", now=now)
        self.assertTrue(ok)
        after = scheduler.list_entries()[0]["next_run_at"]
        self.assertNotEqual(after, before)
        self.assertEqual(after, _mk(2025, 6, 2, 20, 0))

    def test_enable_recomputes_without_catchup(self) -> None:
        add_now = _mk(2025, 6, 2, 0, 0)
        scheduler.add("daily 09:00", "prompt", now=add_now)
        scheduler.set_enabled(1, False)
        # set_enabled uses time.time() internally for "now"; patch it so the
        # re-enable recomputes relative to a much later `later`, not a
        # catch-up run of the original (now stale) 2025-06-02 09:00 slot.
        later = _mk(2025, 6, 5, 0, 0)
        with mock.patch.object(scheduler.time, "time", return_value=later):
            ok, _msg = scheduler.set_enabled(1, True)
        self.assertTrue(ok)
        entry = scheduler.list_entries()[0]
        self.assertGreater(entry["next_run_at"], later)
        self.assertNotEqual(entry["next_run_at"], _mk(2025, 6, 2, 9, 0))

    def test_save_creates_0600_file(self) -> None:
        scheduler.add("daily 09:00", "prompt", now=_mk(2025, 6, 2, 0, 0))
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_corrupt_file_is_quarantined(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("not json {{{")
        data = scheduler.load()
        self.assertEqual(data["schedules"], [])
        quarantined = [
            name for name in os.listdir(os.path.dirname(self.path))
            if name.startswith("schedules.json.corrupt-")
        ]
        self.assertEqual(len(quarantined), 1)

    def test_at_kind_deleted_after_firing(self) -> None:
        """A spent one-shot self-deletes on fire instead of lingering as a
        disabled/"done" entry (live-test feedback: it must go away for good)."""
        now = _mk(2025, 6, 2, 0, 0)
        scheduler.add("at 2025-06-02 00:30", "one shot", now=now)
        fired = []
        scheduler.tick(_mk(2025, 6, 2, 0, 31), fired.append)
        self.assertEqual(len(fired), 1)
        self.assertEqual(scheduler.list_entries(), [])

    def test_at_kind_deleted_after_stale_skip(self) -> None:
        now = _mk(2025, 6, 2, 0, 0)
        scheduler.add("at 2025-06-02 00:30", "one shot", now=now)
        way_later = _mk(2025, 6, 2, 0, 30) + scheduler._MISSED_GRACE_SEC + 60
        fired = []
        scheduler.tick(way_later, fired.append)
        self.assertEqual(fired, [])
        self.assertEqual(scheduler.list_entries(), [])

    def test_at_kind_ids_not_reused_after_delete(self) -> None:
        now = _mk(2025, 6, 2, 0, 0)
        scheduler.add("at 2025-06-02 00:30", "one shot", now=now)
        scheduler.tick(_mk(2025, 6, 2, 0, 31), lambda _entry: True)
        self.assertEqual(scheduler.list_entries(), [])
        scheduler.add("daily 09:00", "next", now=now)
        ids = [e["id"] for e in scheduler.list_entries()]
        self.assertEqual(ids, [2])

    def test_mark_finished_is_noop_on_already_deleted_id(self) -> None:
        now = _mk(2025, 6, 2, 0, 0)
        scheduler.add("at 2025-06-02 00:30", "one shot", now=now)
        scheduler.tick(_mk(2025, 6, 2, 0, 31), lambda _entry: True)
        self.assertEqual(scheduler.list_entries(), [])
        # Must never raise, even though id 1 no longer exists in the file.
        scheduler.mark_finished(1)

    def test_run_now_on_already_deleted_one_shot_is_unknown(self) -> None:
        now = _mk(2025, 6, 2, 0, 0)
        scheduler.add("at 2025-06-02 00:30", "one shot", now=now)
        scheduler.tick(_mk(2025, 6, 2, 0, 31), lambda _entry: True)
        self.assertIsNone(scheduler.run_now(1))

    def test_disabling_unfired_at_keeps_it_listed_as_paused(self) -> None:
        now = _mk(2025, 6, 2, 0, 0)
        scheduler.add("at 2099-01-01 00:00", "future one shot", now=now)
        ok, _msg = scheduler.set_enabled(1, False)
        self.assertTrue(ok)
        entries = scheduler.list_entries()
        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0]["enabled"])
        text = scheduler.format_entry(entries[0], now)
        self.assertIn("[paused]", text)

    def test_paused_entry_never_shows_due_even_with_stale_next_run_at(self) -> None:
        now = _mk(2025, 6, 2, 9, 0)
        scheduler.add("daily 09:00", "prompt", now=_mk(2025, 6, 2, 0, 0))
        scheduler.set_enabled(1, False)
        # Force a stale (in the past) next_run_at directly, as if the pause
        # happened long after the entry was last due.
        data = scheduler.load()
        data["schedules"][0]["next_run_at"] = now - 3600
        scheduler._save(data)
        entry = scheduler.list_entries()[0]
        text = scheduler.format_entry(entry, now)
        self.assertIn("paused", text)
        self.assertNotIn("(due)", text)

    def test_enabled_entry_past_next_run_at_shows_due(self) -> None:
        now = _mk(2025, 6, 2, 9, 0)
        scheduler.add("daily 09:00", "prompt", now=_mk(2025, 6, 2, 0, 0))
        data = scheduler.load()
        data["schedules"][0]["next_run_at"] = now - 60
        scheduler._save(data)
        entry = scheduler.list_entries()[0]
        text = scheduler.format_entry(entry, now)
        self.assertIn("(due)", text)

    def test_remove_unknown_id(self) -> None:
        ok, message = scheduler.remove(999)
        self.assertFalse(ok)
        self.assertIn("Unknown", message)

    def test_get_schedule_formats_entry(self) -> None:
        scheduler.add("daily 09:00", "prompt", name="Morning", now=_mk(2025, 6, 2, 0, 0))
        ok, message = scheduler.get_schedule(1)
        self.assertTrue(ok)
        self.assertIn("Morning", message)

    def test_get_schedule_unknown_id(self) -> None:
        ok, message = scheduler.get_schedule(999)
        self.assertFalse(ok)
        self.assertIn("Unknown", message)

    def test_format_list_joins_entries(self) -> None:
        now = _mk(2025, 6, 2, 0, 0)
        scheduler.add("daily 09:00", "A", now=now)
        scheduler.add("hourly :15", "B", now=now)
        text = scheduler.format_list(scheduler.list_entries(), now=now)
        self.assertIn("A", text)
        self.assertIn("B", text)
        self.assertEqual(len(text.splitlines()), 2)

    def test_gemini_get_update_aliases_forward(self) -> None:
        """core/gemini.py's manage_schedule runner calls scheduler.get(...)
        and scheduler.update(...) (not get_schedule/update_schedule)."""
        scheduler.add("daily 09:00", "prompt", now=_mk(2025, 6, 2, 0, 0))
        ok, message = scheduler.get(1)
        self.assertTrue(ok)
        self.assertIn("prompt", message)

        ok, message = scheduler.update(1, spec="daily 10:00", prompt=None, name=None)
        self.assertTrue(ok)
        self.assertTrue(message.startswith("OK: "))


class RunNowTests(SchedulerTestCase):
    def test_run_now_refuses_in_flight(self) -> None:
        scheduler.add("daily 09:00", "prompt", now=_mk(2025, 6, 2, 0, 0))
        first = scheduler.run_now(1)
        self.assertIsNotNone(first)
        second = scheduler.run_now(1)
        self.assertIsNone(second)
        scheduler.mark_finished(1)
        third = scheduler.run_now(1)
        self.assertIsNotNone(third)

    def test_run_now_unknown_id(self) -> None:
        self.assertIsNone(scheduler.run_now(999))


class TickTests(SchedulerTestCase):
    def _add_due(self, spec: str, prompt: str, next_run_at: float, now: float, enabled: bool = True) -> int:
        scheduler.add(spec, prompt, now=now)
        entries = scheduler.list_entries()
        entry = entries[-1]
        # Directly force next_run_at into the past/near-due for the test via
        # a raw load/modify/save cycle (scheduler has no public setter).
        data = scheduler.load()
        for e in data["schedules"]:
            if e["id"] == entry["id"]:
                e["next_run_at"] = next_run_at
                e["enabled"] = enabled
        scheduler._save(data)
        return entry["id"]

    def test_due_within_grace_fires_once(self) -> None:
        now = _mk(2025, 6, 2, 9, 0)
        sid = self._add_due("daily 09:00", "prompt", now - 60, now)
        fired = []
        scheduler.tick(now, fired.append)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]["id"], sid)
        entry = scheduler.list_entries()[0]
        self.assertEqual(entry["last_run_at"], now)

    def test_second_tick_same_time_does_not_refire(self) -> None:
        now = _mk(2025, 6, 2, 9, 0)
        self._add_due("daily 09:00", "prompt", now - 60, now)
        fired = []
        scheduler.tick(now, fired.append)
        scheduler.tick(now, fired.append)
        self.assertEqual(len(fired), 1)

    def test_stale_beyond_grace_is_skipped_and_advanced(self) -> None:
        now = _mk(2025, 6, 2, 9, 0)
        overdue_next_run = now - scheduler._MISSED_GRACE_SEC - 60
        self._add_due("daily 09:00", "prompt", overdue_next_run, now)
        fired = []
        scheduler.tick(now, fired.append)
        self.assertEqual(fired, [])
        entry = scheduler.list_entries()[0]
        self.assertGreater(entry["next_run_at"], now)

    def test_disabled_entry_is_ignored(self) -> None:
        now = _mk(2025, 6, 2, 9, 0)
        self._add_due("daily 09:00", "prompt", now - 60, now, enabled=False)
        fired = []
        scheduler.tick(now, fired.append)
        self.assertEqual(fired, [])

    def test_in_flight_overlap_skipped_and_advanced(self) -> None:
        now = _mk(2025, 6, 2, 9, 0)
        sid = self._add_due("daily 09:00", "prompt", now - 60, now)
        scheduler._inflight.add(sid)
        fired = []
        scheduler.tick(now, fired.append)
        self.assertEqual(fired, [])
        entry = scheduler.list_entries()[0]
        self.assertGreater(entry["next_run_at"], now)

    def test_enqueue_false_marks_finished(self) -> None:
        now = _mk(2025, 6, 2, 9, 0)
        sid = self._add_due("daily 09:00", "prompt", now - 60, now)
        scheduler.tick(now, lambda _entry: False)
        self.assertNotIn(sid, scheduler._inflight)

    def test_enqueue_true_keeps_inflight_until_mark_finished(self) -> None:
        now = _mk(2025, 6, 2, 9, 0)
        sid = self._add_due("daily 09:00", "prompt", now - 60, now)
        scheduler.tick(now, lambda _entry: True)
        self.assertIn(sid, scheduler._inflight)
        scheduler.mark_finished(sid)
        self.assertNotIn(sid, scheduler._inflight)

    def test_save_happens_before_enqueue(self) -> None:
        now = _mk(2025, 6, 2, 9, 0)
        self._add_due("daily 09:00", "prompt", now - 60, now)
        order = []
        orig_save = scheduler._save

        def _tracking_save(data):
            order.append("save")
            return orig_save(data)

        def _tracking_enqueue(_entry):
            order.append("enqueue")
            return True

        with mock.patch.object(scheduler, "_save", side_effect=_tracking_save):
            scheduler.tick(now, _tracking_enqueue)
        self.assertEqual(order, ["save", "enqueue"])

    def test_mixed_cadences_independent(self) -> None:
        now = _mk(2025, 6, 2, 9, 0)
        due_id = self._add_due("daily 09:00", "due", now - 60, now)
        not_due_id = self._add_due("hourly :45", "not due", now + 3600, now)
        fired = []
        scheduler.tick(now, fired.append)
        fired_ids = [e["id"] for e in fired]
        self.assertIn(due_id, fired_ids)
        self.assertNotIn(not_due_id, fired_ids)


class StartTickerTests(SchedulerTestCase):
    def test_idempotent(self) -> None:
        with mock.patch.object(scheduler.threading, "Thread") as mock_thread:
            scheduler.start_ticker(lambda _entry: True)
            scheduler.start_ticker(lambda _entry: True)
        mock_thread.assert_called_once()


if __name__ == "__main__":
    unittest.main()
