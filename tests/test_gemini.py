from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from src.agent.core import gemini


def _settings_get(overrides: dict):
    defaults = {
        "GEMINI_MODEL_CHAIN": ("m1", "m2"),
        "GEMINI_API_KEY": "test-api-key-1234567890",
        "GEMINI_TIMEOUT_SEC": 30,
        "GEMINI_FALLBACK_DELAY_SEC": 1,
        "GEMINI_RETRY_BASE_DELAY_SEC": 60,
        "GEMINI_MAX_RETRIES": 0,
        "SYSTEM_PROMPT": "SYS",
    }
    defaults.update(overrides)

    def _get(key: str):
        return defaults[key]

    return _get


class CallGeminiFallbackTests(unittest.TestCase):
    """Fallback matrix and header/URL contract for call_gemini()."""

    def setUp(self) -> None:
        self.api_key = "test-api-key-1234567890"

        settings_patcher = mock.patch.object(gemini, "settings")
        http_post_patcher = mock.patch.object(gemini, "http_post_h")
        sleep_patcher = mock.patch.object(gemini.time, "sleep")
        read_memory_patcher = mock.patch.object(gemini, "read_memory", return_value="mem")
        list_skills_patcher = mock.patch.object(gemini, "list_skills", return_value="")
        exec_env_patcher = mock.patch.object(
            gemini.shell,
            "execution_environment",
            return_value={"os": "Linux", "arch": "x86_64", "shell": "/bin/sh", "cwd": "/base"},
        )

        self.mock_settings = settings_patcher.start()
        self.mock_http_post = http_post_patcher.start()
        self.mock_sleep = sleep_patcher.start()
        read_memory_patcher.start()
        list_skills_patcher.start()
        exec_env_patcher.start()

        for patcher in (
            settings_patcher,
            http_post_patcher,
            sleep_patcher,
            read_memory_patcher,
            list_skills_patcher,
            exec_env_patcher,
        ):
            self.addCleanup(patcher.stop)

        self._configure_settings({})

    def _configure_settings(self, overrides: dict) -> None:
        self.mock_settings.get.side_effect = _settings_get(overrides)

    def _contents(self):
        return [{"role": "user", "parts": [{"text": "hi"}]}]

    def _rpd_429(self) -> dict:
        """Real-world RPD 429 shape with QuotaFailure.quotaId containing PerDay."""
        return {
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaValue": "20"}],
                    },
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "30s"},
                ],
            }
        }

    def _rpm_429(self, delay_s: str = "50s") -> dict:
        """Real-world RPM 429 shape with QuotaFailure.quotaId containing PerMinute."""
        return {
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "quotaValue": "5"}],
                    },
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay_s},
                ],
            }
        }

    def test_429_rpd_falls_back_to_second_model(self) -> None:
        """RPD 429 (PerDay quotaId) falls back to the next model immediately."""
        res2 = {"candidates": [], "_marker": "second"}
        self.mock_http_post.side_effect = [(self._rpd_429(), 429, {}), (res2, 200, {})]

        result, model = gemini.call_gemini(self._contents())

        self.assertEqual(model, "m2")
        self.assertEqual(result, res2)
        self.assertEqual(self.mock_http_post.call_count, 2)
        second_url = self.mock_http_post.call_args_list[1][0][0]
        self.assertIn("m2", second_url)

    def test_429_rpm_retries_same_model_then_succeeds(self) -> None:
        """RPM 429 (PerMinute quotaId) sleeps the retryDelay and retries the same model."""
        res2 = {"candidates": [], "_marker": "retry"}
        self.mock_http_post.side_effect = [(self._rpm_429("30s"), 429, {}), (res2, 200, {})]

        result, model = gemini.call_gemini(self._contents())

        self.assertEqual(model, "m1")
        self.assertEqual(result, res2)
        self.assertEqual(self.mock_http_post.call_count, 2)
        self.assertIn("m1", self.mock_http_post.call_args_list[0][0][0])
        self.assertIn("m1", self.mock_http_post.call_args_list[1][0][0])
        self.mock_sleep.assert_called_once_with(30.0)

    def test_429_rpm_retry_fails_falls_back_to_second_model(self) -> None:
        """RPM 429 that still fails after one retry falls back to next model."""
        res2 = {"candidates": [], "_marker": "second"}
        self.mock_http_post.side_effect = [
            (self._rpm_429("5s"), 429, {}),
            (self._rpm_429("5s"), 429, {}),
            (res2, 200, {}),
        ]

        result, model = gemini.call_gemini(self._contents())

        self.assertEqual(model, "m2")
        self.assertEqual(result, res2)

    def test_resource_exhausted_body_on_non_200_falls_back(self) -> None:
        """RESOURCE_EXHAUSTED on non-429 status (e.g. 403) always falls back."""
        res1 = {"error": {"status": "RESOURCE_EXHAUSTED"}}
        res2 = {"candidates": []}
        self.mock_http_post.side_effect = [(res1, 403, {}), (res2, 200, {})]

        result, model = gemini.call_gemini(self._contents())

        self.assertEqual(model, "m2")
        self.assertEqual(result, res2)

    def test_503_retries_same_model_then_succeeds(self) -> None:
        """503 retries the same model with short backoff before succeeding."""
        res_503 = {"error": {"code": 503, "status": "UNAVAILABLE"}}
        res_ok = {"candidates": []}
        self.mock_http_post.side_effect = [(res_503, 503, {}), (res_ok, 200, {})]

        _result, model = gemini.call_gemini(self._contents())

        self.assertEqual(model, "m1")
        # First retry sleep is _5XX_RETRY_BASE_SEC * 2^0 = 1
        self.mock_sleep.assert_called_once_with(gemini._5XX_RETRY_BASE_SEC)

    def test_503_exhausts_retries_then_falls_back_to_second_model(self) -> None:
        """503 that persists through all same-model retries falls back to next model."""
        res_503 = {"error": {"code": 503, "status": "UNAVAILABLE"}}
        res_ok = {"candidates": []}
        # 1 initial + _5XX_MAX_RETRIES same-model retries, then m2 succeeds
        side = [(res_503, 503, {})] * (1 + gemini._5XX_MAX_RETRIES) + [(res_ok, 200, {})]
        self.mock_http_post.side_effect = side

        _result, model = gemini.call_gemini(self._contents())

        self.assertEqual(model, "m2")
        # Sleeps: 1s, 2s, 4s (backoff) then fallback_delay (1s) before m2
        expected_sleeps = [
            mock.call(gemini._5XX_RETRY_BASE_SEC * (2 ** i))
            for i in range(gemini._5XX_MAX_RETRIES)
        ] + [mock.call(1)]  # GEMINI_FALLBACK_DELAY_SEC between models
        self.assertEqual(self.mock_sleep.call_args_list, expected_sleeps)

    def test_503_falls_back(self) -> None:
        """503 that persists falls back after retries (covered by exhausts test above)."""
        res_503 = {"error": {"code": 503, "status": "UNAVAILABLE"}}
        res_ok = {"candidates": []}
        side = [(res_503, 503, {})] * (1 + gemini._5XX_MAX_RETRIES) + [(res_ok, 200, {})]
        self.mock_http_post.side_effect = side

        _result, model = gemini.call_gemini(self._contents())

        self.assertEqual(model, "m2")

    def test_status_zero_falls_back(self) -> None:
        res_err = {"error": "transport failure"}
        res_ok = {"candidates": []}
        side = [(res_err, 0, {})] * (1 + gemini._5XX_MAX_RETRIES) + [(res_ok, 200, {})]
        self.mock_http_post.side_effect = side

        _result, model = gemini.call_gemini(self._contents())

        self.assertEqual(model, "m2")

    def test_400_raises_immediately_without_second_call(self) -> None:
        res1 = {"error": {"status": "INVALID_ARGUMENT", "message": "bad request"}}
        self.mock_http_post.side_effect = [(res1, 400, {})]

        with self.assertRaises(Exception) as ctx:
            gemini.call_gemini(self._contents())

        self.assertEqual(self.mock_http_post.call_count, 1)
        self.assertNotIn(self.api_key, str(ctx.exception))

    def test_all_models_fail_raises_final_exception(self) -> None:
        res = {"error": {"code": 503, "status": "UNAVAILABLE"}}
        # Each model needs 1 initial + _5XX_MAX_RETRIES attempts
        per_model = (1 + gemini._5XX_MAX_RETRIES)
        self.mock_http_post.side_effect = [(res, 503, {})] * (per_model * 2)  # 2 models

        with self.assertRaises(Exception) as ctx:
            gemini.call_gemini(self._contents())

        self.assertIn("All Gemini models", str(ctx.exception))

    def test_uses_api_key_header_never_in_url(self) -> None:
        res = {"candidates": []}
        self.mock_http_post.return_value = (res, 200, {})

        gemini.call_gemini(self._contents())

        args, kwargs = self.mock_http_post.call_args
        url = args[0]
        headers = kwargs["headers"]
        self.assertNotIn(self.api_key, url)
        self.assertEqual(headers["x-goog-api-key"], self.api_key)

    def test_fallback_delay_used_for_rpm_sleep_when_no_hint(self) -> None:
        """When no retryDelay hint is present, RPM sleep falls back to GEMINI_FALLBACK_DELAY_SEC."""
        self._configure_settings({"GEMINI_FALLBACK_DELAY_SEC": 4})
        # RPM 429 with no RetryInfo detail
        res1 = {
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "details": [{
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}],
                }],
            }
        }
        res2 = {"candidates": []}
        self.mock_http_post.side_effect = [(res1, 429, {}), (res2, 200, {})]

        gemini.call_gemini(self._contents())

        self.mock_sleep.assert_called_once_with(4)

    def test_retry_after_cooldown_succeeds_on_second_pass(self) -> None:
        """After all models fail (RPD), the whole chain is retried after cooldown."""
        self._configure_settings({
            "GEMINI_MODEL_CHAIN": ("m1",),
            "GEMINI_MAX_RETRIES": 1,
            "GEMINI_RETRY_BASE_DELAY_SEC": 60,
        })
        res2 = {"candidates": [], "_marker": "second-pass"}
        self.mock_http_post.side_effect = [(self._rpd_429(), 429, {}), (res2, 200, {})]

        result, model = gemini.call_gemini(self._contents())

        self.assertEqual(model, "m1")
        self.assertEqual(result, res2)
        self.mock_sleep.assert_called_once_with(60)

    def test_server_hint_overrides_exponential_backoff(self) -> None:
        """RPD 429 with a server retryDelay hint larger than base_delay uses the hint for cooldown."""
        self._configure_settings({
            "GEMINI_MODEL_CHAIN": ("m1",),
            "GEMINI_MAX_RETRIES": 1,
            "GEMINI_RETRY_BASE_DELAY_SEC": 60,
        })
        res1 = {
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}],
                    },
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "90s"},
                ],
            }
        }
        res2 = {"candidates": []}
        self.mock_http_post.side_effect = [(res1, 429, {}), (res2, 200, {})]

        gemini.call_gemini(self._contents())

        self.mock_sleep.assert_called_once_with(90.0)

    def test_exponential_backoff_growth_then_raises(self) -> None:
        self._configure_settings({
            "GEMINI_MODEL_CHAIN": ("m1",),
            "GEMINI_MAX_RETRIES": 2,
            "GEMINI_RETRY_BASE_DELAY_SEC": 60,
        })
        res = {"error": {"code": 503, "status": "UNAVAILABLE"}}
        # Each chain pass: 1 initial + _5XX_MAX_RETRIES same-model retries; 3 passes total
        per_pass = 1 + gemini._5XX_MAX_RETRIES
        self.mock_http_post.side_effect = [(res, 503, {})] * (per_pass * 3)

        with self.assertRaises(Exception) as ctx:
            gemini.call_gemini(self._contents())

        self.assertIn("All Gemini models", str(ctx.exception))
        # Chain-retry cooldown sleeps: 60, 120 (exponential backoff between passes)
        chain_cooldown_calls = [c for c in self.mock_sleep.call_args_list if c == mock.call(60) or c == mock.call(120)]
        self.assertEqual(chain_cooldown_calls, [mock.call(60), mock.call(120)])

    def test_cooldown_delay_is_capped(self) -> None:
        self._configure_settings({
            "GEMINI_MODEL_CHAIN": ("m1",),
            "GEMINI_MAX_RETRIES": 2,
            "GEMINI_RETRY_BASE_DELAY_SEC": 200,
        })
        res2 = {"candidates": []}
        self.mock_http_post.side_effect = [(self._rpd_429(), 429, {}), (self._rpd_429(), 429, {}), (res2, 200, {})]

        gemini.call_gemini(self._contents())

        self.assertEqual(
            self.mock_sleep.call_args_list,
            [mock.call(200), mock.call(gemini._MAX_COOLDOWN_SEC)],
        )

    def test_non_fallback_status_raises_without_retry_or_cooldown(self) -> None:
        self._configure_settings({
            "GEMINI_MODEL_CHAIN": ("m1",),
            "GEMINI_MAX_RETRIES": 2,
        })
        res = {"error": {"status": "PERMISSION_DENIED", "message": "forbidden"}}
        self.mock_http_post.side_effect = [(res, 403, {})]

        with self.assertRaises(Exception):
            gemini.call_gemini(self._contents())

        self.assertEqual(self.mock_http_post.call_count, 1)
        self.mock_sleep.assert_not_called()

    def test_on_cooldown_callback_invoked_and_survives_exception(self) -> None:
        self._configure_settings({
            "GEMINI_MODEL_CHAIN": ("m1",),
            "GEMINI_MAX_RETRIES": 1,
            "GEMINI_RETRY_BASE_DELAY_SEC": 60,
        })
        res2 = {"candidates": []}
        self.mock_http_post.side_effect = [(self._rpd_429(), 429, {}), (res2, 200, {})]
        on_cooldown = mock.Mock(side_effect=RuntimeError("boom"))

        result, _model = gemini.call_gemini(self._contents(), on_cooldown=on_cooldown)

        on_cooldown.assert_called_once_with(1, 1, 60)
        self.assertEqual(result, res2)

    def test_no_inter_model_sleep_after_last_model_in_pass(self) -> None:
        """No inter-model fallback sleep after the last model in a pass."""
        self._configure_settings({
            "GEMINI_MODEL_CHAIN": ("m1", "m2"),
            "GEMINI_MAX_RETRIES": 0,
            "GEMINI_FALLBACK_DELAY_SEC": 4,
        })
        self.mock_http_post.side_effect = [(self._rpd_429(), 429, {}), (self._rpd_429(), 429, {})]

        with self.assertRaises(Exception):
            gemini.call_gemini(self._contents())

        self.mock_sleep.assert_called_once_with(4)


