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

_BOOL_TRUE = ("true", "on", "1", "yes", "켜기")
_BOOL_FALSE = ("false", "off", "0", "no", "끄기")

_TOKEN_RE = re.compile(r"^\d{5,16}:[A-Za-z0-9_-]{30,64}$")
_GEMINI_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{20,128}$")
_MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,63}$")
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
_MCP_SERVER_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")
_MCP_URL_RE = re.compile(r"^https?://")
_MCP_SERVERS_CONSTRAINT = "JSON 배열: name(영숫자_-)·url(http/https)·headers(선택)"


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
        raise ValueError("정수 값이 필요합니다")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            raise ValueError("정수 값이 필요합니다")
    raise ValueError("정수 값이 필요합니다")


def _make_int_parser(lo: int, hi: int) -> Callable[[Any], int]:
    def parser(raw: Any) -> int:
        n = _parse_int_generic(raw)
        if not (lo <= n <= hi):
            raise ValueError("허용 범위: {}~{}".format(lo, hi))
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
    raise ValueError("불리언 값이 필요합니다 (true/false/on/off/1/0/yes/no/켜기/끄기)")


def _parse_log_level(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ValueError("문자열 값이 필요합니다")
    s = raw.strip().upper()
    if s not in _LOG_LEVELS:
        raise ValueError("허용 값: {}".format("/".join(_LOG_LEVELS)))
    return s


def _parse_token(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ValueError("문자열 값이 필요합니다")
    s = raw.strip()
    if not _TOKEN_RE.match(s):
        raise ValueError("형식 오류: 숫자ID:토큰(영문/숫자/-/_ 30~64자)")
    return s


def _parse_gemini_key(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ValueError("문자열 값이 필요합니다")
    s = raw.strip()
    if not _GEMINI_KEY_RE.match(s):
        raise ValueError("형식 오류: 영문/숫자/-/_ 20~128자")
    return s


def _parse_allowed_user_id(raw: Any) -> int:
    n = _parse_int_generic(raw)
    if not (1 <= n <= (2 ** 53 - 1)):
        raise ValueError("허용 범위: 1~2^53-1")
    return n


def _parse_system_prompt(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ValueError("문자열 값이 필요합니다")
    if not (1 <= len(raw) <= 4000):
        raise ValueError("길이 제한: 1~4000자")
    return raw


def _parse_shell_path(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ValueError("문자열 값이 필요합니다")
    s = raw.strip()
    if s == "auto":
        return s
    if not os.path.isabs(s):
        raise ValueError("auto 또는 절대경로여야 합니다")
    basename = os.path.basename(s)
    if basename.endswith("nologin") or basename.endswith("false"):
        raise ValueError("사용할 수 없는 셸입니다 (nologin/false)")
    if not (os.path.exists(s) and os.access(s, os.X_OK)):
        raise ValueError("실행 가능한 파일이 존재하지 않습니다")
    if os.path.exists("/etc/shells"):
        allowed = set()
        with open("/etc/shells", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                allowed.add(line)
        if s not in allowed:
            raise ValueError("/etc/shells에 등록되지 않았습니다")
    return s


def _parse_mcp_servers(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ValueError("문자열 값이 필요합니다")
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
        raise ValueError("콤마로 구분된 모델 목록 문자열이 필요합니다")
    if not (1 <= len(items) <= 5):
        raise ValueError("모델은 1~5개여야 합니다")
    seen = set()
    for item in items:
        if not _MODEL_RE.match(item):
            raise ValueError("모델명 형식 오류: {}".format(item))
        if item in seen:
            raise ValueError("중복된 모델명: {}".format(item))
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
        apply_timing: str = "즉시",
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
    "당신은 Shellie, 사용자의 컴퓨터에서 구동되는 경량 에이전트입니다. "
    "필요시 셸 명령어와 메모리 툴을 활용하세요."
)

CATALOG: Tuple[SettingSpec, ...] = (
    SettingSpec(
        "TELEGRAM_BOT_TOKEN", "str", _parse_token,
        default=None, secret=True, telegram_editable=False, required=True,
        apply_timing="폴링",
        description="텔레그램 봇 API 토큰",
        constraint="형식: 숫자ID:토큰(영문/숫자/-/_ 30~64자)",
    ),
    SettingSpec(
        "GEMINI_API_KEY", "str", _parse_gemini_key,
        default=None, secret=True, telegram_editable=False, required=True,
        apply_timing="즉시",
        description="Gemini API 키",
        constraint="영문/숫자/-/_ 20~128자",
    ),
    SettingSpec(
        "ALLOWED_USER_ID", "int", _parse_allowed_user_id,
        default=None, secret=False, telegram_editable=False, required=True,
        apply_timing="즉시",
        description="봇 사용을 허용할 텔레그램 사용자 ID",
        constraint="1~2^53-1 사이 정수, 봇 자신의 ID는 불가",
    ),
    SettingSpec(
        "GEMINI_MODEL_CHAIN", "list", _parse_model_chain,
        default=("gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.5-flash-lite"),
        apply_timing="즉시",
        description="429 발생 시 순서대로 전환할 Gemini 모델 체인",
        constraint="콤마로 구분된 1~5개 모델명, 중복 불가",
    ),
    SettingSpec(
        "GEMINI_TIMEOUT_SEC", "int", _make_int_parser(5, 300),
        default=60, apply_timing="즉시",
        description="Gemini API 호출 타임아웃(초)",
        constraint="5~300",
    ),
    SettingSpec(
        "GEMINI_FALLBACK_DELAY_SEC", "int", _make_int_parser(0, 10),
        default=1, apply_timing="즉시",
        description="모델 전환 전 대기 시간(초)",
        constraint="0~10",
    ),
    SettingSpec(
        "SYSTEM_PROMPT", "str", _parse_system_prompt,
        default=_DEFAULT_SYSTEM_PROMPT, apply_timing="즉시",
        description="LLM에게 전달할 시스템 프롬프트",
        constraint="1~4000자, 여러 줄 허용",
    ),
    SettingSpec(
        "FC_MAX_LOOPS", "int", _make_int_parser(1, 10),
        default=5, apply_timing="즉시",
        description="Function Calling 최대 반복 횟수",
        constraint="1~10",
    ),
    SettingSpec(
        "CONTEXT_TURNS", "int", _make_int_parser(0, 50),
        default=10, apply_timing="즉시",
        description="유지할 최근 대화 턴 수(0=단발 호출)",
        constraint="0~50",
    ),
    SettingSpec(
        "IDLE_RESET_MINUTES", "int", _make_int_parser(0, 1440),
        default=30, apply_timing="즉시",
        description="대화 맥락을 자동 초기화하는 유휴 시간(분, 0=끔)",
        constraint="0~1440",
    ),
    SettingSpec(
        "SHELL_TIMEOUT_SEC", "int", _make_int_parser(1, 600),
        default=45, apply_timing="즉시",
        description="셸 명령 실행 타임아웃(초)",
        constraint="1~600",
    ),
    SettingSpec(
        "SHELL_OUTPUT_LIMIT", "int", _make_int_parser(200, 20000),
        default=3000, apply_timing="즉시",
        description="셸 출력 최대 길이(문자)",
        constraint="200~20000",
    ),
    SettingSpec(
        "POLL_TIMEOUT_SEC", "int", _make_int_parser(1, 50),
        default=30, apply_timing="폴링",
        description="getUpdates 롱폴링 타임아웃(초)",
        constraint="1~50",
    ),
    SettingSpec(
        "LOG_LEVEL", "enum", _parse_log_level,
        default="INFO", apply_timing="즉시(훅)",
        description="로그 출력 수준",
        constraint="DEBUG/INFO/WARNING/ERROR 중 하나(대소문자 무관)",
    ),
    SettingSpec(
        "WEB_ENABLED", "bool", _parse_bool,
        default=True, apply_timing="리스너",
        description="로컬 설정 웹 서버 사용 여부",
        constraint="true/false/on/off/1/0/yes/no/켜기/끄기",
    ),
    SettingSpec(
        "WEB_PORT", "int", _make_int_parser(1024, 65535),
        default=8321, apply_timing="리스너",
        description="로컬 설정 웹 서버 포트",
        constraint="1024~65535",
    ),
    SettingSpec(
        "SHELL_PATH", "str", _parse_shell_path,
        default="auto", apply_timing="즉시(다음 명령)",
        description="셸 실행 파일 경로. auto면 자동 결정: macOS는 /bin/zsh 우선, 그 외는 $SHELL→로그인 셸→/bin/sh 순",
        constraint="auto 또는 절대경로(/etc/shells 등록·실행 가능)",
    ),
    SettingSpec(
        "MCP_SERVERS", "str", _parse_mcp_servers,
        default="", secret=True, telegram_editable=False, required=False,
        apply_timing="즉시(다음 LLM 호출)",
        description=(
            'HTTP MCP 서버 목록(JSON). 예: [{"name":"svc","url":"https://...",'
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
                _LOGGER.warning("설정 무시(env 레이어) key=%s reason=%s", spec.key, exc)
                continue
            values[spec.key] = parsed
            sources[spec.key] = "env"
            env_layer[spec.key] = parsed

        override_raw = self._read_settings_file(path)
        overrides: Dict[str, Any] = {}
        for key, raw in override_raw.items():
            spec = self._catalog.get(key)
            if spec is None:
                _LOGGER.warning("알 수 없는 설정 키 무시(override) key=%s", key)
                continue
            try:
                parsed = spec.parser(raw)
            except ValueError as exc:
                _LOGGER.warning("설정 무시(override 레이어) key=%s reason=%s", key, exc)
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
            source = self._sources.get(key, "default" if spec.default is not None else "없음")
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
                    errors[key] = "🔒 로컬 웹에서만 수정할 수 있습니다."
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
                return Result(False, [], {key: "🔒 로컬 웹에서만 수정할 수 있습니다."}, {})
            if key not in self._overrides:
                return Result(True, [], {}, {})

            if spec.required and self._resolve_lower_layer(key) is None:
                return Result(
                    False, [],
                    {key: "필수 항목이라 하위 레이어에 유효한 값이 없으면 해제할 수 없습니다."},
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
            return "알 수 없는 키입니다. 혹시 {}?".format(suggestion[0])
        return "알 수 없는 키입니다."

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
                    _LOGGER.exception("설정 변경 훅 실행 중 예외 발생 key=%s", key)

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
                    _LOGGER.warning("settings.json 권한을 0600으로 변경했습니다")
                except OSError as exc:
                    _LOGGER.warning("settings.json 권한 변경 실패: %s", exc)
        except OSError:
            pass

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            corrupt_path = "{}.corrupt-{}".format(path, int(time.time()))
            try:
                os.replace(path, corrupt_path)
                _LOGGER.warning("손상된 settings.json을 %s로 보존하고 무시합니다", corrupt_path)
            except OSError as exc:
                _LOGGER.warning("손상된 settings.json 처리 실패: %s", exc)
            return {}

        values = data.get("values") if isinstance(data, dict) else None
        if not isinstance(values, dict):
            _LOGGER.warning("settings.json 형식이 올바르지 않아 무시합니다")
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
