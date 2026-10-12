from __future__ import annotations

import unittest
from unittest import mock

from src.agent.core import gemini
from src.agent.core import openai as openai_backend
from src.agent.settings import _parse_openai_api_base, _parse_openai_model_chain

_TOOLS = [{
    "functionDeclarations": [{
        "name": "execute_shell",
        "description": "Run a command",
        "parameters": {
            "type": "OBJECT",
            "properties": {"command": {"type": "STRING"}},
            "required": ["command"],
        },
    }]
}]


def _settings_get(overrides: dict):
    defaults = {
        "OPENAI_MODEL_CHAIN": ("m1", "m2"),
        "OPENAI_API_BASE": "http://localhost:11434/v1",
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_TIMEOUT_SEC": 30,
    }
    defaults.update(overrides)
    return lambda key: defaults[key]


def _completion(content=None, tool_calls=None, finish="stop"):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message, "finish_reason": finish}]}


class ConversionTests(unittest.TestCase):
    def test_tool_schema_types_are_lowercased(self) -> None:
        tools = openai_backend.to_openai_tools(_TOOLS)
        self.assertEqual(len(tools), 1)
        fn = tools[0]["function"]
        self.assertEqual(tools[0]["type"], "function")
        self.assertEqual(fn["parameters"]["type"], "object")
        self.assertEqual(fn["parameters"]["properties"]["command"]["type"], "string")

    def test_tool_call_and_result_are_paired(self) -> None:
        contents = [
            {"role": "user", "parts": [{"text": "list files"}]},
            {"role": "model", "parts": [
                {"functionCall": {"id": "call_abc", "name": "execute_shell", "args": {"command": "ls"}}},
            ]},
            {"role": "user", "parts": [
                {"functionResponse": {"name": "execute_shell", "response": {"output": "a.txt"}}},
                {"text": "answer briefly"},
            ]},
        ]
        messages = openai_backend.to_openai_messages(contents, "SYS")
        self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant", "tool", "user"])
        assistant = messages[2]
        self.assertIsNone(assistant["content"])
        self.assertEqual(assistant["tool_calls"][0]["id"], "call_abc")
        self.assertEqual(assistant["tool_calls"][0]["function"]["arguments"], '{"command": "ls"}')
        self.assertEqual(messages[3], {"role": "tool", "tool_call_id": "call_abc", "content": "a.txt"})
        self.assertEqual(messages[4], {"role": "user", "content": "answer briefly"})

    def test_unpaired_tool_result_is_skipped(self) -> None:
        contents = [
            {"role": "user", "parts": [
                {"functionResponse": {"name": "x", "response": {"output": "orphan"}}},
            ]},
        ]
        messages = openai_backend.to_openai_messages(contents, "SYS")
        self.assertEqual([m["role"] for m in messages], ["system"])

    def test_non_image_attachment_becomes_placeholder(self) -> None:
        contents = [{"role": "user", "parts": [
            {"text": "look"},
            {"inlineData": {"mimeType": "application/pdf", "data": "AAAA"}},
        ]}]
        messages = openai_backend.to_openai_messages(contents, "SYS")
        self.assertIn("[attachment omitted", messages[1]["content"][1]["text"])

    def test_image_attachment_becomes_data_url(self) -> None:
        contents = [{"role": "user", "parts": [
            {"text": "what is this"},
            {"inlineData": {"mimeType": "image/png", "data": "AAAA"}},
        ]}]
        messages = openai_backend.to_openai_messages(contents, "SYS")
        image = messages[1]["content"][1]
        self.assertEqual(image["type"], "image_url")
        self.assertEqual(image["image_url"]["url"], "data:image/png;base64,AAAA")

    def test_response_maps_to_parse_response(self) -> None:
        res = _completion(tool_calls=[{
            "id": "call_9",
            "type": "function",
            "function": {"name": "execute_shell", "arguments": '{"command": "pwd"}'},
        }], finish="tool_calls")
        parsed = gemini.parse_response(openai_backend.to_gemini_response(res))
        self.assertEqual(parsed.function_calls, [("execute_shell", {"command": "pwd"})])
        self.assertFalse(parsed.blocked)

    def test_text_response_maps_to_text(self) -> None:
        parsed = gemini.parse_response(openai_backend.to_gemini_response(_completion("hello")))
        self.assertEqual(parsed.text, "hello")
        self.assertEqual(parsed.finish_reason, "STOP")

    def test_length_finish_maps_to_max_tokens(self) -> None:
        parsed = gemini.parse_response(openai_backend.to_gemini_response(_completion("cut", finish="length")))
        self.assertEqual(parsed.finish_reason, "MAX_TOKENS")

    def test_content_filter_maps_to_blocked(self) -> None:
        res = {"choices": [{"message": {"content": None}, "finish_reason": "content_filter"}]}
        parsed = gemini.parse_response(openai_backend.to_gemini_response(res))
        self.assertTrue(parsed.blocked)


