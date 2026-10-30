from __future__ import annotations

import ast
import logging
import os
import re
import time
from collections import namedtuple
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.agent.config import REPO_SKILLS_DIR, SKILLS_DIR, settings
from src.agent.core import chat_admin, mcp, scheduler, shell
from src.agent.core.memory import append_memory, read_memory
from src.agent.utils.http import http_post_h

_LOGGER = logging.getLogger("shellie.gemini")

_GEMINI_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"

_FALLBACK_5XX = (500, 502, 503, 504)

# Cap for a single cooldown sleep between whole-chain retry passes.
_MAX_COOLDOWN_SEC = 300

# Outcome of one chain entry: ok + response, or fallback + server delay hint.
Attempt = namedtuple("Attempt", ["ok", "response", "hint"])

# Short exponential backoff for 5xx/transport errors (transient overload).
# Retries the same model up to this many times before falling back.
_5XX_RETRY_BASE_SEC = 10
_5XX_MAX_RETRIES = 3

# Live retry/cooldown status, for display purposes only (e.g. web UI / bypass
# commands). Updated by the task-queue worker thread as the LLM chain
# progresses; never consulted for retry/backoff/fallback decisions.
_RETRY_STATUS: Dict[str, Any] = {
    "phase": "idle",
    "reason": None,
    "model": None,
    "until": None,
    "attempt": None,
    "max_attempts": None,
    "updated_at": time.time(),
}


def _set_retry_status(
    phase: str,
    reason: Optional[str] = None,
    model: Optional[str] = None,
    delay: Optional[float] = None,
    attempt: Optional[int] = None,
    max_attempts: Optional[int] = None,
) -> None:
    """Publish the current retry/cooldown state for display via get_retry_status().

    Builds a brand-new dict and rebinds the module global _RETRY_STATUS in one
    step (copy-on-write); the rebind is atomic under the GIL, so readers on any
    thread never observe a partially-updated dict. The only writer is the
    task-queue worker thread; if that ever changes, last-writer-wins. Never
    raises.
    """
    try:
        now = time.time()
        until = now + float(delay) if delay is not None else None
        global _RETRY_STATUS
        _RETRY_STATUS = {
            "phase": phase,
            "reason": reason,
            "model": model,
            "until": until,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "updated_at": now,
        }
    except Exception:
        _LOGGER.debug("Exception setting retry status, ignoring", exc_info=True)


def get_retry_status() -> Dict[str, Any]:
    """Return a copy of the current retry/cooldown status.

    Safe to call from any thread; the caller may freely mutate the returned
    dict without affecting the module's internal state.
    """
    return dict(_RETRY_STATUS)


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
    description = (
        "Runs a shell command on {}({}) in {}. Supports file management, script execution, etc. "
        "Pass the command exactly as it would be typed in a terminal; never backslash-escape quote "
        "characters."
    ).format(env["os"], env["arch"], env["shell"])
    return {
        "name": "execute_shell",
        "description": description,
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "command": {
                    "type": "STRING",
                    "description": (
                        "Shell command to execute, verbatim as typed in a terminal (no added "
                        "backslash-escaping of quotes)"
                    ),
                },
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


def _send_file_declaration() -> Dict[str, Any]:
    return {
        "name": "send_file",
        "description": "Sends a file from the host machine to the user in this Telegram chat as a document (max 50 MB). Use it when the user asks for a file, or to deliver a file you generated. Directories must be archived first (e.g. zip).",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "path": {
                    "type": "STRING",
                    "description": "Path to the file, relative to the working directory/workspace or absolute",
                },
                "caption": {
                    "type": "STRING",
                    "description": "Short plain-text caption",
                },
            },
            "required": ["path"],
        },
    }


def _send_file_run(args: Dict[str, Any]) -> str:
    return "send_file is only available inside a Telegram chat."


def _view_file_declaration() -> Dict[str, Any]:
    return {
        "name": "view_file",
        "description": "Attaches a file from the host so YOU can see or read it: images, PDFs, audio and video are attached natively; text/code files return their content. Use this whenever the user refers to an existing file by name or path (e.g. 'explain photo.jpg', 'summarize report.pdf') instead of guessing its contents. Limits per request: 5 attachments / 5 MB total; text files up to 100 KB. To deliver a file to the user, use send_file instead.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "path": {
                    "type": "STRING",
                    "description": "Path to the file, relative to the workspace (cwd) or absolute",
                },
            },
            "required": ["path"],
        },
    }


