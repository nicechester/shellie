from __future__ import annotations

import json
import logging
import os
import re
import stat
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from src.agent.config import SCHEDULES_FILE
from src.agent.core import tasks

_LOGGER = logging.getLogger("shellie.scheduler")

# Every load-modify-save cycle (main-thread /schedule commands, the worker
# thread's mark_finished/run_now, and the ticker thread's tick()) goes
# through this lock. _inflight is likewise only ever touched while holding it.
_lock = threading.Lock()
_inflight: Set[int] = set()

_TICK_SEC = 20
_MISSED_GRACE_SEC = 600
_MAX_SCHEDULES = 20
_MAX_PROMPT_CHARS = 4000
_MAX_NAME_CHARS = 60
_MIN_EVERY_MIN = 15
_MAX_EVERY_MIN = 1440
_MAX_EVERY_HOURS = 24
_DOW = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

_SCHEMA_VERSION = 1

_started = False
_stop_event = threading.Event()


# ---------------------------------------------------------------------------
# Grammar parsing. Case-insensitive; never logs the prompt (callers only ever
# hand this module a spec string, never the prompt).
# ---------------------------------------------------------------------------


def _parse_hhmm(token: str) -> Tuple[Optional[int], Optional[int], Optional[str]]:
    m = re.match(r"^(\d{1,2}):(\d{2})$", token)
    if not m:
        return None, None, "Invalid time '{}': expected HH:MM".format(token)
    hh = int(m.group(1))
    mm = int(m.group(2))
    if not (0 <= hh <= 23):
        return None, None, "Invalid hour in '{}': must be 0-23".format(token)
    if not (0 <= mm <= 59):
        return None, None, "Invalid minute in '{}': must be 0-59".format(token)
    return hh, mm, None


def _parse_times(token: str) -> Tuple[Optional[List[Tuple[int, int]]], Optional[str]]:
    times: List[Tuple[int, int]] = []
    for raw in token.split(","):
        raw = raw.strip()
        if not raw:
            return None, "Invalid time list '{}': empty entry".format(token)
        hh, mm, err = _parse_hhmm(raw)
        if err:
            return None, err
        times.append((hh, mm))  # type: ignore[arg-type]
    times = sorted(set(times))
    if not times:
        return None, "At least one time is required."
    return times, None


def _format_times(times: List[Tuple[int, int]]) -> str:
    return ",".join("{:02d}:{:02d}".format(h, m) for h, m in times)


def _parse_mm(token: str) -> Tuple[Optional[int], Optional[str]]:
    m = re.match(r"^:?(\d{1,2})$", token)
    if not m:
        return None, "Invalid minute '{}': expected :MM or MM".format(token)
    mm = int(m.group(1))
    if not (0 <= mm <= 59):
        return None, "Invalid minute '{}': must be 0-59".format(token)
    return mm, None


def _parse_interval(token: str) -> Tuple[Optional[int], Optional[str]]:
    m = re.match(r"^(\d+)([mMhH])$", token)
    if not m:
        return None, "Invalid interval '{}': expected N followed by m or h (e.g. 30m, 2h)".format(token)
    n = int(m.group(1))
    unit = m.group(2).lower()
    if unit == "m":
        if not (_MIN_EVERY_MIN <= n <= _MAX_EVERY_MIN):
            return None, "Interval out of range: every {}m must be {}-{} minutes".format(
                n, _MIN_EVERY_MIN, _MAX_EVERY_MIN
            )
        return n, None
    if not (1 <= n <= _MAX_EVERY_HOURS):
        return None, "Interval out of range: every {}h must be 1-{} hours".format(n, _MAX_EVERY_HOURS)
    return n * 60, None


def _parse_date(token: str) -> Tuple[Optional[int], Optional[int], Optional[int], Optional[str]]:
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", token)
    if not m:
        return None, None, None, "Invalid date '{}': expected YYYY-MM-DD".format(token)
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= mo <= 12) or not (1 <= d <= 31):
        return None, None, None, "Invalid date '{}': month/day out of range".format(token)
    return y, mo, d, None


