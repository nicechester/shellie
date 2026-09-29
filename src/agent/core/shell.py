from __future__ import annotations

import os
import platform
import pwd
import re
import signal
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional, Tuple

from src.agent.config import BASE_DIR, SHELLIE_HOME, SHELLIE_WORKSPACE, settings
from src.agent.config import parse_dotenv as _parse_dotenv

_active_pgid: Optional[int] = None

# Detects shell parser errors typically caused by over-escaped quotes.
_SHELL_SYNTAX_ERROR_RE = re.compile(r"unmatched|unexpected EOF|syntax error|unterminated", re.IGNORECASE)

# Hint returned to the LLM when a command fails with a shell syntax error.
_QUOTING_HINT = (
    "Hint: the shell rejected the command due to a quoting error. Write the command exactly "
    "as you would type it in a terminal; do not backslash-escape quote characters (\\' or \\\"). "
    "To embed a single quote inside a single-quoted string, close and reopen quotes ('\\''), "
    "or wrap the whole argument in double quotes instead."
)

# Shell-mode `cd` persistence (issue #18 follow-up): a private env var name
# used to pass the pwd-tracking temp file path to the child shell, and an
# EXIT trap that writes the child's final $PWD to that file. This trap is
# only ever prefixed onto shell-mode session commands (see execute_shell_in);
# LLM tool calls and one-off `!`/`/sh` commands (execute_shell) never get it.
_PWD_FILE_ENV = "__SHELLIE_PWD_FILE"
_PWD_TRAP = "trap 'printf \"%s\" \"$PWD\" > \"$" + _PWD_FILE_ENV + "\"' EXIT; "
_POSIX_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "mksh", "ash", "yash"}


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


def _syntax_ok(shell_path: str, command: str) -> bool:
    """Parse-only check (shell -n) that the command is syntactically well-formed.

    Nothing is executed; this fails open (returns True) on any unexpected error
    so a preflight problem never blocks running the original command.
    """
    try:
        proc = subprocess.run(
            [shell_path, "-n", "-c", command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
        return proc.returncode == 0
    except Exception:
        return True


def _repair_candidates(command: str) -> List[str]:
    """Candidate repairs for commands with over-escaped quote characters, in order of preference."""
    return [
        # Assume the model meant to embed a literal quote inside a single-quoted
        # string; rewrite backslash-quote as the shell idiom '\'' (close, escaped
        # quote, reopen).
        command.replace("\\'", "'\\''").replace('\\"', '"'),
        # Naive strip: just drop the stray backslash.
        command.replace("\\'", "'").replace('\\"', '"'),
    ]


def _preflight_command(shell_path: str, command: str) -> Tuple[str, Optional[str]]:
    """Detect and, if possible, auto-repair over-escaped quotes before execution.

    Returns (command_to_run, repair_note). repair_note is None unless a repaired
    candidate was substituted for the original command.
    """
    if "\\'" not in command and '\\"' not in command:
        return command, None
    if _syntax_ok(shell_path, command):
        return command, None
    for candidate in _repair_candidates(command):
        if candidate != command and _syntax_ok(shell_path, candidate):
            return candidate, "[note] Auto-repaired over-escaped quotes in the command before execution."
    return command, None


def _supports_pwd_trap(shell_path: str) -> bool:
    """Whether shell_path is a POSIX-trap-compatible shell (trap ... EXIT
    with $PWD works the same way). fish/tcsh and other non-POSIX shells are
    excluded — commands there simply run untracked (last dir, no cd persistence).
    """
    return os.path.basename(shell_path) in _POSIX_SHELLS


def _execute(command: str, cwd: str, pwd_file: Optional[str] = None) -> Tuple[str, bool]:
    """Run one command in a fresh shell rooted at cwd. Returns (output, timed_out).

    When pwd_file is given and the resolved shell supports it, the command is
    prefixed with an EXIT trap that writes the child's final $PWD to pwd_file
    (issue #18 follow-up: shell-mode `cd` persistence). This prefixing only
    ever happens for shell-mode sessions via execute_shell_in — LLM tool
    calls and one-off `!`/`/sh` commands (execute_shell) never get it, and the
    quote-repair preflight always runs on the original, un-prefixed command.
    """
    global _active_pgid

    try:
        shell = resolve_shell()
    except ValueError as exc:
        return str(exc), False

    timeout = settings.get("SHELL_TIMEOUT_SEC")

    command, repair_note = _preflight_command(shell, command)

    env = _child_env()
    to_run = command
    if pwd_file and _supports_pwd_trap(shell):
        env[_PWD_FILE_ENV] = pwd_file
        to_run = _PWD_TRAP + command

    proc = subprocess.Popen(
        [shell, "-c", to_run],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
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
            return _truncate(message), True

        body = _combine_streams(stdout, stderr)
        if not body:
            body = "Executed successfully (no output)."
        if proc.returncode and _SHELL_SYNTAX_ERROR_RE.search(stderr or ""):
            body = body + "\n" + _QUOTING_HINT
        if proc.returncode:
            body = body + "\n[exit {}]".format(proc.returncode)
        if repair_note:
            body = repair_note + "\n" + body
        return _truncate(body), False
    finally:
        _active_pgid = None


def execute_shell(command: str) -> str:
    return _execute(command, SHELLIE_WORKSPACE)[0]


def execute_shell_in(command: str, cwd: Optional[str]) -> Tuple[str, str]:
    """Run command in a shell-mode session rooted at cwd, tracking `cd` across
    calls via an EXIT-trap-written pwd file. Returns (output, new_cwd); new_cwd
    is always an existing directory (falls back to SHELLIE_WORKSPACE with a
    note if cwd no longer exists).
    """
    note = None
    if not cwd or not os.path.isdir(cwd):
        if cwd:
            note = "[note] Previous directory no longer exists; back to the workspace."
        cwd = SHELLIE_WORKSPACE

    pwd_file: Optional[str] = None
    try:
        fd, pwd_file = tempfile.mkstemp(prefix=".shellie-pwd-", dir=SHELLIE_HOME)
        os.close(fd)
    except OSError:
        pwd_file = None

    try:
        out, timed_out = _execute(command, cwd, pwd_file)
        new_cwd = cwd
        if pwd_file and not timed_out:
            try:
                with open(pwd_file, "rb") as f:
                    raw = f.read()
            except OSError:
                raw = b""
            candidate = raw.decode("utf-8", "surrogateescape")
            if candidate and os.path.isabs(candidate) and os.path.isdir(candidate):
                new_cwd = candidate
    finally:
        if pwd_file:
            try:
                os.unlink(pwd_file)
            except OSError:
                pass

    if note:
        out = note + "\n" + out
    return out, new_cwd


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
