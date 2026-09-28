from __future__ import annotations

import ast
import logging
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.agent.config import REPO_SKILLS_DIR, SKILLS_DIR, settings
from src.agent.core import mcp, shell
from src.agent.core.memory import append_memory, read_memory
from src.agent.utils.http import http_post_h

_LOGGER = logging.getLogger("shellie.gemini")

_GEMINI_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"

_FALLBACK_5XX = (500, 502, 503, 504)

# Cap for a single cooldown sleep between whole-chain retry passes.
_MAX_COOLDOWN_SEC = 300

# Short exponential backoff for 5xx/transport errors (transient overload).
# Retries the same model up to this many times before falling back.
_5XX_RETRY_BASE_SEC = 10
_5XX_MAX_RETRIES = 3

_RETRY_DELAY_MESSAGE_RE = re.compile(r"retry in ([\d.]+)s", re.IGNORECASE)

# Retry delays at or above this threshold are treated as RPD (Requests Per Day)
# exhaustion → fall back to next model. Below this → RPM/TPM → sleep and retry
# the same model.
_RPD_THRESHOLD_SEC = 300

_RPD_MESSAGE_RE = re.compile(r"per.?day|daily|rpd|exceeded your current quota", re.IGNORECASE)


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


def call_web_search(query: str) -> str:
    """Call the search model with google_search grounding and return the answer text."""
    key = settings.get("GEMINI_API_KEY")
    timeout = settings.get("GEMINI_TIMEOUT_SEC")
    model = settings.get("SEARCH_MODEL")[0]
    url = _GEMINI_URL_TEMPLATE.format(model)
    payload = {
        "contents": [{"parts": [{"text": query}]}],
        "tools": [{"google_search": {}}],
    }
    _LOGGER.debug("web search model=%s query=%s", model, query)
    res, status, _headers = http_post_h(url, payload, headers={"x-goog-api-key": key}, timeout=timeout)
    _LOGGER.debug("web search response model=%s status=%s", model, status)
    if status != 200:
        return "Web search failed (HTTP {})".format(status)
    parsed = parse_response(res)
    return parsed.text or "(no result)"


def _execute_web_search_declaration() -> Dict[str, Any]:
    return {
        "name": "execute_web_search",
        "description": "Search the web for current information using Google Search grounding.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {"type": "STRING", "description": "Search query"},
            },
            "required": ["query"],
        },
    }


def _execute_web_search_run(args: Dict[str, Any]) -> str:
    return call_web_search(str(args.get("query", "")))


