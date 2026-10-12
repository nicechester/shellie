from __future__ import annotations

import unittest
from unittest import mock

from src.agent.core import gemini, llm
from src.agent.core import openai as openai_backend
from src.agent.core.gemini import Attempt


def _settings_get(overrides: dict):
    defaults = {
        "LLM_MODEL_CHAIN": ("gemini/g1", "openai/o1"),
        "GEMINI_API_KEY": "test-api-key-1234567890",
        "GEMINI_TIMEOUT_SEC": 30,
        "GEMINI_FALLBACK_DELAY_SEC": 1,
        "GEMINI_RETRY_BASE_DELAY_SEC": 60,
        "GEMINI_MAX_RETRIES": 0,
        "OPENAI_API_BASE": "https://api.example.com/v1",
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_TIMEOUT_SEC": 30,
    }
    defaults.update(overrides)
    return lambda key: defaults[key]


class MixedChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = mock.patch.object(llm, "settings").start()
        self.sleep = mock.patch.object(llm.time, "sleep").start()
        self.g_attempt = mock.patch.object(gemini, "attempt_model").start()
        self.o_attempt = mock.patch.object(openai_backend, "attempt_model").start()
        mock.patch.object(gemini, "build_payload", return_value={"contents": []}).start()
        mock.patch.object(openai_backend, "build_payload", return_value={"messages": []}).start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(gemini._set_retry_status, "idle")
        self.settings.get.side_effect = _settings_get({})

    def test_falls_back_from_gemini_to_openai_and_reports_entry(self) -> None:
        self.g_attempt.return_value = Attempt(False, None, None)
        self.o_attempt.return_value = Attempt(True, {"candidates": []}, None)
        response, entry = llm.call_llm([{"role": "user", "parts": [{"text": "hi"}]}])
        self.assertEqual(entry, "openai/o1")
        self.assertEqual(response, {"candidates": []})
        self.g_attempt.assert_called_once()
        self.sleep.assert_called_with(1)  # GEMINI_FALLBACK_DELAY_SEC between entries

    def test_openai_entry_gets_its_own_base_and_key(self) -> None:
        self.settings.get.side_effect = _settings_get({"LLM_MODEL_CHAIN": ("openai/o1",)})
        self.o_attempt.return_value = Attempt(True, {}, None)
        llm.call_llm([])
        args = self.o_attempt.call_args.args
        self.assertEqual(args[0], "o1")
        self.assertEqual(args[2], "https://api.example.com/v1")
        self.assertEqual(args[3], "sk-test")

    def test_whole_chain_cooldown_then_second_pass_succeeds(self) -> None:
        self.settings.get.side_effect = _settings_get({"GEMINI_MAX_RETRIES": 1})
        self.g_attempt.side_effect = [Attempt(False, None, None), Attempt(True, {}, None)]
        self.o_attempt.return_value = Attempt(False, None, None)
        on_cooldown = mock.Mock()
        _response, entry = llm.call_llm([], on_cooldown=on_cooldown)
        self.assertEqual(entry, "gemini/g1")
        on_cooldown.assert_called_once_with(1, 1, 60)
        self.assertIn(mock.call(60), self.sleep.call_args_list)

    def test_all_entries_failing_on_last_pass_raises(self) -> None:
        self.g_attempt.return_value = Attempt(False, None, None)
        self.o_attempt.return_value = Attempt(False, None, None)
        with self.assertRaises(Exception):
            llm.call_llm([])
        self.assertEqual(gemini.get_retry_status()["phase"], "idle")


class OpenAIAttemptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.http = mock.patch.object(openai_backend, "http_post_h").start()
        mock.patch.object(openai_backend, "build_system_instruction", return_value={"parts": []}).start()
        mock.patch.object(openai_backend, "build_tools_schema", return_value=[]).start()
        self.addCleanup(mock.patch.stopall)

    def test_skips_without_request_when_key_missing_on_remote_host(self) -> None:
        result = openai_backend.attempt_model("o1", {}, "https://api.openai.com/v1", "", 30)
        self.assertFalse(result.ok)
        self.http.assert_not_called()

    def test_empty_key_allowed_for_local_servers(self) -> None:
        self.http.return_value = ({"choices": [{"message": {"content": "ok"}}]}, 200, {})
        result = openai_backend.attempt_model("o1", {}, "http://192.168.1.188:11234/v1", "", 30)
        self.assertTrue(result.ok)

    def test_local_base_detection(self) -> None:
        self.assertTrue(openai_backend._is_local_base("http://localhost:11434/v1"))
        self.assertTrue(openai_backend._is_local_base("http://127.0.0.1:1234/v1"))
        self.assertTrue(openai_backend._is_local_base("http://[::1]:11434/v1"))
        self.assertTrue(openai_backend._is_local_base("http://192.168.1.5/v1"))
        self.assertTrue(openai_backend._is_local_base("http://mac-mini.local/v1"))
        self.assertFalse(openai_backend._is_local_base("https://openrouter.ai/api/v1"))
        self.assertFalse(openai_backend._is_local_base("https://api.openai.com/v1"))


if __name__ == "__main__":
    unittest.main()
