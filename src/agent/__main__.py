from __future__ import annotations

import fcntl
import logging
import os
import signal
import sys
import time
from typing import Any, Optional

from src.agent.config import BASE_DIR, OFFSET_FILE, SHELLIE_HOME, settings
from src.agent.core import shell
from src.agent.telegram.client import get_me, get_updates, register_prechecks, send_message
from src.agent.telegram.handlers import process_update
from src.agent.web.server import manager

_LOGGER = logging.getLogger("shellie.main")

_LOCK_PATH = os.path.join(SHELLIE_HOME, ".shellie.lock")
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
        print("Another instance is already running.")
        sys.exit(1)
    _lock_fd = fd


def _apply_log_level(level_name: Any) -> None:
    level = getattr(logging, str(level_name).upper(), logging.INFO)
    logging.getLogger().setLevel(level)


def _handle_termination(signum: int, frame: Any) -> None:
    shell.kill_active()
    manager.stop()
    name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
    _LOGGER.info("%s received, shutting down cleanly", name)
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
        _LOGGER.exception("Failed to save offset")


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
                    "Bot token change detected (old bot ID=%s, new bot ID=%s), resetting offset",
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
        print("Missing required settings: {}".format(", ".join(missing)))
        print("Web UI is disabled; exiting.")
        sys.exit(1)

    _setup_required = True
    _LOGGER.info(
        "Setup required: http://127.0.0.1:%s/ (missing: %s)",
        settings.get("WEB_PORT"), ", ".join(missing),
    )
    while not all(settings.get(key) for key in _D10_REQUIRED_KEYS):
        time.sleep(2)
    _setup_required = False
    _LOGGER.info("Required values provided, starting polling")


def _notify_startup() -> None:
    """Best-effort: send a startup notice to the allowed user."""
    user_id = settings.get("ALLOWED_USER_ID")
    if not user_id:
        return
    try:
        send_message(user_id, "✅ Shellie started.", parse_mode=None)
    except Exception:
        _LOGGER.debug("Failed to send startup notification", exc_info=True)


def _run_polling_loop() -> None:
    global _polling_active
    _polling_active = True
    _notify_startup()
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
                    _LOGGER.error("Invalid token (401). Update it via the web UI.")
                elif status == 409:
                    _LOGGER.error("Another instance is polling or a webhook is set (409).")
                else:
                    _LOGGER.warning("getUpdates failed: status=%s", status)
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
                    _LOGGER.exception("Error processing update")
        except SystemExit:
            raise
        except Exception:
            # Never let a transient bug kill the process; the service
            # manager restart is a last resort, not the first response.
            _LOGGER.exception("Unexpected error in polling loop")
            time.sleep(2)
            continue


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        stream=sys.stdout,
    )

    if os.name != "posix":
        print("Unsupported platform (POSIX only).")
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
