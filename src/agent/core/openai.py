from __future__ import annotations

# OpenAI-compatible chat completions backend (issue #13).
#
# The app keeps one internal conversation format, the Gemini-shaped one that
# handlers build: turns with role user/model and parts text, inlineData,
# functionCall and functionResponse. This module converts that history into
# OpenAI chat messages and maps the reply back into the Gemini response shape,
# so parse_response() and the function-calling loop work unchanged for both
# backends.
#
# Stdlib only, via utils.http.http_post_h. The API key is sent in the
# Authorization and X-API-Key headers and is never logged. Chain walking, cooldown and
# fallback live in core/llm.py; this module only builds the request and
# attempts one model.

import ipaddress
import json
import logging
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from src.agent.core.gemini import Attempt, _set_retry_status, build_system_instruction, build_tools_schema
from src.agent.utils.http import http_post_h
from src.agent.version import __version__

_LOGGER = logging.getLogger("shellie.openai")

# Model fallback on these statuses (404 = model not served here, 0 = transport).
_FALLBACK_STATUSES = (404, 408, 429, 500, 502, 503, 504)

_FINISH_REASONS = {
    "stop": "STOP",
    "tool_calls": "STOP",
    "function_call": "STOP",
    "length": "MAX_TOKENS",
}


def _tool_result_text(response: Any) -> str:
    """Tool output as text. The handler wraps results as {"output": ...}."""
    if isinstance(response, dict) and "output" in response:
        output = response["output"]
        if isinstance(output, str):
            return output
        return json.dumps(output, ensure_ascii=False)
    return json.dumps(response, ensure_ascii=False)


