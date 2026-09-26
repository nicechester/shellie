from __future__ import annotations

import json
import unittest
from unittest import mock

from src.agent.core import mcp

_SERVER_URL = "https://example.com/mcp"


def _config(servers):
    return json.dumps(servers)


def _init_ok(session_id="sess-1"):
    """A successful "initialize" response: (data, status, headers)."""
    return ({"jsonrpc": "2.0", "id": "initialize", "result": {}}, 200, {
        "content-type": "application/json",
        "mcp-session-id": session_id,
    })


def _notify_ok():
    return ({}, 202, {})


def _tools_list_ok(tools):
    return ({"jsonrpc": "2.0", "id": "tools-list", "result": {"tools": tools}}, 200, {
        "content-type": "application/json",
    })


class _McpTestCase(unittest.TestCase):
    """Base test case: resets mcp's module-level caches for every test and
    provides a mocked settings.get / http_post_h pair (mcp's own contract)."""

    def setUp(self) -> None:
        mcp._cache = {}
        mcp._tool_name_map = {}
        mcp._warned_invalid_config = False

        settings_patcher = mock.patch.object(mcp, "settings")
        http_patcher = mock.patch.object(mcp, "http_post_h")
        self.mock_settings = settings_patcher.start()
        self.mock_http = http_patcher.start()
        self.addCleanup(settings_patcher.stop)
        self.addCleanup(http_patcher.stop)

    def _set_servers_config(self, servers) -> None:
        self.mock_settings.get.return_value = _config(servers) if servers is not None else ""

    def _bring_up_server(self, name="svc", tools=None, headers=None):
        """Configure one server and drive it through a successful
        initialize + tools/list cycle, returning the declarations."""
        if tools is None:
            tools = [{"name": "do_thing", "description": "does a thing", "inputSchema": {
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "required": ["x"],
            }}]
        server = {"name": name, "url": _SERVER_URL}
        if headers:
            server["headers"] = headers
        self._set_servers_config([server])
        self.mock_http.side_effect = [_init_ok(), _notify_ok(), _tools_list_ok(tools)]
        declarations = mcp.tool_declarations()
        return declarations


class LoadServersTests(_McpTestCase):
    def test_empty_config_no_declarations_no_network(self) -> None:
        self._set_servers_config(None)
        self.assertEqual(mcp.tool_declarations(), [])
        self.mock_http.assert_not_called()

    def test_invalid_json_no_declarations_no_network(self) -> None:
        self.mock_settings.get.return_value = "{not json"
        self.assertEqual(mcp.tool_declarations(), [])
        self.mock_http.assert_not_called()

    def test_non_list_json_no_declarations_no_network(self) -> None:
        self.mock_settings.get.return_value = json.dumps({"name": "svc", "url": _SERVER_URL})
        self.assertEqual(mcp.tool_declarations(), [])
        self.mock_http.assert_not_called()


