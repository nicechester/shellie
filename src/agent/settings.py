from __future__ import annotations

import difflib
import json
import logging
import os
import re
import stat
import threading
import time
from collections import namedtuple
from typing import Any, Callable, Dict, List, Optional, Tuple

_LOGGER = logging.getLogger("shellie.settings")

Result = namedtuple("Result", ["ok", "applied", "errors", "changes"])

_BOOL_TRUE = ("true", "on", "1", "yes")
_BOOL_FALSE = ("false", "off", "0", "no")

_TOKEN_RE = re.compile(r"^\d{5,16}:[A-Za-z0-9_-]{30,64}$")
_GEMINI_KEY_RE = re.compile(r"^[A-Za-z0-9._-]{20,128}$")
_MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,63}$")
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
_MCP_SERVER_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")
_MCP_URL_RE = re.compile(r"^https?://")
_MCP_SERVERS_CONSTRAINT = "JSON array: name(alphanumeric_-), url(http/https), headers(optional)"


def mask_secret_value(key: str, value: Any) -> str:
    """Mask a secret value for logs/UI. Never returns the raw value."""
    if value is None:
        return ""
    s = str(value)
    if key == "TELEGRAM_BOT_TOKEN" and ":" in s:
        bot_id, _, rest = s.partition(":")
        tail = rest[-4:] if len(rest) >= 4 else rest
        return "{}:••••{}".format(bot_id, tail)
    if len(s) >= 12:
        return "••••" + s[-4:]
    return "••••"


def _parse_int_generic(raw: Any) -> int:
    if isinstance(raw, bool):
        raise ValueError("An integer value is required")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            raise ValueError("An integer value is required")
    raise ValueError("An integer value is required")


def _make_int_parser(lo: int, hi: int) -> Callable[[Any], int]:
    def parser(raw: Any) -> int:
        n = _parse_int_generic(raw)
        if not (lo <= n <= hi):
            raise ValueError("Allowed range: {}–{}".format(lo, hi))
        return n

    return parser


def _parse_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if s in _BOOL_TRUE:
        return True
    if s in _BOOL_FALSE:
        return False
    raise ValueError("A boolean value is required (true/false/on/off/1/0/yes/no)")


