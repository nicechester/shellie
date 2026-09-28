from __future__ import annotations

import difflib
import html
import logging
import os
import queue
import re
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from src.agent.config import settings
from src.agent.core.gemini import call_gemini, parse_response, run_tool
from src.agent.core.memory import read_memory
from src.agent.core.shell import execute_shell
from src.agent.telegram.client import delete_message, send_chat_action, send_message

_LOGGER = logging.getLogger("shellie.telegram.handlers")

# Gemini contents history (user/model turns only). Settings and bypass
# commands are never added here (P6). Worker thread only after queue introduced.
_history: List[Dict[str, Any]] = []
_last_activity: float = 0.0

_SETTINGS_TOKENS = ("/settings", "/get", "/set", "/unset")
_BYPASS_TOKENS = ("/mem", "/restart", "/reset", "/sh", "/help", "/queue")

_HELP_TEXT = (
    "<b>Bypass commands</b> (no LLM)\n"
    "<code>!&lt;cmd&gt;</code> / <code>/sh &lt;cmd&gt;</code> — run shell command\n"
    "<code>/mem</code> — show long-term memory\n"
    "<code>/reset</code> — clear conversation history\n"
    "<code>/restart</code> — restart the process\n"
    "<code>/queue</code> — show task queue status\n"
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
    _task_queue.put(_QueueItem(chat_id, text))


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


def _queue_status_text() -> str:
    depth = _task_queue.qsize()
    active = _queue_active_item
    cooldown_until = _queue_cooldown_until
    lines = ["<b>Queue status</b>"]
    if active is not None:
        preview = html.escape(active.text[:80])
        lines.append("🔄 처리 중: <code>{}</code>".format(preview))
    else:
        lines.append("💤 대기 중 (idle)")
    lines.append("대기 항목: {}".format(depth))
    remaining = cooldown_until - time.time()
    if remaining > 0:
        lines.append("⏳ 429 쿨다운: {}초 남음".format(int(remaining)))
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
        send_message(chat_id, "🔄 Conversation context cleared.")
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

_QUEUE_RPM_COOLDOWN_SEC = 60


class _QueueItem:
    __slots__ = ("chat_id", "text")

    def __init__(self, chat_id: int, text: str) -> None:
        self.chat_id = chat_id
        self.text = text


_task_queue: queue.Queue = queue.Queue()  # unbounded
_queue_active_item: Optional[_QueueItem] = None  # worker thread only (read by /queue on main)
_queue_cooldown_until: float = 0.0  # worker thread only (read by /queue on main)


def start_queue_worker() -> None:
    t = threading.Thread(target=_queue_worker, daemon=True, name="shellie-queue-worker")
    t.start()


def _queue_worker() -> None:
    global _queue_active_item, _queue_cooldown_until
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


def _process_llm_item(item: _QueueItem) -> None:
    """Process one LLM queue item, retrying on RPM 429 with cooldown.
    Raises only on non-retryable failures (RPD exhaustion / non-429 errors).
    """
    global _queue_cooldown_until
    while True:
        try:
            _handle_llm(item.chat_id, item.text)
            return
        except Exception as exc:
            msg = str(exc)
            # call_gemini raises "All Gemini models failed" when the whole
            # chain is exhausted. Treat as RPM cooldown and retry.
            # Any other exception (RPD exhaustion propagated, non-429) is re-raised.
            if "All Gemini models failed" in msg:
                _queue_cooldown_until = time.time() + _QUEUE_RPM_COOLDOWN_SEC
                _LOGGER.warning(
                    "Queue: all models failed (RPM), cooling down %ds before retry",
                    _QUEUE_RPM_COOLDOWN_SEC,
                )
                try:
                    send_message(
                        item.chat_id,
                        "⏳ 모든 모델이 한도에 도달했습니다. {}초 후 재시도합니다…".format(_QUEUE_RPM_COOLDOWN_SEC),
                    )
                except Exception:
                    _LOGGER.debug("Failed to send RPM cooldown notice", exc_info=True)
                time.sleep(_QUEUE_RPM_COOLDOWN_SEC)
                _queue_cooldown_until = 0.0
                continue
            raise


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
                "⏳ Rate limited. Retrying in {}s… ({}/{})".format(
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


def _run_llm_turn(
    chat_id: int,
    contents: List[Dict[str, Any]],
    user_turn: Dict[str, Any],
    context_turns: int,
) -> None:
    fc_max_loops = settings.get("FC_MAX_LOOPS")
    loop_count = 0
    last_text = ""
    on_cooldown = _make_cooldown_notifier(chat_id)

    while True:
        response, _model = call_gemini(contents, on_cooldown=on_cooldown)
        parsed = parse_response(response)

        if parsed.blocked:
            reason = parsed.block_reason or "unknown"
            send_message(chat_id, "⚠️ Response was blocked: {}".format(html.escape(str(reason))))
            return

        if parsed.text:
            last_text = parsed.text

        if parsed.function_calls:
            if loop_count >= fc_max_loops:
                message = "⚠️ Function call limit ({} iterations) reached.".format(fc_max_loops)
                if last_text:
                    message += "\n" + _markdown_to_html(last_text)
                send_message(chat_id, message)
                return

            contents.append(parsed.raw_content)
            response_parts = []
            for name, args in parsed.function_calls:
                _LOGGER.info("tool_call name=%s args=%r", name, args)
                result = run_tool(name, args)
                _LOGGER.info("tool_result name=%s result=%r", name, result[:200] if isinstance(result, str) else result)
                response_parts.append(
                    {"functionResponse": {"name": name, "response": {"output": result}}}
                )
            contents.append({"role": "user", "parts": response_parts})
            loop_count += 1
            continue

        reply = _markdown_to_html(parsed.text) if parsed.text else "No response."
        if parsed.finish_reason == "MAX_TOKENS":
            reply += "\n\n⚠️ Response was truncated at the maximum token limit (MAX_TOKENS)."
        send_message(chat_id, reply)

        _history.append(user_turn)
        _history.append(parsed.raw_content)
        _trim_history_pairs(context_turns)
        return


def _handle_llm(chat_id: int, text: str) -> None:
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
        _run_llm_turn(chat_id, contents, user_turn, context_turns)
    except Exception as exc:
        _LOGGER.exception("Error in LLM track")
        send_message(chat_id, "⚠️ An error occurred: {}".format(html.escape(str(exc))))
    finally:
        stop_typing.set()