class CallOpenAITests(unittest.TestCase):
    def setUp(self) -> None:
        settings_patcher = mock.patch.object(openai_backend, "settings")
        http_patcher = mock.patch.object(openai_backend, "http_post_h")
        sleep_patcher = mock.patch.object(openai_backend.time, "sleep")
        system_patcher = mock.patch.object(
            openai_backend, "build_system_instruction", return_value={"parts": [{"text": "SYS"}]}
        )
        tools_patcher = mock.patch.object(openai_backend, "build_tools_schema", return_value=_TOOLS)
        self.settings = settings_patcher.start()
        self.http = http_patcher.start()
        self.sleep = sleep_patcher.start()
        system_patcher.start()
        tools_patcher.start()
        self.addCleanup(mock.patch.stopall)
        self.settings.get.side_effect = _settings_get({})

    def _contents(self):
        return [{"role": "user", "parts": [{"text": "hi"}]}]

    def test_falls_back_to_next_model_on_429(self) -> None:
        self.http.side_effect = [
            ({}, 429, {}),
            (_completion("ok"), 200, {}),
        ]
        response, model = openai_backend.call_openai(self._contents())
        self.assertEqual(model, "m2")
        self.assertEqual(gemini.parse_response(response).text, "ok")
        self.sleep.assert_called_with(openai_backend._FALLBACK_DELAY_SEC)

    def test_sends_bearer_key_and_chat_path(self) -> None:
        self.http.return_value = (_completion("ok"), 200, {})
        openai_backend.call_openai(self._contents())
        url = self.http.call_args.args[0]
        headers = self.http.call_args.kwargs["headers"]
        self.assertEqual(url, "http://localhost:11434/v1/chat/completions")
        self.assertEqual(headers, {"Authorization": "Bearer sk-test"})

    def test_no_auth_header_when_key_is_empty(self) -> None:
        self.settings.get.side_effect = _settings_get({"OPENAI_API_KEY": ""})
        self.http.return_value = (_completion("ok"), 200, {})
        openai_backend.call_openai(self._contents())
        self.assertEqual(self.http.call_args.kwargs["headers"], {})

    def test_tools_disabled_sets_tool_choice_none_keeps_tools(self) -> None:
        self.http.return_value = (_completion("ok"), 200, {})
        openai_backend.call_openai(self._contents(), allow_tools=False)
        body = self.http.call_args.args[1]
        self.assertEqual(body["tool_choice"], "none")
        self.assertEqual(len(body["tools"]), 1)

    def test_non_fallback_status_raises_without_retry(self) -> None:
        self.http.return_value = ({}, 401, {})
        with self.assertRaises(Exception):
            openai_backend.call_openai(self._contents())
        self.assertEqual(self.http.call_count, 1)


class SettingsParserTests(unittest.TestCase):
    def test_model_chain_accepts_ollama_tags(self) -> None:
        self.assertEqual(_parse_openai_model_chain("llama3.2:latest,qwen/qwen2.5"), ("llama3.2:latest", "qwen/qwen2.5"))

    def test_api_base_rejects_non_http(self) -> None:
        with self.assertRaises(ValueError):
            _parse_openai_api_base("ftp://example.com/v1")

    def test_api_base_strips_trailing_slash(self) -> None:
        self.assertEqual(_parse_openai_api_base("https://api.openai.com/v1/"), "https://api.openai.com/v1")


if __name__ == "__main__":
    unittest.main()
