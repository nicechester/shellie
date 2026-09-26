from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from src.agent.core import memory

_TODAY = "2024-01-15"
_YESTERDAY = "2024-01-14"


class TestMemory(unittest.TestCase):
    """Tests for the memory module."""

    def setUp(self) -> None:
        """Set up a temporary directory and deterministic date for each test."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.memory_dir = self.temp_dir.name
        self.memory_file = os.path.join(self.memory_dir, "MEMORY.md")
        self._patches = [
            patch.object(memory, "MEMORY_DIR", self.memory_dir),
            patch.object(memory, "MEMORY_FILE", self.memory_file),
            patch.object(memory, "_today", lambda: _TODAY),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        """Stop patches and clean up the temporary directory."""
        for p in self._patches:
            p.stop()
        self.temp_dir.cleanup()

    def _dated_path(self, day: str) -> str:
        return os.path.join(self.memory_dir, "{}.md".format(day))

    # -- read_memory ------------------------------------------------------

    def test_read_memory_both_missing_returns_sentinel(self) -> None:
        """Test read_memory returns sentinel when core and dated files are both absent."""
        result = memory.read_memory()
        self.assertEqual(result, "No memory entries found.")

    def test_read_memory_core_empty_and_no_dated_file_returns_sentinel(self) -> None:
        """Test read_memory returns sentinel when the core file exists but is empty."""
        open(self.memory_file, "w").close()
        result = memory.read_memory()
        self.assertEqual(result, "No memory entries found.")

    def test_read_memory_core_only(self) -> None:
        """Test read_memory returns core content when no dated file exists (legacy MEMORY.md)."""
        with open(self.memory_file, "w", encoding="utf-8") as f:
            f.write("- core rule\n")
        result = memory.read_memory()
        self.assertEqual(result, "- core rule")

    def test_read_memory_today_only(self) -> None:
        """Test read_memory returns today's dated section when core is absent."""
        with open(self._dated_path(_TODAY), "w", encoding="utf-8") as f:
            f.write("- today entry\n")
        result = memory.read_memory()
        self.assertEqual(result, "## {}\n- today entry".format(_TODAY))

    def test_read_memory_core_and_today(self) -> None:
        """Test read_memory combines core content and today's dated section."""
        with open(self.memory_file, "w", encoding="utf-8") as f:
            f.write("- core rule\n")
        with open(self._dated_path(_TODAY), "w", encoding="utf-8") as f:
            f.write("- today entry\n")
        result = memory.read_memory()
        self.assertEqual(
            result,
            "- core rule\n\n## {}\n- today entry".format(_TODAY),
        )

    def test_read_memory_excludes_other_dates(self) -> None:
        """Test read_memory does not inject a dated file from a day other than today."""
        with open(self._dated_path(_YESTERDAY), "w", encoding="utf-8") as f:
            f.write("- old entry\n")
        result = memory.read_memory()
        self.assertEqual(result, "No memory entries found.")
        self.assertNotIn("old entry", result)

    # -- append_memory ------------------------------------------------------

    def test_append_memory_creates_todays_dated_file(self) -> None:
        """Test append_memory writes to today's dated file, not MEMORY.md."""
        result = memory.append_memory("hello")
        self.assertEqual(result, "Memory saved.")
        dated_path = self._dated_path(_TODAY)
        self.assertTrue(os.path.exists(dated_path))
        self.assertFalse(os.path.exists(self.memory_file))
        with open(dated_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertEqual(content, "- hello\n")

    def test_append_memory_file_ends_with_newline(self) -> None:
        """Test append_memory appends new entry when the dated file ends with a newline."""
        dated_path = self._dated_path(_TODAY)
        with open(dated_path, "w", encoding="utf-8") as f:
            f.write("- item1\n")
        result = memory.append_memory("item2")
        self.assertEqual(result, "Memory saved.")
        with open(dated_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertEqual(content, "- item1\n- item2\n")

    def test_append_memory_file_ends_without_newline(self) -> None:
        """Test append_memory inserts a newline before the entry when the dated file lacks one."""
        dated_path = self._dated_path(_TODAY)
        with open(dated_path, "w", encoding="utf-8") as f:
            f.write("- item1")
        result = memory.append_memory("item2")
        self.assertEqual(result, "Memory saved.")
        with open(dated_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertEqual(content, "- item1\n- item2\n")

    def test_append_memory_multiline_input_normalized(self) -> None:
        """Test append_memory normalizes multi-line input to a single line."""
        result = memory.append_memory("a\nb\r\nc")
        self.assertEqual(result, "Memory saved.")
        with open(self._dated_path(_TODAY), "r", encoding="utf-8") as f:
            content = f.read()
        self.assertEqual(content, "- a b c\n")

    def test_append_memory_whitespace_collapsed(self) -> None:
        """Test append_memory collapses multiple whitespace runs into single spaces."""
        result = memory.append_memory("a    b\t c")
        self.assertEqual(result, "Memory saved.")
        with open(self._dated_path(_TODAY), "r", encoding="utf-8") as f:
            content = f.read()
        self.assertEqual(content, "- a b c\n")

    def test_append_memory_empty_input(self) -> None:
        """Test append_memory with empty input returns error and does not create a dated file."""
        result = memory.append_memory("")
        self.assertEqual(result, "Nothing to save.")
        self.assertFalse(os.path.exists(self._dated_path(_TODAY)))

    def test_append_memory_whitespace_only_input(self) -> None:
        """Test append_memory with whitespace-only input returns error and creates no file."""
        result = memory.append_memory("   \n\t\n   ")
        self.assertEqual(result, "Nothing to save.")
        self.assertFalse(os.path.exists(self._dated_path(_TODAY)))

    def test_append_memory_success_return_value(self) -> None:
        """Test append_memory returns success message on successful save."""
        result = memory.append_memory("test content")
        self.assertEqual(result, "Memory saved.")

    def test_append_memory_does_not_write_core_file(self) -> None:
        """Test append_memory never writes to the core MEMORY.md file."""
        result = memory.append_memory("new fact")
        self.assertEqual(result, "Memory saved.")
        self.assertFalse(os.path.exists(self.memory_file))


if __name__ == "__main__":
    unittest.main()