def _to_openai_schema(schema: Any) -> Any:
    """Gemini schema to JSON Schema: lowercase the types (OBJECT -> object),
    and turn nullable into a null type. Recurses through every nested dict and
    list, so property names are never touched (only str values under "type")."""
    if isinstance(schema, list):
        return [_to_openai_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out: Dict[str, Any] = {}
    for key, value in schema.items():
        if key == "nullable":
            continue
        if key == "type" and isinstance(value, str):
            value = value.lower()
            if schema.get("nullable") is True:
                value = [value, "null"]
            out[key] = value
            continue
        out[key] = _to_openai_schema(value)
    return out


def to_openai_tools(gemini_tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    tools: List[Dict[str, Any]] = []
    for group in gemini_tools or []:
        for decl in group.get("functionDeclarations") or []:
            params = decl.get("parameters") or {"type": "OBJECT", "properties": {}}
            tools.append({
                "type": "function",
                "function": {
                    "name": decl.get("name", ""),
                    "description": decl.get("description", ""),
                    "parameters": _to_openai_schema(params),
                },
            })
    return tools


def to_openai_messages(contents: List[Dict[str, Any]], system_text: str) -> List[Dict[str, Any]]:
    """Convert internal Gemini-shaped history to OpenAI chat messages.

    Tool-call ids: a functionCall keeps the OpenAI id it was parsed with (stored
    in its "id" field). Responses are paired with calls in order, so an
    unpaired response is skipped instead of producing an invalid tool message.
    """
    messages: List[Dict[str, Any]] = [{"role": "system", "content": system_text}]
    pending: Deque[str] = deque()
    counter = 0

    for turn in contents:
        if not isinstance(turn, dict):
            continue
        parts = [p for p in (turn.get("parts") or []) if isinstance(p, dict) and not p.get("thought")]

        if turn.get("role") == "model":
            texts: List[str] = []
            calls: List[Dict[str, Any]] = []
            for part in parts:
                if "functionCall" in part:
                    fc = part.get("functionCall") or {}
                    call_id = fc.get("id")
                    if not call_id:
                        counter += 1
                        call_id = "call_{}".format(counter)
                    pending.append(call_id)
                    calls.append({
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": fc.get("name", ""),
                            "arguments": json.dumps(fc.get("args") or {}, ensure_ascii=False),
                        },
                    })
                elif "text" in part:
                    texts.append(part.get("text") or "")
            if not texts and not calls:
                continue
            message: Dict[str, Any] = {"role": "assistant", "content": "".join(texts) or None}
            if calls:
                message["tool_calls"] = calls
            messages.append(message)
            continue

        # User turn: tool results first (they must follow the assistant call
        # message directly), then any text or attachments in the same turn.
        user_items: List[Dict[str, Any]] = []
        for part in parts:
            if "functionResponse" in part:
                fr = part.get("functionResponse") or {}
                if not pending:
                    _LOGGER.warning("Dropping tool result with no pending tool call")
                    continue
                messages.append({
                    "role": "tool",
                    "tool_call_id": pending.popleft(),
                    "content": _tool_result_text(fr.get("response")),
                })
            elif "text" in part:
                user_items.append({"type": "text", "text": part.get("text") or ""})
            elif "inlineData" in part:
                inline = part.get("inlineData") or {}
                mime = str(inline.get("mimeType", ""))
                data = inline.get("data")
                if mime.startswith("image/") and isinstance(data, str):
                    user_items.append({
                        "type": "image_url",
                        "image_url": {"url": "data:{};base64,{}".format(mime, data)},
                    })
                else:
                    user_items.append({
                        "type": "text",
                        "text": "[attachment omitted: {} is not supported by this backend]".format(mime or "unknown"),
                    })

        if user_items:
            if all(item["type"] == "text" for item in user_items):
                content: Any = "".join(item["text"] for item in user_items)
            else:
                content = user_items
            messages.append({"role": "user", "content": content})

    return messages


def _parse_arguments(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
        except ValueError:
            # Do not log the raw arguments: they may contain user content.
            _LOGGER.warning("Model returned tool arguments that are not valid JSON")
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def _retry_after(headers: Dict[str, str]) -> Optional[float]:
    try:
        value = float((headers or {}).get("retry-after"))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def to_gemini_response(res: Any) -> Dict[str, Any]:
    """Map an OpenAI chat completion to the Gemini response shape that
    parse_response() reads. Content filtering becomes a promptFeedback block."""
    choices = res.get("choices") if isinstance(res, dict) else None
    if not choices or not isinstance(choices[0], dict):
        return {}
    choice = choices[0]
    finish = choice.get("finish_reason")
    if finish == "content_filter":
        return {"promptFeedback": {"blockReason": "CONTENT_FILTER"}}

    message = choice.get("message") or {}
    parts: List[Dict[str, Any]] = []
    text = message.get("content")
    if isinstance(text, str) and text:
        parts.append({"text": text})
    for tool_call in message.get("tool_calls") or []:
        if not isinstance(tool_call, dict):
            continue
        fn = tool_call.get("function") or {}
        fc: Dict[str, Any] = {"name": fn.get("name", ""), "args": _parse_arguments(fn.get("arguments"))}
        if tool_call.get("id"):
            fc["id"] = tool_call["id"]
        parts.append({"functionCall": fc})

    return {
        "candidates": [{
            "content": {"role": "model", "parts": parts},
            "finishReason": _FINISH_REASONS.get(finish or "", finish),
        }]
    }


def _system_text() -> str:
    return "".join(part.get("text", "") for part in build_system_instruction().get("parts", []))


def _is_local_base(base: str) -> bool:
    """True for loopback, private-LAN and .local hosts, where an empty API key
    is allowed (e.g. Ollama or LM Studio on the home network)."""
    host = (urlsplit(base).hostname or "").lower()
    if host in ("localhost", "::1") or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def build_payload(contents: List[Dict[str, Any]], allow_tools: bool) -> Dict[str, Any]:
    """OpenAI chat-completions body (without model) for one chat turn. Built
    once per call by the chain orchestrator (core/llm.py)."""
    payload: Dict[str, Any] = {"messages": to_openai_messages(contents, _system_text())}
    tools = to_openai_tools(build_tools_schema())
    if tools:
        payload["tools"] = tools
        # Tools stay declared when disabled, so earlier tool-call turns in the
        # history remain valid (same reason as Gemini's mode NONE).
        payload["tool_choice"] = "auto" if allow_tools else "none"
    return payload


def attempt_model(model: str, payload: Dict[str, Any], base: str, key: str, timeout: float) -> Attempt:
    """Try one OpenAI-compatible model. Returns Attempt(ok=True, response) with
    the response mapped to the Gemini shape, or Attempt(ok=False, hint=...)
    on 404/408/429/5xx/transport so the caller falls back. Other statuses
    raise. An openai/ entry with no API key and a non-local base URL is
    skipped with a warning, without a request."""
    if not key and not _is_local_base(base):
        _LOGGER.warning("OpenAI model %s skipped: OPENAI_API_KEY is not set", model)
        return Attempt(False, None, None)
    _set_retry_status("calling", model=model)
    # Both headers: Authorization is the OpenAI standard, X-API-Key is what
    # some gateways (e.g. Launch-hosted APIs) require instead.
    # An explicit User-Agent: Python's default (Python-urllib/x.y) is blocked with
    # HTTP 403 "error code: 1010" by Cloudflare in front of some hosts (e.g. Groq).
    headers = {"User-Agent": "Shellie/" + __version__}
    if key:
        headers["Authorization"] = "Bearer " + key
        headers["X-API-Key"] = key
    body = dict(payload, model=model)
    res, status, resp_headers = http_post_h(base + "/chat/completions", body, headers=headers, timeout=timeout)

    if status == 200:
        return Attempt(True, to_gemini_response(res), None)

    if not (status == 0 or status in _FALLBACK_STATUSES):
        raise Exception("OpenAI-compatible API call failed (model={}, status={})".format(model, status))

    _LOGGER.warning("OpenAI model %s failed (HTTP %s), switching to next model", model, status)
    return Attempt(False, None, _retry_after(resp_headers))