def _parse_log_level(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ValueError("A string value is required")
    s = raw.strip().upper()
    if s not in _LOG_LEVELS:
        raise ValueError("Allowed values: {}".format("/".join(_LOG_LEVELS)))
    return s


def _parse_token(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ValueError("A string value is required")
    s = raw.strip()
    if not _TOKEN_RE.match(s):
        raise ValueError("Invalid format: numericID:token(alphanumeric/-/_ 30–64 chars)")
    return s


def _parse_gemini_key(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ValueError("A string value is required")
    s = raw.strip()
    if not _GEMINI_KEY_RE.match(s):
        raise ValueError("Invalid format: alphanumeric/./-/_ 20–128 chars")
    return s


def _parse_allowed_user_id(raw: Any) -> int:
    n = _parse_int_generic(raw)
    if not (1 <= n <= (2 ** 53 - 1)):
        raise ValueError("Allowed range: 1–2^53-1")
    return n


def _parse_system_prompt(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ValueError("A string value is required")
    if not (1 <= len(raw) <= 4000):
        raise ValueError("Length limit: 1–4000 characters")
    return raw


def _parse_shell_path(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ValueError("A string value is required")
    s = raw.strip()
    if s == "auto":
        return s
    if not os.path.isabs(s):
        raise ValueError("Must be 'auto' or an absolute path")
    basename = os.path.basename(s)
    if basename.endswith("nologin") or basename.endswith("false"):
        raise ValueError("Unusable shell (nologin/false)")
    if not (os.path.exists(s) and os.access(s, os.X_OK)):
        raise ValueError("Executable file does not exist")
    if os.path.exists("/etc/shells"):
        allowed = set()
        with open("/etc/shells", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                allowed.add(line)
        if s not in allowed:
            raise ValueError("Not listed in /etc/shells")
    return s


def _parse_mcp_servers(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ValueError("A string value is required")
    s = raw.strip()
    if s == "":
        return s
    try:
        data = json.loads(s)
    except ValueError:
        raise ValueError(_MCP_SERVERS_CONSTRAINT)
    if not isinstance(data, list):
        raise ValueError(_MCP_SERVERS_CONSTRAINT)
    seen_names = set()
    for entry in data:
        if not isinstance(entry, dict):
            raise ValueError(_MCP_SERVERS_CONSTRAINT)
        name = entry.get("name")
        if not isinstance(name, str) or not _MCP_SERVER_NAME_RE.match(name):
            raise ValueError(_MCP_SERVERS_CONSTRAINT)
        if name in seen_names:
            raise ValueError(_MCP_SERVERS_CONSTRAINT)
        seen_names.add(name)
        url = entry.get("url")
        if not isinstance(url, str) or not _MCP_URL_RE.match(url):
            raise ValueError(_MCP_SERVERS_CONSTRAINT)
        headers = entry.get("headers")
        if headers is not None:
            if not isinstance(headers, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in headers.items()
            ):
                raise ValueError(_MCP_SERVERS_CONSTRAINT)
    return s


def _parse_model_chain(raw: Any) -> Tuple[str, ...]:
    if isinstance(raw, str):
        items = [x.strip() for x in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        items = [str(x).strip() for x in raw]
    else:
        raise ValueError("A comma-separated model list string is required")
    if not (1 <= len(items) <= 5):
        raise ValueError("Must have 1–5 models")
    seen = set()
    for item in items:
        if not _MODEL_RE.match(item):
            raise ValueError("Invalid model name format: {}".format(item))
        if item in seen:
            raise ValueError("Duplicate model name: {}".format(item))
        seen.add(item)
    return tuple(items)


class SettingSpec:
    def __init__(
        self,
        key: str,
        value_type: str,
        parser: Callable[[Any], Any],
        default: Any = None,
        secret: bool = False,
        telegram_editable: bool = True,
        required: bool = False,
        apply_timing: str = "immediate",
        description: str = "",
        constraint: str = "",
        precheck: Optional[Callable[[Any], None]] = None,
    ):
        self.key = key
        self.value_type = value_type
        self.parser = parser
        self.default = default
        self.secret = secret
        self.telegram_editable = telegram_editable
        self.required = required
        self.apply_timing = apply_timing
        self.description = description
        self.constraint = constraint
        # Precheck slots (getMe, port bind test, bot-self-id check) are wired
        # in later phases (telegram client / web manager do not exist yet).
        self.precheck = precheck


_DEFAULT_SYSTEM_PROMPT = (
    "You are Shellie, a lightweight agent running on the user's computer. "
    "Use shell commands and memory tools as needed."
)

CATALOG: Tuple[SettingSpec, ...] = (
    SettingSpec(
        "TELEGRAM_BOT_TOKEN", "str", _parse_token,
        default=None, secret=True, telegram_editable=False, required=True,
        apply_timing="next poll cycle",
        description="Telegram bot API token",
        constraint="Format: numericID:token(alphanumeric/-/_ 30–64 chars)",
    ),
    SettingSpec(
        "GEMINI_API_KEY", "str", _parse_gemini_key,
        default=None, secret=True, telegram_editable=False, required=True,
        apply_timing="immediate",
        description="Gemini API key",
        constraint="alphanumeric/./-/_ 20–128 chars",
    ),
    SettingSpec(
        "ALLOWED_USER_ID", "int", _parse_allowed_user_id,
        default=None, secret=False, telegram_editable=False, required=True,
        apply_timing="immediate",
        description="Telegram user ID allowed to use the bot",
        constraint="Integer 1–2^53-1, cannot be the bot's own ID",
    ),
    SettingSpec(
        "GEMINI_MODEL_CHAIN", "list", _parse_model_chain,
        default=("gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.5-flash-lite"),
        apply_timing="immediate",
        description="Gemini model chain to try in order on 429/errors",
        constraint="1–5 comma-separated model names, no duplicates",
    ),
    SettingSpec(
        "GEMINI_TIMEOUT_SEC", "int", _make_int_parser(5, 300),
        default=60, apply_timing="immediate",
        description="Gemini API call timeout (seconds)",
        constraint="5~300",
    ),
    SettingSpec(
        "GEMINI_FALLBACK_DELAY_SEC", "int", _make_int_parser(0, 10),
        default=1, apply_timing="immediate",
        description="Delay before switching to the next model (seconds)",
        constraint="0~10",
    ),
    SettingSpec(
        "GEMINI_RETRY_BASE_DELAY_SEC", "int", _make_int_parser(5, 300),
        default=60, apply_timing="immediate",
        description="Base cooldown before retrying the whole model chain after rate limit (seconds)",
        constraint="5~300",
    ),
    SettingSpec(
        "GEMINI_MAX_RETRIES", "int", _make_int_parser(0, 5),
        default=2, apply_timing="immediate",
        description="Max retries of the whole model chain after cooldown (0 = no retry)",
        constraint="0~5",
    ),
    SettingSpec(
        "SYSTEM_PROMPT", "str", _parse_system_prompt,
        default=_DEFAULT_SYSTEM_PROMPT, apply_timing="immediate",
        description="System prompt sent to the LLM",
        constraint="1–4000 chars, multi-line allowed",
    ),
    SettingSpec(
        "FC_MAX_LOOPS", "int", _make_int_parser(1, 10),
        default=5, apply_timing="immediate",
        description="Maximum function-calling loop iterations",
        constraint="1~10",
    ),
    SettingSpec(
        "CONTEXT_TURNS", "int", _make_int_parser(0, 50),
        default=10, apply_timing="immediate",
        description="Recent conversation turns to retain (0=single-shot)",
        constraint="0~50",
    ),
    SettingSpec(
        "IDLE_RESET_MINUTES", "int", _make_int_parser(0, 1440),
        default=30, apply_timing="immediate",
        description="Idle time before auto-resetting conversation context (minutes, 0=off)",
        constraint="0~1440",
    ),
    SettingSpec(
        "SHELL_TIMEOUT_SEC", "int", _make_int_parser(1, 600),
        default=45, apply_timing="immediate",
        description="Shell command execution timeout (seconds)",
        constraint="1~600",
    ),
    SettingSpec(
        "SHELL_OUTPUT_LIMIT", "int", _make_int_parser(200, 20000),
        default=3000, apply_timing="immediate",
        description="Maximum shell output length (characters)",
        constraint="200~20000",
    ),
    SettingSpec(
        "POLL_TIMEOUT_SEC", "int", _make_int_parser(1, 50),
        default=30, apply_timing="next poll cycle",
        description="getUpdates long-polling timeout (seconds)",
        constraint="1~50",
    ),
    SettingSpec(
        "LOG_LEVEL", "enum", _parse_log_level,
        default="INFO", apply_timing="immediate (hook)",
        description="Log output level",
        constraint="One of DEBUG/INFO/WARNING/ERROR (case-insensitive)",
    ),
    SettingSpec(
        "WEB_ENABLED", "bool", _parse_bool,
        default=True, apply_timing="listener reconcile",
        description="Enable the local settings web server",
        constraint="true/false/on/off/1/0/yes/no",
    ),
    SettingSpec(
        "WEB_PORT", "int", _make_int_parser(1024, 65535),
        default=8321, apply_timing="listener reconcile",
        description="Local settings web server port",
        constraint="1024~65535",
    ),
    SettingSpec(
        "SHELL_PATH", "str", _parse_shell_path,
        default="auto", apply_timing="immediate (next shell command)",
        description="Shell executable path. 'auto' resolves: /bin/zsh on macOS, then $SHELL, login shell, /bin/sh",
        constraint="'auto' or absolute path (listed in /etc/shells, executable)",
    ),
    SettingSpec(
        "MCP_SERVERS", "str", _parse_mcp_servers,
        default="", secret=True, telegram_editable=False, required=False,
        apply_timing="immediate (next LLM call)",
        description=(
            'HTTP MCP server list (JSON). Example: [{"name":"svc","url":"https://...",'
            '"headers":{"Authorization":"Bearer ..."}}]'
        ),
        constraint=_MCP_SERVERS_CONSTRAINT,
    ),
)


class SettingsStore:
    def __init__(self) -> None:
        self._catalog: Dict[str, SettingSpec] = {spec.key: spec for spec in CATALOG}
        self._catalog_order: List[str] = [spec.key for spec in CATALOG]
        self._lock = threading.Lock()
        self._values: Dict[str, Any] = {}
        self._sources: Dict[str, str] = {}
        self._env_layer: Dict[str, Any] = {}
        self._overrides: Dict[str, Any] = {}
        self._hooks: Dict[str, List[Callable[[Any], None]]] = {}
        self._revision = 0
        self._path: Optional[str] = None

    def load(self, env: Optional[Dict[str, str]] = None, path: Optional[str] = None) -> List[str]:
        if path is None:
            from . import config as _config
            path = _config.SETTINGS_FILE
        self._path = path

        if env is None:
            from . import config as _config
            dotenv_values = _config.parse_dotenv(os.path.join(_config.BASE_DIR, ".env"))
            merged: Dict[str, str] = dict(dotenv_values)
            merged.update(os.environ)
        else:
            merged = dict(env)

        values: Dict[str, Any] = {}
        sources: Dict[str, str] = {}
        env_layer: Dict[str, Any] = {}

        for spec in self._catalog.values():
            if spec.default is not None:
                values[spec.key] = spec.default
                sources[spec.key] = "default"

        for spec in self._catalog.values():
            if spec.key not in merged:
                continue
            try:
                parsed = spec.parser(merged[spec.key])
            except ValueError as exc:
                _LOGGER.warning("Ignoring setting (env layer) key=%s reason=%s", spec.key, exc)
                continue
            values[spec.key] = parsed
            sources[spec.key] = "env"
            env_layer[spec.key] = parsed

        override_raw = self._read_settings_file(path)
        overrides: Dict[str, Any] = {}
        for key, raw in override_raw.items():
            spec = self._catalog.get(key)
            if spec is None:
                _LOGGER.warning("Ignoring unknown setting key (override) key=%s", key)
                continue
            try:
                parsed = spec.parser(raw)
            except ValueError as exc:
                _LOGGER.warning("Ignoring setting (override layer) key=%s reason=%s", key, exc)
                continue
            values[spec.key] = parsed
            sources[spec.key] = "override"
            overrides[spec.key] = parsed

        self._values = values
        self._sources = sources
        self._env_layer = env_layer
        self._overrides = overrides
        self._revision = 0

        missing = [spec.key for spec in self._catalog.values() if spec.required and spec.key not in values]
        return missing

    def get(self, key: str) -> Any:
        if key not in self._catalog:
            raise KeyError(key)
        return self._values.get(key)

    def snapshot(self) -> Dict[str, Any]:
        return dict(self._values)

    def rows(self, mask: bool = True) -> List[Dict[str, Any]]:
        result = []
        for key in self._catalog_order:
            spec = self._catalog[key]
            value = self._values.get(key)
            source = self._sources.get(key, "default" if spec.default is not None else "none")
            result.append({
                "key": key,
                "value": self._display(spec, value, mask),
                "source": source,
                "telegram_editable": spec.telegram_editable,
                "apply_timing": spec.apply_timing,
                "description": spec.description,
                "constraint": spec.constraint,
                "default": self._display(spec, spec.default, mask),
                "required": spec.required,
                "secret": spec.secret,
            })
        return result

    def on_change(self, key: str, fn: Callable[[Any], None]) -> None:
        key = key.strip().upper()
        with self._lock:
            self._hooks.setdefault(key, []).append(fn)

    def register_precheck(self, key: str, fn: Optional[Callable[[Any], None]]) -> None:
        self._catalog[key.strip().upper()].precheck = fn

    @property
    def revision(self) -> int:
        return self._revision

    def update(self, changes: Dict[str, Any], actor: str) -> Result:
        with self._lock:
            errors: Dict[str, str] = {}
            normalized: Dict[str, Any] = {}
            for raw_key, raw_value in changes.items():
                key = str(raw_key).strip().upper()
                if key not in self._catalog:
                    errors[key] = self._unknown_key_message(key)
                    continue
                normalized[key] = raw_value
            if errors:
                return Result(False, [], errors, {})

            for key in normalized:
                spec = self._catalog[key]
                if actor == "telegram" and not spec.telegram_editable:
                    errors[key] = "🔒 Can only be changed via the local web UI."
            if errors:
                return Result(False, [], errors, {})

            parsed: Dict[str, Any] = {}
            for key, raw_value in normalized.items():
                spec = self._catalog[key]
                try:
                    parsed[key] = spec.parser(raw_value)
                except ValueError as exc:
                    errors[key] = str(exc)
            if errors:
                return Result(False, [], errors, {})

            applied: Dict[str, Any] = {}
            changes_info: Dict[str, Tuple[Any, Any]] = {}
            for key, new_value in parsed.items():
                old_value = self._values.get(key)
                if old_value == new_value:
                    continue
                applied[key] = new_value
                changes_info[key] = (old_value, new_value)

            if not applied:
                return Result(True, [], {}, {})

            for key, new_value in applied.items():
                spec = self._catalog[key]
                if spec.precheck is not None:
                    try:
                        spec.precheck(new_value)
                    except ValueError as exc:
                        errors[key] = str(exc)
            if errors:
                return Result(False, [], errors, {})

            new_overrides = dict(self._overrides)
            new_overrides.update(applied)
            ok, err = self._persist(new_overrides)
            if not ok:
                return Result(False, [], {"_persist": err}, {})

            new_values = dict(self._values)
            new_values.update(applied)
            new_sources = dict(self._sources)
            for key in applied:
                new_sources[key] = "override"
            self._values = new_values
            self._sources = new_sources
            self._overrides = new_overrides
            self._revision += 1

        self._run_hooks(list(applied.keys()))
        for key, (old_value, new_value) in changes_info.items():
            self._audit(actor, key, old_value, new_value)

        display_changes = {}
        for key, (old_value, new_value) in changes_info.items():
            spec = self._catalog[key]
            display_changes[key] = (
                self._display(spec, old_value, True),
                self._display(spec, new_value, True),
            )
        return Result(True, list(applied.keys()), {}, display_changes)

    def unset(self, key: str, actor: str) -> Result:
        key = str(key).strip().upper()
        with self._lock:
            if key not in self._catalog:
                return Result(False, [], {key: self._unknown_key_message(key)}, {})
            spec = self._catalog[key]
            if actor == "telegram" and not spec.telegram_editable:
                return Result(False, [], {key: "🔒 Can only be changed via the local web UI."}, {})
            if key not in self._overrides:
                return Result(True, [], {}, {})

            if spec.required and self._resolve_lower_layer(key) is None:
                return Result(
                    False, [],
                    {key: "Cannot unset a required key when no valid value exists in a lower layer."},
                    {},
                )

            old_value = self._values.get(key)
            new_overrides = dict(self._overrides)
            del new_overrides[key]
            ok, err = self._persist(new_overrides)
            if not ok:
                return Result(False, [], {"_persist": err}, {})

            new_value = self._resolve_lower_layer(key)
            new_values = dict(self._values)
            new_sources = dict(self._sources)
            if new_value is None:
                new_values.pop(key, None)
                new_sources.pop(key, None)
            else:
                new_values[key] = new_value
                new_sources[key] = self._lower_source(key)
            self._values = new_values
            self._sources = new_sources
            self._overrides = new_overrides
            self._revision += 1

        self._run_hooks([key])
        self._audit(actor, key, old_value, new_value)
        display_changes = {
            key: (self._display(spec, old_value, True), self._display(spec, new_value, True))
        }
        return Result(True, [key], {}, display_changes)

    def _unknown_key_message(self, key: str) -> str:
        suggestion = difflib.get_close_matches(key, self._catalog_order, n=1)
        if suggestion:
            return "Unknown key. Did you mean {}?".format(suggestion[0])
        return "Unknown key."

    def _resolve_lower_layer(self, key: str) -> Any:
        if key in self._env_layer:
            return self._env_layer[key]
        return self._catalog[key].default

    def _lower_source(self, key: str) -> str:
        return "env" if key in self._env_layer else "default"

    def _display(self, spec: SettingSpec, value: Any, mask: bool) -> str:
        if value is None:
            return ""
        if mask and spec.secret:
            return mask_secret_value(spec.key, value)
        if isinstance(value, tuple):
            return ",".join(value)
        return str(value)

    def _run_hooks(self, keys: List[str]) -> None:
        for key in keys:
            for fn in list(self._hooks.get(key, ())):
                try:
                    fn(self._values.get(key))
                except Exception:
                    _LOGGER.exception("Exception in settings change hook key=%s", key)

    def _audit(self, actor: str, key: str, old_value: Any, new_value: Any) -> None:
        spec = self._catalog[key]
        old_disp = self._display(spec, old_value, True)
        new_disp = self._display(spec, new_value, True)
        _LOGGER.info("settings change actor=%s key=%s old=%s new=%s", actor, key, old_disp, new_disp)

    def _read_settings_file(self, path: str) -> Dict[str, Any]:
        if not os.path.exists(path):
            return {}
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
            if mode & 0o077:
                try:
                    os.chmod(path, 0o600)
                    _LOGGER.warning("Fixed settings.json permissions to 0600")
                except OSError as exc:
                    _LOGGER.warning("Failed to fix settings.json permissions: %s", exc)
        except OSError:
            pass

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            corrupt_path = "{}.corrupt-{}".format(path, int(time.time()))
            try:
                os.replace(path, corrupt_path)
                _LOGGER.warning("Corrupt settings.json preserved as %s, ignoring", corrupt_path)
            except OSError as exc:
                _LOGGER.warning("Failed to handle corrupt settings.json: %s", exc)
            return {}

        values = data.get("values") if isinstance(data, dict) else None
        if not isinstance(values, dict):
            _LOGGER.warning("settings.json has invalid format, ignoring")
            return {}
        return values

    def _persist(self, overrides: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        serializable = {}
        for key, value in overrides.items():
            serializable[key] = list(value) if isinstance(value, tuple) else value
        payload = json.dumps({"version": 1, "values": serializable}, ensure_ascii=False, indent=2)
        tmp_path = self._path + ".tmp"
        try:
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._path)
            return True, None
        except OSError as exc:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            return False, str(exc)


settings = SettingsStore()
