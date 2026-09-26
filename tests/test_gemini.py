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
        http_post_patcher = mock.patch.object(gemini, "http_post")
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

    def test_429_falls_back_to_second_model(self) -> None:
        res1 = {"error": {"status": "TOO_MANY"}}
        res2 = {"candidates": [], "_marker": "second"}
        self.mock_http_post.side_effect = [(res1, 429), (res2, 200)]

        result, model = gemini.call_gemini(self._contents())

        self.assertEqual(model, "m2")
        self.assertEqual(result, res2)
        self.assertEqual(self.mock_http_post.call_count, 2)
        second_url = self.mock_http_post.call_args_list[1][0][0]
        self.assertIn("m2", second_url)

    def test_resource_exhausted_body_on_non_200_falls_back(self) -> None:
        res1 = {"error": {"status": "RESOURCE_EXHAUSTED"}}
        res2 = {"candidates": []}
        self.mock_http_post.side_effect = [(res1, 403), (res2, 200)]

        result, model = gemini.call_gemini(self._contents())

        self.assertEqual(model, "m2")
        self.assertEqual(result, res2)

    def test_503_falls_back(self) -> None:
        res1 = {"error": {"status": "UNAVAILABLE"}}
        res2 = {"candidates": []}
        self.mock_http_post.side_effect = [(res1, 503), (res2, 200)]

        _result, model = gemini.call_gemini(self._contents())

        self.assertEqual(model, "m2")

    def test_status_zero_falls_back(self) -> None:
        res1 = {"error": "transport failure"}
        res2 = {"candidates": []}
        self.mock_http_post.side_effect = [(res1, 0), (res2, 200)]

        _result, model = gemini.call_gemini(self._contents())

        self.assertEqual(model, "m2")

    def test_400_raises_immediately_without_second_call(self) -> None:
        res1 = {"error": {"status": "INVALID_ARGUMENT", "message": "bad request"}}
        self.mock_http_post.side_effect = [(res1, 400)]

        with self.assertRaises(Exception) as ctx:
            gemini.call_gemini(self._contents())

        self.assertEqual(self.mock_http_post.call_count, 1)
        self.assertNotIn(self.api_key, str(ctx.exception))

    def test_all_models_fail_raises_final_exception(self) -> None:
        res = {"error": {"status": "UNAVAILABLE"}}
        self.mock_http_post.side_effect = [(res, 503), (res, 503)]

        with self.assertRaises(Exception) as ctx:
            gemini.call_gemini(self._contents())

        self.assertIn("모든 Gemini 모델", str(ctx.exception))

    def test_uses_api_key_header_never_in_url(self) -> None:
        res = {"candidates": []}
        self.mock_http_post.return_value = (res, 200)

        gemini.call_gemini(self._contents())

        args, kwargs = self.mock_http_post.call_args
        url = args[0]
        headers = kwargs["headers"]
        self.assertNotIn(self.api_key, url)
        self.assertEqual(headers["x-goog-api-key"], self.api_key)

    def test_fallback_delay_uses_configured_seconds(self) -> None:
        self._configure_settings({"GEMINI_FALLBACK_DELAY_SEC": 4})
        res1 = {"error": {"status": "TOO_MANY"}}
        res2 = {"candidates": []}
        self.mock_http_post.side_effect = [(res1, 429), (res2, 200)]

        gemini.call_gemini(self._contents())

        self.mock_sleep.assert_called_once_with(4)


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

        declarations = schema[0]["functionDeclarations"]
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

            with mock.patch.object(gemini, "SKILLS_DIR", tmp):
                result = gemini.list_skills()

        lines = result.split("\n")
        self.assertIn("- skills/alpha.py: Alpha skill does something useful.", lines)
        self.assertIn("- skills/broken.py", lines)
        self.assertFalse(any("_internal" in line for line in lines))

    def test_md_skill_with_heading_strips_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "x.md"), "w", encoding="utf-8") as f:
                f.write("# Title\n\nSome procedure body.\n")

            with mock.patch.object(gemini, "SKILLS_DIR", tmp):
                result = gemini.list_skills()

        self.assertEqual(result, "- skills/x.md: Title")

    def test_md_skill_with_plain_text_first_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "x.md"), "w", encoding="utf-8") as f:
                f.write("Plain text summary line.\nMore body.\n")

            with mock.patch.object(gemini, "SKILLS_DIR", tmp):
                result = gemini.list_skills()

        self.assertEqual(result, "- skills/x.md: Plain text summary line.")

    def test_empty_md_skill_is_name_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "x.md"), "w", encoding="utf-8") as f:
                f.write("")

            with mock.patch.object(gemini, "SKILLS_DIR", tmp):
                result = gemini.list_skills()

        self.assertEqual(result, "- skills/x.md")

    def test_readme_md_is_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "README.md"), "w", encoding="utf-8") as f:
                f.write("# skills/\n\nShould never appear.\n")

            with mock.patch.object(gemini, "SKILLS_DIR", tmp):
                result = gemini.list_skills()

        self.assertEqual(result, "")

    def test_underscore_prefixed_md_is_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "_draft.md"), "w", encoding="utf-8") as f:
                f.write("# Draft\n\nWork in progress.\n")

            with mock.patch.object(gemini, "SKILLS_DIR", tmp):
                result = gemini.list_skills()

        self.assertEqual(result, "")

    def test_mixed_py_and_md_listing_includes_both(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "alpha.py"), "w", encoding="utf-8") as f:
                f.write('"""Alpha skill summary."""\n')
            with open(os.path.join(tmp, "beta.md"), "w", encoding="utf-8") as f:
                f.write("# Beta procedure\n")

            with mock.patch.object(gemini, "SKILLS_DIR", tmp):
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
        self.assertIn("[장기 기억]", text)
        self.assertIn("MEMCONTENT", text)
        self.assertIn("[실행 환경]", text)
        self.assertIn("[스킬 목록]", text)
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
        self.assertNotIn("[스킬 목록]", text)


if __name__ == "__main__":
    unittest.main()
