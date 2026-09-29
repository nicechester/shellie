from __future__ import annotations

import json
import logging
import os
import stat
import time
import uuid
from typing import Any, Dict, Optional

from src.agent.config import TASKS_DIR

_LOGGER = logging.getLogger("shellie.core.tasks")

# Single slot: one worker thread, at most one in-flight task at a time.
_TASK_FILENAME = "current.json"

TASK_MAX_AGE_SEC = 3 * 86400
_MAX_PROMPT_CHARS = 20000
_MAX_WRAPUP_CHARS = 20000
_SCHEMA_VERSION = 1

_STOP_REASONS = ("limit", "exhausted", "loop", "no_progress", "error")


def _task_path() -> str:
    """Resolve the task file path at call time (TASKS_DIR is a module global
    so tests can patch it via mock.patch.object)."""
    return os.path.join(TASKS_DIR, _TASK_FILENAME)


def new_task(
    chat_id: int, user_id: Optional[int], prompt: str, now: Optional[float] = None
) -> Dict[str, Any]:
    """Pure builder for a new task record. Does not touch the filesystem."""
    ts = now if now is not None else time.time()
    return {
        "version": _SCHEMA_VERSION,
        "task_id": uuid.uuid4().hex,
        "chat_id": chat_id,
        "user_id": user_id,
        "prompt": prompt,
        "stop_reason": None,
        "continuations": 0,
        "last_wrapup": "",
        "created_at": ts,
        "updated_at": ts,
    }


def save_task(record: Dict[str, Any]) -> bool:
    """Atomically persist a task record. Never raises; returns False and
    logs on failure. Never logs the prompt or wrap-up content."""
    record["updated_at"] = time.time()
    prompt = record.get("prompt", "")
    if isinstance(prompt, str) and len(prompt) > _MAX_PROMPT_CHARS:
        record["prompt"] = prompt[:_MAX_PROMPT_CHARS]
    last_wrapup = record.get("last_wrapup", "")
    if isinstance(last_wrapup, str) and len(last_wrapup) > _MAX_WRAPUP_CHARS:
        record["last_wrapup"] = last_wrapup[:_MAX_WRAPUP_CHARS]

    path = _task_path()
    tmp_path = path + ".tmp"
    try:
        os.makedirs(TASKS_DIR, mode=0o700, exist_ok=True)
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        return True
    except OSError as exc:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        _LOGGER.warning("Failed to save task file: %s", exc)
        return False


def _valid(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    if data.get("version") != _SCHEMA_VERSION:
        return False
    task_id = data.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        return False
    chat_id = data.get("chat_id")
    if isinstance(chat_id, bool) or not isinstance(chat_id, int):
        return False
    user_id = data.get("user_id")
    if user_id is not None and (isinstance(user_id, bool) or not isinstance(user_id, int)):
        return False
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        return False
    continuations = data.get("continuations")
    if isinstance(continuations, bool) or not isinstance(continuations, int) or continuations < 0:
        return False
    last_wrapup = data.get("last_wrapup")
    if not isinstance(last_wrapup, str):
        return False
    stop_reason = data.get("stop_reason")
    if stop_reason is not None and stop_reason not in _STOP_REASONS:
        return False
    for key in ("created_at", "updated_at"):
        ts = data.get(key)
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            return False
    return True


def load_task(now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Load the current task record, if any. Handles permission repair,
    corrupt-file quarantine, and stale-task expiry. Never raises."""
    path = _task_path()
    if not os.path.exists(path):
        return None

    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & 0o077:
            try:
                os.chmod(path, 0o600)
                _LOGGER.warning("Fixed task file permissions to 0600")
            except OSError as exc:
                _LOGGER.warning("Failed to fix task file permissions: %s", exc)
    except OSError:
        pass

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = None

    if data is None or not _valid(data):
        corrupt_path = "{}.corrupt-{}".format(path, int(time.time()))
        try:
            os.replace(path, corrupt_path)
            _LOGGER.warning("Corrupt task file preserved as %s, ignoring", corrupt_path)
        except OSError as exc:
            _LOGGER.warning("Failed to handle corrupt task file: %s", exc)
        return None

    ts_now = now if now is not None else time.time()
    age = max(0.0, ts_now - data["updated_at"])
    if age > TASK_MAX_AGE_SEC:
        try:
            os.remove(path)
        except OSError as exc:
            _LOGGER.warning("Failed to discard stale task file: %s", exc)
        _LOGGER.info(
            "Discarded stale task file task_id=%s age_sec=%d", data["task_id"], int(age)
        )
        return None

    return data


def delete_task(task_id: Optional[str] = None) -> bool:
    """Delete the current task file. If task_id is given, only delete when
    the file's task_id matches (protects a newer task from a stale deleter);
    an unreadable file only counts as a match when task_id is None."""
    path = _task_path()
    if not os.path.exists(path):
        return False

    if task_id is not None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict) or data.get("task_id") != task_id:
            return False

    try:
        os.remove(path)
        return True
    except OSError as exc:
        _LOGGER.warning("Failed to delete task file: %s", exc)
        return False


def format_age(seconds: float) -> str:
    """Format a duration in seconds as a compact English age string, e.g.
    '45s', '12m', '3h 5m', '2d 4h'. Negative durations clamp to 0."""
    total = int(max(0.0, seconds))
    if total < 60:
        return "{}s".format(total)
    minutes, _ = divmod(total, 60)
    if minutes < 60:
        return "{}m".format(minutes)
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        if minutes:
            return "{}h {}m".format(hours, minutes)
        return "{}h".format(hours)
    days, hours = divmod(hours, 24)
    if hours:
        return "{}d {}h".format(days, hours)
    return "{}d".format(days)