TOOL_REGISTRY: List[ToolEntry] = [
    ToolEntry("execute_shell", _execute_shell_declaration, _execute_shell_run),
    ToolEntry("append_memory", _append_memory_declaration, _append_memory_run),
    ToolEntry("execute_web_search", _execute_web_search_declaration, _execute_web_search_run),
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


def _describe_py_skill(name: str, path: str) -> str:
    line = "- skills/{}".format(name)
    try:
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


def _describe_md_skill(name: str, path: str) -> str:
    line = "- skills/{}".format(name)
    try:
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
    # Repo skills are the base; user skills (~/.shellie/skills/) override by name.
    skill_map: Dict[str, str] = {}  # name -> absolute path
    for directory in (REPO_SKILLS_DIR, SKILLS_DIR):
        try:
            for name in sorted(os.listdir(directory)):
                if (name.endswith(".py") or name.endswith(".md")) and not name.startswith("_"):
                    skill_map[name] = os.path.join(directory, name)
        except OSError:
            pass

    lines: List[str] = []
    for name in sorted(skill_map):
        if name.lower() == "readme.md":
            continue
        path = skill_map[name]
        if name.endswith(".py"):
            lines.append(_describe_py_skill(name, path))
        else:
            lines.append(_describe_md_skill(name, path))
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


def _parse_retry_delay_with_headers(res: Dict[str, Any], resp_headers: Dict[str, str]) -> Optional[float]:
    """Like _parse_retry_delay but also checks the Retry-After response header."""
    delay = _parse_retry_delay(res)
    retry_after = resp_headers.get("retry-after")
    if retry_after:
        try:
            header_delay = float(retry_after)
            if delay is None or header_delay > delay:
                delay = header_delay
        except ValueError:
            pass
    return delay


_RPD_QUOTA_ID_RE = re.compile(r"PerDay", re.IGNORECASE)


def _is_rpd_limit(res: Dict[str, Any], delay: Optional[float]) -> bool:
    """Return True if a 429 looks like an RPD (per-day) exhaustion.

    Primary signal: a QuotaFailure violation whose quotaId contains "PerDay".
    Fallback: error message contains "per day", "daily", or "rpd" keywords.
    The retry delay is NOT a reliable signal — RPD retryDelay is the same
    order of magnitude as RPM (seconds, not hours).
    """
    try:
        error = res.get("error") if isinstance(res, dict) else None
        if isinstance(error, dict):
            for detail in (error.get("details") or []):
                if not isinstance(detail, dict):
                    continue
                if "QuotaFailure" not in detail.get("@type", ""):
                    continue
                for v in (detail.get("violations") or []):
                    quota_id = v.get("quotaId", "") if isinstance(v, dict) else ""
                    if _RPD_QUOTA_ID_RE.search(quota_id):
                        return True
            message = error.get("message", "")
            if isinstance(message, str) and _RPD_MESSAGE_RE.search(message):
                return True
    except Exception:
        _LOGGER.debug("Exception parsing RPD limit details, assuming RPM", exc_info=True)
    return False


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
            req_headers = {"x-goog-api-key": key}
            _LOGGER.debug("Gemini request model=%s url=%s", model, url)
            res, status, resp_headers = http_post_h(url, payload, headers=req_headers, timeout=timeout)
            _LOGGER.debug("Gemini raw response model=%s status=%s body=%s", model, status, res)

            if status == 200:
                return res, model

            if status == 429:
                parsed_delay = _parse_retry_delay_with_headers(res, resp_headers)
                if _is_rpd_limit(res, parsed_delay):
                    # RPD exhaustion: this model is done for the day, fall back.
                    _LOGGER.warning(
                        "Gemini model %s RPD limit hit, falling back to next model", model
                    )
                    if parsed_delay is not None and (hinted_delay is None or parsed_delay > hinted_delay):
                        hinted_delay = parsed_delay
                    if index < len(chain) - 1:
                        time.sleep(delay)
                    continue
                else:
                    # RPM/TPM: short-lived rate limit, sleep and retry same model.
                    rpm_sleep = parsed_delay if parsed_delay is not None else delay
                    hint_source = "server hint" if parsed_delay is not None else "fallback delay (no server hint)"
                    _LOGGER.warning(
                        "Gemini model %s 429 RPM/TPM, sleeping %.1fs (%s) before retry",
                        model, rpm_sleep, hint_source,
                    )
                    time.sleep(rpm_sleep)
                    # Retry same model: redo this iteration.
                    res, status, resp_headers = http_post_h(url, payload, headers=req_headers, timeout=timeout)
                    _LOGGER.debug("Gemini raw response model=%s status=%s body=%s", model, status, res)
                    if status == 200:
                        return res, model
                    # Still failing after one RPM retry — treat as fallback.
                    parsed_delay2 = _parse_retry_delay_with_headers(res, resp_headers)
                    if parsed_delay2 is not None and (hinted_delay is None or parsed_delay2 > hinted_delay):
                        hinted_delay = parsed_delay2
                    if index < len(chain) - 1:
                        time.sleep(delay)
                    continue

            if status in _FALLBACK_5XX or status == 0:
                # Transient overload/transport error: retry same model with short
                # exponential backoff before giving up and falling back.
                for retry in range(1, _5XX_MAX_RETRIES + 1):
                    backoff = _5XX_RETRY_BASE_SEC * (2 ** (retry - 1))
                    _LOGGER.warning(
                        "Gemini model %s transient error (HTTP %s), retry %d/%d in %.1fs",
                        model, status, retry, _5XX_MAX_RETRIES, backoff,
                    )
                    time.sleep(backoff)
                    res, status, resp_headers = http_post_h(url, payload, headers=req_headers, timeout=timeout)
                    _LOGGER.debug("Gemini raw response model=%s status=%s body=%s", model, status, res)
                    if status == 200:
                        return res, model
                    if status not in _FALLBACK_5XX and status != 0:
                        break  # non-transient error, stop retrying
                # All same-model retries exhausted, fall back to next model.
                _LOGGER.warning(
                    "Gemini model %s still failing after %d retries, falling back",
                    model, _5XX_MAX_RETRIES,
                )
                if index < len(chain) - 1:
                    time.sleep(delay)
                continue

            if _should_fallback(status, res):
                _LOGGER.warning(
                    "Gemini model %s call failed (%s), switching to next model", model, _fallback_reason(status, res)
                )
                parsed_delay = _parse_retry_delay_with_headers(res, resp_headers)
                if parsed_delay is not None and (hinted_delay is None or parsed_delay > hinted_delay):
                    hinted_delay = parsed_delay
                if index < len(chain) - 1:
                    time.sleep(delay)
                continue

            raise Exception("Gemini API call failed (model={}, status={})".format(model, status))

        if attempt == max_retries:
            raise Exception("All Gemini models failed (rate limit or transient error).")

        cooldown = min(max(hinted_delay or 0, base_delay * (2 ** attempt)), _MAX_COOLDOWN_SEC)
        if hinted_delay is not None and cooldown == hinted_delay:
            cooldown_source = "server hint"
        elif attempt == 0:
            cooldown_source = "base delay"
        else:
            cooldown_source = "exponential backoff (attempt {})".format(attempt + 1)
        _LOGGER.warning(
            "All Gemini models failed on attempt %d/%d, cooling down %.1fs before retry (source=%s)",
            attempt + 1, max_retries + 1, cooldown, cooldown_source,
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
