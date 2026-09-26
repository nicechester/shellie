from __future__ import annotations

import datetime
import os

from src.agent.config import MEMORY_DIR, MEMORY_FILE

_NO_MEMORY_MESSAGE = "No memory entries found."


def _today() -> str:
    """Return today's local date as YYYY-MM-DD. Wrapped so tests can patch it."""
    return datetime.date.today().isoformat()


def _dated_memory_path(day: str) -> str:
    return os.path.join(MEMORY_DIR, "{}.md".format(day))


def _read_stripped(path: str) -> str:
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def read_memory() -> str:
    core = _read_stripped(MEMORY_FILE)
    today = _today()
    dated_content = _read_stripped(_dated_memory_path(today))

    sections = []
    if core:
        sections.append(core)
    if dated_content:
        sections.append("## {}\n{}".format(today, dated_content))

    if not sections:
        return _NO_MEMORY_MESSAGE

    return "\n\n".join(sections)


def append_memory(content: str) -> str:
    normalized = " ".join(content.strip().split())

    if not normalized:
        return "Nothing to save."

    line = f"- {normalized}\n"

    path = _dated_memory_path(_today())

    if os.path.exists(path) and os.path.getsize(path) > 0:
        with open(path, "rb") as f:
            f.seek(-1, 2)
            last_byte = f.read(1)

        if last_byte != b"\n":
            line = "\n" + line

    with open(path, "a", encoding="utf-8") as f:
        f.write(line)

    return "Memory saved."
