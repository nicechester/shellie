from __future__ import annotations

import fcntl
import logging
import os
import signal
import sys
import time
from typing import Any, Optional

from src.agent.config import BASE_DIR, OFFSET_FILE, settings
from src.agent.core import shell
from src.agent.telegram.client import get_me, get_updates, register_prechecks
from src.agent.telegram.handlers import process_update
from src.agent.web.server import manager

_LOGGER = logging.getLogger("shellie.main")

_LOCK_PATH = os.path.join(BASE_DIR, ".shellie.lock")
_lock_fd: Optional[int] = None  # kept open for the process lifetime (R50)

_D10_REQUIRED_KEYS = ("TELEGRAM_BOT_TOKEN", "GEMINI_API_KEY", "ALLOWED_USER_ID")

# Exposed to the web UI via manager.status_provider (step 8). Main thread only.
_polling_active = False
_setup_required = False

# Token-change detection state (R41). Main thread only.
_current_token: Optional[str] = None
_bot_id: Optional[int] = None


def _acquire_single_instance_lock() -> None:
    global _lock_fd
    fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        print("다른 인스턴스가 실행 중입니다.")
        sys.exit(1)
    _lock_fd = fd


def _apply_log_level(level_name: Any) -> None:
    level = getattr(logging, str(level_name).upper(), logging.INFO)
    logging.getLogger().setLevel(level)


def _handle_termination(signum: int, frame: Any) -> None:
    shell.kill_active()
    manager.stop()
    name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
    _LOGGER.info("%s 수신, 정상 종료", name)
    sys.exit(0)


def _read_offset() -> Optional[int]:
    try:
        with open(OFFSET_FILE, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _write_offset(value: int) -> None:
    # Committed before process_update() runs (see the polling loop below) so
    # /restart's sys.exit(0) and mid-handler crashes never reprocess the same
    # update (at-most-once, D4/R3/R4).
    tmp_path = OFFSET_FILE + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(str(value))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, OFFSET_FILE)
    except OSError:
        _LOGGER.exception("오프셋 저장 실패")


def _handle_token_change() -> bool:
    """Detects a TELEGRAM_BOT_TOKEN change; resets the offset if the bot id
    itself changed (R41). Returns True when the offset file was reset."""
    global _current_token, _bot_id
    token = settings.get("TELEGRAM_BOT_TOKEN")
    if token == _current_token:
        return False

    reset = False
    if token:
        info = get_me(token=token)
        if not info.get("_status") and info.get("ok"):
            new_bot_id = info.get("result", {}).get("id")
            if _bot_id is not None and new_bot_id is not None and new_bot_id != _bot_id:
                _LOGGER.info(
                    "봇 토큰 변경 감지(이전 봇 ID=%s, 새 봇 ID=%s), 오프셋을 초기화합니다",
                    _bot_id, new_bot_id,
                )
                try:
                    os.remove(OFFSET_FILE)
                except OSError:
                    pass
                reset = True
            _bot_id = new_bot_id
        # get_me failure is tolerated: _bot_id is left as-is (None if unset).

    _current_token = token
    return reset


def _run_setup_required_mode(missing: Any) -> None:
    global _setup_required
    if not settings.get("WEB_ENABLED"):
        print("다음 필수 설정값이 없습니다: {}".format(", ".join(missing)))
        print("웹 설정이 꺼져 있어 종료합니다.")
        sys.exit(1)

    _setup_required = True
    _LOGGER.info(
        "설정 필요: http://127.0.0.1:%s/ (누락: %s)",
        settings.get("WEB_PORT"), ", ".join(missing),
    )
    while not all(settings.get(key) for key in _D10_REQUIRED_KEYS):
        time.sleep(2)
    _setup_required = False
    _LOGGER.info("필수값 입력 완료, polling 시작")


def _run_polling_loop() -> None:
    global _polling_active
    _polling_active = True
    offset = _read_offset()
    backoff = 1

    while True:
        try:
            if _handle_token_change():
                offset = None

            res = get_updates(offset)
            if "_status" in res:
                status = res["_status"]
                if status == 401:
                    _LOGGER.error("토큰이 유효하지 않습니다(401). 웹에서 수정하세요.")
                elif status == 409:
                    _LOGGER.error("다른 인스턴스가 polling 중이거나 webhook이 설정되어 있습니다(409).")
                else:
                    _LOGGER.warning("getUpdates 실패: status=%s", status)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue

            backoff = 1
            for result in res.get("result", []):
                offset = result["update_id"] + 1
                _write_offset(offset)
                try:
                    process_update(result)
                except SystemExit:
                    raise
                except Exception:
                    _LOGGER.exception("update 처리 오류")
        except SystemExit:
            raise
        except Exception:
            # Never let a transient bug kill the process; the service
            # manager restart is a last resort, not the first response.
            _LOGGER.exception("polling 루프에서 예기치 못한 오류 발생")
            time.sleep(2)
            continue


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        stream=sys.stdout,
    )

    if os.name != "posix":
        print("지원하지 않는 플랫폼입니다 (POSIX 전용).")
        sys.exit(1)

    _acquire_single_instance_lock()

    missing = settings.load()

    _apply_log_level(settings.get("LOG_LEVEL"))
    settings.on_change("LOG_LEVEL", _apply_log_level)

    register_prechecks()

    signal.signal(signal.SIGTERM, _handle_termination)
    signal.signal(signal.SIGINT, _handle_termination)

    manager.status_provider = lambda: {
        "polling": _polling_active,
        "setup_required": bool(_setup_required),
    }

    manager.start()

    if missing:
        _run_setup_required_mode(missing)

    _run_polling_loop()


if __name__ == "__main__":
    main()
