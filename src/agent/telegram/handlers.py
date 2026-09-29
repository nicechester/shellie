from __future__ import annotations

import difflib
import html
import json
import logging
import os
import queue
import re
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.agent.config import settings
from src.agent.core import tasks
from src.agent.core.gemini import call_gemini, get_retry_status, parse_response, run_tool
from src.agent.core.memory import read_memory
from src.agent.core.shell import execute_shell
from src.agent.telegram.client import delete_message, send_chat_action, send_message

_LOGGER = logging.getLogger("shellie.telegram.handlers")

# Gemini contents history (user/model turns only). Settings and bypass
# commands are never added here (P6). Worker thread only after queue introduced.
_history: List[Dict[str, Any]] = []
_last_activity: float = 0.0

# Task persistence (issue #4): guards every tasks.* file-mutating call and
# the epoch counter used to detect/abandon an in-flight continuation chain
# after /reset, /kill or /discard.
_task_lock = threading.Lock()
_task_epoch: int = 0
_active_continuation: int = 0  # worker writes; /queue reads (best-effort, no lock)

_SETTINGS_TOKENS = ("/settings", "/get", "/set", "/unset")
_BYPASS_TOKENS = (
    "/mem", "/restart", "/reset", "/sh", "/help", "/queue", "/kill", "/systemlog",
    "/continue", "/discard",
)

_HELP_TEXT = (
    "<b>Bypass commands</b> (no LLM)\n"
    "<code>!&lt;cmd&gt;</code> / <code>/sh &lt;cmd&gt;</code> — run shell command\n"
    "<code>/mem</code> — show long-term memory\n"
    "<code>/reset</code> — clear conversation history\n"
    "<code>/restart</code> — restart the process\n"
    "<code>/queue</code> — show task queue status\n"
    "<code>/kill</code> — drain queue and discard pending tasks\n"
    "<code>/continue</code> — resume the unfinished task\n"
    "<code>/discard</code> — drop the unfinished task\n"
    "<code>/systemlog [N]</code> — show last N lines of agent.log (default 50)\n"
    "<code>/help</code> — show this message\n"
    "\n"
    "<b>Settings commands</b>\n"
    "<code>/settings</code> / <code>/get</code> — list all settings\n"
    "<code>/get KEY</code> — detail for one key\n"
    "<code>/set KEY VALUE</code> — apply an override\n"
    "<code>/unset KEY</code> — remove an override\n"
    "\n"
    "<b>Anything else</b> — sent to Gemini LLM"
)

_FENCE_RE = re.compile(r"```(?:[^\n`]*\n)?(.*?)```", re.DOTALL)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_CODE_RE = re.compile(r"`([^`]+?)`")


def _reset_history() -> None:
    global _history
    _history = []


def _drain_queue() -> int:
    drained = 0
    while True:
        try:
            _task_queue.get_nowait()
            _task_queue.task_done()
            drained += 1
        except queue.Empty:
            break
    return drained


def process_update(update: Dict[str, Any]) -> None:
    message = update.get("message")
    if not isinstance(message, dict):
        return
    text = message.get("text")
    if not isinstance(text, str) or not text:
        return
    chat = message.get("chat")
    if not isinstance(chat, dict) or "id" not in chat:
        return
    from_user = message.get("from")
    if not isinstance(from_user, dict) or "id" not in from_user:
        return

    chat_id = chat["id"]
    user_id = from_user["id"]
    message_id = message.get("message_id")

    allowed = settings.get("ALLOWED_USER_ID")
    if allowed is None or user_id != allowed:
        _LOGGER.warning("Unauthorized access: user_id=%s", user_id)
        return

    if _handle_settings_command(chat_id, message_id, text):
        return
    if _handle_bypass_command(chat_id, text):
        return
    _task_queue.put(_QueueItem(chat_id, text, user_id=user_id))


def _first_token_command(text: str) -> str:
    parts = text.split(None, 1)
    if not parts:
        return ""
    return parts[0].split("@", 1)[0]


def _esc(value: Any) -> str:
    return html.escape(str(value))


def _row_map() -> Dict[str, Dict[str, Any]]:
    return {row["key"]: row for row in settings.rows(mask=True)}


# ---------------------------------------------------------------------------
# Settings commands (dev-plan §6.4), excluded from LLM history, HTML output.
# ---------------------------------------------------------------------------


def _settings_listing_text() -> str:
    lines = []
    for row in settings.rows(mask=True):
        value = row["value"]
        if len(value) > 60:
            value = value[:60] + "…"
        marker = " [🔒web-only]" if not row["telegram_editable"] else ""
        lines.append(
            "{} = {} [{}]{} ({})".format(
                row["key"], value, row["source"], marker, row["apply_timing"]
            )
        )
    body = "\n".join(lines)
    return "<pre>{}</pre>".format(html.escape(body))


