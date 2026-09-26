from __future__ import annotations

import os

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

MEMORY_DIR = os.path.join(BASE_DIR, "memory")
MEMORY_FILE = os.path.join(MEMORY_DIR, "MEMORY.md")
SKILLS_DIR = os.path.join(BASE_DIR, "skills")
OFFSET_FILE = os.path.join(BASE_DIR, ".update_offset")
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")

os.makedirs(MEMORY_DIR, exist_ok=True)
os.makedirs(SKILLS_DIR, exist_ok=True)


def parse_dotenv(path: str) -> dict:
    """Parse a .env file into a dict. Pure: never touches os.environ (R40)."""
    result: dict = {}
    if not os.path.exists(path):
        return result
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if (value.startswith('"') and value.endswith('"')) or (
                value.startswith("'") and value.endswith("'")
            ):
                value = value[1:-1]
            result[key] = value
    return result


# Deferred to avoid a hard circular import at module-load time; settings.py
# only reaches back into this module lazily, inside SettingsStore.load().
from .settings import settings, SettingsStore, SettingSpec, mask_secret_value  # noqa: E402