def parse_spec(tokens: List[str]) -> Tuple[Optional[Dict[str, Any]], int, Optional[str]]:
    """Parse a schedule grammar from an already-split token list. Returns
    (parsed, tokens_consumed, error). Never raises."""
    if not tokens:
        return None, 0, "Empty schedule spec."
    kind_token = tokens[0].lower()

    if kind_token in ("daily", "weekdays"):
        if len(tokens) < 2:
            return None, 0, "Usage: {} HH:MM[,HH:MM...]".format(kind_token)
        times, err = _parse_times(tokens[1])
        if err:
            return None, 0, err
        dows = set(range(7)) if kind_token == "daily" else {0, 1, 2, 3, 4}
        text = "{} {}".format(kind_token, _format_times(times))  # type: ignore[arg-type]
        return {"kind": kind_token, "minutes": None, "times": times, "dows": dows, "ts": None, "text": text}, 2, None

    if kind_token == "weekly":
        if len(tokens) < 3:
            return None, 0, "Usage: weekly DOW[,DOW...] HH:MM[,HH:MM...]"
        dow_tokens = tokens[1].lower().split(",")
        dows = set()
        for dt in dow_tokens:
            dt = dt.strip()
            if dt not in _DOW:
                return None, 0, "Unknown weekday '{}': use mon,tue,wed,thu,fri,sat,sun".format(dt)
            dows.add(_DOW.index(dt))
        if not dows:
            return None, 0, "Usage: weekly DOW[,DOW...] HH:MM[,HH:MM...]"
        times, err = _parse_times(tokens[2])
        if err:
            return None, 0, err
        dow_text = ",".join(_DOW[i] for i in sorted(dows))
        text = "weekly {} {}".format(dow_text, _format_times(times))  # type: ignore[arg-type]
        return {"kind": "weekly", "minutes": None, "times": times, "dows": dows, "ts": None, "text": text}, 3, None

    if kind_token == "hourly":
        if len(tokens) < 2:
            return None, 0, "Usage: hourly :MM"
        mm, err = _parse_mm(tokens[1])
        if err:
            return None, 0, err
        text = "hourly :{:02d}".format(mm)  # type: ignore[str-format]
        return {"kind": "hourly", "minutes": mm, "times": None, "dows": None, "ts": None, "text": text}, 2, None

    if kind_token == "every":
        if len(tokens) < 2:
            return None, 0, "Usage: every N(m|h)"
        minutes, err = _parse_interval(tokens[1])
        if err:
            return None, 0, err
        text = "every {}".format(tokens[1].lower())
        return {"kind": "every", "minutes": minutes, "times": None, "dows": None, "ts": None, "text": text}, 2, None

    if kind_token == "at":
        if len(tokens) < 3:
            return None, 0, "Usage: at YYYY-MM-DD HH:MM"
        y, mo, d, err = _parse_date(tokens[1])
        if err:
            return None, 0, err
        hh, mm, err = _parse_hhmm(tokens[2])
        if err:
            return None, 0, err
        try:
            ts = time.mktime((y, mo, d, hh, mm, 0, 0, 0, -1))  # type: ignore[arg-type]
        except (OverflowError, ValueError):
            return None, 0, "Invalid date/time '{} {}': out of range".format(tokens[1], tokens[2])
        text = "at {}-{:02d}-{:02d} {:02d}:{:02d}".format(y, mo, d, hh, mm)
        return {"kind": "at", "minutes": None, "times": None, "dows": None, "ts": ts, "text": text}, 3, None

    return None, 0, "Unknown schedule kind '{}'. Use daily, weekdays, weekly, hourly, every, or at.".format(
        kind_token
    )


def split_spec_prompt(text: str) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]:
    """Split "<spec> <prompt>" into (parsed_spec, prompt, error)."""
    if not isinstance(text, str):
        return None, None, "Usage: <spec> <prompt>"
    stripped = text.strip()
    if not stripped:
        return None, None, "Usage: <spec> <prompt>"
    tokens = stripped.split()
    parsed, n, err = parse_spec(tokens)
    if err:
        return None, None, err
    parts = stripped.split(None, n)
    if len(parts) <= n or not parts[n].strip():
        return None, None, "Missing prompt after the schedule spec."
    prompt = parts[n]
    if len(prompt) > _MAX_PROMPT_CHARS:
        return None, None, "Prompt too long (max {} chars).".format(_MAX_PROMPT_CHARS)
    return parsed, prompt, None


