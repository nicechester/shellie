from __future__ import annotations

import os
import time
import unittest
from unittest import mock

from src.agent.core import shell


class ShellResolutionTests(unittest.TestCase):
    """Tests for resolve_shell()'s resolution chain (dev-plan A2-1 section 1.2)."""

    def setUp(self) -> None:
        self._usable_patcher = mock.patch.object(shell, "_is_usable_shell")
        self._settings_patcher = mock.patch.object(shell, "settings")
        self._environ_patcher = mock.patch.dict(os.environ, {}, clear=True)
        self._platform_patcher = mock.patch.object(shell.sys, "platform", "darwin")
        self._pwd_patcher = mock.patch.object(shell, "pwd")

        self.mock_usable = self._usable_patcher.start()
        self.mock_settings = self._settings_patcher.start()
        self._environ_patcher.start()
        self._platform_patcher.start()
        self.mock_pwd = self._pwd_patcher.start()

        self.addCleanup(self._usable_patcher.stop)
        self.addCleanup(self._settings_patcher.stop)
        self.addCleanup(self._environ_patcher.stop)
        self.addCleanup(self._platform_patcher.stop)
        self.addCleanup(self._pwd_patcher.stop)

        self.mock_settings.get.return_value = "auto"
        # By default, pwd lookups raise so tests must opt in explicitly.
        self.mock_pwd.getpwuid.side_effect = KeyError("no such user")

    def test_darwin_prefers_zsh(self) -> None:
        self.mock_usable.side_effect = lambda path: path == "/bin/zsh"
        result = shell.resolve_shell()
        self.assertEqual(result, "/bin/zsh")

    def test_non_darwin_uses_env_shell_when_usable(self) -> None:
        shell.sys.platform = "linux"
        os.environ["SHELL"] = "/usr/bin/fish"
        self.mock_usable.side_effect = lambda path: path == "/usr/bin/fish"
        result = shell.resolve_shell()
        self.assertEqual(result, "/usr/bin/fish")

    def test_env_shell_unset_falls_back_to_login_shell(self) -> None:
        shell.sys.platform = "linux"
        os.environ.pop("SHELL", None)
        self.mock_pwd.getpwuid.side_effect = None
        self.mock_pwd.getpwuid.return_value = mock.Mock(pw_shell="/usr/bin/login-shell")
        self.mock_usable.side_effect = lambda path: path == "/usr/bin/login-shell"
        result = shell.resolve_shell()
        self.assertEqual(result, "/usr/bin/login-shell")

    def test_all_candidates_unusable_falls_back_to_bin_sh(self) -> None:
        shell.sys.platform = "linux"
        os.environ.pop("SHELL", None)
        self.mock_pwd.getpwuid.side_effect = KeyError("no such user")
        self.mock_usable.side_effect = lambda path: path == "/bin/sh"
        result = shell.resolve_shell()
        self.assertEqual(result, "/bin/sh")

    def test_all_candidates_unusable_raises_when_bin_sh_also_unusable(self) -> None:
        shell.sys.platform = "linux"
        os.environ.pop("SHELL", None)
        self.mock_pwd.getpwuid.side_effect = KeyError("no such user")
        self.mock_usable.return_value = False
        with self.assertRaises(ValueError):
            shell.resolve_shell()

    def test_explicit_configured_shell_used_when_usable(self) -> None:
        self.mock_settings.get.return_value = "/opt/custom/shell"
        self.mock_usable.side_effect = lambda path: path == "/opt/custom/shell"
        result = shell.resolve_shell()
        self.assertEqual(result, "/opt/custom/shell")

    def test_explicit_configured_shell_raises_when_unusable(self) -> None:
        self.mock_settings.get.return_value = "/opt/does/not/exist"
        self.mock_usable.return_value = False
        with self.assertRaises(ValueError) as ctx:
            shell.resolve_shell()
        self.assertIn("/opt/does/not/exist", str(ctx.exception))


class ExecuteShellUnresolvableShellTests(unittest.TestCase):
    """execute_shell() must surface resolve_shell()'s ValueError as its return message."""

    def test_unusable_configured_shell_returns_korean_error_message(self) -> None:
        with mock.patch.object(shell, "settings") as mock_settings:
            mock_settings.get.side_effect = lambda key: {
                "SHELL_PATH": "/opt/does/not/exist",
            }.get(key)
            result = shell.execute_shell("echo hi")
        self.assertIn("Configured shell not found", result)
        self.assertIn("/opt/does/not/exist", result)


def _settings_get_factory(overrides: dict) -> "mock.Mock":
    defaults = {
        "SHELL_PATH": "auto",
        "SHELL_TIMEOUT_SEC": 10,
        "SHELL_OUTPUT_LIMIT": 3000,
    }
    defaults.update(overrides)

    def _get(key: str):
        return defaults[key]

    return _get


