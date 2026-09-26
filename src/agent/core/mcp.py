from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from src.agent.config import settings
from src.agent.utils.http import http_post_h

_LOGGER = logging.getLogger("shellie.mcp")

# Non-goals (deliberately out of scope for this client):
# - stdio transport (only Streamable HTTP is supported)
# - OAuth / any auth flow beyond static headers configured per server
# - SSE stream parsing: a server whose response Content-Type contains
#   "text/event-stream" is logged as unsupported and its tools are skipped
#   for the rest of the current cache period.

_CACHE_TTL_SEC = 300
_REQUEST_TIMEOUT_SEC = 30

_PROTOCOL_VERSION = "2024-11-05"
_CLIENT_INFO = {"name": "shellie", "version": "0.1.0"}

_SERVER_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")
_NAME_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9_-]")
_MAX_TOOL_NAME_LEN = 63

_SCHEMA_KEYS = ("type", "properties", "required", "description", "items", "enum")


class _ServerCache:
    """Per-server session state and tools cache, keyed by server name in _cache.

    `fingerprint` is the server's own JSON config entry (as a sorted-key JSON
    string) so that editing MCP_SERVERS invalidates the cache for that server;
    `expiry_ts` enforces the 300s TTL independent of config changes.
    """

    def __init__(self, entry: Dict[str, Any], fingerprint: str) -> None:
        self.entry = entry
        self.fingerprint = fingerprint
        self.expiry_ts: float = 0.0
        self.tools: List[Dict[str, Any]] = []
        self.session_id: Optional[str] = None
        self.initialized = False
        self.usable = True


# server name -> _ServerCache
_cache: Dict[str, _ServerCache] = {}

# mcp_<server>_<tool> (sanitized) -> (server_name, original_tool_name),
# rebuilt on every tool_declarations() call.
_tool_name_map: Dict[str, Tuple[str, str]] = {}

_warned_invalid_config = False


def _load_servers() -> List[Dict[str, Any]]:
    """Parse MCP_SERVERS at call time. Invalid/empty config -> [] (warn once)."""
    global _warned_invalid_config
    raw = settings.get("MCP_SERVERS")
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        if not _warned_invalid_config:
            _LOGGER.warning("MCP_SERVERS setting is not valid JSON, ignoring")
            _warned_invalid_config = True
        return []
    if not isinstance(data, list):
        if not _warned_invalid_config:
            _LOGGER.warning("MCP_SERVERS setting is not a list, ignoring")
            _warned_invalid_config = True
        return []
    _warned_invalid_config = False
    servers: List[Dict[str, Any]] = []
    for entry in data:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str) and isinstance(entry.get("url"), str):
            servers.append(entry)
    return servers


def _cache_for(entry: Dict[str, Any]) -> _ServerCache:
    name = entry["name"]
    fingerprint = json.dumps(entry, sort_keys=True)
    now = time.time()
    cached = _cache.get(name)
    if cached is not None and cached.fingerprint == fingerprint and cached.expiry_ts > now:
        return cached
    cached = _ServerCache(entry, fingerprint)
    cached.expiry_ts = now + _CACHE_TTL_SEC
    _cache[name] = cached
    return cached


def _request_headers(entry: Dict[str, Any], cache: _ServerCache) -> Dict[str, str]:
    # Never log header values here or anywhere else -- they may carry credentials.
    headers: Dict[str, str] = {"Accept": "application/json, text/event-stream"}
    custom = entry.get("headers")
    if isinstance(custom, dict):
        for key, value in custom.items():
            headers[str(key)] = str(value)
    if cache.session_id:
        headers["Mcp-Session-Id"] = cache.session_id
    return headers


def _is_sse(resp_headers: Optional[Dict[str, str]]) -> bool:
    content_type = (resp_headers or {}).get("content-type", "")
    return "text/event-stream" in content_type


def _ensure_initialized(entry: Dict[str, Any], cache: _ServerCache) -> None:
    """Run the JSON-RPC "initialize" handshake, then best-effort notify.

    Always sets cache.initialized = True (success or failure) so a broken or
    SSE-only server is not retried again until the cache period expires.
    """
    name = entry.get("name")
    url = entry.get("url")
    headers = _request_headers(entry, cache)
    payload = {
        "jsonrpc": "2.0",
        "id": "initialize",
        "method": "initialize",
        "params": {
            "protocolVersion": _PROTOCOL_VERSION,
            "clientInfo": _CLIENT_INFO,
            "capabilities": {},
        },
    }
    data, status, resp_headers = http_post_h(url, payload, headers=headers, timeout=_REQUEST_TIMEOUT_SEC)
    cache.initialized = True

    if _is_sse(resp_headers):
        _LOGGER.warning("MCP server %s: SSE responses are not supported", name)
        cache.usable = False
        return

    if not isinstance(data, dict) or status != 200 or isinstance(data.get("error"), dict):
        _LOGGER.warning("MCP server %s initialization failed (status=%s)", name, status)
        cache.usable = False
        return

    session_id = (resp_headers or {}).get("mcp-session-id")
    if session_id:
        cache.session_id = session_id
    cache.usable = True

    # Best-effort notification: no id, ignore response and errors entirely.
    notify_headers = _request_headers(entry, cache)
    notify_payload = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    http_post_h(url, notify_payload, headers=notify_headers, timeout=_REQUEST_TIMEOUT_SEC)


