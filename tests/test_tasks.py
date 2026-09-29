from __future__ import annotations

import json
import os
import stat
import tempfile
import time
import unittest
from unittest.mock import patch

from src.agent.core import tasks

_NOW = 1234567890.0


class TestTasks(unittest.TestCase):
    """Tests for the tasks module."""

    def setUp(self) -> None:
        """Set up a temporary directory for each test."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tasks_dir = self.temp_dir.name
        self._patch = patch.object(tasks, "TASKS_DIR", self.tasks_dir)
        self._patch.start()

    def tearDown(self) -> None:
        """Stop patches and clean up the temporary directory."""
        self._patch.stop()
        self.temp_dir.cleanup()

    def _current_path(self) -> str:
        return os.path.join(self.tasks_dir, "current.json")

    # -- new_task ---------------------------------------------------------

    def test_new_task_fields_and_types(self) -> None:
        """Test new_task returns a dict with expected fields and types."""
        record = tasks.new_task(chat_id=123, user_id=456, prompt="test", now=_NOW)
        self.assertIsInstance(record, dict)
        self.assertEqual(record["version"], 1)
        self.assertEqual(record["chat_id"], 123)
        self.assertEqual(record["user_id"], 456)
        self.assertEqual(record["prompt"], "test")
        self.assertIsNone(record["stop_reason"])
        self.assertEqual(record["continuations"], 0)
        self.assertEqual(record["last_wrapup"], "")
        self.assertEqual(record["created_at"], _NOW)
        self.assertEqual(record["updated_at"], _NOW)
        self.assertIsInstance(record["task_id"], str)
        self.assertEqual(len(record["task_id"]), 32)  # uuid4 hex is 32 chars

    def test_new_task_without_now_uses_time(self) -> None:
        """Test new_task without now parameter uses current time."""
        before = time.time()
        record = tasks.new_task(chat_id=123, user_id=456, prompt="test")
        after = time.time()
        self.assertGreaterEqual(record["created_at"], before)
        self.assertLessEqual(record["created_at"], after)
        self.assertEqual(record["created_at"], record["updated_at"])

    def test_new_task_user_id_optional(self) -> None:
        """Test new_task with None user_id."""
        record = tasks.new_task(chat_id=123, user_id=None, prompt="test", now=_NOW)
        self.assertIsNone(record["user_id"])

    # -- save and load round-trip -----------------------------------------

    def test_save_and_load_round_trip(self) -> None:
        """Test save then load returns the same task."""
        record = tasks.new_task(chat_id=123, user_id=456, prompt="hello world", now=_NOW)
        self.assertTrue(tasks.save_task(record))
        loaded = tasks.load_task(now=_NOW)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["chat_id"], 123)
        self.assertEqual(loaded["user_id"], 456)
        self.assertEqual(loaded["prompt"], "hello world")
        self.assertEqual(loaded["task_id"], record["task_id"])

    def test_save_and_load_preserves_all_fields(self) -> None:
        """Test round-trip preserves all non-timestamp fields."""
        record = tasks.new_task(chat_id=999, user_id=888, prompt="test content", now=_NOW)
        record["stop_reason"] = "limit"
        record["continuations"] = 1
        record["last_wrapup"] = "Summary of work done."
        self.assertTrue(tasks.save_task(record))
        loaded = tasks.load_task(now=_NOW)
        self.assertEqual(loaded["stop_reason"], "limit")
        self.assertEqual(loaded["continuations"], 1)
        self.assertEqual(loaded["last_wrapup"], "Summary of work done.")

    # -- file permissions ---------------------------------------------------

    def test_save_creates_file_with_0600_mode(self) -> None:
        """Test save creates current.json with mode 0600."""
        record = tasks.new_task(chat_id=123, user_id=456, prompt="test", now=_NOW)
        self.assertTrue(tasks.save_task(record))
        path = self._current_path()
        self.assertTrue(os.path.exists(path))
        mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_save_removes_tmp_file(self) -> None:
        """Test save removes the .tmp file after successful write."""
        record = tasks.new_task(chat_id=123, user_id=456, prompt="test", now=_NOW)
        self.assertTrue(tasks.save_task(record))
        tmp_path = self._current_path() + ".tmp"
        self.assertFalse(os.path.exists(tmp_path))

    def test_load_fixes_loose_permissions(self) -> None:
        """Test load fixes a file with 0644 permissions to 0600."""
        record = tasks.new_task(chat_id=123, user_id=456, prompt="test", now=_NOW)
        path = self._current_path()
        os.makedirs(self.tasks_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f)
        os.chmod(path, 0o644)
        mode_before = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode_before, 0o644)
        loaded = tasks.load_task(now=_NOW)
        self.assertIsNotNone(loaded)
        mode_after = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode_after, 0o600)

    # -- directory creation ------------------------------------------------

    def test_save_creates_directory_if_missing(self) -> None:
        """Test save creates TASKS_DIR if it doesn't exist."""
        os.rmdir(self.tasks_dir)
        self.assertFalse(os.path.exists(self.tasks_dir))
        record = tasks.new_task(chat_id=123, user_id=456, prompt="test", now=_NOW)
        self.assertTrue(tasks.save_task(record))
        self.assertTrue(os.path.exists(self.tasks_dir))

    # -- save error handling -----------------------------------------------

    def test_save_returns_false_on_os_error(self) -> None:
        """Test save returns False and cleans up when os.replace fails."""
        record = tasks.new_task(chat_id=123, user_id=456, prompt="test", now=_NOW)
        with patch("os.replace", side_effect=OSError("Mock error")):
            result = tasks.save_task(record)
        self.assertFalse(result)
        tmp_path = self._current_path() + ".tmp"
        self.assertFalse(os.path.exists(tmp_path))
        self.assertFalse(os.path.exists(self._current_path()))

    # -- invalid JSON and schema violations --------------------------------

    def test_load_corrupt_json_renames_to_corrupt(self) -> None:
        """Test load renames invalid JSON to .corrupt-* and returns None."""
        path = self._current_path()
        os.makedirs(self.tasks_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{invalid json")
        result = tasks.load_task(now=_NOW)
        self.assertIsNone(result)
        self.assertFalse(os.path.exists(path))
        corrupt_files = [f for f in os.listdir(self.tasks_dir) if f.startswith("current.json.corrupt-")]
        self.assertEqual(len(corrupt_files), 1)

    def test_load_invalid_schema_chat_id_bool(self) -> None:
        """Test load renames to .corrupt when chat_id is bool instead of int."""
        path = self._current_path()
        os.makedirs(self.tasks_dir, exist_ok=True)
        record = tasks.new_task(chat_id=123, user_id=456, prompt="test", now=_NOW)
        record["chat_id"] = True
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f)
        result = tasks.load_task(now=_NOW)
        self.assertIsNone(result)
        self.assertFalse(os.path.exists(path))
        corrupt_files = [f for f in os.listdir(self.tasks_dir) if f.startswith("current.json.corrupt-")]
        self.assertEqual(len(corrupt_files), 1)

    def test_load_invalid_schema_empty_prompt(self) -> None:
        """Test load renames to .corrupt when prompt is empty."""
        path = self._current_path()
        os.makedirs(self.tasks_dir, exist_ok=True)
        record = tasks.new_task(chat_id=123, user_id=456, prompt="test", now=_NOW)
        record["prompt"] = ""
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f)
        result = tasks.load_task(now=_NOW)
        self.assertIsNone(result)
        self.assertFalse(os.path.exists(path))
        corrupt_files = [f for f in os.listdir(self.tasks_dir) if f.startswith("current.json.corrupt-")]
        self.assertEqual(len(corrupt_files), 1)

    def test_load_invalid_schema_negative_continuations(self) -> None:
        """Test load renames to .corrupt when continuations is negative."""
        path = self._current_path()
        os.makedirs(self.tasks_dir, exist_ok=True)
        record = tasks.new_task(chat_id=123, user_id=456, prompt="test", now=_NOW)
        record["continuations"] = -1
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f)
        result = tasks.load_task(now=_NOW)
        self.assertIsNone(result)
        self.assertFalse(os.path.exists(path))
        corrupt_files = [f for f in os.listdir(self.tasks_dir) if f.startswith("current.json.corrupt-")]
        self.assertEqual(len(corrupt_files), 1)

    def test_load_invalid_schema_bad_stop_reason(self) -> None:
        """Test load renames to .corrupt when stop_reason has an invalid value."""
        path = self._current_path()
        os.makedirs(self.tasks_dir, exist_ok=True)
        record = tasks.new_task(chat_id=123, user_id=456, prompt="test", now=_NOW)
        record["stop_reason"] = "invalid_reason"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f)
        result = tasks.load_task(now=_NOW)
        self.assertIsNone(result)
        self.assertFalse(os.path.exists(path))
        corrupt_files = [f for f in os.listdir(self.tasks_dir) if f.startswith("current.json.corrupt-")]
        self.assertEqual(len(corrupt_files), 1)

    def test_load_invalid_schema_wrong_version(self) -> None:
        """Test load renames to .corrupt when version is not 1."""
        path = self._current_path()
        os.makedirs(self.tasks_dir, exist_ok=True)
        record = tasks.new_task(chat_id=123, user_id=456, prompt="test", now=_NOW)
        record["version"] = 2
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f)
        result = tasks.load_task(now=_NOW)
        self.assertIsNone(result)
        self.assertFalse(os.path.exists(path))
        corrupt_files = [f for f in os.listdir(self.tasks_dir) if f.startswith("current.json.corrupt-")]
        self.assertEqual(len(corrupt_files), 1)

    # -- expiry -----------------------------------------------------------

    def _write_record(self, record: dict) -> str:
        """Write a record directly (bypassing save_task, which resets
        updated_at to the current time)."""
        path = self._current_path()
        os.makedirs(self.tasks_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f)
        os.chmod(path, 0o600)
        return path

    def test_load_stale_task_deletes_and_returns_none(self) -> None:
        """Test load deletes and returns None when task is older than TASK_MAX_AGE_SEC."""
        old_time = time.time() - (4 * 86400)  # 4 days ago
        record = tasks.new_task(chat_id=123, user_id=456, prompt="test", now=old_time)
        path = self._write_record(record)
        result = tasks.load_task(now=time.time())
        self.assertIsNone(result)
        self.assertFalse(os.path.exists(path))

    def test_load_future_updated_at_clamps_and_loads(self) -> None:
        """Test load with future updated_at clamps the age to 0 and still loads."""
        future_time = time.time() + 3600
        record = tasks.new_task(chat_id=123, user_id=456, prompt="test", now=future_time)
        self._write_record(record)
        loaded = tasks.load_task(now=time.time())
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["chat_id"], 123)

    # -- delete_task ------------------------------------------------------

    def test_delete_task_with_matching_id(self) -> None:
        """Test delete_task with matching task_id deletes the file."""
        record = tasks.new_task(chat_id=123, user_id=456, prompt="test", now=_NOW)
        self.assertTrue(tasks.save_task(record))
        path = self._current_path()
        self.assertTrue(os.path.exists(path))
        result = tasks.delete_task(task_id=record["task_id"])
        self.assertTrue(result)
        self.assertFalse(os.path.exists(path))

    def test_delete_task_with_nonmatching_id_keeps_file(self) -> None:
        """Test delete_task with non-matching task_id keeps the file."""
        record = tasks.new_task(chat_id=123, user_id=456, prompt="test", now=_NOW)
        self.assertTrue(tasks.save_task(record))
        path = self._current_path()
        self.assertTrue(os.path.exists(path))
        result = tasks.delete_task(task_id="other-task-id")
        self.assertFalse(result)
        self.assertTrue(os.path.exists(path))

    def test_delete_task_with_none_id_unconditional(self) -> None:
        """Test delete_task with None task_id deletes regardless."""
        record = tasks.new_task(chat_id=123, user_id=456, prompt="test", now=_NOW)
        self.assertTrue(tasks.save_task(record))
        path = self._current_path()
        self.assertTrue(os.path.exists(path))
        result = tasks.delete_task(task_id=None)
        self.assertTrue(result)
        self.assertFalse(os.path.exists(path))

    def test_delete_task_missing_file_returns_false(self) -> None:
        """Test delete_task returns False when file is missing."""
        result = tasks.delete_task(task_id=None)
        self.assertFalse(result)

    # -- load_missing_file ------------------------------------------------

    def test_load_missing_file_returns_none(self) -> None:
        """Test load returns None when file is missing."""
        result = tasks.load_task(now=_NOW)
        self.assertIsNone(result)

    # -- format_age -------------------------------------------------------

    def test_format_age_zero(self) -> None:
        """Test format_age with 0 seconds."""
        self.assertEqual(tasks.format_age(0), "0s")

    def test_format_age_45_seconds(self) -> None:
        """Test format_age with 45 seconds."""
        self.assertEqual(tasks.format_age(45), "45s")

    def test_format_age_750_seconds(self) -> None:
        """Test format_age with 750 seconds (12m 30s)."""
        self.assertEqual(tasks.format_age(750), "12m")

    def test_format_age_11100_seconds(self) -> None:
        """Test format_age with 11100 seconds (3h 5m)."""
        self.assertEqual(tasks.format_age(11100), "3h 5m")

    def test_format_age_187200_seconds(self) -> None:
        """Test format_age with 187200 seconds (2d 4h)."""
        self.assertEqual(tasks.format_age(187200), "2d 4h")

    def test_format_age_negative_clamps_to_zero(self) -> None:
        """Test format_age with negative value clamps to 0s."""
        self.assertEqual(tasks.format_age(-10), "0s")


if __name__ == "__main__":
    unittest.main()