def _view_file_run(args: Dict[str, Any]) -> str:
    return "view_file is only available inside a Telegram chat."


def _manage_schedule_declaration() -> Dict[str, Any]:
    return {
        "name": "manage_schedule",
        "description": "Create, list, get, enable/disable or remove scheduled prompts that run automatically and report back in this chat. Only add/remove/disable/update when the user explicitly asks. ALWAYS set a short name (≤60 chars) on add. Call `list` first to resolve plain-English references like 'the news briefing' to an id. Minimum interval 15 minutes. Preferred spec: 5-field cron in local time (minute hour day-of-month month day-of-week), e.g. '0 9 * * *' (daily 09:00), '0 9 * * 1-5' (weekdays 09:00), '15 0,2,4 * * *', '*/30 * * * *'. Day-of-week 0 or 7 is Sunday; names like mon or jan are accepted. If both day-of-month and day-of-week are restricted, a day matches when either matches. Legacy forms are still accepted: 'daily 09:00', 'daily 09:00,18:00', 'weekdays 08:30', 'weekly mon,thu 09:00', 'hourly :15', 'every 30m', 'every 2h'. One-shot: 'at YYYY-MM-DD HH:MM' (must be in the future).",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "Action to perform: list, get, add, update, remove, enable, or disable",
                },
                "spec": {
                    "type": "STRING",
                    "description": "Schedule spec (required for add, optional for update): 5-field cron (preferred, e.g. '0 9 * * 1-5'), a legacy form (e.g. 'daily 09:00'), or 'at YYYY-MM-DD HH:MM' for a one-shot",
                },
                "prompt": {
                    "type": "STRING",
                    "description": "Prompt text (required for add, optional for update)",
                },
                "name": {
                    "type": "STRING",
                    "description": "Short schedule name ≤60 chars (recommended for add, optional for update)",
                },
                "id": {
                    "type": "INTEGER",
                    "description": "Schedule ID (required for get, update, remove, enable, disable)",
                },
            },
            "required": ["action"],
        },
    }


def _manage_schedule_run(args: Dict[str, Any]) -> str:
    try:
        action = str(args.get("action", "")).strip()
        spec = str(args.get("spec", "")).strip() if args.get("spec") else None
        prompt = str(args.get("prompt", "")).strip() if args.get("prompt") else None
        name = str(args.get("name", "")).strip() if args.get("name") else None
        schedule_id = args.get("id")

        if action == "list":
            entries = scheduler.list_entries()
            if not entries:
                return "No schedules."
            try:
                return scheduler.format_list(entries)
            except Exception:
                return "No schedules."

        elif action == "get":
            if schedule_id is None:
                return "Error: get requires id"
            ok, message = scheduler.get(schedule_id)
            return message

        elif action == "add":
            if not spec or not prompt:
                return "Error: add requires spec and prompt"
            ok, message = scheduler.add(spec, prompt, name)
            return message

        elif action == "update":
            if schedule_id is None:
                return "Error: update requires id"
            ok, message = scheduler.update(schedule_id, spec, prompt, name)
            return message

        elif action == "remove":
            if schedule_id is None:
                return "Error: remove requires id"
            ok, message = scheduler.remove(schedule_id)
            return message

        elif action == "enable":
            if schedule_id is None:
                return "Error: enable requires id"
            ok, message = scheduler.set_enabled(schedule_id, True)
            return message

        elif action == "disable":
            if schedule_id is None:
                return "Error: disable requires id"
            ok, message = scheduler.set_enabled(schedule_id, False)
            return message

        else:
            return "Error: unknown action {}".format(action)

    except Exception as exc:
        return "Error: {}".format(exc)