def _unknown_key_reply(key: str, known_keys: List[str]) -> str:
    suggestion = difflib.get_close_matches(key, known_keys, n=1)
    if suggestion:
        return "❓ Unknown key: {}. Did you mean {}? Use /settings to list all keys.".format(
            _esc(key), _esc(suggestion[0])
        )
    return "❓ Unknown key: {}. Use /settings to list all keys.".format(_esc(key))


def _get_key_detail(key: str) -> str:
    rows = _row_map()
    if key not in rows:
        return _unknown_key_reply(key, list(rows.keys()))
    row = rows[key]
    lock_note = "allowed" if row["telegram_editable"] else "🔒 web UI only"
    lines = [
        "<b>{}</b>".format(key),
        "value: {}".format(_esc(row["value"])),
        "source: {}".format(_esc(row["source"])),
        "default: {}".format(_esc(row["default"])),
        "constraint: {}".format(_esc(row["constraint"])),
        "apply timing: {}".format(_esc(row["apply_timing"])),
        "description: {}".format(_esc(row["description"])),
        "Telegram editable: {}".format(lock_note),
    ]
    return "\n".join(lines)


def _set_usage_reply() -> str:
    keys = [row["key"] for row in settings.rows(mask=False) if row["telegram_editable"]]
    return (
        "Usage: /set KEY VALUE (empty value not allowed; restore default with /unset KEY)\n"
        "Editable keys: {}".format(_esc(", ".join(keys)))
    )


def _blocked_key_reply(chat_id: int, message_id: Optional[int], key: str, secret: bool) -> str:
    port = settings.get("WEB_PORT")
    reply = "🔒 {} can only be changed via the local web UI (http://127.0.0.1:{}/) for security.".format(key, port)
    if secret:
        if message_id is not None:
            delete_message(chat_id, message_id)
        reply += " Attempted to delete the message. The secret may still be visible in chat history — consider rotating it."
    return reply


def _format_update_result(key: str, result: Any) -> str:
    if not result.ok:
        if "_persist" in result.errors:
            return "❌ Save failed, no changes applied."
        return "⚠️ {}: {}".format(key, _esc(result.errors.get(key, "unknown error")))
    if not result.applied:
        return "ℹ️ No change (already the same value)"
    old_val, new_val = result.changes[key]
    apply_timing = _row_map()[key]["apply_timing"]
    return "✅ {}: {} → {} (apply: {})".format(key, _esc(old_val), _esc(new_val), _esc(apply_timing))


def _format_unset_result(key: str, result: Any) -> str:
    if not result.ok:
        if "_persist" in result.errors:
            return "❌ Save failed, no changes applied."
        return "⚠️ {}: {}".format(key, _esc(result.errors.get(key, "unknown error")))
    if not result.applied:
        return "ℹ️ No change (no override was set)"
    _, new_val = result.changes[key]
    source = _row_map()[key]["source"]
    return "✅ {}: override removed → {} ({})".format(key, _esc(new_val), _esc(source))


def _handle_set(chat_id: int, message_id: Optional[int], key: str, value: str) -> str:
    key = key.strip().upper()
    rows = _row_map()
    if key not in rows:
        return _unknown_key_reply(key, list(rows.keys()))
    row = rows[key]
    if not row["telegram_editable"]:
        return _blocked_key_reply(chat_id, message_id, key, row["secret"])
    if not value:
        return _set_usage_reply()
    if "\n" in value and key != "SYSTEM_PROMPT":
        return "⚠️ {}: value cannot contain newlines".format(key)
    result = settings.update({key: value}, actor="telegram")
    return _format_update_result(key, result)


def _handle_unset(chat_id: int, message_id: Optional[int], key: str) -> str:
    key = key.strip().upper()
    rows = _row_map()
    if key not in rows:
        return _unknown_key_reply(key, list(rows.keys()))
    row = rows[key]
    if not row["telegram_editable"]:
        return _blocked_key_reply(chat_id, message_id, key, row["secret"])
    result = settings.unset(key, actor="telegram")
    return _format_unset_result(key, result)