# ---------------------------------------------------------------------------
# Next-run computation. Strictly after `after`; day-based scans +8 days,
# hourly scans +25 hours, every scans +2 extra days (midnight-aligned).
# "at" is a one-shot: returns its timestamp if still in the future, else
# None (spent — never an error).
# ---------------------------------------------------------------------------


def _next_run_every(parsed: Dict[str, Any], after: float) -> float:
    interval_sec = parsed["minutes"] * 60
    lt = time.localtime(after)
    for day_offset in range(0, 3):
        try:
            midnight = time.mktime(
                (lt.tm_year, lt.tm_mon, lt.tm_mday + day_offset, 0, 0, 0, 0, 0, -1)
            )
        except (OverflowError, ValueError):
            continue
        try:
            next_midnight = time.mktime(
                (lt.tm_year, lt.tm_mon, lt.tm_mday + day_offset + 1, 0, 0, 0, 0, 0, -1)
            )
        except (OverflowError, ValueError):
            next_midnight = midnight + 86400
        k = 0
        candidate = midnight
        while candidate < next_midnight:
            if candidate > after:
                return candidate
            k += 1
            candidate = midnight + k * interval_sec
    raise ValueError("next_run: no candidate found for 'every' spec")


def _next_run_hourly(parsed: Dict[str, Any], after: float) -> float:
    mm = parsed["minutes"]
    lt = time.localtime(after)
    for off in range(0, 26):
        try:
            candidate = time.mktime(
                (lt.tm_year, lt.tm_mon, lt.tm_mday, lt.tm_hour + off, mm, 0, 0, 0, -1)
            )
        except (OverflowError, ValueError):
            continue
        if candidate > after:
            return candidate
    raise ValueError("next_run: no candidate found for 'hourly' spec")


def _next_run_daily(parsed: Dict[str, Any], after: float) -> float:
    times = parsed["times"]
    dows = parsed["dows"]
    lt = time.localtime(after)
    for day_offset in range(0, 9):
        try:
            day_ts = time.mktime(
                (lt.tm_year, lt.tm_mon, lt.tm_mday + day_offset, 0, 0, 0, 0, 0, -1)
            )
        except (OverflowError, ValueError):
            continue
        day_lt = time.localtime(day_ts)
        if day_lt.tm_wday not in dows:
            continue
        for hh, mm in times:
            try:
                candidate = time.mktime(
                    (day_lt.tm_year, day_lt.tm_mon, day_lt.tm_mday, hh, mm, 0, 0, 0, -1)
                )
            except (OverflowError, ValueError):
                continue
            if candidate > after:
                return candidate
    raise ValueError("next_run: no candidate found for day-based spec")


def next_run(parsed: Dict[str, Any], after: float) -> Optional[float]:
    """First occurrence strictly after `after`, or None for a spent one-shot
    ("at" in the past — a legitimate result, not an error). May raise
    ValueError in the (extremely unlikely) case no candidate is found within
    the scan window for a recurring spec; callers must treat that as a soft
    failure (log + skip), never crash."""
    kind = parsed["kind"]
    if kind == "at":
        ts = parsed["ts"]
        return ts if ts > after else None
    if kind == "every":
        return _next_run_every(parsed, after)
    if kind == "hourly":
        return _next_run_hourly(parsed, after)
    return _next_run_daily(parsed, after)  # daily / weekdays / weekly


# ---------------------------------------------------------------------------
# Persistence: {"version": 1, "next_id": N, "schedules": [...]}. Mirrors
# core/tasks.py's atomic-write + quarantine pattern. Never logs prompts.
# ---------------------------------------------------------------------------


def _fresh_data() -> Dict[str, Any]:
    return {"version": _SCHEMA_VERSION, "next_id": 1, "schedules": []}


