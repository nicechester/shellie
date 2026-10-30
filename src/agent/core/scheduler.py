from __future__ import annotations

import json
import logging
import os
import re
import stat
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from src.agent.config import SCHEDULES_FILE, SCHEDULE_STATE_FILE
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
_LEGACY_KINDS = ("daily", "weekdays", "weekly", "hourly", "every", "at")

# Definitions file (schedules.json) is version 2; version 1 kept run state
# inline and is migrated on load. The running-state file is versioned apart.
_SCHEMA_VERSION = 2
_LEGACY_SCHEMA_VERSION = 1
_STATE_VERSION = 1
_JSON_INDENT = 2

# Cron day scan: the longest gap between two matching days is 8 years
# (Feb 29 across a skipped century leap year), so scan at least that far.
_CRON_SCAN_DAYS = 366 * 8

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


# Cron fields: (label, min, max, name table). Cron weekday 0 = Sunday, and 7
# is accepted as an alias for Sunday. Names are matched case-insensitively.
_CRON_MONTH_NAMES = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
                     "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_CRON_DOW_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
_CRON_FIELD_SPECS = (
    ("minute", 0, 59, None),
    ("hour", 0, 23, None),
    ("day-of-month", 1, 31, None),
    ("month", 1, 12, _CRON_MONTH_NAMES),
    ("day-of-week", 0, 7, _CRON_DOW_NAMES),
)
# First token of a cron expression starts with a digit or '*' (or contains
# one of the cron operators); legacy keywords never match this.
_CRON_HINT = re.compile(r"^[\d*]|[*/,]")


def _cron_value(
    label: str, token: str, lo: int, hi: int, names: Optional[Dict[str, int]]
) -> Tuple[Optional[int], Optional[str]]:
    lowered = token.lower()
    if names is not None and lowered in names:
        value = names[lowered]
    elif re.match(r"^\d+$", token):
        value = int(token)
    else:
        return None, "Invalid {} value '{}'".format(label, token)
    if not (lo <= value <= hi):
        return None, "{} value '{}' out of range {}-{}".format(label, token, lo, hi)
    return value, None


def _parse_cron_field(
    label: str, token: str, lo: int, hi: int, names: Optional[Dict[str, int]]
) -> Tuple[Optional[Set[int]], Optional[str]]:
    """One cron field: '*', 'N', 'A-B', '*/S', 'A-B/S', 'N/S' (= N-max/S) and
    comma lists of those."""
    values: Set[int] = set()
    for part in token.split(","):
        if not part:
            return None, "Empty entry in {} field '{}'".format(label, token)
        base, slash, step_text = part.partition("/")
        step = 1
        if slash:
            if not re.match(r"^\d+$", step_text) or int(step_text) < 1:
                return None, "Invalid step '{}' in {} field '{}'".format(step_text, label, token)
            step = int(step_text)
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a, _dash, b = base.partition("-")
            start, err = _cron_value(label, a, lo, hi, names)
            if err:
                return None, err
            end, err = _cron_value(label, b, lo, hi, names)
            if err:
                return None, err
            if start > end:
                return None, "Descending range '{}' in {} field".format(base, label)
        else:
            start, err = _cron_value(label, base, lo, hi, names)
            if err:
                return None, err
            end = hi if slash else start
        values.update(range(start, end + 1, step))
    return values, None


