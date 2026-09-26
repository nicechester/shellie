from __future__ import annotations

import ast
import logging
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.agent.config import SKILLS_DIR, settings
from src.agent.core import mcp, shell
from src.agent.core.memory import append_memory, read_memory
from src.agent.utils.http import http_post

_LOGGER = logging.getLogger("shellie.gemini")

_GEMINI_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"

_FALLBACK_5XX = (500, 502, 503, 504)

# Cap for a single cooldown sleep between whole-chain retry passes.
_MAX_COOLDOWN_SEC = 300

_RETRY_DELAY_MESSAGE_RE = re.compile(r"retry in ([\d.]+)s", re.IGNORECASE)


class ToolEntry:
    """Registry entry: a Gemini tool declaration builder plus its runner.

    Structured so an external adapter can append entries without changing
    run_tool/build_tools_schema. The HTTP MCP client (src.agent.core.mcp) is
    one such adapter: it is not a TOOL_REGISTRY entry, but is wired into
    build_tools_schema()/run_tool() directly below.
    """

    def __init__(
        self,
        name: str,
        declaration: Callable[[], Dict[str, Any]],
        run: Callable[[Dict[str, Any]], str],
    ) -> None:
        self.name = name
        self.declaration = declaration
        self.run = run


def _execute_shell_declaration() -> Dict[str, Any]:
    env = shell.execution_environment()
    description = "Runs a shell command on {}({}) in {}. Supports file management, script execution, etc.".format(
        env["os"], env["arch"], env["shell"]
    )
    return {
        "name": "execute_shell",
        "description": description,
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "command": {"type": "STRING", "description": "Shell command to execute"},
            },
            "required": ["command"],
        },
    }


def _execute_shell_run(args: Dict[str, Any]) -> str:
    return shell.execute_shell(str(args.get("command", "")))


def _append_memory_declaration() -> Dict[str, Any]:
    return {
        "name": "append_memory",
        "description": "Saves important rules or user personalization info to MEMORY.md.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "content": {"type": "STRING", "description": "Key summary to remember"},
            },
            "required": ["content"],
        },
    }


def _append_memory_run(args: Dict[str, Any]) -> str:
    return append_memory(str(args.get("content", "")))


TOOL_REGISTRY: List[ToolEntry] = [
    ToolEntry("execute_shell", _execute_shell_declaration, _execute_shell_run),
    ToolEntry("append_memory", _append_memory_declaration, _append_memory_run),
]


def run_tool(name: str, args: Dict[str, Any]) -> str:
    for entry in TOOL_REGISTRY:
        if entry.name == name:
            return entry.run(args)
    mcp_result = mcp.try_run(name, args)
    if mcp_result is not None:
        return mcp_result
    return "Unknown function: {}".format(name)


def build_tools_schema() -> List[Dict[str, Any]]:
    declarations = [entry.declaration() for entry in TOOL_REGISTRY]
    try:
        declarations.extend(mcp.tool_declarations())
    except Exception:
        _LOGGER.exception("Exception building MCP tool declarations, using built-in tools only")
    return [{"functionDeclarations": declarations}]


def _describe_py_skill(name: str) -> str:
    line = "- skills/{}".format(name)
    try:
        path = os.path.join(SKILLS_DIR, name)
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        doc = ast.get_docstring(ast.parse(source))
        if doc:
            first_line = doc.strip().splitlines()[0].strip()
            if first_line:
                line = "- skills/{}: {}".format(name, first_line)
    except Exception:
        pass
    return line


def _describe_md_skill(name: str) -> str:
    line = "- skills/{}".format(name)
    try:
        path = os.path.join(SKILLS_DIR, name)
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        for raw_line in content.splitlines():
            stripped = raw_line.strip()
            if stripped:
                summary = stripped.lstrip("#").strip()
                if summary:
                    line = "- skills/{}: {}".format(name, summary[:80])
                break
    except Exception:
        pass
    return line


