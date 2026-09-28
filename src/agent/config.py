from __future__ import annotations

import os

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

# Config/context directory: settings, memories, offset file live here.
# Override with SHELLIE_HOME env var before starting the process.
SHELLIE_HOME = os.path.abspath(
    os.environ.get("SHELLIE_HOME") or os.path.join(os.path.expanduser("~"), ".shellie")
)

# User workspace: job outputs, generated code, documents saved here (separate from config).
# Override with SHELLIE_WORKSPACE env var before starting the process.
SHELLIE_WORKSPACE = os.path.abspath(
    os.environ.get("SHELLIE_WORKSPACE") or os.path.join(os.path.expanduser("~"), "workspace")
)

MEMORY_DIR = os.path.join(SHELLIE_HOME, "memory")
MEMORY_FILE = os.path.join(MEMORY_DIR, "MEMORY.md")
SKILLS_DIR = os.path.join(SHELLIE_HOME, "skills")       # user skills (takes priority)
REPO_SKILLS_DIR = os.path.join(BASE_DIR, "skills")      # built-in repo skills
OFFSET_FILE = os.path.join(SHELLIE_HOME, ".update_offset")
SETTINGS_FILE = os.path.join(SHELLIE_HOME, "settings.json")

os.makedirs(MEMORY_DIR, exist_ok=True)
os.makedirs(SKILLS_DIR, exist_ok=True)
os.makedirs(SHELLIE_WORKSPACE, exist_ok=True)

# Seed MEMORY.md from the repo template on first run.
_MEMORY_TEMPLATE = os.path.join(BASE_DIR, "memory", "MEMORY.md")
if not os.path.exists(MEMORY_FILE) and os.path.exists(_MEMORY_TEMPLATE):
    import shutil
    shutil.copy2(_MEMORY_TEMPLATE, MEMORY_FILE)


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