def _handle_settings_command(chat_id: int, message_id: Optional[int], text: str) -> bool:
    cmd = _first_token_command(text)
    if cmd not in _SETTINGS_TOKENS:
        return False

    if cmd == "/settings":
        send_message(chat_id, _settings_listing_text())
        return True

    if cmd == "/get":
        rest = text.split(None, 1)
        key = rest[1].split()[0] if len(rest) > 1 and rest[1].strip() else ""
        if not key:
            send_message(chat_id, _settings_listing_text())
        else:
            send_message(chat_id, _get_key_detail(key.strip().upper()))
        return True

    if cmd == "/set":
        parts = text.split(None, 2)
        if len(parts) < 2:
            send_message(chat_id, _set_usage_reply())
            return True
        key = parts[1]
        value = parts[2].strip() if len(parts) > 2 else ""
        send_message(chat_id, _handle_set(chat_id, message_id, key, value))
        return True

    if cmd == "/unset":
        rest = text.split(None, 1)
        key = rest[1].split()[0] if len(rest) > 1 and rest[1].strip() else ""
        if not key:
            send_message(chat_id, "Usage: /unset KEY")
            return True
        send_message(chat_id, _handle_unset(chat_id, message_id, key.strip().upper()))
        return True

    return False


# ---------------------------------------------------------------------------
# Bypass commands (LLM 0%), excluded from LLM history.
# ---------------------------------------------------------------------------


def _retry_status_lines(status: Dict[str, Any]) -> List[str]:
    try:
        phase = status.get("phase")
        if phase == "idle" or phase is None:
            return []

        model_html = html.escape(str(status.get("model") or ""))
        remaining = "{:.0f}".format(max(0.0, (status.get("until") or 0.0) - time.time()))
        a = status.get("attempt")
        m = status.get("max_attempts")
        reason = status.get("reason")

        lines: List[str] = []

        if phase == "calling":
            if model_html:
                lines.append("📡 모델 호출 중: <code>{}</code>".format(model_html))
            else:
                lines.append("📡 모델 호출 중")
        elif phase == "cooldown":
            if reason == "429_rpm":
                lines.append("⏳ 429(RPM) 쿨다운: {}초 남음 — <code>{}</code> 재시도 {}/{}".format(
                    remaining, model_html, a, m
                ))
            elif reason == "5xx":
                lines.append("⏳ 서버 오류(5xx) 재시도 대기: {}초 남음 — <code>{}</code> {}/{}회차".format(
                    remaining, model_html, a, m
                ))
            elif reason == "transport":
                lines.append("⏳ 네트워크 오류 재시도 대기: {}초 남음 — <code>{}</code> {}/{}회차".format(
                    remaining, model_html, a, m
                ))
            elif reason == "chain_cooldown":
                lines.append("⏳ 모든 모델 실패, 전체 재시도 대기: {}초 남음 ({}/{})".format(
                    remaining, a, m
                ))
            else:
                lines.append("⏳ 재시도 대기: {}초 남음".format(remaining))

        return lines
    except Exception:
        return []


def _queue_status_text() -> str:
    depth = _task_queue.qsize()
    active = _queue_active_item
    lines = ["<b>Queue status</b>"]
    if active is not None:
        preview = html.escape(active.text[:80])
        lines.append("🔄 처리 중: <code>{}</code>".format(preview))
        if _active_continuation > 0:
            lines.append(
                "Auto-continuation: {}/{}".format(
                    _active_continuation, settings.get("FC_MAX_CONTINUATIONS")
                )
            )
    else:
        lines.append("💤 대기 중 (idle)")
    lines.extend(_retry_status_lines(get_retry_status()))
    lines.append("대기 항목: {}".format(depth))
    return "\n".join(lines)


def _run_shell_bypass(chat_id: int, command: str) -> None:
    send_chat_action(chat_id)
    send_message(chat_id, "⚙️ <code>{}</code>".format(html.escape(command)))
    output = execute_shell(command)
    send_message(chat_id, "<pre>{}</pre>".format(html.escape(output)))


def _handle_restart(chat_id: int) -> None:
    has_systemd = bool(os.environ.get("INVOCATION_ID"))
    xpc = os.environ.get("XPC_SERVICE_NAME")
    has_launchd = xpc not in (None, "", "0")
    prefix = ""
    if not (has_systemd or has_launchd):
        prefix = "⚠️ Running without a service manager. Will not restart automatically.\n"
    send_message(chat_id, prefix + "🔄 Shutting down process. Service manager will restart it...")
    sys.exit(0)