class ExecuteShellRealExecutionTests(unittest.TestCase):
    """Real subprocess execution using fast, POSIX-portable commands only."""

    def setUp(self) -> None:
        self._settings_patcher = mock.patch.object(shell, "settings")
        self.mock_settings = self._settings_patcher.start()
        self.addCleanup(self._settings_patcher.stop)
        self.mock_settings.get.side_effect = _settings_get_factory({})

    def test_stdout_only(self) -> None:
        result = shell.execute_shell("echo hello-out")
        self.assertIn("hello-out", result)
        self.assertNotIn("[stderr]", result)

    def test_stderr_only(self) -> None:
        result = shell.execute_shell("echo hello-err 1>&2")
        self.assertIn("hello-err", result)
        self.assertNotIn("[stderr]", result)

    def test_stdout_and_stderr_combined_with_separator(self) -> None:
        result = shell.execute_shell("echo out-part; echo err-part 1>&2")
        self.assertIn("out-part", result)
        self.assertIn("err-part", result)
        self.assertIn("[stderr]", result)

    def test_nonzero_exit_code_annotated(self) -> None:
        result = shell.execute_shell("exit 7")
        self.assertIn("[exit 7]", result)

    def test_empty_output_message(self) -> None:
        result = shell.execute_shell("true")
        self.assertIn("Executed successfully", result)

    def test_truncation_applies_configured_limit(self) -> None:
        self.mock_settings.get.side_effect = _settings_get_factory({"SHELL_OUTPUT_LIMIT": 200})
        # Build a 300-char output using only shell builtins and printf (POSIX-portable).
        long_output_cmd = "i=0; while [ $i -lt 300 ]; do printf X; i=$((i+1)); done"
        result = shell.execute_shell(long_output_cmd)
        self.assertLessEqual(len(result[:result.index("\n...")]), 200)
        self.assertIn("truncated", result)

    def test_secret_env_vars_removed_from_child(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "123:abcabcabcabcabcabcabcabcabcabc", "GEMINI_API_KEY": "secretkey1234567890"},
        ):
            result = shell.execute_shell(
                'sh -c \'echo "T=${TELEGRAM_BOT_TOKEN:-unset} G=${GEMINI_API_KEY:-unset}"\''
            )
        self.assertIn("T=unset", result)
        self.assertIn("G=unset", result)


class ExecuteShellQuoteRepairTests(unittest.TestCase):
    """Preflight quote-repair for over-escaped quote characters (real subprocess execution)."""

    def setUp(self) -> None:
        self._settings_patcher = mock.patch.object(shell, "settings")
        self.mock_settings = self._settings_patcher.start()
        self.addCleanup(self._settings_patcher.stop)
        self.mock_settings.get.side_effect = _settings_get_factory({})

    def test_over_escaped_single_quotes_repaired(self) -> None:
        # Literal command: echo '{"q": "\'abc\' in parents"}'
        command = "echo '{\"q\": \"\\'abc\\' in parents\"}'"
        result = shell.execute_shell(command)
        self.assertIn('{"q": "\'abc\' in parents"}', result)
        self.assertIn("Auto-repaired", result)
        self.assertNotIn("[exit", result)

    def test_syntax_error_result_includes_hint(self) -> None:
        result = shell.execute_shell('echo "')
        self.assertIn("Hint:", result)
        self.assertIn("[exit", result)

    def test_valid_backslash_double_quote_untouched(self) -> None:
        # Literal command: echo "say \"hi\""
        command = 'echo "say \\"hi\\""'
        result = shell.execute_shell(command)
        self.assertIn('say "hi"', result)
        self.assertNotIn("Auto-repaired", result)

    def test_command_without_backslash_quotes_skips_preflight(self) -> None:
        with mock.patch.object(shell, "_syntax_ok") as mock_syntax_ok:
            result = shell.execute_shell("echo plain")
        mock_syntax_ok.assert_not_called()
        self.assertIn("plain", result)


class ExecuteShellTimeoutTests(unittest.TestCase):
    """Timeout handling and grandchild process-group cleanup."""

    def setUp(self) -> None:
        self._settings_patcher = mock.patch.object(shell, "settings")
        self.mock_settings = self._settings_patcher.start()
        self.addCleanup(self._settings_patcher.stop)
        self.mock_settings.get.side_effect = _settings_get_factory(
            {"SHELL_TIMEOUT_SEC": 1}
        )

    def test_timeout_message_and_grandchild_cleanup(self) -> None:
        start = time.time()
        result = shell.execute_shell("sleep 30 & sleep 30 & wait")
        elapsed = time.time() - start

        self.assertIn("timed out", result)
        # Should return well before the grandchildren's own 30s sleep would finish.
        self.assertLess(elapsed, 6.0)

        # The module clears _active_pgid in its finally block once done.
        self.assertIsNone(shell._active_pgid)


class KillActiveTests(unittest.TestCase):
    """Direct unit test of kill_active() against a deterministic spawned process."""

    def tearDown(self) -> None:
        shell._active_pgid = None

    def test_kill_active_terminates_tracked_process_group(self) -> None:
        import subprocess

        proc = subprocess.Popen(
            ["sleep", "30"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            shell._active_pgid = proc.pid
            shell.kill_active()
            proc.wait(timeout=5)
            self.assertIsNotNone(proc.returncode)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_kill_active_noop_when_nothing_tracked(self) -> None:
        shell._active_pgid = None
        # Must not raise.
        shell.kill_active()


if __name__ == "__main__":
    unittest.main()