class ParseRetryDelayTests(unittest.TestCase):
    """_parse_retry_delay() pure-function behavior; no mocking needed."""

    def test_parses_fractional_seconds_from_retry_info_details(self) -> None:
        res = {
            "error": {
                "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "18.65s"}],
            }
        }
        self.assertEqual(gemini._parse_retry_delay(res), 18.65)

    def test_parses_integer_seconds_from_retry_info_details(self) -> None:
        res = {
            "error": {
                "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "18s"}],
            }
        }
        self.assertEqual(gemini._parse_retry_delay(res), 18.0)

    def test_parses_from_message_fallback_when_no_details(self) -> None:
        res = {"error": {"message": "Quota exceeded. Please retry in 7s."}}
        self.assertEqual(gemini._parse_retry_delay(res), 7.0)

    def test_returns_none_on_garbage_or_missing_input(self) -> None:
        self.assertIsNone(gemini._parse_retry_delay({}))
        self.assertIsNone(gemini._parse_retry_delay({"error": "not-a-dict"}))
        self.assertIsNone(gemini._parse_retry_delay({"error": {"message": "no delay info here"}}))
        self.assertIsNone(gemini._parse_retry_delay({"error": {"details": "not-a-list"}}))
        self.assertIsNone(gemini._parse_retry_delay("not-a-dict"))  # type: ignore[arg-type]
        self.assertIsNone(gemini._parse_retry_delay(None))  # type: ignore[arg-type]