def _handle_bypass_command(chat_id: int, text: str) -> bool:
    if text.startswith("!"):
        _run_shell_bypass(chat_id, text[1:])
        return True

    cmd = _first_token_command(text)
    if cmd not in _BYPASS_TOKENS:
        return False

    if cmd == "/sh":
        parts = text.split(None, 1)
        shell_cmd = parts[1] if len(parts) > 1 else ""
        if not shell_cmd.strip():
            send_message(chat_id, "Usage: /sh <command>")
            return True
        _run_shell_bypass(chat_id, shell_cmd)
        return True

    if cmd == "/mem":
        send_message(
            chat_id,
            "🧠 <b>Long-term memory (core + today)</b>\n\n<pre>{}</pre>".format(html.escape(read_memory())),
        )
        return True

    if cmd == "/reset":
        _reset_history()
        drained = _drain_queue()
        msg = "🔄 Conversation context cleared."
        if drained:
            msg += " (대기 항목 {}개 삭제됨)".format(drained)
        if _abandon_task("reset") is not None:
            msg += " Unfinished task discarded."
        send_message(chat_id, msg)
        return True

    if cmd == "/kill":
        drained = _drain_queue()
        active = _queue_active_item
        active_note = " 현재 처리 중인 항목은 완료 후 중단됩니다." if active else ""
        msg = "🗑️ 대기 항목 {}개 삭제됨.{}".format(drained, active_note)
        if _abandon_task("kill") is not None:
            msg += " Unfinished task discarded."
        send_message(chat_id, msg)
        return True

    if cmd == "/continue":
        with _task_lock:
            record = tasks.load_task()
        if record is None or record.get("chat_id") != chat_id:
            send_message(chat_id, "No unfinished task to continue.")
            return True
        active = _queue_active_item
        if active is not None and active.task_id == record["task_id"]:
            send_message(chat_id, "That task is already in progress.")
            return True
        _task_queue.put(
            _QueueItem(chat_id, "/continue", user_id=None, kind="continue", task_id=record["task_id"])
        )
        age = tasks.format_age(time.time() - record["created_at"])
        send_message(chat_id, "Resuming the unfinished task (started {} ago).".format(age))
        return True

    if cmd == "/discard":
        with _task_lock:
            record = tasks.load_task()
        if record is None or record.get("chat_id") != chat_id:
            send_message(chat_id, "No unfinished task to discard.")
            return True
        active = _queue_active_item
        _abandon_task("discard")
        age = tasks.format_age(time.time() - record["created_at"])
        msg = "Unfinished task discarded (started {} ago).".format(age)
        if active is not None and active.task_id == record["task_id"]:
            msg += " The current step will finish, but it will not continue."
        send_message(chat_id, msg)
        return True

    if cmd == "/systemlog":
        parts = text.split(None, 1)
        try:
            n = int(parts[1]) if len(parts) > 1 else 50
            n = max(1, min(n, 500))
        except ValueError:
            n = 50
        log_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))), "agent.log")
        output = execute_shell("tail -{} {}".format(n, log_path))
        send_message(chat_id, "<pre>{}</pre>".format(html.escape(output)))
        return True

    if cmd == "/restart":
        _handle_restart(chat_id)
        return True

    if cmd == "/help":
        send_message(chat_id, _HELP_TEXT)
        return True

    if cmd == "/queue":
        send_message(chat_id, _queue_status_text())
        return True

    return False


# ---------------------------------------------------------------------------
# Minimal markdown -> Telegram HTML converter (D5).
# ---------------------------------------------------------------------------


def _convert_plain_segment(segment: str) -> str:
    escaped = html.escape(segment)
    escaped = _BOLD_RE.sub(lambda m: "<b>{}</b>".format(m.group(1)), escaped)
    escaped = _CODE_RE.sub(lambda m: "<code>{}</code>".format(m.group(1)), escaped)
    return escaped


def _markdown_to_html(text: str) -> str:
    result: List[str] = []
    last_end = 0
    for match in _FENCE_RE.finditer(text):
        before = text[last_end:match.start()]
        if before:
            result.append(_convert_plain_segment(before))
        result.append("<pre>{}</pre>".format(html.escape(match.group(1))))
        last_end = match.end()
    tail = text[last_end:]
    if tail:
        result.append(_convert_plain_segment(tail))
    return "".join(result)


# ---------------------------------------------------------------------------
# Task queue: LLM messages are enqueued by process_update and processed
# serially by a single worker thread.
# ---------------------------------------------------------------------------



class _QueueItem:
    __slots__ = ("chat_id", "text", "user_id", "kind", "task_id")

    def __init__(
        self,
        chat_id: int,
        text: str,
        user_id: Optional[int] = None,
        kind: str = "message",
        task_id: Optional[str] = None,
    ) -> None:
        self.chat_id = chat_id
        self.text = text
        self.user_id = user_id
        self.kind = kind
        self.task_id = task_id


_task_queue: queue.Queue = queue.Queue()  # unbounded
_queue_active_item: Optional[_QueueItem] = None  # worker thread only (read by /queue on main)


def start_queue_worker() -> None:
    t = threading.Thread(target=_queue_worker, daemon=True, name="shellie-queue-worker")
    t.start()