def _fetch_tools(entry: Dict[str, Any], cache: _ServerCache) -> List[Dict[str, Any]]:
    name = entry.get("name")
    url = entry.get("url")
    headers = _request_headers(entry, cache)
    payload = {"jsonrpc": "2.0", "id": "tools-list", "method": "tools/list", "params": {}}
    data, status, resp_headers = http_post_h(url, payload, headers=headers, timeout=_REQUEST_TIMEOUT_SEC)

    if _is_sse(resp_headers):
        _LOGGER.warning("MCP server %s: SSE responses are not supported", name)
        cache.usable = False
        return []

    if not isinstance(data, dict) or status != 200 or isinstance(data.get("error"), dict):
        _LOGGER.warning("MCP server %s tools/list failed (status=%s)", name, status)
        return []

    result = data.get("result")
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        return []
    return [t for t in tools if isinstance(t, dict) and isinstance(t.get("name"), str)]


def _mangled_name(server: str, tool: str) -> str:
    raw = "mcp_{}_{}".format(server, tool)
    sanitized = _NAME_SANITIZE_RE.sub("_", raw)
    return sanitized[:_MAX_TOOL_NAME_LEN]


def _convert_schema(schema: Any) -> Dict[str, Any]:
    """Keep only type/properties/required/description/items/enum, recursively."""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    result: Dict[str, Any] = {}
    for key in _SCHEMA_KEYS:
        if key not in schema:
            continue
        value = schema[key]
        if key == "properties" and isinstance(value, dict):
            result[key] = {k: _convert_schema(v) for k, v in value.items()}
        elif key == "items" and isinstance(value, dict):
            result[key] = _convert_schema(value)
        elif key == "items" and isinstance(value, list):
            result[key] = [_convert_schema(v) for v in value]
        else:
            result[key] = value
    if "type" not in result:
        result["type"] = "object"
    if result.get("type") == "object" and "properties" not in result:
        result["properties"] = {}
    return result


def _tool_to_declaration(server: str, tool: Dict[str, Any], mapped_name: str) -> Dict[str, Any]:
    description = tool.get("description")
    if not isinstance(description, str) or not description:
        description = str(tool.get("name", ""))
    description = "[MCP:{}] {}".format(server, description)
    return {
        "name": mapped_name,
        "description": description,
        "parameters": _convert_schema(tool.get("inputSchema")),
    }


def _declarations_for_server(
    entry: Dict[str, Any], new_map: Dict[str, Tuple[str, str]]
) -> List[Dict[str, Any]]:
    name = entry.get("name")
    if not isinstance(name, str) or not _SERVER_NAME_RE.match(name):
        return []

    cache = _cache_for(entry)
    if not cache.initialized:
        _ensure_initialized(entry, cache)
        if cache.usable:
            cache.tools = _fetch_tools(entry, cache)

    if not cache.usable:
        return []

    declarations: List[Dict[str, Any]] = []
    for tool in cache.tools:
        tool_name = tool.get("name")
        if not isinstance(tool_name, str) or not tool_name:
            continue
        mapped_name = _mangled_name(name, tool_name)
        new_map[mapped_name] = (name, tool_name)
        declarations.append(_tool_to_declaration(name, tool, mapped_name))
    return declarations


def tool_declarations() -> List[Dict[str, Any]]:
    """Build Gemini functionDeclaration dicts for every configured MCP server.

    Never raises: any per-server failure is logged and that server is skipped.
    """
    global _tool_name_map
    new_map: Dict[str, Tuple[str, str]] = {}
    declarations: List[Dict[str, Any]] = []
    for entry in _load_servers():
        try:
            declarations.extend(_declarations_for_server(entry, new_map))
        except Exception:
            _LOGGER.exception("Exception processing MCP server: %s", entry.get("name"))
            continue
    _tool_name_map = new_map
    return declarations


def _format_call_result(server_name: str, data: Any) -> str:
    if not isinstance(data, dict):
        return "MCP server connection failed: {}".format(server_name)

    error = data.get("error")
    if isinstance(error, dict):
        return "MCP error: {}".format(error.get("message", "unknown error"))

    result = data.get("result")
    if not isinstance(result, dict):
        return "MCP server connection failed: {}".format(server_name)

    content = result.get("content")
    pieces: List[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                pieces.append(str(item.get("text", "")))
            else:
                pieces.append(json.dumps(item, ensure_ascii=False))
    text = "\n".join(pieces)
    if result.get("isError"):
        text = "MCP tool error: {}".format(text)
    return text


def _try_run_impl(name: str, args: Dict[str, Any]) -> str:
    mapping = _tool_name_map.get(name)
    if mapping is None:
        return "MCP error: unknown tool"

    server_name, tool_name = mapping
    cache = _cache.get(server_name)
    if cache is None:
        return "MCP server connection failed: {}".format(server_name)

    entry = cache.entry
    headers = _request_headers(entry, cache)
    payload = {
        "jsonrpc": "2.0",
        "id": "tools-call",
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": args},
    }
    data, _status, _resp_headers = http_post_h(
        entry.get("url"), payload, headers=headers, timeout=_REQUEST_TIMEOUT_SEC
    )
    return _format_call_result(server_name, data)


def try_run(name: str, args: Dict[str, Any]) -> Optional[str]:
    """Run an mcp_<server>_<tool> function call. Returns None for non-MCP names.

    Never raises: any unexpected failure is logged and turned into an error string.
    """
    if not name.startswith("mcp_"):
        return None
    try:
        return _try_run_impl(name, args)
    except Exception:
        _LOGGER.exception("Exception running MCP tool: %s", name)
        return "MCP error: unknown error"