class IsRpdLimitTests(unittest.TestCase):
    """_is_rpd_limit() detects RPD via QuotaFailure.quotaId containing 'PerDay'."""

    def _rpd_res(self, quota_id: str = "GenerateRequestsPerDayPerProjectPerModel-FreeTier") -> dict:
        return {
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaId": quota_id, "quotaValue": "20"}],
                    }
                ],
            }
        }

    def _rpm_res(self) -> dict:
        return {
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "quotaValue": "5"}],
                    }
                ],
            }
        }

    def test_quota_id_perday_is_rpd(self) -> None:
        self.assertTrue(gemini._is_rpd_limit(self._rpd_res(), None))

    def test_quota_id_perday_case_insensitive(self) -> None:
        self.assertTrue(gemini._is_rpd_limit(self._rpd_res("generateRequestsperDAYperProject"), None))

    def test_quota_id_perminute_is_not_rpd(self) -> None:
        self.assertFalse(gemini._is_rpd_limit(self._rpm_res(), None))

    def test_no_quota_failure_detail_is_not_rpd(self) -> None:
        res = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": []}}
        self.assertFalse(gemini._is_rpd_limit(res, None))

    def test_per_day_message_fallback_is_rpd(self) -> None:
        res = {"error": {"message": "Quota exceeded for requests per day.", "details": []}}
        self.assertTrue(gemini._is_rpd_limit(res, None))

    def test_daily_message_fallback_is_rpd(self) -> None:
        res = {"error": {"message": "Daily quota limit reached.", "details": []}}
        self.assertTrue(gemini._is_rpd_limit(res, None))

    def test_generic_message_is_not_rpd(self) -> None:
        res = {"error": {"message": "Rate limit exceeded, retry in 30s.", "details": []}}
        self.assertFalse(gemini._is_rpd_limit(res, None))

    def test_account_quota_exhausted_message_is_rpd(self) -> None:
        """Account-level RPD: no QuotaFailure detail, only google.rpc.Help link."""
        res = {
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "message": "You exceeded your current quota, please check your plan and billing details.",
                "details": [{"@type": "type.googleapis.com/google.rpc.Help", "links": []}],
            }
        }
        self.assertTrue(gemini._is_rpd_limit(res, None))

    def test_short_delay_with_rpd_quota_id_is_still_rpd(self) -> None:
        # Real RPD responses have short retryDelay (18-50s), not hours.
        # The delay value must NOT affect the result.
        self.assertTrue(gemini._is_rpd_limit(self._rpd_res(), 30.0))

    def test_short_delay_with_rpm_quota_id_is_not_rpd(self) -> None:
        self.assertFalse(gemini._is_rpd_limit(self._rpm_res(), 50.0))