def _queue_worker() -> None:
    global _queue_active_item
    while True:
        item: _QueueItem = _task_queue.get()
        _queue_active_item = item
        try:
            _process_llm_item(item)
        except Exception:
            _LOGGER.exception("Unexpected error in queue worker for chat_id=%s", item.chat_id)
            try:
                send_message(item.chat_id, "⚠️ 처리 중 오류가 발생했습니다.")
            except Exception:
                _LOGGER.debug("Failed to send worker error notice", exc_info=True)
        finally:
            _queue_active_item = None
            _task_queue.task_done()


_CONT_PROMPT_EMBED_CHARS = 4000
_CONT_WRAPUP_EMBED_CHARS = 6000

_CONT_STOP_HINTS = {
    "exhausted": (
        "Auto-continuation limit ({max}) reached; the task is not finished yet. "
        "Send /continue to keep going or /discard to drop it."
    ),
    "limit": (
        "The task is not finished yet. Send /continue to keep going or /discard to drop it."
    ),
    "loop": (
        "No progress is being made, so auto-continuation was stopped. "
        "Send /continue to try again or /discard to drop it."
    ),
    "no_progress": (
        "No progress is being made, so auto-continuation was stopped. "
        "Send /continue to try again or /discard to drop it."
    ),
    "error": (
        "The task stopped because of an error. Send /continue to retry or /discard to drop it."
    ),
}


def _build_continuation_prompt(record: Dict[str, Any], label: str) -> str:
    """Build a self-contained continuation prompt (works after idle reset,
    restart, or CONTEXT_TURNS=0 since it embeds the original request and the
    last wrap-up rather than relying on _history)."""
    prompt = record["prompt"][:_CONT_PROMPT_EMBED_CHARS]
    last_wrapup = record.get("last_wrapup", "")

    if last_wrapup:
        wrapup = last_wrapup[:_CONT_WRAPUP_EMBED_CHARS]
        return (
            "[system] Continuation ({label}) of an unfinished task. The previous turn stopped because the tool-call budget ran out.\n\n"
            "Original request:\n<<<\n{prompt}\n>>>\n\n"
            "Your last progress summary:\n<<<\n{wrapup}\n>>>\n\n"
            "Now EXECUTE the remaining unfinished work using your tools. Do not repeat steps that are already done. Do NOT reply with a plan, a summary, or instructions for the user — perform the remaining actions yourself with tool calls. Only after the remaining actions have actually been executed, give the final answer with concrete results (files created, URLs, ids)."
        ).format(label=label, prompt=prompt, wrapup=wrapup)

    return (
        "[system] Continuation ({label}) of a task that was interrupted before it finished (for example by a process restart). Some steps may already have run.\n\n"
        "Original request:\n<<<\n{prompt}\n>>>\n\n"
        "First check the current state (files, outputs) with your tools before acting, and do not repeat work that is already done. Then EXECUTE the remaining work — do NOT reply with a plan, a summary, or instructions for the user. Only after the remaining actions have actually been executed, give the final answer with concrete results (files created, URLs, ids)."
    ).format(label=label, prompt=prompt)


def _abandon_task(by: str) -> Optional[Dict[str, Any]]:
    """Discard the current task file (if any) and bump the epoch so any
    in-flight continuation chain notices and abandons itself."""
    global _task_epoch
    with _task_lock:
        record = tasks.load_task()
        _task_epoch += 1
        tasks.delete_task()
    if record is not None:
        _LOGGER.info("task_abandoned task_id=%s by=%s", record["task_id"], by)
    return record


