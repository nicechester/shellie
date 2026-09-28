from __future__ import annotations

import os
import platform
import pwd
import signal
import subprocess
import sys
from typing import Dict, Optional

from src.agent.config import BASE_DIR, SHELLIE_WORKSPACE, settings
from src.agent.config import parse_dotenv as _parse_dotenv

_active_pgid: Optional[int] = None


def _is_usable_shell(path: str) -> bool:
    if not os.path.isabs(path):
        return False
    if not (os.path.exists(path) and os.access(path, os.X_OK)):
        return False
    basename = os.path.basename(path)
    if basename.endswith("nologin") or basename.endswith("false"):
        return False
    return True


def resolve_shell() -> str:
    """Resolve the shell executable to use, per dev-plan A2-1 §1.2. Never cached."""
    configured = settings.get("SHELL_PATH")
    if configured and configured != "auto":
        if not _is_usable_shell(configured):
            raise ValueError("Configured shell not found: {}".format(configured))
        return configured

    candidates = []
    if sys.platform == "darwin":
        candidates.append("/bin/zsh")
    env_shell = os.environ.get("SHELL")
    if env_shell:
        candidates.append(env_shell)
    try:
        pw_shell = pwd.getpwuid(os.getuid()).pw_shell
        if pw_shell:
            candidates.append(pw_shell)
    except (KeyError, OSError):
        pass
    candidates.append("/bin/sh")

    for candidate in candidates:
        if _is_usable_shell(candidate):
            return candidate

    raise ValueError("No usable shell found")


def _dotenv_path_dirs() -> list:
    """Return extra PATH dirs declared in .env, re-read on every shell call."""
    dotenv = _parse_dotenv(os.path.join(BASE_DIR, ".env"))
    raw = dotenv.get("PATH", "")
    return [d for d in raw.split(os.pathsep) if d] if raw else []


def _augmented_path(base_path: str) -> str:
    dirs = base_path.split(os.pathsep) if base_path else []
    # Prepend dirs from .env PATH (re-read each call, so edits take effect without restart)
    for d in reversed(_dotenv_path_dirs()):
        if d not in dirs:
            dirs.insert(0, d)
    if sys.platform == "darwin":
        extra_dirs = ["/opt/homebrew/bin", "/usr/local/bin"]
    else:
        extra_dirs = [os.path.expanduser("~/.local/bin"), "/usr/local/bin", "/snap/bin"]
    for d in extra_dirs:
        if d not in dirs and os.path.isdir(d):
            dirs.append(d)
    return os.pathsep.join(dirs)


def _child_env() -> Dict[str, str]:
    env = dict(os.environ)
    env.pop("TELEGRAM_BOT_TOKEN", None)
    env.pop("GEMINI_API_KEY", None)
    env["PATH"] = _augmented_path(env.get("PATH", ""))
    return env


def _combine_streams(stdout: str, stderr: str) -> str:
    stdout = stdout or ""
    stderr = stderr or ""
    if stdout and stderr:
        return stdout + "\n[stderr]\n" + stderr
    return stdout or stderr


def _truncate(text: str) -> str:
    limit = settings.get("SHELL_OUTPUT_LIMIT")
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... (output truncated at {} chars)".format(limit)


def kill_active() -> None:
    """SIGTERM the currently tracked process group, if any (for the SIGTERM handler, R52)."""
    pgid = _active_pgid
    if pgid is None:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def execute_shell(command: str) -> str:
    global _active_pgid

    try:
        shell = resolve_shell()
    except ValueError as exc:
        return str(exc)

    timeout = settings.get("SHELL_TIMEOUT_SEC")

    proc = subprocess.Popen(
        [shell, "-c", command],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=SHELLIE_WORKSPACE,
        env=_child_env(),
        encoding="utf-8",
        errors="replace",
    )
    _active_pgid = proc.pid
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                stdout, stderr = proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    stdout, stderr = proc.communicate()
                except Exception:
                    stdout, stderr = "", ""
            message = "Error: command timed out ({}s limit).".format(timeout)
            partial = _combine_streams(stdout, stderr)
            if partial:
                message = message + "\n" + partial
            return _truncate(message)

        body = _combine_streams(stdout, stderr)
        if not body:
            body = "Executed successfully (no output)."
        if proc.returncode:
            body = body + "\n[exit {}]".format(proc.returncode)
        return _truncate(body)
    finally:
        _active_pgid = None


def execution_environment() -> Dict[str, str]:
    """Never raises; used for the [execution environment] block and the dynamic tool description."""
    try:
        shell_path = resolve_shell()
    except ValueError as exc:
        shell_path = str(exc)
    return {
        "os": platform.system(),
        "arch": platform.machine(),
        "shell": shell_path,
        "cwd": SHELLIE_WORKSPACE,
    }