def list_skills() -> str:
    try:
        names = sorted(os.listdir(SKILLS_DIR))
    except OSError:
        return ""

    lines: List[str] = []
    for name in names:
        if name.endswith(".py") and not name.startswith("_"):
            lines.append(_describe_py_skill(name))
        elif name.endswith(".md") and not name.startswith("_") and name.lower() != "readme.md":
            lines.append(_describe_md_skill(name))
    return "\n".join(lines)


def build_system_instruction() -> Dict[str, Any]:
    text = settings.get("SYSTEM_PROMPT")
    text += "\n\n[long-term memory]\n" + read_memory()
    text += '\n(search older memories: run grep -ri "<keyword>" memory/ in the shell)'

    skills_text = list_skills()
    if skills_text:
        text += (
            "\n\n[skills] (.py: run with python3 skills/<name>.py / "
            ".md: read full text with cat skills/<name>.md and follow the procedure)\n" + skills_text
        )

    env = shell.execution_environment()
    text += "\n\n[execution environment]\nOS: {} / arch: {} / shell: {} / cwd: {}".format(
        env["os"], env["arch"], env["shell"], env["cwd"]
    )
    return {"parts": [{"text": text}]}


def _error_status(res: Dict[str, Any]) -> Optional[str]:
    error = res.get("error") if isinstance(res, dict) else None
    if isinstance(error, dict):
        status = error.get("status")
        if isinstance(status, str):
            return status
    return None


def _should_fallback(status: int, res: Dict[str, Any]) -> bool:
    if status == 429:
        return True
    if status in _FALLBACK_5XX:
        return True
    if status == 0:
        return True
    if _error_status(res) == "RESOURCE_EXHAUSTED":
        return True
    return False


def _fallback_reason(status: int, res: Dict[str, Any]) -> str:
    if status == 429:
        return "429"
    if status == 0:
        return "transport error"
    if status in _FALLBACK_5XX:
        return "HTTP {}".format(status)
    if _error_status(res) == "RESOURCE_EXHAUSTED":
        return "RESOURCE_EXHAUSTED"
    return "unknown error"


def _parse_retry_delay(res: Dict[str, Any]) -> Optional[float]:
    """Best-effort extraction of a server-suggested retry delay (seconds) from
    a Gemini 429 error body. Fully defensive: never raises, returns None if
    nothing parseable.
    """
    try:
        error = res.get("error") if isinstance(res, dict) else None
        if not isinstance(error, dict):
            return None

        details = error.get("details")
        if isinstance(details, list):
            for detail in details:
                if not isinstance(detail, dict):
                    continue
                type_name = detail.get("@type")
                if isinstance(type_name, str) and type_name.endswith("RetryInfo"):
                    retry_delay = detail.get("retryDelay")
                    if isinstance(retry_delay, str) and retry_delay.endswith("s"):
                        try:
                            return float(retry_delay[:-1])
                        except ValueError:
                            pass

        message = error.get("message")
        if isinstance(message, str):
            match = _RETRY_DELAY_MESSAGE_RE.search(message)
            if match:
                try:
                    return float(match.group(1))
                except ValueError:
                    pass
    except Exception:
        return None
    return None