def _valid_entry(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    eid = entry.get("id")
    if isinstance(eid, bool) or not isinstance(eid, int) or eid < 1:
        return False
    spec = entry.get("spec")
    if not isinstance(spec, str) or not spec:
        return False
    spec_tokens = spec.split()
    parsed, n, err = parse_spec(spec_tokens)
    if err or parsed is None or n != len(spec_tokens):
        return False
    prompt = entry.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        return False
    name = entry.get("name")
    if not isinstance(name, str) or not name or len(name) > _MAX_NAME_CHARS:
        return False
    enabled = entry.get("enabled")
    if not isinstance(enabled, bool):
        return False
    created_at = entry.get("created_at")
    if isinstance(created_at, bool) or not isinstance(created_at, (int, float)):
        return False
    next_run_at = entry.get("next_run_at")
    if isinstance(next_run_at, bool) or not isinstance(next_run_at, (int, float)):
        return False
    last_run_at = entry.get("last_run_at")
    if last_run_at is not None and (
        isinstance(last_run_at, bool) or not isinstance(last_run_at, (int, float))
    ):
        return False
    return True


def _valid_file(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    if data.get("version") != _SCHEMA_VERSION:
        return False
    next_id = data.get("next_id")
    if isinstance(next_id, bool) or not isinstance(next_id, int) or next_id < 1:
        return False
    schedules = data.get("schedules")
    if not isinstance(schedules, list):
        return False
    seen_ids: Set[int] = set()
    for entry in schedules:
        if not _valid_entry(entry):
            return False
        eid = entry["id"]
        if eid in seen_ids or eid >= next_id:
            return False
        seen_ids.add(eid)
    return True


def load() -> Dict[str, Any]:
    """Load the whole schedules file. Never raises; missing/corrupt file
    quarantines and returns a fresh empty structure."""
    path = SCHEDULES_FILE
    if not os.path.exists(path):
        return _fresh_data()

    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & 0o077:
            try:
                os.chmod(path, 0o600)
                _LOGGER.warning("Fixed schedules file permissions to 0600")
            except OSError as exc:
                _LOGGER.warning("Failed to fix schedules file permissions: %s", exc)
    except OSError:
        pass

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = None

    if data is None or not _valid_file(data):
        corrupt_path = "{}.corrupt-{}".format(path, int(time.time()))
        try:
            os.replace(path, corrupt_path)
            _LOGGER.warning("Corrupt schedules file preserved as %s, ignoring", corrupt_path)
        except OSError as exc:
            _LOGGER.warning("Failed to handle corrupt schedules file: %s", exc)
        return _fresh_data()

    return data


def _save(data: Dict[str, Any]) -> bool:
    path = SCHEDULES_FILE
    tmp_path = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
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
        _LOGGER.warning("Failed to save schedules file: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Formatting helpers, shared by the /schedule list and add/enable/update
# replies (all mutation success messages start with "OK: ").
# ---------------------------------------------------------------------------


def _next_str(entry: Dict[str, Any], now: float) -> str:
    # Disabled/paused entries must never render a stale "next ... (due)"
    # time: display is derived strictly from enabled + next_run(now), never
    # from a leftover next_run_at that predates the pause.
    if not entry["enabled"]:
        return "paused"
    dt = time.strftime("%a %H:%M", time.localtime(entry["next_run_at"]))
    delta = entry["next_run_at"] - now
    if delta > 0:
        return "{} (in {})".format(dt, tasks.format_age(delta))
    return "{} (due)".format(dt)


def format_entry(entry: Dict[str, Any], now: Optional[float] = None) -> str:
    now = now if now is not None else time.time()
    # There is no "done" state: a spent one-shot (`at ...`) self-deletes on
    # fire/stale-skip (see _plan) instead of lingering as a disabled entry,
    # so every entry still in the file is either "on" or manually "paused".
    state = "on" if entry["enabled"] else "paused"
    last_run_at = entry.get("last_run_at")
    last_str = "{} ago".format(tasks.format_age(now - last_run_at)) if last_run_at else "never"
    prompt_preview = entry["prompt"][:80]
    name = entry.get("name", "")
    return "#{} [{}] {} ({}) — next {} — last {} — {}".format(
        entry["id"], state, name, entry["spec"], _next_str(entry, now), last_str, prompt_preview
    )


# ---------------------------------------------------------------------------
# Mutations. Every one of these is a full load-modify-save cycle under
# _lock; each returns (ok, message) except run_now/list_entries (get_schedule
# below is a read-only lookup but shares the same (ok, message) shape since
# it also formats a reply for the manage_schedule "get" action).
# Success messages always start with "OK: " (handlers key on that prefix to
# decide whether to surface a visibility notice for LLM-driven changes).
# ---------------------------------------------------------------------------


def _resolve_name(name: Optional[str], prompt: str) -> str:
    if isinstance(name, str) and name.strip():
        return name.strip()[:_MAX_NAME_CHARS]
    return prompt.strip()[:40]


def add(
    spec_text_or_parsed: Any, prompt: str, name: Optional[str] = None, now: Optional[float] = None
) -> Tuple[bool, str]:
    if not isinstance(prompt, str) or not prompt.strip():
        return False, "Prompt cannot be empty."
    if len(prompt) > _MAX_PROMPT_CHARS:
        return False, "Prompt too long (max {} chars).".format(_MAX_PROMPT_CHARS)

    if isinstance(spec_text_or_parsed, dict):
        parsed = spec_text_or_parsed
    else:
        tokens = str(spec_text_or_parsed).strip().split()
        parsed, n, err = parse_spec(tokens)
        if err:
            return False, err
        if n != len(tokens):
            return False, "Unexpected extra tokens in schedule spec: '{}'".format(
                " ".join(tokens[n:])
            )

    now = now if now is not None else time.time()
    try:
        next_run_at = next_run(parsed, now)
    except (OverflowError, ValueError):
        _LOGGER.error("schedule_next_run_failed spec=%s", parsed.get("text"))
        return False, "Could not compute the next run time for this schedule."
    if next_run_at is None:
        return False, "Schedule time must be in the future."

    resolved_name = _resolve_name(name, prompt)

    with _lock:
        data = load()
        if len(data["schedules"]) >= _MAX_SCHEDULES:
            return False, "Schedule limit reached (max {}).".format(_MAX_SCHEDULES)
        eid = data["next_id"]
        data["next_id"] = eid + 1
        entry = {
            "id": eid,
            "spec": parsed["text"],
            "prompt": prompt,
            "name": resolved_name,
            "enabled": True,
            "created_at": now,
            "last_run_at": None,
            "next_run_at": next_run_at,
        }
        data["schedules"].append(entry)
        if not _save(data):
            return False, "Failed to save schedule."

    return True, "OK: schedule #{} added — next run {}".format(eid, _next_str(entry, now))


def update_schedule(
    schedule_id: int,
    spec: Any = None,
    prompt: Optional[str] = None,
    name: Optional[str] = None,
    now: Optional[float] = None,
) -> Tuple[bool, str]:
    if spec is None and prompt is None and name is None:
        return False, "Nothing to update."
    if prompt is not None:
        if not isinstance(prompt, str) or not prompt.strip():
            return False, "Prompt cannot be empty."
        if len(prompt) > _MAX_PROMPT_CHARS:
            return False, "Prompt too long (max {} chars).".format(_MAX_PROMPT_CHARS)
    resolved_name = None
    if name is not None:
        resolved_name = name.strip()
        if not resolved_name:
            return False, "Name cannot be empty."
        resolved_name = resolved_name[:_MAX_NAME_CHARS]

    parsed = None
    if spec is not None:
        if isinstance(spec, dict):
            parsed = spec
        else:
            tokens = str(spec).strip().split()
            parsed, n, err = parse_spec(tokens)
            if err:
                return False, err
            if n != len(tokens):
                return False, "Unexpected extra tokens in schedule spec: '{}'".format(
                    " ".join(tokens[n:])
                )

    now = now if now is not None else time.time()

    with _lock:
        data = load()
        target = None
        for e in data["schedules"]:
            if e["id"] == schedule_id:
                target = e
                break
        if target is None:
            return False, "Unknown schedule id: {}".format(schedule_id)

        if parsed is not None:
            try:
                next_run_at = next_run(parsed, now)
            except (OverflowError, ValueError):
                return False, "Could not compute the next run time for this schedule."
            if next_run_at is None:
                return False, "Schedule time must be in the future."
            target["spec"] = parsed["text"]
            target["next_run_at"] = next_run_at

        if prompt is not None:
            target["prompt"] = prompt
        if resolved_name is not None:
            target["name"] = resolved_name

        if not _save(data):
            return False, "Failed to save schedule."
        entry_copy = dict(target)

    return True, "OK: schedule #{} updated — next run {}".format(
        schedule_id, _next_str(entry_copy, now)
    )


def remove(schedule_id: int) -> Tuple[bool, str]:
    with _lock:
        data = load()
        before = len(data["schedules"])
        data["schedules"] = [e for e in data["schedules"] if e["id"] != schedule_id]
        if len(data["schedules"]) == before:
            return False, "Unknown schedule id: {}".format(schedule_id)
        if not _save(data):
            return False, "Failed to save schedule."
        _inflight.discard(schedule_id)
    return True, "OK: schedule #{} removed.".format(schedule_id)


def set_enabled(schedule_id: int, enabled: bool) -> Tuple[bool, str]:
    now = time.time()
    with _lock:
        data = load()
        target = None
        for e in data["schedules"]:
            if e["id"] == schedule_id:
                target = e
                break
        if target is None:
            return False, "Unknown schedule id: {}".format(schedule_id)
        target["enabled"] = bool(enabled)
        if enabled:
            parsed, _n, err = parse_spec(target["spec"].split())
            if not err:
                try:
                    next_run_at = next_run(parsed, now)  # type: ignore[arg-type]
                    if next_run_at is not None:
                        target["next_run_at"] = next_run_at
                except (OverflowError, ValueError):
                    _LOGGER.error("schedule_next_run_failed id=%s", schedule_id)
        if not _save(data):
            return False, "Failed to save schedule."
        entry_copy = dict(target)

    if enabled:
        return True, "OK: schedule #{} enabled — next run {}".format(
            schedule_id, _next_str(entry_copy, now)
        )
    return True, "OK: schedule #{} disabled.".format(schedule_id)


def run_now(schedule_id: int) -> Optional[Dict[str, Any]]:
    """Mark a schedule in-flight for a manual run and return a copy of it.
    Returns None when the id is unknown or already running. Does not touch
    next_run_at (a manual run never perturbs the regular schedule)."""
    now = time.time()
    with _lock:
        if schedule_id in _inflight:
            return None
        data = load()
        target = None
        for e in data["schedules"]:
            if e["id"] == schedule_id:
                target = e
                break
        if target is None:
            return None
        target["last_run_at"] = now
        if not _save(data):
            return None
        _inflight.add(schedule_id)
        return dict(target)


def mark_finished(schedule_id: int) -> None:
    with _lock:
        _inflight.discard(schedule_id)


def get_schedule(schedule_id: int, now: Optional[float] = None) -> Tuple[bool, str]:
    """Look up one schedule and return a formatted (ok, message) tuple (the
    `manage_schedule` LLM tool's "get" action returns `message` verbatim)."""
    with _lock:
        data = load()
        target = None
        for e in data["schedules"]:
            if e["id"] == schedule_id:
                target = e
                break
    if target is None:
        return False, "Unknown schedule id: {}".format(schedule_id)
    return True, format_entry(target, now)


def list_entries() -> List[Dict[str, Any]]:
    with _lock:
        data = load()
        return [dict(e) for e in data["schedules"]]


def format_list(entries: List[Dict[str, Any]], now: Optional[float] = None) -> str:
    """Plain-text listing shared by the `/schedule list` bypass command and
    the `manage_schedule` LLM tool's "list" action."""
    now = now if now is not None else time.time()
    return "\n".join(format_entry(e, now) for e in entries)


# ---------------------------------------------------------------------------
# Thin naming-compatibility aliases: core/gemini.py's manage_schedule tool
# runner calls scheduler.get(...)/scheduler.update(...) (not get_schedule/
# update_schedule). Defined here — rather than changing gemini.py, which is
# owned by a parallel agent — so they resolve the current module-global
# get_schedule/update_schedule at call time (patchable in tests the same way).
# ---------------------------------------------------------------------------


def get(schedule_id: int) -> Tuple[bool, str]:
    return get_schedule(schedule_id)


def update(
    schedule_id: int,
    spec: Any = None,
    prompt: Optional[str] = None,
    name: Optional[str] = None,
) -> Tuple[bool, str]:
    return update_schedule(schedule_id, spec=spec, prompt=prompt, name=name)


# ---------------------------------------------------------------------------
# Ticker: tick() always saves before ever calling `enqueue`.
# ---------------------------------------------------------------------------


def _plan(
    entries: List[Dict[str, Any]], now: float
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[int], List[int]]:
    updated: List[Dict[str, Any]] = []
    to_fire: List[Dict[str, Any]] = []
    skipped_ids: List[int] = []
    overlapped_ids: List[int] = []

    for original in entries:
        entry = dict(original)
        eid = entry["id"]
        tokens = entry["spec"].split()
        parsed, _n, err = parse_spec(tokens)
        if err:
            _LOGGER.warning("schedule_spec_invalid id=%s", eid)
            updated.append(entry)
            continue

        try:
            recomputed: Optional[float] = next_run(parsed, now)
        except (OverflowError, ValueError):
            recomputed = None
            _LOGGER.error("schedule_next_run_failed id=%s", eid)

        # Clock-back reset takes precedence and applies even to disabled
        # entries: the stored next_run_at is implausibly far in the future
        # compared to what `now` would compute.
        if recomputed is not None and entry["next_run_at"] > recomputed + 60:
            entry["next_run_at"] = recomputed
            updated.append(entry)
            continue

        delete_entry = False

        if entry["enabled"] and now >= entry["next_run_at"]:
            overdue = now - entry["next_run_at"]
            is_one_shot = parsed["kind"] == "at"
            if overdue <= _MISSED_GRACE_SEC:
                if eid in _inflight:
                    overlapped_ids.append(eid)
                    if recomputed is not None:
                        entry["next_run_at"] = recomputed
                else:
                    entry["last_run_at"] = now
                    if is_one_shot:
                        # Spent one-shots self-delete instead of merely being
                        # disabled, so they never linger in /schedule as a
                        # stale "done" entry. Deleted in the same
                        # save-before-enqueue write that persists the fire,
                        # so at-most-once still holds even if the enqueue
                        # then fails/returns False (acceptable for one-shots:
                        # the fire itself is the record, not the schedule
                        # list entry).
                        delete_entry = True
                    elif recomputed is not None:
                        entry["next_run_at"] = recomputed
                    to_fire.append(dict(entry))
            else:
                skipped_ids.append(eid)
                if is_one_shot:
                    delete_entry = True
                elif recomputed is not None:
                    entry["next_run_at"] = recomputed

        if not delete_entry:
            updated.append(entry)

    return updated, to_fire, skipped_ids, overlapped_ids


def tick(now: float, enqueue: Callable[[Dict[str, Any]], bool]) -> None:
    with _lock:
        data = load()
        updated, to_fire, skipped_ids, overlapped_ids = _plan(data["schedules"], now)
        for entry in to_fire:
            _inflight.add(entry["id"])
        data["schedules"] = updated
        saved = _save(data)
        if not saved:
            for entry in to_fire:
                _inflight.discard(entry["id"])

    if not saved:
        _LOGGER.warning("schedule_save_failed; skipping enqueue this tick")
        return

    for sid in skipped_ids:
        _LOGGER.warning("schedule_missed id=%s", sid)
    for sid in overlapped_ids:
        _LOGGER.warning("schedule_overlap id=%s", sid)

    for entry in to_fire:
        try:
            ok = enqueue(entry)
        except Exception:
            _LOGGER.exception("schedule_enqueue_error id=%s", entry["id"])
            mark_finished(entry["id"])
            continue
        if ok:
            _LOGGER.info("schedule_fired id=%s spec=%s", entry["id"], entry["spec"])
        else:
            _LOGGER.warning("schedule_skipped_busy id=%s spec=%s", entry["id"], entry["spec"])
            mark_finished(entry["id"])


def start_ticker(enqueue: Callable[[Dict[str, Any]], bool]) -> None:
    """Start the daemon ticker thread. Idempotent (no-op after the first
    call unless a test resets the module-level `_started` flag)."""
    global _started
    if _started:
        return
    _started = True

    def _loop() -> None:
        while True:
            try:
                tick(time.time(), enqueue)
            except Exception:
                _LOGGER.exception("scheduler_tick_error")
            _stop_event.wait(_TICK_SEC)

    t = threading.Thread(target=_loop, daemon=True, name="shellie-scheduler")
    t.start()