class InitializeAndToolsListTests(_McpTestCase):
    def test_initialize_called_with_protocol_version_and_client_info(self) -> None:
        self._bring_up_server()
        first_call = self.mock_http.call_args_list[0]
        payload = first_call[0][1]
        self.assertEqual(payload["method"], "initialize")
        self.assertEqual(payload["params"]["protocolVersion"], "2024-11-05")
        self.assertEqual(payload["params"]["clientInfo"], {"name": "shellie", "version": "0.1.0"})

    def test_session_id_captured_and_echoed_on_subsequent_calls(self) -> None:
        self._bring_up_server()
        notify_headers = self.mock_http.call_args_list[1][1]["headers"]
        list_headers = self.mock_http.call_args_list[2][1]["headers"]
        self.assertEqual(notify_headers["Mcp-Session-Id"], "sess-1")
        self.assertEqual(list_headers["Mcp-Session-Id"], "sess-1")

    def test_tools_mapped_to_prefixed_declarations(self) -> None:
        declarations = self._bring_up_server()
        self.assertEqual(len(declarations), 1)
        decl = declarations[0]
        self.assertEqual(decl["name"], "mcp_svc_do_thing")
        self.assertEqual(decl["description"], "[MCP:svc] does a thing")
        self.assertEqual(
            decl["parameters"],
            {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
        )

    def test_missing_input_schema_defaults_to_empty_object(self) -> None:
        declarations = self._bring_up_server(
            tools=[{"name": "bare_tool"}]
        )
        self.assertEqual(declarations[0]["parameters"], {"type": "object", "properties": {}})

    def test_missing_description_falls_back_to_name(self) -> None:
        declarations = self._bring_up_server(tools=[{"name": "bare_tool"}])
        self.assertEqual(declarations[0]["description"], "[MCP:svc] bare_tool")

    def test_tool_name_sanitized_and_truncated(self) -> None:
        long_tool_name = "weird tool!name" + ("x" * 80)
        declarations = self._bring_up_server(tools=[{"name": long_tool_name}])
        decl_name = declarations[0]["name"]
        self.assertLessEqual(len(decl_name), 63)
        self.assertRegex(decl_name, r"^[a-zA-Z0-9_-]+$")


class CacheTests(_McpTestCase):
    def test_second_call_within_ttl_makes_no_new_network_calls(self) -> None:
        self._bring_up_server()
        self.assertEqual(self.mock_http.call_count, 3)

        declarations = mcp.tool_declarations()
        self.assertEqual(self.mock_http.call_count, 3)
        self.assertEqual(len(declarations), 1)

    def test_changed_server_config_invalidates_cache(self) -> None:
        self._bring_up_server()
        self.assertEqual(self.mock_http.call_count, 3)

        # Same server name, different URL -> different fingerprint.
        self.mock_settings.get.return_value = _config(
            [{"name": "svc", "url": "https://example.com/other"}]
        )
        self.mock_http.side_effect = [_init_ok(), _notify_ok(), _tools_list_ok(
            [{"name": "do_thing"}]
        )]
        mcp.tool_declarations()
        self.assertEqual(self.mock_http.call_count, 6)

    def test_sse_response_skips_server_with_no_declarations(self) -> None:
        self._set_servers_config([{"name": "svc", "url": _SERVER_URL}])
        self.mock_http.side_effect = [
            ({}, 200, {"content-type": "text/event-stream"}),
        ]
        with self.assertLogs("shellie.mcp", level="WARNING"):
            declarations = mcp.tool_declarations()
        self.assertEqual(declarations, [])
        self.assertEqual(self.mock_http.call_count, 1)

        # Second call within TTL must not retry the broken server.
        declarations = mcp.tool_declarations()
        self.assertEqual(declarations, [])
        self.assertEqual(self.mock_http.call_count, 1)


class TryRunTests(_McpTestCase):
    def test_non_mcp_name_returns_none_without_network(self) -> None:
        result = mcp.try_run("execute_shell", {"command": "ls"})
        self.assertIsNone(result)
        self.mock_http.assert_not_called()

    def test_unmapped_mcp_name_returns_error_string(self) -> None:
        result = mcp.try_run("mcp_unknown_tool", {})
        self.assertEqual(result, "MCP 오류: 알 수 없는 툴")

    def test_tools_call_uses_original_unprefixed_tool_name(self) -> None:
        self._bring_up_server()
        self.mock_http.side_effect = None
        self.mock_http.return_value = (
            {"result": {"content": [{"type": "text", "text": "ok"}]}}, 200, {}
        )
        mcp.try_run("mcp_svc_do_thing", {"x": "1"})
        payload = self.mock_http.call_args[0][1]
        self.assertEqual(payload["method"], "tools/call")
        self.assertEqual(payload["params"]["name"], "do_thing")
        self.assertEqual(payload["params"]["arguments"], {"x": "1"})

    def test_text_content_items_are_concatenated(self) -> None:
        self._bring_up_server()
        self.mock_http.side_effect = None
        self.mock_http.return_value = (
            {"result": {"content": [
                {"type": "text", "text": "hello"},
                {"type": "text", "text": "world"},
            ]}}, 200, {}
        )
        result = mcp.try_run("mcp_svc_do_thing", {})
        self.assertEqual(result, "hello\nworld")

    def test_non_text_content_item_is_json_dumped(self) -> None:
        self._bring_up_server()
        self.mock_http.side_effect = None
        self.mock_http.return_value = (
            {"result": {"content": [{"type": "image", "data": "abc"}]}}, 200, {}
        )
        result = mcp.try_run("mcp_svc_do_thing", {})
        self.assertEqual(json.loads(result), {"type": "image", "data": "abc"})

    def test_is_error_result_gets_prefixed(self) -> None:
        self._bring_up_server()
        self.mock_http.side_effect = None
        self.mock_http.return_value = (
            {"result": {"isError": True, "content": [{"type": "text", "text": "boom"}]}}, 200, {}
        )
        result = mcp.try_run("mcp_svc_do_thing", {})
        self.assertEqual(result, "MCP 툴 오류: boom")

    def test_json_rpc_error_object_is_mapped(self) -> None:
        self._bring_up_server()
        self.mock_http.side_effect = None
        self.mock_http.return_value = ({"error": {"code": -1, "message": "bad request"}}, 400, {})
        result = mcp.try_run("mcp_svc_do_thing", {})
        self.assertEqual(result, "MCP 오류: bad request")

    def test_transport_failure_returns_connection_message(self) -> None:
        self._bring_up_server()
        self.mock_http.side_effect = None
        self.mock_http.return_value = ({"error": "Connection refused"}, 0, {})
        result = mcp.try_run("mcp_svc_do_thing", {})
        self.assertEqual(result, "MCP 서버 연결 실패: svc")


if __name__ == "__main__":
    unittest.main()