class ParseRetryDelayWithHeadersTests(unittest.TestCase):
    """_parse_retry_delay_with_headers() picks the larger of body vs header."""

    def test_header_wins_when_larger(self) -> None:
        res = {"error": {"message": "retry in 10s"}}
        self.assertEqual(gemini._parse_retry_delay_with_headers(res, {"retry-after": "120"}), 120.0)

    def test_body_wins_when_larger(self) -> None:
        res = {
            "error": {
                "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "90s"}]
            }
        }
        self.assertEqual(gemini._parse_retry_delay_with_headers(res, {"retry-after": "30"}), 90.0)

    def test_header_only(self) -> None:
        self.assertEqual(gemini._parse_retry_delay_with_headers({}, {"retry-after": "45"}), 45.0)

    def test_no_hint_returns_none(self) -> None:
        self.assertIsNone(gemini._parse_retry_delay_with_headers({}, {}))

    def test_non_numeric_header_ignored(self) -> None:
        res = {"error": {"message": "retry in 5s"}}
        self.assertEqual(gemini._parse_retry_delay_with_headers(res, {"retry-after": "Wed, 01 Jan 2025 00:00:00 GMT"}), 5.0)


class ParseResponseTests(unittest.TestCase):
    """parse_response() pure-function behavior; no mocking needed."""

    def test_empty_candidates_with_block_reason_is_blocked(self) -> None:
        response = {"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}}
        parsed = gemini.parse_response(response)
        self.assertTrue(parsed.blocked)
        self.assertEqual(parsed.block_reason, "SAFETY")
        self.assertEqual(parsed.text, "")
        self.assertEqual(parsed.function_calls, [])

    def test_empty_candidates_without_block_reason_is_not_blocked(self) -> None:
        response = {"candidates": []}
        parsed = gemini.parse_response(response)
        self.assertFalse(parsed.blocked)
        self.assertIsNone(parsed.block_reason)

    def test_content_missing_preserves_finish_reason_and_empty_text(self) -> None:
        response = {"candidates": [{"finishReason": "MAX_TOKENS"}]}
        parsed = gemini.parse_response(response)
        self.assertEqual(parsed.finish_reason, "MAX_TOKENS")
        self.assertEqual(parsed.text, "")
        self.assertEqual(parsed.function_calls, [])
        self.assertFalse(parsed.blocked)

    def test_multi_part_text_joined_and_function_calls_in_order(self) -> None:
        content = {
            "parts": [
                {"text": "hello "},
                {"functionCall": {"name": "f1", "args": {"a": 1}}},
                {"text": "world"},
                {"functionCall": {"name": "f2", "args": {"b": 2}}},
            ]
        }
        response = {"candidates": [{"content": content, "finishReason": "STOP"}]}
        parsed = gemini.parse_response(response)
        self.assertEqual(parsed.text, "hello world")
        self.assertEqual(parsed.function_calls, [("f1", {"a": 1}), ("f2", {"b": 2})])
        self.assertIs(parsed.raw_content, content)
        self.assertEqual(parsed.finish_reason, "STOP")

    def test_thought_part_is_skipped(self) -> None:
        content = {"parts": [{"thought": True, "text": "internal reasoning"}, {"text": "final"}]}
        response = {"candidates": [{"content": content}]}
        parsed = gemini.parse_response(response)
        self.assertEqual(parsed.text, "final")

    def test_malformed_non_dict_input_returns_safe_empty(self) -> None:
        parsed = gemini.parse_response("not a dict")  # type: ignore[arg-type]
        self.assertEqual(parsed.text, "")
        self.assertEqual(parsed.function_calls, [])
        self.assertFalse(parsed.blocked)
        self.assertIsNone(parsed.block_reason)
        self.assertIsNone(parsed.finish_reason)
        self.assertEqual(parsed.raw_content, {})

    def test_non_dict_candidate_returns_safe_empty(self) -> None:
        response = {"candidates": ["not-a-dict"]}
        parsed = gemini.parse_response(response)
        self.assertEqual(parsed.text, "")
        self.assertEqual(parsed.function_calls, [])
        self.assertIsNone(parsed.finish_reason)


class ToolsRegistryTests(unittest.TestCase):
    def test_execute_shell_declaration_description_contains_environment_info(self) -> None:
        with mock.patch.object(
            gemini.shell,
            "execution_environment",
            return_value={"os": "Linux", "arch": "x86_64", "shell": "/bin/bash", "cwd": "/base"},
        ):
            schema = gemini.build_tools_schema()

        fc_entry = next(e for e in schema if "functionDeclarations" in e)
        declarations = fc_entry["functionDeclarations"]
        exec_decl = next(d for d in declarations if d["name"] == "execute_shell")
        self.assertIn("Linux", exec_decl["description"])
        self.assertIn("x86_64", exec_decl["description"])
        self.assertIn("/bin/bash", exec_decl["description"])

    def test_run_tool_unknown_function_name(self) -> None:
        result = gemini.run_tool("unknown", {})
        self.assertEqual(result, "Unknown function: unknown")


class ListSkillsTests(unittest.TestCase):
    def test_docstringed_private_and_syntax_error_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "alpha.py"), "w", encoding="utf-8") as f:
                f.write('"""Alpha skill does something useful.\n\nMore detail below.\n"""\n')
            with open(os.path.join(tmp, "_internal.py"), "w", encoding="utf-8") as f:
                f.write('"""Should never appear in the listing."""\n')
            with open(os.path.join(tmp, "broken.py"), "w", encoding="utf-8") as f:
                f.write("def broken(:\n    pass\n")

            with mock.patch.object(gemini, "SKILLS_DIR", tmp), \
                 mock.patch.object(gemini, "REPO_SKILLS_DIR", "/nonexistent"):
                result = gemini.list_skills()

        lines = result.split("\n")
        self.assertIn("- skills/alpha.py: Alpha skill does something useful.", lines)
        self.assertIn("- skills/broken.py", lines)
        self.assertFalse(any("_internal" in line for line in lines))

    def test_md_skill_with_heading_strips_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "x.md"), "w", encoding="utf-8") as f:
                f.write("# Title\n\nSome procedure body.\n")

            with mock.patch.object(gemini, "SKILLS_DIR", tmp), \
                 mock.patch.object(gemini, "REPO_SKILLS_DIR", "/nonexistent"):
                result = gemini.list_skills()

        self.assertEqual(result, "- skills/x.md: Title")

    def test_md_skill_with_plain_text_first_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "x.md"), "w", encoding="utf-8") as f:
                f.write("Plain text summary line.\nMore body.\n")

            with mock.patch.object(gemini, "SKILLS_DIR", tmp), \
                 mock.patch.object(gemini, "REPO_SKILLS_DIR", "/nonexistent"):
                result = gemini.list_skills()

        self.assertEqual(result, "- skills/x.md: Plain text summary line.")

    def test_empty_md_skill_is_name_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "x.md"), "w", encoding="utf-8") as f:
                f.write("")

            with mock.patch.object(gemini, "SKILLS_DIR", tmp), \
                 mock.patch.object(gemini, "REPO_SKILLS_DIR", "/nonexistent"):
                result = gemini.list_skills()

        self.assertEqual(result, "- skills/x.md")

    def test_readme_md_is_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "README.md"), "w", encoding="utf-8") as f:
                f.write("# skills/\n\nShould never appear.\n")

            with mock.patch.object(gemini, "SKILLS_DIR", tmp), \
                 mock.patch.object(gemini, "REPO_SKILLS_DIR", "/nonexistent"):
                result = gemini.list_skills()

        self.assertEqual(result, "")

    def test_underscore_prefixed_md_is_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "_draft.md"), "w", encoding="utf-8") as f:
                f.write("# Draft\n\nWork in progress.\n")

            with mock.patch.object(gemini, "SKILLS_DIR", tmp), \
                 mock.patch.object(gemini, "REPO_SKILLS_DIR", "/nonexistent"):
                result = gemini.list_skills()

        self.assertEqual(result, "")

    def test_mixed_py_and_md_listing_includes_both(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "alpha.py"), "w", encoding="utf-8") as f:
                f.write('"""Alpha skill summary."""\n')
            with open(os.path.join(tmp, "beta.md"), "w", encoding="utf-8") as f:
                f.write("# Beta procedure\n")

            with mock.patch.object(gemini, "SKILLS_DIR", tmp), \
                 mock.patch.object(gemini, "REPO_SKILLS_DIR", "/nonexistent"):
                result = gemini.list_skills()

        lines = result.split("\n")
        self.assertIn("- skills/alpha.py: Alpha skill summary.", lines)
        self.assertIn("- skills/beta.md: Beta procedure", lines)