TOOL_REGISTRY: List[ToolEntry] = [
    ToolEntry("execute_shell", _execute_shell_declaration, _execute_shell_run),
    ToolEntry("append_memory", _append_memory_declaration, _append_memory_run),
    ToolEntry("execute_web_search", _execute_web_search_declaration, _execute_web_search_run),
    ToolEntry("send_file", _send_file_declaration, _send_file_run),
    ToolEntry("view_file", _view_file_declaration, _view_file_run),
    ToolEntry("manage_schedule", _manage_schedule_declaration, _manage_schedule_run),
    ToolEntry("manage_skill", chat_admin.skill_declaration, chat_admin.skill_run),
    ToolEntry("propose_mcp_server", chat_admin.mcp_declaration, chat_admin.mcp_run),
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


def _describe_py_skill(path: str) -> str:
    # Absolute path: the shell cwd is the workspace, so relative skills/ paths would miss.
    line = "- {}".format(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        doc = ast.get_docstring(ast.parse(source))
        if doc:
            first_line = doc.strip().splitlines()[0].strip()
            if first_line:
                line = "- {}: {}".format(path, first_line)
    except Exception:
        pass
    return line


def _describe_md_skill(path: str) -> str:
    line = "- {}".format(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        for raw_line in content.splitlines():
            stripped = raw_line.strip()
            if stripped:
                summary = stripped.lstrip("#").strip()
                if summary:
                    line = "- {}: {}".format(path, summary[:80])
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
            lines.append(_describe_py_skill(path))
        else:
            lines.append(_describe_md_skill(path))
    return "\n".join(lines)


def build_system_instruction() -> Dict[str, Any]:
    text = settings.get("SYSTEM_PROMPT")
    text += "\n\n[long-term memory]\n" + read_memory()
    text += '\n(search older memories: run grep -ri "<keyword>" memory/ in the shell)'

    skills_text = list_skills()
    if skills_text:
        text += (
            "\n\n[skills] (paths are absolute; the shell cwd is the workspace, so never use "
            "relative skills/ paths and never create a skills folder in the workspace. "
            "Built-in skills dir: {}; user skills dir: {}. A procedure that mentions skills/<file> "
            "means the file in one of these dirs. "
            ".py: run with python3 <absolute path> / "
            ".md: read full text with cat <absolute path> and follow the procedure; "
            "create new skills only as .md in ~/.shellie/skills/; existing .py skills stay as they are)\n"
            .format(REPO_SKILLS_DIR, SKILLS_DIR)
            + skills_text
        )

    text += (
        "\n\n[tool errors] If a tool returns an error (for example 'MCP tool error' or a "
        "permission denied message), report that error to the user as it is. Do not search "
        "the filesystem or try unrelated commands to work around it."
    )

    env = shell.execution_environment()
    text += "\n\n[execution environment]\nOS: {} / arch: {} / shell: {} / cwd: {}".format(
        env["os"], env["arch"], env["shell"], env["cwd"]
    )

    # Add local time at call time (no caching)
    try:
        local_time = time.localtime()
        local_time_str = time.strftime("%Y-%m-%d %H:%M", local_time)
        tz_name = time.strftime("%Z", local_time)
        # Calculate UTC offset: use timezone (standard) or altzone (DST) based on tm_isdst
        offset_sec = time.altzone if local_time.tm_isdst > 0 else time.timezone
        offset_hours = -offset_sec // 3600
        offset_mins = (-offset_sec % 3600) // 60
        offset_str = "UTC{:+03d}:{:02d}".format(offset_hours, offset_mins)
        text += "\nlocal time: {} ({}, {})".format(local_time_str, tz_name, offset_str)
    except Exception:
        pass

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
    This is authoritative when present. Fallback: error message contains
    "per day", "daily", or "rpd" keywords — used only when no quotaId detail
    is present. The retry delay is NOT a reliable signal — RPD retryDelay is
    the same order of magnitude as RPM (seconds, not hours).
    """
    try:
        error = res.get("error") if isinstance(res, dict) else None
        if isinstance(error, dict):
            saw_quota_id = False
            for detail in (error.get("details") or []):
                if not isinstance(detail, dict):
                    continue
                if "QuotaFailure" not in detail.get("@type", ""):
                    continue
                for v in (detail.get("violations") or []):
                    quota_id = v.get("quotaId", "") if isinstance(v, dict) else ""
                    if quota_id:
                        saw_quota_id = True
                    if _RPD_QUOTA_ID_RE.search(quota_id):
                        return True
            # quotaId is authoritative when present; don't consult message heuristic
            if saw_quota_id:
                return False
            # No quotaId detail; fall back to message keywords
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


def build_payload(contents: List[Dict[str, Any]], allow_tools: bool) -> Dict[str, Any]:
    """Gemini generateContent body for one chat turn. Built once per call by
    the chain orchestrator (core/llm.py) and reused by every Gemini entry.
    When allow_tools is False, function calling is disabled (mode "NONE")
    while the tool declarations stay in the payload so history referencing
    prior functionCall/functionResponse parts remains valid."""
    payload: Dict[str, Any] = {
        "contents": contents,
        "systemInstruction": build_system_instruction(),
        "tools": build_tools_schema(),
    }
    if not allow_tools:
        payload["toolConfig"] = {"functionCallingConfig": {"mode": "NONE"}}
    return payload


def attempt_model(model: str, payload: Dict[str, Any], key: str, timeout: float, delay: float) -> Attempt:
    """Try one Gemini model, including its same-model retries: one RPM sleep
    and retry on 429, and exponential backoff on 5xx/transport errors.

    Returns Attempt(ok=True, response=...) on success. Returns
    Attempt(ok=False, hint=...) when the caller should fall back to the next
    chain entry; hint is the server-suggested delay in seconds, or None.
    Raises for non-fallback statuses (e.g. 400/401/403). The pause between
    chain entries (delay) is the caller's job, not this function's."""
    url = _GEMINI_URL_TEMPLATE.format(model)
    req_headers = {"x-goog-api-key": key}
    _set_retry_status("calling", model=model)
    _LOGGER.debug("Gemini request model=%s url=%s", model, url)
    res, status, resp_headers = http_post_h(url, payload, headers=req_headers, timeout=timeout)
    _LOGGER.debug("Gemini raw response model=%s status=%s body=%s", model, status, res)

    if status == 200:
        return Attempt(True, res, None)

    if status == 429:
        parsed_delay = _parse_retry_delay_with_headers(res, resp_headers)
        if _is_rpd_limit(res, parsed_delay):
            # RPD exhaustion: this model is done for the day, fall back.
            _LOGGER.warning("Gemini model %s RPD limit hit, falling back to next model", model)
            return Attempt(False, None, parsed_delay)
        # RPM/TPM: short-lived rate limit, sleep and retry same model once.
        rpm_sleep = parsed_delay if parsed_delay is not None else delay
        hint_source = "server hint" if parsed_delay is not None else "fallback delay (no server hint)"
        _LOGGER.warning(
            "Gemini model %s 429 RPM/TPM, sleeping %.1fs (%s) before retry",
            model, rpm_sleep, hint_source,
        )
        _set_retry_status(
            "cooldown", reason="429_rpm", model=model, delay=rpm_sleep, attempt=1, max_attempts=1
        )
        time.sleep(rpm_sleep)
        _set_retry_status("calling", model=model)
        res, status, resp_headers = http_post_h(url, payload, headers=req_headers, timeout=timeout)
        _LOGGER.debug("Gemini raw response model=%s status=%s body=%s", model, status, res)
        if status == 200:
            return Attempt(True, res, None)
        # Still failing after one RPM retry: treat as fallback.
        return Attempt(False, None, _parse_retry_delay_with_headers(res, resp_headers))

    if status in _FALLBACK_5XX or status == 0:
        # Transient overload/transport error: retry same model with short
        # exponential backoff before giving up and falling back.
        for retry in range(1, _5XX_MAX_RETRIES + 1):
            backoff = _5XX_RETRY_BASE_SEC * (2 ** (retry - 1))
            _LOGGER.warning(
                "Gemini model %s transient error (HTTP %s), retry %d/%d in %.1fs",
                model, status, retry, _5XX_MAX_RETRIES, backoff,
            )
            _set_retry_status(
                "cooldown",
                reason="5xx" if status in _FALLBACK_5XX else "transport",
                model=model,
                delay=backoff,
                attempt=retry,
                max_attempts=_5XX_MAX_RETRIES,
            )
            time.sleep(backoff)
            _set_retry_status("calling", model=model)
            res, status, resp_headers = http_post_h(url, payload, headers=req_headers, timeout=timeout)
            _LOGGER.debug("Gemini raw response model=%s status=%s body=%s", model, status, res)
            if status == 200:
                return Attempt(True, res, None)
            if status not in _FALLBACK_5XX and status != 0:
                break  # non-transient error, stop retrying
        # All same-model retries exhausted, fall back to next model.
        _LOGGER.warning(
            "Gemini model %s still failing after %d retries, falling back",
            model, _5XX_MAX_RETRIES,
        )
        return Attempt(False, None, None)

    if _should_fallback(status, res):
        _LOGGER.warning(
            "Gemini model %s call failed (%s), switching to next model", model, _fallback_reason(status, res)
        )
        return Attempt(False, None, _parse_retry_delay_with_headers(res, resp_headers))

    raise Exception("Gemini API call failed (model={}, status={})".format(model, status))


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