def call_gemini(
    contents: List[Dict[str, Any]],
    on_cooldown: Optional[Callable[[int, int, float], None]] = None,
) -> Tuple[Dict[str, Any], str]:
    """Call the Gemini model chain, retrying the whole chain with a cooldown
    on rate limit / transient errors.

    Settings (chain, key, timeout, inter-model fallback delay, retry base
    delay, max retries) are snapshotted once at the top of the call; the
    snapshot covers the whole call including all retry passes. If every
    model in the chain fails with a fallback-eligible status (429/5xx/
    transport/RESOURCE_EXHAUSTED), the whole chain is retried up to
    GEMINI_MAX_RETRIES times, sleeping a cooldown between passes: the
    largest server-suggested retry delay seen in the pass (if any),
    otherwise exponential backoff from GEMINI_RETRY_BASE_DELAY_SEC, capped
    at _MAX_COOLDOWN_SEC. Non-fallback statuses (e.g. 400/401/403) raise
    immediately without any retry. `on_cooldown(attempt, max_retries, delay)`
    is invoked (best-effort, exceptions swallowed) right before each
    cooldown sleep, so callers can notify the user.
    """
    chain = settings.get("GEMINI_MODEL_CHAIN")
    key = settings.get("GEMINI_API_KEY")
    timeout = settings.get("GEMINI_TIMEOUT_SEC")
    delay = settings.get("GEMINI_FALLBACK_DELAY_SEC")
    base_delay = settings.get("GEMINI_RETRY_BASE_DELAY_SEC")
    max_retries = settings.get("GEMINI_MAX_RETRIES")

    payload = {
        "contents": contents,
        "systemInstruction": build_system_instruction(),
        "tools": build_tools_schema(),
    }

    for attempt in range(max_retries + 1):
        hinted_delay: Optional[float] = None

        for index, model in enumerate(chain):
            url = _GEMINI_URL_TEMPLATE.format(model)
            headers = {"x-goog-api-key": key}
            res, status = http_post(url, payload, headers=headers, timeout=timeout)

            if status == 200:
                return res, model

            if _should_fallback(status, res):
                _LOGGER.warning(
                    "Gemini model %s call failed (%s), switching to next model", model, _fallback_reason(status, res)
                )
                parsed_delay = _parse_retry_delay(res)
                if parsed_delay is not None and (hinted_delay is None or parsed_delay > hinted_delay):
                    hinted_delay = parsed_delay
                if index < len(chain) - 1:
                    time.sleep(delay)
                continue

            raise Exception("Gemini API call failed (model={}, status={})".format(model, status))

        if attempt == max_retries:
            raise Exception("All Gemini models failed (rate limit or transient error).")

        cooldown = min(max(hinted_delay or 0, base_delay * (2 ** attempt)), _MAX_COOLDOWN_SEC)
        from_server_hint = hinted_delay is not None and cooldown == hinted_delay
        _LOGGER.warning(
            "All Gemini models failed on attempt %d/%d, cooling down %.1fs before retry (source=%s)",
            attempt + 1, max_retries + 1, cooldown, "server hint" if from_server_hint else "exponential backoff",
        )
        if on_cooldown is not None:
            try:
                on_cooldown(attempt + 1, max_retries, cooldown)
            except Exception:
                _LOGGER.debug("on_cooldown callback raised, ignoring", exc_info=True)
        time.sleep(cooldown)

    raise Exception("All Gemini models failed (rate limit or transient error).")


class ParsedReply:
    def __init__(
        self,
        text: str,
        function_calls: List[Tuple[str, Dict[str, Any]]],
        blocked: bool,
        block_reason: Optional[str],
        finish_reason: Optional[str],
        raw_content: Dict[str, Any],
    ) -> None:
        self.text = text
        self.function_calls = function_calls
        self.blocked = blocked
        self.block_reason = block_reason
        self.finish_reason = finish_reason
        self.raw_content = raw_content


def parse_response(response: Dict[str, Any]) -> ParsedReply:
    if not isinstance(response, dict):
        return ParsedReply("", [], False, None, None, {})

    candidates = response.get("candidates")
    if not candidates:
        block_reason = None
        prompt_feedback = response.get("promptFeedback")
        if isinstance(prompt_feedback, dict):
            block_reason = prompt_feedback.get("blockReason")
        return ParsedReply("", [], bool(block_reason), block_reason, None, {})

    candidate = candidates[0]
    if not isinstance(candidate, dict):
        return ParsedReply("", [], False, None, None, {})

    finish_reason = candidate.get("finishReason")
    content = candidate.get("content")
    if not isinstance(content, dict):
        return ParsedReply("", [], False, None, finish_reason, {})

    parts = content.get("parts")
    if not isinstance(parts, list):
        parts = []

    text_chunks: List[str] = []
    function_calls: List[Tuple[str, Dict[str, Any]]] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("thought"):
            continue
        if "functionCall" in part:
            fc = part.get("functionCall") or {}
            function_calls.append((fc.get("name", ""), fc.get("args") or {}))
            continue
        if "text" in part:
            text_chunks.append(part.get("text") or "")

    return ParsedReply("".join(text_chunks), function_calls, False, None, finish_reason, content)