class BuildSystemInstructionTests(unittest.TestCase):
    def test_all_sections_present_when_skills_exist(self) -> None:
        with mock.patch.object(gemini, "settings") as mock_settings, \
                mock.patch.object(gemini, "read_memory", return_value="MEMCONTENT"), \
                mock.patch.object(gemini, "list_skills", return_value="- skills/foo.py: desc"), \
                mock.patch.object(
                    gemini.shell,
                    "execution_environment",
                    return_value={"os": "Linux", "arch": "x86_64", "shell": "/bin/bash", "cwd": "/base"},
                ):
            mock_settings.get.return_value = "SYSTEM PROMPT TEXT"
            instruction = gemini.build_system_instruction()

        text = instruction["parts"][0]["text"]
        self.assertIn("SYSTEM PROMPT TEXT", text)
        self.assertIn("[long-term memory]", text)
        self.assertIn("MEMCONTENT", text)
        self.assertIn("[execution environment]", text)
        self.assertIn("[skills]", text)
        self.assertIn("- skills/foo.py: desc", text)
        self.assertIn("cat skills/", text)

    def test_skills_section_omitted_when_no_skills(self) -> None:
        with mock.patch.object(gemini, "settings") as mock_settings, \
                mock.patch.object(gemini, "read_memory", return_value="MEMCONTENT"), \
                mock.patch.object(gemini, "list_skills", return_value=""), \
                mock.patch.object(
                    gemini.shell,
                    "execution_environment",
                    return_value={"os": "Linux", "arch": "x86_64", "shell": "/bin/bash", "cwd": "/base"},
                ):
            mock_settings.get.return_value = "SYSTEM PROMPT TEXT"
            instruction = gemini.build_system_instruction()

        text = instruction["parts"][0]["text"]
        self.assertNotIn("[skills]", text)


if __name__ == "__main__":
    unittest.main()