def _run_task_chain(
    item: _QueueItem, record: Dict[str, Any], epoch: int, outcome: str, wrapup: str
) -> None:
    """Drive the auto-continuation loop for one task until it finishes,
    gets stopped, or is abandoned by a concurrent /reset, /kill or /discard.
    """
    global _active_continuation
    while True:
        with _task_lock:
            if _task_epoch != epoch:
                _LOGGER.info("task_abandoned task_id=%s", record["task_id"])
                return

        if outcome in ("done", "blocked"):
            with _task_lock:
                if _task_epoch == epoch:
                    tasks.delete_task(record["task_id"])
            _LOGGER.info(
                "task_done task_id=%s continuations=%d outcome=%s",
                record["task_id"], record["continuations"], outcome,
            )
            return

        max_cont = settings.get("FC_MAX_CONTINUATIONS")

        if outcome == "limit" and record["last_wrapup"] and wrapup.strip() == record["last_wrapup"].strip():
            outcome = "no_progress"

        if outcome == "limit" and wrapup and record["continuations"] < max_cont:
            record["continuations"] += 1
            record["last_wrapup"] = wrapup
            record["stop_reason"] = None
            with _task_lock:
                if _task_epoch != epoch:
                    return
                tasks.save_task(record)
            n = record["continuations"]
            _active_continuation = n
            _LOGGER.info(
                "task_continuation task_id=%s n=%d max=%d", record["task_id"], n, max_cont
            )
            send_message(
                item.chat_id,
                "Continuing the remaining work automatically ({}/{})...".format(n, max_cont),
            )
            outcome, wrapup = _handle_llm(
                item.chat_id, _build_continuation_prompt(record, "{}/{}".format(n, max_cont))
            )
            continue

        if outcome == "limit" and max_cont > 0 and record["continuations"] >= max_cont:
            stop_reason = "exhausted"
        else:
            stop_reason = outcome

        if wrapup:
            record["last_wrapup"] = wrapup
        record["stop_reason"] = stop_reason
        with _task_lock:
            if _task_epoch != epoch:
                return
            tasks.save_task(record)
        _LOGGER.info(
            "task_stopped task_id=%s reason=%s continuations=%d",
            record["task_id"], stop_reason, record["continuations"],
        )
        hint = _CONT_STOP_HINTS.get(stop_reason, _CONT_STOP_HINTS["limit"])
        if stop_reason == "exhausted":
            hint = hint.format(max=max_cont)
        send_message(item.chat_id, hint)
        return


def _process_continue_item(item: _QueueItem) -> None:
    with _task_lock:
        epoch = _task_epoch
        record = tasks.load_task()
        if record is None or record["task_id"] != item.task_id:
            record = None
        else:
            record["continuations"] = 0
            record["stop_reason"] = None
            tasks.save_task(record)

    if record is None:
        send_message(
            item.chat_id, "That task was already finished or discarded; nothing to continue."
        )
        return

    item.task_id = record["task_id"]
    outcome, wrapup = _handle_llm(item.chat_id, _build_continuation_prompt(record, "manual"))
    _run_task_chain(item, record, epoch, outcome, wrapup)


def _process_llm_item(item: _QueueItem) -> None:
    """Process one LLM queue item. call_gemini already handles all retries
    internally (RPM sleep + exponential backoff across the whole chain).
    If it still raises, propagate so the worker can notify the user.
    """
    global _active_continuation
    try:
        if item.kind == "continue":
            _process_continue_item(item)
            return

        with _task_lock:
            epoch = _task_epoch
            existing = tasks.load_task()
            if existing is not None:
                _LOGGER.info("task_replaced old_task_id=%s", existing["task_id"])
            record = tasks.new_task(item.chat_id, item.user_id, item.text)
            if not tasks.save_task(record):
                _LOGGER.warning("task_save_failed task_id=%s", record["task_id"])
            item.task_id = record["task_id"]

        _LOGGER.info("task_created task_id=%s chat_id=%s", record["task_id"], item.chat_id)

        outcome, wrapup = _handle_llm(item.chat_id, item.text)
        _run_task_chain(item, record, epoch, outcome, wrapup)
    finally:
        _active_continuation = 0


def _trimmed_history(context_turns: int) -> List[Dict[str, Any]]:
    if context_turns <= 0:
        return []
    limit = context_turns * 2
    return list(_history[-limit:]) if len(_history) > limit else list(_history)


def _trim_history_pairs(context_turns: int) -> None:
    global _history
    if context_turns <= 0:
        _history = []
        return
    limit = context_turns * 2
    if len(_history) > limit:
        _history = _history[-limit:]


def _make_cooldown_notifier(chat_id: int) -> Callable[[int, int, float], None]:
    """Best-effort Telegram notice sent while call_gemini() is cooling down
    between whole-chain retry passes. Never recorded into history/context.
    """

    def _notify(attempt: int, max_retries: int, delay: float) -> None:
        try:
            send_message(
                chat_id,
                "⏳ 모든 모델 응답 실패. {}초 후 재시도… ({}/{})".format(
                    int(delay), attempt, max_retries
                ),
            )
        except Exception:
            _LOGGER.debug("Failed to send cooldown notice", exc_info=True)

    return _notify


def _typing_keepalive(chat_id: int, stop: threading.Event) -> None:
    """Send sendChatAction every 4s until stop is set. Runs in a daemon thread."""
    while not stop.wait(4):
        try:
            send_chat_action(chat_id)
        except Exception:
            _LOGGER.debug("sendChatAction failed in keepalive", exc_info=True)


# ---------------------------------------------------------------------------
# Function-call loop limit handling (issue #9): a hard iteration cap and a
# same-call/same-result repeat detector both hand off to a tools-disabled
# wrap-up turn instead of silently dropping the conversation.
# ---------------------------------------------------------------------------