def _parse_cron(text: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Parse a 5-field cron expression (minute hour day-of-month month
    day-of-week). Returns (parsed, error). Never raises.

    Day matching follows Vixie cron: if day-of-month or day-of-week is `*`
    (or starts with `*`), both must match; if both are restricted, a day
    matches when either does."""
    fields = text.split()
    if len(fields) != 5:
        return None, "Cron needs exactly 5 fields: minute hour day-of-month month day-of-week"
    sets: List[Set[int]] = []
    for field, (label, lo, hi, names) in zip(fields, _CRON_FIELD_SPECS):
        values, err = _parse_cron_field(label, field, lo, hi, names)
        if err:
            return None, err
        sets.append(values)  # type: ignore[arg-type]
    minutes, hours, doms, months, dow_values = sets
    dows = {0 if d == 7 else d for d in dow_values}

    # Minimum interval: fire times within a day (wrapping at midnight) must
    # be at least _MIN_EVERY_MIN apart. Same-day pairs and midnight wrap are
    # both covered; a restricted day list can only widen the real gaps.
    day_times = sorted(h * 60 + m for h in hours for m in minutes)
    gaps = [b - a for a, b in zip(day_times, day_times[1:])]
    gaps.append(day_times[0] + 1440 - day_times[-1])
    if min(gaps) < _MIN_EVERY_MIN:
        return None, "Interval too short: cron fires {} minutes apart; minimum is {} minutes".format(
            min(gaps), _MIN_EVERY_MIN
        )

    parsed = {
        "kind": "cron",
        "minutes": minutes,
        "hours": hours,
        "doms": doms,
        "months": months,
        "dows": dows,
        "dom_star": fields[2].startswith("*"),
        "dow_star": fields[4].startswith("*"),
        "times": None,
        "ts": None,
        "text": " ".join(fields).lower(),
    }
    return parsed, None


def parse_spec(tokens: List[str]) -> Tuple[Optional[Dict[str, Any]], int, Optional[str]]:
    """Parse a schedule grammar from an already-split token list. Returns
    (parsed, tokens_consumed, error). Never raises."""
    if not tokens:
        return None, 0, "Empty schedule spec."
    kind_token = tokens[0].lower()

    if kind_token not in _LEGACY_KINDS and _CRON_HINT.search(tokens[0]):
        if len(tokens) < 5:
            return None, 0, "Cron needs 5 fields: minute hour day-of-month month day-of-week"
        parsed, err = _parse_cron(" ".join(tokens[:5]))
        if err:
            return None, 0, err
        return parsed, 5, None

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

    return None, 0, (
        "Unknown schedule kind '{}'. Use a 5-field cron expression, daily, weekdays, weekly, "
        "hourly, every, or at.".format(kind_token)
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


def _to_cron_text(parsed: Dict[str, Any]) -> Optional[str]:
    """Cron text for a legacy spec when an exactly equivalent cron expression
    exists, else None (the legacy text is stored as-is). The stored text is
    the only thing that changes; next runs are identical (see tests)."""
    kind = parsed["kind"]
    if kind in ("daily", "weekdays", "weekly"):
        times = parsed["times"]
        if len({m for _h, m in times}) != 1:
            return None
        dows = parsed["dows"]
        if len(dows) == 7:
            dow_field = "*"
        else:
            # Python tm_wday 0 = Monday; cron 0 = Sunday.
            dow_field = ",".join(str(v) for v in sorted((d + 1) % 7 for d in dows))
        hours_field = ",".join(str(h) for h, _m in times)
        return "{} {} * * {}".format(times[0][1], hours_field, dow_field)
    if kind == "hourly":
        return "{} * * * *".format(parsed["minutes"])
    if kind == "every":
        n = parsed["minutes"]
        if n < 60 and 60 % n == 0:
            return "*/{} * * * *".format(n)
        if n % 60 == 0 and 24 % (n // 60) == 0:
            return "0 */{} * * *".format(n // 60)
    return None


def _stored_text(parsed: Dict[str, Any]) -> str:
    return _to_cron_text(parsed) or parsed["text"]


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


def _next_run_cron(parsed: Dict[str, Any], after: float) -> float:
    minutes = sorted(parsed["minutes"])
    hours = sorted(parsed["hours"])
    lt = time.localtime(after)
    for day_offset in range(0, _CRON_SCAN_DAYS):
        try:
            # Noon avoids DST-gap midnights; only the calendar date is used.
            noon = time.mktime(
                (lt.tm_year, lt.tm_mon, lt.tm_mday + day_offset, 12, 0, 0, 0, 0, -1)
            )
        except (OverflowError, ValueError):
            continue
        day_lt = time.localtime(noon)
        if day_lt.tm_mon not in parsed["months"]:
            continue
        cron_dow = (day_lt.tm_wday + 1) % 7
        dom_ok = day_lt.tm_mday in parsed["doms"]
        dow_ok = cron_dow in parsed["dows"]
        if parsed["dom_star"] or parsed["dow_star"]:
            day_ok = dom_ok and dow_ok
        else:
            day_ok = dom_ok or dow_ok
        if not day_ok:
            continue
        for hh in hours:
            for mm in minutes:
                try:
                    candidate = time.mktime(
                        (day_lt.tm_year, day_lt.tm_mon, day_lt.tm_mday, hh, mm, 0, 0, 0, -1)
                    )
                except (OverflowError, ValueError):
                    continue
                if candidate > after:
                    return candidate
    raise ValueError("next_run: no candidate found for cron spec")


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
    if kind == "cron":
        return _next_run_cron(parsed, after)
    return _next_run_daily(parsed, after)  # daily / weekdays / weekly


# ---------------------------------------------------------------------------
# Plain-English rendering (pure). describe_spec never raises: anything it
# cannot describe is shown as the raw spec text.
# ---------------------------------------------------------------------------

_DOW_LABELS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")  # cron values 0-6
_MAX_DESCRIBED_TIMES = 6


def _plural(n: int, unit: str) -> str:
    return "{} {}".format(n, unit if n == 1 else unit + "s")


def _describe_hm_list(times: List[Tuple[int, int]]) -> Optional[str]:
    if len(times) > _MAX_DESCRIBED_TIMES:
        return None
    return ", ".join("{:02d}:{:02d}".format(h, m) for h, m in times)


def _describe_dows(dows: Set[int]) -> str:
    if dows == {1, 2, 3, 4, 5}:
        return "weekdays"
    if dows == {0, 6}:
        return "weekends"
    return ", ".join(_DOW_LABELS[d] for d in sorted(dows, key=lambda d: (d - 1) % 7))


def _describe_legacy(parsed: Dict[str, Any]) -> Optional[str]:
    kind = parsed["kind"]
    if kind in ("daily", "weekdays", "weekly"):
        times = _describe_hm_list(parsed["times"])
        if times is None:
            return None
        if kind == "daily":
            return "daily at {}".format(times)
        if kind == "weekdays":
            return "weekdays at {}".format(times)
        days = ", ".join(_DOW[i].capitalize() for i in sorted(parsed["dows"]))
        return "{} at {}".format(days, times)
    if kind == "hourly":
        return "hourly at :{:02d}".format(parsed["minutes"])
    if kind == "every":
        n = parsed["minutes"]
        if n >= 60 and n % 60 == 0:
            return "every {}".format(_plural(n // 60, "hour"))
        return "every {}".format(_plural(n, "minute"))
    if kind == "at":
        _at, day, clock = parsed["text"].split()
        return "once on {} at {}".format(day, clock)
    return None


def _describe_cron(parsed: Dict[str, Any]) -> Optional[str]:
    if len(parsed["months"]) != 12:
        return None
    doms = parsed["doms"]
    dows = parsed["dows"]
    dom_all = len(doms) == 31
    dow_all = len(dows) == 7
    if not dom_all and not dow_all:
        return None  # day-of-month and day-of-week both set: raw (OR rule)
    minutes = sorted(parsed["minutes"])
    hours = sorted(parsed["hours"])
    every_hour = len(hours) == 24

    if every_hour:
        if len(minutes) == 1:  # M * * * *
            if dom_all and dow_all:
                return "hourly at :{:02d}".format(minutes[0])
            return None
        if not (dom_all and dow_all) or len(minutes) < 2:
            return None
        step = minutes[1] - minutes[0]  # */N * * * *
        if minutes[0] == 0 and minutes == list(range(0, 60, step)):
            return "every {}".format(_plural(step, "minute"))
        return None

    if len(minutes) != 1:
        return None
    minute = minutes[0]
    if dom_all and dow_all and minute == 0 and len(hours) > 1:
        step = hours[1] - hours[0]  # 0 */N * * *
        if hours == list(range(0, 24, step)):
            return "every {}".format(_plural(step, "hour"))

    times = _describe_hm_list([(h, minute) for h in hours])
    if times is None:
        return None
    if not dom_all:
        if len(doms) != 1:
            return None
        return "monthly on day {} at {}".format(min(doms), times)
    if dow_all:
        return "daily at {}".format(times)
    return "{} at {}".format(_describe_dows(dows), times)


def describe_spec(spec_text: str) -> str:
    """Plain-English description of a stored spec (legacy or cron). Pure;
    never raises; falls back to the raw spec text."""
    try:
        tokens = str(spec_text).split()
        parsed, n, err = parse_spec(tokens)
        if err or parsed is None or n != len(tokens):
            return spec_text
        if parsed["kind"] == "cron":
            text = _describe_cron(parsed)
        else:
            text = _describe_legacy(parsed)
    except Exception:
        return spec_text
    return text if text else spec_text


# ---------------------------------------------------------------------------
# Persistence. Two files, both atomic 0600 writes with corrupt-file
# quarantine (mirrors core/tasks.py):
#   schedules.json       version 2 — user-owned definitions only
#   schedule_state.json  version 1 — last_run_at / next_run_at keyed by id
# Never logs prompts.
# ---------------------------------------------------------------------------


def _fresh_defs() -> Dict[str, Any]:
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
    return True


def _valid_entry_v1(entry: Any) -> bool:
    """Version 1 entries carried their run state inline."""
    if not _valid_entry(entry):
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


def _valid_file(data: Any, version: int, entry_check: Callable[[Any], bool]) -> bool:
    if not isinstance(data, dict):
        return False
    if data.get("version") != version:
        return False
    next_id = data.get("next_id")
    if isinstance(next_id, bool) or not isinstance(next_id, int) or next_id < 1:
        return False
    schedules = data.get("schedules")
    if not isinstance(schedules, list):
        return False
    seen_ids: Set[int] = set()
    for entry in schedules:
        if not entry_check(entry):
            return False
        eid = entry["id"]
        if eid in seen_ids or eid >= next_id:
            return False
        seen_ids.add(eid)
    return True


def _valid_state(data: Any) -> bool:
    if not isinstance(data, dict) or data.get("version") != _STATE_VERSION:
        return False
    states = data.get("state")
    if not isinstance(states, dict):
        return False
    for st in states.values():
        if not isinstance(st, dict):
            return False
        for key in ("last_run_at", "next_run_at"):
            value = st.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                return False
    return True


def _fix_file_mode(path: str) -> None:
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & 0o077:
            try:
                os.chmod(path, 0o600)
                _LOGGER.warning("Fixed %s permissions to 0600", os.path.basename(path))
            except OSError as exc:
                _LOGGER.warning("Failed to fix %s permissions: %s", os.path.basename(path), exc)
    except OSError:
        pass


def _quarantine(path: str) -> None:
    corrupt_path = "{}.corrupt-{}".format(path, int(time.time()))
    try:
        os.replace(path, corrupt_path)
        _LOGGER.warning("Corrupt %s preserved as %s, ignoring", os.path.basename(path), corrupt_path)
    except OSError as exc:
        _LOGGER.warning("Failed to handle corrupt %s: %s", os.path.basename(path), exc)


def _read_json_quarantine(path: str, validator: Callable[[Any], bool]) -> Optional[Any]:
    """Read and validate a JSON file. Returns the data, or None when the file
    is missing or corrupt (a corrupt file is moved aside, never deleted)."""
    if not os.path.exists(path):
        return None
    _fix_file_mode(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = None
    if data is not None and validator(data):
        return data
    _quarantine(path)
    return None


def _atomic_write(path: str, obj: Any, indent: Optional[int]) -> bool:
    tmp_path = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=indent)
            f.write("\n")
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
        _LOGGER.warning("Failed to save %s: %s", os.path.basename(path), exc)
        return False


def _definition_of(entry: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": entry["id"],
        "spec": entry["spec"],
        "prompt": entry["prompt"],
        "name": entry["name"],
        "enabled": entry["enabled"],
        "created_at": entry["created_at"],
    }


def _state_from_entries(entries: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {
        str(e["id"]): {"last_run_at": e.get("last_run_at"), "next_run_at": e.get("next_run_at")}
        for e in entries
    }


def _state_doc(states: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    return {"version": _STATE_VERSION, "state": states}


def _load_state() -> Dict[str, Dict[str, Any]]:
    data = _read_json_quarantine(SCHEDULE_STATE_FILE, _valid_state)
    if data is None:
        return {}
    return dict(data["state"])


def _migrate_legacy(data: Dict[str, Any]) -> Dict[str, Any]:
    """Version 1 schedules.json -> version 2 definitions + state file. State
    is written first; the definitions are rewritten only after it succeeds,
    so a crash in between re-runs the migration on the next load. State that
    already exists (from an earlier partial run) wins over inline values."""
    definitions = [_definition_of(e) for e in data["schedules"]]
    defs = {"version": _SCHEMA_VERSION, "next_id": data["next_id"], "schedules": definitions}
    states = _state_from_entries(data["schedules"])
    states.update(_load_state())
    if not _atomic_write(SCHEDULE_STATE_FILE, _state_doc(states), _JSON_INDENT):
        _LOGGER.warning("Schedule migration deferred: running state could not be written")
        return defs
    if not _atomic_write(SCHEDULES_FILE, defs, _JSON_INDENT):
        _LOGGER.warning("Schedule migration deferred: definitions could not be rewritten")
        return defs
    _LOGGER.info(
        "Migrated %d schedule(s) to schedules.json version %d; running state moved to %s",
        len(definitions), _SCHEMA_VERSION, os.path.basename(SCHEDULE_STATE_FILE),
    )
    return defs


def _load_defs() -> Dict[str, Any]:
    data = _read_json_quarantine(
        SCHEDULES_FILE,
        lambda d: _valid_file(d, _SCHEMA_VERSION, _valid_entry)
        or _valid_file(d, _LEGACY_SCHEMA_VERSION, _valid_entry_v1),
    )
    if data is None:
        return _fresh_defs()
    if data.get("version") == _SCHEMA_VERSION:
        return data
    return _migrate_legacy(data)


def _fill_missing_next_run(entry: Dict[str, Any], now: float) -> None:
    """An enabled entry with no stored next run (state file missing or
    rebuilt) gets one computed from now. It is never treated as due."""
    if not entry["enabled"] or entry["next_run_at"] is not None:
        return
    parsed, _n, err = parse_spec(entry["spec"].split())
    if err or parsed is None:
        return
    try:
        entry["next_run_at"] = next_run(parsed, now)
    except (OverflowError, ValueError):
        _LOGGER.error("schedule_next_run_failed id=%s", entry["id"])


def load() -> Dict[str, Any]:
    """Merged view of definitions and running state: {"next_id", "schedules"},
    each entry carrying last_run_at/next_run_at from the state file. Never
    raises; missing or corrupt files fall back to empty data."""
    defs = _load_defs()
    states = _load_state()
    now = time.time()
    entries = []
    for definition in defs["schedules"]:
        entry = dict(definition)
        st = states.get(str(definition["id"]), {})
        entry["last_run_at"] = st.get("last_run_at")
        entry["next_run_at"] = st.get("next_run_at")
        _fill_missing_next_run(entry, now)
        entries.append(entry)
    return {"next_id": defs["next_id"], "schedules": entries}


def _commit(data: Dict[str, Any], defs_changed: bool) -> bool:
    """Persist a merged view from load(). Definitions are rewritten only when
    defs_changed; running state is always rewritten and pruned to the ids
    that still exist. Definitions are written first."""
    if defs_changed:
        defs = {
            "version": _SCHEMA_VERSION,
            "next_id": data["next_id"],
            "schedules": [_definition_of(e) for e in data["schedules"]],
        }
        if not _atomic_write(SCHEDULES_FILE, defs, _JSON_INDENT):
            return False
    return _atomic_write(
        SCHEDULE_STATE_FILE, _state_doc(_state_from_entries(data["schedules"])), _JSON_INDENT
    )


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
    if entry["next_run_at"] is None:
        return "not scheduled"
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
    spec = entry["spec"]
    english = describe_spec(spec)
    schedule = spec if english == spec else "{} ({})".format(spec, english)
    return "#{} [{}] {} — {} — next {} — last {} — {}".format(
        entry["id"], state, name, schedule, _next_str(entry, now), last_str, prompt_preview
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
            "spec": _stored_text(parsed),
            "prompt": prompt,
            "name": resolved_name,
            "enabled": True,
            "created_at": now,
            "last_run_at": None,
            "next_run_at": next_run_at,
        }
        data["schedules"].append(entry)
        if not _commit(data, defs_changed=True):
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
            target["spec"] = _stored_text(parsed)
            target["next_run_at"] = next_run_at

        if prompt is not None:
            target["prompt"] = prompt
        if resolved_name is not None:
            target["name"] = resolved_name

        if not _commit(data, defs_changed=True):
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
        if not _commit(data, defs_changed=True):
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
        if not _commit(data, defs_changed=True):
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
    next_run_at (a manual run never perturbs the regular schedule). Only the
    running-state file is rewritten."""
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
        if not _commit(data, defs_changed=False):
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

        # A missing stored next run is never "due". A disabled entry keeps
        # none; an enabled one gets it recomputed from now, and a spent
        # one-shot whose state went missing is dropped.
        if entry["next_run_at"] is None:
            if not entry["enabled"]:
                updated.append(entry)
                continue
            if recomputed is None and parsed["kind"] == "at":
                _LOGGER.warning("schedule_spent_without_state id=%s", eid)
                continue
            entry["next_run_at"] = recomputed
            updated.append(entry)
            continue

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
        # Definitions change only when a spent one-shot was deleted; the
        # per-tick bookkeeping goes to the running-state file alone.
        defs_changed = len(updated) != len(data["schedules"])
        data["schedules"] = updated
        saved = _commit(data, defs_changed=defs_changed)
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
