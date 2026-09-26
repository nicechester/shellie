from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import unittest
from unittest import mock

from src.agent.settings import CATALOG, SettingsStore, mask_secret_value

_CATALOG_BY_KEY = {spec.key: spec for spec in CATALOG}


def _valid_env(**overrides):
    """A minimal env dict that satisfies all three required keys, so
    load() never reports anything missing unless a test deliberately
    breaks one of them."""
    env = {
        "TELEGRAM_BOT_TOKEN": "123456789:" + "B" * 32,
        "GEMINI_API_KEY": "C" * 40,
        "ALLOWED_USER_ID": "222222222",
    }
    env.update(overrides)
    return env


class _SettingsTestCase(unittest.TestCase):
    """Base test case: gives every test a fresh temp dir and a helper to
    build fresh SettingsStore instances with injected env/path (5.1)."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.settings_path = os.path.join(self._tmpdir.name, "settings.json")

    def _new_store(self, env=None, path=None):
        store = SettingsStore()
        missing = store.load(
            env=env if env is not None else _valid_env(),
            path=path if path is not None else self.settings_path,
        )
        return store, missing

    def _write_override_file(self, values, path=None):
        payload = {"version": 1, "values": values}
        with open(path or self.settings_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

    def _row(self, store, key):
        for row in store.rows():
            if row["key"] == key:
                return row
        raise AssertionError("row not found: {}".format(key))


class TestLayerPrecedence(_SettingsTestCase):
    def test_default_layer_used_when_nothing_else_set(self):
        store, missing = self._new_store()
        self.assertEqual(missing, [])
        self.assertEqual(store.get("SHELL_TIMEOUT_SEC"), 45)
        self.assertEqual(self._row(store, "SHELL_TIMEOUT_SEC")["source"], "default")

    def test_env_layer_overrides_default(self):
        store, missing = self._new_store(env=_valid_env(SHELL_TIMEOUT_SEC="99"))
        self.assertEqual(missing, [])
        self.assertEqual(store.get("SHELL_TIMEOUT_SEC"), 99)
        self.assertEqual(self._row(store, "SHELL_TIMEOUT_SEC")["source"], "env")

    def test_override_layer_beats_env_layer(self):
        self._write_override_file({"SHELL_TIMEOUT_SEC": 150})
        store, missing = self._new_store(env=_valid_env(SHELL_TIMEOUT_SEC="99"))
        self.assertEqual(missing, [])
        self.assertEqual(store.get("SHELL_TIMEOUT_SEC"), 150)
        self.assertEqual(self._row(store, "SHELL_TIMEOUT_SEC")["source"], "override")

    def test_env_layer_only_reads_catalog_keys(self):
        env = _valid_env(TOTALLY_UNKNOWN_ENV_KEY="whatever")
        store, missing = self._new_store(env=env)
        self.assertEqual(missing, [])
        with self.assertRaises(KeyError):
            store.get("TOTALLY_UNKNOWN_ENV_KEY")


class TestInvalidValues(_SettingsTestCase):
    def test_invalid_env_value_is_warned_and_falls_back_to_default(self):
        env = _valid_env(SHELL_TIMEOUT_SEC="not-an-int")
        store = SettingsStore()
        with self.assertLogs("shellie.settings", level="WARNING"):
            missing = store.load(env=env, path=self.settings_path)
        self.assertEqual(missing, [])
        self.assertEqual(store.get("SHELL_TIMEOUT_SEC"), 45)
        self.assertEqual(self._row(store, "SHELL_TIMEOUT_SEC")["source"], "default")

    def test_invalid_required_env_value_is_reported_missing(self):
        env = _valid_env(TELEGRAM_BOT_TOKEN="not-a-valid-token")
        store = SettingsStore()
        with self.assertLogs("shellie.settings", level="WARNING"):
            missing = store.load(env=env, path=self.settings_path)
        self.assertIn("TELEGRAM_BOT_TOKEN", missing)


class TestBoundaryValidation(_SettingsTestCase):
    def test_shell_timeout_sec_boundaries(self):
        parser = _CATALOG_BY_KEY["SHELL_TIMEOUT_SEC"].parser
        for raw, expect_ok in (("0", False), ("1", True), ("600", True), ("601", False)):
            with self.subTest(raw=raw):
                if expect_ok:
                    self.assertEqual(parser(raw), int(raw))
                else:
                    with self.assertRaises(ValueError):
                        parser(raw)

    def test_gemini_model_chain_valid(self):
        parser = _CATALOG_BY_KEY["GEMINI_MODEL_CHAIN"].parser
        self.assertEqual(
            parser("gemini-2.5-flash,gemini-2.5-pro"),
            ("gemini-2.5-flash", "gemini-2.5-pro"),
        )

    def test_gemini_model_chain_rejects_bad_regex_item(self):
        parser = _CATALOG_BY_KEY["GEMINI_MODEL_CHAIN"].parser
        with self.assertRaises(ValueError):
            parser("Gemini-Pro")

    def test_gemini_model_chain_rejects_duplicates(self):
        parser = _CATALOG_BY_KEY["GEMINI_MODEL_CHAIN"].parser
        with self.assertRaises(ValueError):
            parser("gemini-2.5-flash,gemini-2.5-flash")

    def test_gemini_model_chain_rejects_trailing_comma(self):
        parser = _CATALOG_BY_KEY["GEMINI_MODEL_CHAIN"].parser
        with self.assertRaises(ValueError):
            parser("gemini-2.5-flash,")

    def test_bool_parser_accepts_known_forms(self):
        parser = _CATALOG_BY_KEY["WEB_ENABLED"].parser
        for raw in ("true", "on", "1", "켜기", "yes", "TRUE", "On"):
            with self.subTest(raw=raw):
                self.assertIs(parser(raw), True)
        for raw in ("false", "off", "0", "끄기", "no", "FALSE"):
            with self.subTest(raw=raw):
                self.assertIs(parser(raw), False)
        with self.assertRaises(ValueError):
            parser("maybe")

    def test_log_level_case_insensitive(self):
        parser = _CATALOG_BY_KEY["LOG_LEVEL"].parser
        self.assertEqual(parser("debug"), "DEBUG")
        self.assertEqual(parser("Warning"), "WARNING")
        with self.assertRaises(ValueError):
            parser("TRACE")

    def test_shell_path_auto_is_ok(self):
        parser = _CATALOG_BY_KEY["SHELL_PATH"].parser
        self.assertEqual(parser("auto"), "auto")

    def test_shell_path_relative_rejected(self):
        parser = _CATALOG_BY_KEY["SHELL_PATH"].parser
        with self.assertRaises(ValueError):
            parser("bin/sh")

    def test_shell_path_nonexistent_absolute_rejected(self):
        parser = _CATALOG_BY_KEY["SHELL_PATH"].parser
        with self.assertRaises(ValueError):
            parser("/definitely/not/a/real/shell/xyz-123")

    def test_mcp_servers_empty_string_ok(self):
        parser = _CATALOG_BY_KEY["MCP_SERVERS"].parser
        self.assertEqual(parser(""), "")
        self.assertEqual(parser("   "), "")

    def test_mcp_servers_valid_json_list_accepted(self):
        parser = _CATALOG_BY_KEY["MCP_SERVERS"].parser
        raw = json.dumps([
            {"name": "svc", "url": "https://example.com/mcp", "headers": {"Authorization": "Bearer x"}},
            {"name": "svc-2", "url": "http://localhost:9000/mcp"},
        ])
        self.assertEqual(parser(raw), raw)

    def test_mcp_servers_non_list_json_rejected(self):
        parser = _CATALOG_BY_KEY["MCP_SERVERS"].parser
        with self.assertRaises(ValueError):
            parser(json.dumps({"name": "svc", "url": "https://example.com"}))

    def test_mcp_servers_invalid_json_rejected(self):
        parser = _CATALOG_BY_KEY["MCP_SERVERS"].parser
        with self.assertRaises(ValueError):
            parser("{not valid json")

    def test_mcp_servers_bad_name_regex_rejected(self):
        parser = _CATALOG_BY_KEY["MCP_SERVERS"].parser
        raw = json.dumps([{"name": "bad name!", "url": "https://example.com"}])
        with self.assertRaises(ValueError):
            parser(raw)

    def test_mcp_servers_duplicate_names_rejected(self):
        parser = _CATALOG_BY_KEY["MCP_SERVERS"].parser
        raw = json.dumps([
            {"name": "svc", "url": "https://example.com/a"},
            {"name": "svc", "url": "https://example.com/b"},
        ])
        with self.assertRaises(ValueError):
            parser(raw)

    def test_mcp_servers_bad_url_rejected(self):
        parser = _CATALOG_BY_KEY["MCP_SERVERS"].parser
        raw = json.dumps([{"name": "svc", "url": "ftp://example.com"}])
        with self.assertRaises(ValueError):
            parser(raw)

    def test_mcp_servers_headers_non_dict_rejected(self):
        parser = _CATALOG_BY_KEY["MCP_SERVERS"].parser
        raw = json.dumps([{"name": "svc", "url": "https://example.com", "headers": ["not", "a", "dict"]}])
        with self.assertRaises(ValueError):
            parser(raw)

    def test_mcp_servers_headers_non_string_values_rejected(self):
        parser = _CATALOG_BY_KEY["MCP_SERVERS"].parser
        raw = json.dumps([{"name": "svc", "url": "https://example.com", "headers": {"X": 1}}])
        with self.assertRaises(ValueError):
            parser(raw)


class TestUpdate(_SettingsTestCase):
    def test_all_or_nothing_one_invalid_key_applies_nothing(self):
        store, _ = self._new_store()
        result = store.update(
            {"SHELL_TIMEOUT_SEC": "9999", "FC_MAX_LOOPS": "3"}, actor="web"
        )
        self.assertFalse(result.ok)
        self.assertEqual(store.get("SHELL_TIMEOUT_SEC"), 45)
        self.assertEqual(store.get("FC_MAX_LOOPS"), 5)
        self.assertEqual(store.revision, 0)

    def test_noop_value_excluded_from_applied(self):
        store, _ = self._new_store()
        result = store.update({"SHELL_TIMEOUT_SEC": "45"}, actor="web")
        self.assertTrue(result.ok)
        self.assertEqual(result.applied, [])
        self.assertEqual(store.revision, 0)

    def test_unknown_key_error_includes_difflib_suggestion(self):
        store, _ = self._new_store()
        result = store.update({"SHEL_TIMEOUT_SEC": "50"}, actor="web")
        self.assertFalse(result.ok)
        self.assertIn("SHEL_TIMEOUT_SEC", result.errors)
        self.assertIn("SHELL_TIMEOUT_SEC", result.errors["SHEL_TIMEOUT_SEC"])

    def test_telegram_actor_blocked_for_locked_keys(self):
        store, _ = self._new_store()
        locked_changes = {
            "TELEGRAM_BOT_TOKEN": "555555555:" + "D" * 32,
            "GEMINI_API_KEY": "E" * 40,
            "ALLOWED_USER_ID": "333333333",
        }
        for key, value in locked_changes.items():
            with self.subTest(key=key):
                result = store.update({key: value}, actor="telegram")
                self.assertFalse(result.ok)
                self.assertIn(key, result.errors)

    def test_web_actor_allowed_for_locked_keys(self):
        store, _ = self._new_store()
        new_token = "555555555:" + "D" * 32
        result = store.update({"TELEGRAM_BOT_TOKEN": new_token}, actor="web")
        self.assertTrue(result.ok)
        self.assertEqual(store.get("TELEGRAM_BOT_TOKEN"), new_token)


class TestUnset(_SettingsTestCase):
    def test_unset_removes_override_revealing_lower_layer(self):
        store, _ = self._new_store(env=_valid_env(SHELL_TIMEOUT_SEC="99"))
        result = store.update({"SHELL_TIMEOUT_SEC": "150"}, actor="web")
        self.assertTrue(result.ok)
        self.assertEqual(store.get("SHELL_TIMEOUT_SEC"), 150)

        unset_result = store.unset("SHELL_TIMEOUT_SEC", actor="web")
        self.assertTrue(unset_result.ok)
        self.assertEqual(store.get("SHELL_TIMEOUT_SEC"), 99)
        self.assertEqual(self._row(store, "SHELL_TIMEOUT_SEC")["source"], "env")

    def test_unset_refuses_when_no_valid_lower_layer_for_required_key(self):
        # ALLOWED_USER_ID absent from env entirely -> no env layer, no
        # default -> only a web-created override keeps it valid.
        env = {
            "TELEGRAM_BOT_TOKEN": _valid_env()["TELEGRAM_BOT_TOKEN"],
            "GEMINI_API_KEY": _valid_env()["GEMINI_API_KEY"],
        }
        store, missing = self._new_store(env=env)
        self.assertIn("ALLOWED_USER_ID", missing)

        result = store.update({"ALLOWED_USER_ID": "123456789"}, actor="web")
        self.assertTrue(result.ok)
        self.assertEqual(store.get("ALLOWED_USER_ID"), 123456789)

        unset_result = store.unset("ALLOWED_USER_ID", actor="web")
        self.assertFalse(unset_result.ok)
        self.assertIn("ALLOWED_USER_ID", unset_result.errors)
        # Refused -> override must still be in effect.
        self.assertEqual(store.get("ALLOWED_USER_ID"), 123456789)


class TestPersistence(_SettingsTestCase):
    def test_settings_file_written_with_0600(self):
        store, _ = self._new_store()
        result = store.update({"FC_MAX_LOOPS": "9"}, actor="web")
        self.assertTrue(result.ok)
        mode = stat.S_IMODE(os.stat(self.settings_path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_tmp_file_removed_after_successful_persist(self):
        store, _ = self._new_store()
        result = store.update({"FC_MAX_LOOPS": "9"}, actor="web")
        self.assertTrue(result.ok)
        self.assertFalse(os.path.exists(self.settings_path + ".tmp"))

    def test_persist_failure_keeps_memory_and_file_unchanged(self):
        store, _ = self._new_store()
        # os is imported as a whole module inside settings.py, so patching
        # "src.agent.settings.os.replace" patches the *shared* os module's
        # attribute process-wide for the duration of the `with` block, not
        # just this store instance. Keep the patch scope minimal.
        with mock.patch("src.agent.settings.os.replace", side_effect=OSError("disk full")):
            result = store.update({"FC_MAX_LOOPS": "9"}, actor="web")
        self.assertFalse(result.ok)
        self.assertIn("_persist", result.errors)
        self.assertEqual(store.get("FC_MAX_LOOPS"), 5)
        self.assertEqual(store.revision, 0)
        self.assertFalse(os.path.exists(self.settings_path))

    def test_corrupt_settings_json_is_renamed_and_ignored(self):
        with open(self.settings_path, "w", encoding="utf-8") as f:
            f.write("{not valid json!!")

        store = SettingsStore()
        with self.assertLogs("shellie.settings", level="WARNING"):
            missing = store.load(env=_valid_env(), path=self.settings_path)
        self.assertEqual(missing, [])
        self.assertFalse(os.path.exists(self.settings_path))

        corrupt_names = [
            name
            for name in os.listdir(self._tmpdir.name)
            if name.startswith(os.path.basename(self.settings_path) + ".corrupt-")
        ]
        self.assertEqual(len(corrupt_names), 1)

    def test_unknown_keys_in_override_file_are_warned_and_dropped(self):
        self._write_override_file({"NOT_A_REAL_KEY": "x", "SHELL_TIMEOUT_SEC": 88})
        store = SettingsStore()
        with self.assertLogs("shellie.settings", level="WARNING"):
            missing = store.load(env=_valid_env(), path=self.settings_path)
        self.assertEqual(missing, [])
        self.assertEqual(store.get("SHELL_TIMEOUT_SEC"), 88)
        with self.assertRaises(KeyError):
            store.get("NOT_A_REAL_KEY")


class TestMasking(_SettingsTestCase):
    def test_mask_secret_value_long_secret_shows_only_last_four(self):
        value = "K" * 36 + "6789"
        self.assertEqual(mask_secret_value("GEMINI_API_KEY", value), "••••6789")

    def test_mask_secret_value_short_secret_shows_no_tail(self):
        self.assertEqual(mask_secret_value("GEMINI_API_KEY", "short1"), "••••")

    def test_mask_secret_value_bot_token_form(self):
        value = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcd"
        self.assertEqual(
            mask_secret_value("TELEGRAM_BOT_TOKEN", value), "123456789:••••abcd"
        )

    def test_rows_mask_true_never_contains_raw_secret(self):
        token = "123456789:" + "Z" * 32
        gemini_key = "Q" * 40
        store, _ = self._new_store(
            env=_valid_env(TELEGRAM_BOT_TOKEN=token, GEMINI_API_KEY=gemini_key)
        )
        rendered = repr(store.rows(mask=True))
        self.assertNotIn(token, rendered)
        self.assertNotIn(gemini_key, rendered)


class TestHooks(_SettingsTestCase):
    def test_hook_fires_after_commit_with_new_value(self):
        store, _ = self._new_store()
        captured = []
        store.on_change("FC_MAX_LOOPS", captured.append)
        result = store.update({"FC_MAX_LOOPS": "8"}, actor="web")
        self.assertTrue(result.ok)
        self.assertEqual(captured, [8])

    def test_hook_exception_is_isolated_and_later_hooks_still_run(self):
        store, _ = self._new_store()
        calls = []

        def bad_hook(_new_value):
            raise RuntimeError("boom")

        def good_hook(new_value):
            calls.append(new_value)

        store.on_change("FC_MAX_LOOPS", bad_hook)
        store.on_change("FC_MAX_LOOPS", good_hook)

        with self.assertLogs("shellie.settings", level="ERROR"):
            result = store.update({"FC_MAX_LOOPS": "7"}, actor="web")

        self.assertTrue(result.ok)
        self.assertEqual(calls, [7])

    def test_revision_increments_per_commit_but_not_on_noop(self):
        store, _ = self._new_store()
        self.assertEqual(store.revision, 0)

        r1 = store.update({"FC_MAX_LOOPS": "8"}, actor="web")
        self.assertTrue(r1.ok)
        self.assertEqual(store.revision, 1)

        r2 = store.update({"CONTEXT_TURNS": "20"}, actor="web")
        self.assertTrue(r2.ok)
        self.assertEqual(store.revision, 2)

        r3 = store.update({"CONTEXT_TURNS": "20"}, actor="web")  # no-op
        self.assertTrue(r3.ok)
        self.assertEqual(store.revision, 2)


class TestPrecheck(_SettingsTestCase):
    def test_precheck_rejects_update_and_nothing_persisted(self):
        store, _ = self._new_store()

        def bad_precheck(_new_value):
            raise ValueError("precheck failed")

        store.register_precheck("FC_MAX_LOOPS", bad_precheck)
        # SettingSpec instances are shared module-level singletons (the
        # same CATALOG objects back every SettingsStore), so a precheck
        # registered here would otherwise leak into unrelated tests/files
        # that reuse the same catalog. Always clean up.
        self.addCleanup(store.register_precheck, "FC_MAX_LOOPS", None)

        result = store.update({"FC_MAX_LOOPS": "9"}, actor="web")
        self.assertFalse(result.ok)
        self.assertIn("FC_MAX_LOOPS", result.errors)
        self.assertEqual(store.get("FC_MAX_LOOPS"), 5)
        self.assertEqual(store.revision, 0)
        self.assertFalse(os.path.exists(self.settings_path))


class TestConcurrency(_SettingsTestCase):
    def test_concurrent_reads_during_updates_do_not_raise(self):
        store, _ = self._new_store()
        errors = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                try:
                    store.get("SHELL_TIMEOUT_SEC")
                    store.rows(mask=True)
                except Exception as exc:  # pragma: no cover - failure path
                    errors.append(exc)

        readers = [threading.Thread(target=reader, daemon=True) for _ in range(8)]
        for t in readers:
            t.start()

        last_value = None
        try:
            for i in range(20):
                value = 100 + i
                result = store.update({"SHELL_TIMEOUT_SEC": str(value)}, actor="web")
                self.assertTrue(result.ok)
                last_value = value
        finally:
            stop.set()
            for t in readers:
                t.join(timeout=2.0)

        self.assertEqual(errors, [])
        self.assertEqual(store.get("SHELL_TIMEOUT_SEC"), last_value)


if __name__ == "__main__":
    unittest.main()