_FC_REPEAT_LIMIT = 3

_FC_WRAPUP_LIMIT_NOTE = (
    "[system] Tool-call budget exhausted ({} iterations). Tools are now disabled. "
    "Using only the tool results gathered so far, give the user your best final answer: "
    "what was done, what was found, and what remains unfinished."
)
_FC_WRAPUP_LOOP_NOTE = (
    "[system] The same tool calls returned the same results {} times in a row, so tool use "
    "has been stopped. Using the results gathered so far, give the user your best final "
    "answer and briefly explain what blocked progress."
)

_FC_LIMIT_NOTICE = "⚠️ 도구 호출 한도({}회)에 도달해 작업을 중단했습니다. 지금까지의 결과로 정리합니다."
_FC_LOOP_NOTICE = "⚠️ 동일한 도구 호출이 {}회 연속 반복되어 작업을 중단했습니다."
_FC_NO_TEXT_FALLBACK = "(요약 응답을 생성하지 못했습니다.)"


def _iteration_signature(calls_and_results: List[Tuple[str, Dict[str, Any], Any]]) -> Tuple[Any, ...]:
    """Order-independent signature of one iteration's (name, args, result)
    triples, so reordered parallel batches still match. Results are included
    on purpose so real polling (same command, changing output) is not
    flagged as a repeat.
    """
    entries = []
    for name, args, result in calls_and_results:
        args_json = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
        result_key = result if isinstance(result, str) else repr(result)
        entries.append((name, args_json, result_key))
    return tuple(sorted(entries))


def _fc_wrapup(
    chat_id: int,
    contents: List[Dict[str, Any]],
    user_turn: Dict[str, Any],
    context_turns: int,
    reason: str,
    n: int,
    last_text: str,
    on_cooldown: Callable[[int, int, float], None],
) -> str:
    """Send one tools-disabled final-answer request using the tool results
    gathered so far, then reply and (if any text is produced) save history.
    Never runs tools; never raises. Returns the final wrap-up text produced
    (may be "" if no text was produced).
    """
    if reason == "loop":
        note = _FC_WRAPUP_LOOP_NOTE.format(n)
        notice = _FC_LOOP_NOTICE.format(n)
    else:
        note = _FC_WRAPUP_LIMIT_NOTE.format(n)
        notice = _FC_LIMIT_NOTICE.format(n)

    # contents[-1] is the functionResponse user turn just appended by the
    # caller — a locally-built dict, safe to mutate. Never creates a second
    # consecutive user turn.
    contents[-1]["parts"].append({"text": note})

    final_text = last_text
    finish_reason: Optional[str] = None
    wrapup_model: Optional[str] = None
    try:
        response, wrapup_model = call_gemini(contents, on_cooldown=on_cooldown, allow_tools=False)
    except Exception:
        _LOGGER.warning("fc_wrapup_failed reason=%s", reason, exc_info=True)
    else:
        parsed = parse_response(response)
        finish_reason = parsed.finish_reason
        if not parsed.blocked and parsed.text:
            final_text = parsed.text
        _LOGGER.info("fc_wrapup reason=%s model=%s text_len=%d", reason, wrapup_model, len(final_text))

    reply = notice
    if final_text:
        reply += "\n\n" + _markdown_to_html(final_text)
    else:
        reply += "\n\n" + _FC_NO_TEXT_FALLBACK
    if finish_reason == "MAX_TOKENS":
        reply += "\n\n⚠️ Response was truncated at the maximum token limit (MAX_TOKENS)."
    send_message(chat_id, reply)

    if final_text:
        _history.append(user_turn)
        _history.append({"role": "model", "parts": [{"text": final_text}]})
        _trim_history_pairs(context_turns)

    return final_text


def _run_llm_turn(
    chat_id: int,
    contents: List[Dict[str, Any]],
    user_turn: Dict[str, Any],
    context_turns: int,
) -> Tuple[str, str]:
    fc_max_loops = settings.get("FC_MAX_LOOPS")
    loop_count = 0
    last_text = ""
    on_cooldown = _make_cooldown_notifier(chat_id)

    trace: List[str] = []
    prev_sig: Optional[Tuple[Any, ...]] = None
    repeat_count = 0

    while True:
        response, _model = call_gemini(contents, on_cooldown=on_cooldown)
        parsed = parse_response(response)

        if parsed.blocked:
            reason = parsed.block_reason or "unknown"
            send_message(chat_id, "⚠️ Response was blocked: {}".format(html.escape(str(reason))))
            return "blocked", ""

        if parsed.text:
            last_text = parsed.text

        if parsed.function_calls:
            if loop_count >= fc_max_loops:
                _LOGGER.warning(
                    "fc_limit_reached loops=%d max=%d pending=%s trace=%s",
                    loop_count, fc_max_loops, [name for name, _ in parsed.function_calls], trace,
                )
                wrapup_text = _fc_wrapup(
                    chat_id, contents, user_turn, context_turns,
                    reason="limit", n=fc_max_loops, last_text=last_text, on_cooldown=on_cooldown,
                )
                return "limit", wrapup_text

            contents.append(parsed.raw_content)
            response_parts = []
            triples: List[Tuple[str, Dict[str, Any], Any]] = []
            for name, args in parsed.function_calls:
                _LOGGER.info("tool_call name=%s args=%r", name, args)
                result = run_tool(name, args)
                _LOGGER.info("tool_result name=%s result=%r", name, result[:200] if isinstance(result, str) else result)
                response_parts.append(
                    {"functionResponse": {"name": name, "response": {"output": result}}}
                )
                triples.append((name, args, result))
            contents.append({"role": "user", "parts": response_parts})
            loop_count += 1

            trace.append(",".join(name for name, _, _ in triples))
            sig = _iteration_signature(triples)
            repeat_count = repeat_count + 1 if sig == prev_sig else 1
            prev_sig = sig

            if repeat_count >= _FC_REPEAT_LIMIT:
                calls_desc = [
                    "{}({})".format(name, json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)[:200])
                    for name, args, _ in triples
                ]
                _LOGGER.warning(
                    "fc_loop_detected loops=%d repeats=%d calls=%s trace=%s",
                    loop_count, repeat_count, calls_desc, trace,
                )
                wrapup_text = _fc_wrapup(
                    chat_id, contents, user_turn, context_turns,
                    reason="loop", n=repeat_count, last_text=last_text, on_cooldown=on_cooldown,
                )
                return "loop", wrapup_text
            continue

        if loop_count > 0:
            _LOGGER.info("fc_turn_done loops=%d trace=%s", loop_count, trace)

        reply = _markdown_to_html(parsed.text) if parsed.text else "No response."
        if parsed.finish_reason == "MAX_TOKENS":
            reply += "\n\n⚠️ Response was truncated at the maximum token limit (MAX_TOKENS)."
        send_message(chat_id, reply)

        _history.append(user_turn)
        _history.append(parsed.raw_content)
        _trim_history_pairs(context_turns)
        return "done", ""


def _handle_llm(chat_id: int, text: str) -> Tuple[str, str]:
    global _last_activity

    now = time.time()
    idle_minutes = settings.get("IDLE_RESET_MINUTES")
    if idle_minutes and idle_minutes > 0 and _last_activity and (now - _last_activity) > idle_minutes * 60:
        _reset_history()
    _last_activity = now

    context_turns = settings.get("CONTEXT_TURNS")
    user_turn = {"role": "user", "parts": [{"text": text}]}
    contents = _trimmed_history(context_turns) + [user_turn]

    send_chat_action(chat_id)

    stop_typing = threading.Event()
    typing_thread = threading.Thread(
        target=_typing_keepalive, args=(chat_id, stop_typing), daemon=True
    )
    typing_thread.start()
    try:
        return _run_llm_turn(chat_id, contents, user_turn, context_turns)
    except Exception as exc:
        _LOGGER.exception("Error in LLM track")
        send_message(chat_id, "⚠️ An error occurred: {}".format(html.escape(str(exc))))
        return "error", ""
    finally:
        stop_typing.set()


def notify_pending_task() -> None:
    """Called once at startup (from __main__) to notify the owner about an
    unfinished task left over from a previous run. Never raises, never
    auto-resumes anything."""
    with _task_lock:
        record = tasks.load_task()
    if record is None:
        return

    user_id = record.get("user_id")
    if user_id is not None and user_id != settings.get("ALLOWED_USER_ID"):
        with _task_lock:
            tasks.delete_task(record["task_id"])
        _LOGGER.warning("Task file owner mismatch; discarding task_id=%s", record["task_id"])
        return

    preview = html.escape(record["prompt"][:80])
    age = tasks.format_age(time.time() - record["created_at"])
    lines = [
        "An unfinished task exists (started {} ago):".format(age),
        "<code>{}</code>".format(preview),
    ]
    if record.get("stop_reason") is None:
        lines.append("It was interrupted before finishing.")
    lines.append("/continue to resume, /discard to drop it. Sending a new request will replace it.")
    message = "\n".join(lines)

    try:
        send_message(record["chat_id"], message)
    except Exception:
        _LOGGER.debug("Failed to send pending task notice", exc_info=True)
