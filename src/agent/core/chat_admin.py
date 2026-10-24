from __future__ import annotations

# Prompt-driven changes to user skills and MCP servers.
#
# Skills: the model writes or removes files in SKILLS_DIR directly. This is the
# same trust as execute_shell, which the model already has. Built-in skills are
# never touched. Every change is announced in chat by the handler (🧩).
#
# MCP servers: the model can only *propose* an add or remove. Nothing changes
# until the user taps Confirm on the proposal in Telegram. Proposals cannot carry
# headers or client_secret, so no secret is ever written from chat: those stay in
# the local web UI (MCP_SERVERS is web-only for secrets, D11).

import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from src.agent.config import REPO_SKILLS_DIR, SKILLS_DIR, settings
from src.agent.settings import _parse_mcp_servers

_LOGGER = logging.getLogger("shellie.chat_admin")

_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}\.(md|py)$")
_SKILL_MAX_CHARS = 20000

_SERVER_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")
_URL_RE = re.compile(r"^https?://")
_PROPOSAL_TTL_SEC = 600
_OAUTH_ALLOWED = ("authorization_url", "token_url", "redirect_uri", "client_id", "scope")

_plock = threading.Lock()
# proposal id -> {"action", "name", "entry" (add only), "created"}
_proposals: Dict[str, Dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

def skill_declaration() -> Dict[str, Any]:
    return {
        "name": "manage_skill",
        "description": (
            "Create, update, list or remove user skills (files in the skills folder). "
            "Use only when the user explicitly asks to add, change or delete a skill. "
            "Skills are .md procedures that you read and follow step by step. New skills "
            "must be .md; put any code in a helper script that the procedure calls with "
            "execute_shell. Existing .py skills can be updated but not created. Names: "
            "lowercase letters, digits, - and _, ending in .md (or .py for existing skills). "
            "Built-in skills cannot be changed. Keep content short "
            "and concrete, and never store secrets or tokens in a skill."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "list, create, update or remove"},
                "name": {"type": "STRING", "description": "Skill file name, e.g. weather.md"},
                "content": {"type": "STRING", "description": "Full file content (required for create and update)"},
            },
            "required": ["action"],
        },
    }


def _builtin_names() -> List[str]:
    try:
        return os.listdir(REPO_SKILLS_DIR)
    except OSError:
        return []


def _write_skill(path: str, content: str) -> None:
    os.makedirs(SKILLS_DIR, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=SKILLS_DIR, prefix=".skill.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def skill_run(args: Dict[str, Any]) -> str:
    action = str(args.get("action", "")).strip().lower()

    if action == "list":
        try:
            names = sorted(
                n for n in os.listdir(SKILLS_DIR)
                if _SKILL_NAME_RE.match(n)
            )
        except OSError:
            names = []
        return "OK: user skills: " + (", ".join(names) if names else "none")

    name = str(args.get("name", "")).strip().lower()
    if not _SKILL_NAME_RE.match(name):
        return "Error: name must be lowercase letters, digits, - or _, ending in .md or .py"
    if name in _builtin_names():
        return "Error: '{}' is a built-in skill and cannot be changed. Choose another name.".format(name)
    path = os.path.join(SKILLS_DIR, name)
    exists = os.path.isfile(path)

    if action == "create":
        if name.endswith(".py"):
            return "Error: new skills must be .md prompt files; put any code in a helper script the .md calls"
        if exists:
            return "Error: skill '{}' already exists. Use update.".format(name)
        content = _checked_content(args)
        if content is None:
            return "Error: content is required and must be at most {} characters.".format(_SKILL_MAX_CHARS)
        _write_skill(path, content)
        return "OK: created skill {}".format(name)

    if action == "update":
        if not exists:
            return "Error: skill '{}' does not exist. Use create.".format(name)
        content = _checked_content(args)
        if content is None:
            return "Error: content is required and must be at most {} characters.".format(_SKILL_MAX_CHARS)
        _write_skill(path, content)
        return "OK: updated skill {}".format(name)

    if action == "remove":
        if not exists:
            return "Error: skill '{}' does not exist.".format(name)
        os.unlink(path)
        return "OK: removed skill {}".format(name)

    return "Error: unknown action '{}'. Use list, create, update or remove.".format(action)


def _checked_content(args: Dict[str, Any]) -> Optional[str]:
    content = args.get("content")
    if not isinstance(content, str) or not content.strip() or len(content) > _SKILL_MAX_CHARS:
        return None
    return content


# ---------------------------------------------------------------------------
# MCP servers: proposals that the user confirms in Telegram
# ---------------------------------------------------------------------------

def mcp_declaration() -> Dict[str, Any]:
    return {
        "name": "propose_mcp_server",
        "description": (
            "Propose adding or removing an MCP server. Nothing changes until the user taps "
            "Confirm in the chat, so say that clearly and never claim the server is active. "
            "Only propose when the user explicitly asks. For OAuth servers pass the oauth "
            "object without client_secret; the user then logs in through a link. Servers that "
            "need a static header or any secret must be added in the local web settings, not here."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "add or remove"},
                "name": {"type": "STRING", "description": "Server name: letters, digits, - and _, max 32"},
                "url": {"type": "STRING", "description": "Streamable HTTP endpoint (add only)"},
                "oauth": {
                    "type": "OBJECT",
                    "description": "Optional OAuth settings (add only): authorization_url, token_url, redirect_uri, client_id, scope",
                    "properties": {
                        "authorization_url": {"type": "STRING"},
                        "token_url": {"type": "STRING"},
                        "redirect_uri": {"type": "STRING"},
                        "client_id": {"type": "STRING"},
                        "scope": {"type": "STRING"},
                    },
                },
            },
            "required": ["action", "name"],
        },
    }


def _current_servers() -> List[Dict[str, Any]]:
    raw = settings.get("MCP_SERVERS")
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def _new_proposal(action: str, name: str, entry: Optional[Dict[str, Any]]) -> str:
    pid = secrets.token_hex(4)
    now = time.time()
    with _plock:
        for key in [k for k, v in _proposals.items() if v["created"] + _PROPOSAL_TTL_SEC < now]:
            del _proposals[key]
        _proposals[pid] = {"action": action, "name": name, "entry": entry, "created": now}
    return pid


def mcp_run(args: Dict[str, Any]) -> str:
    action = str(args.get("action", "")).strip().lower()
    name = str(args.get("name", "")).strip()
    if not _SERVER_NAME_RE.match(name):
        return "Error: name must be letters, digits, - or _ (max 32 characters)."
    servers = _current_servers()
    known = [s for s in servers if isinstance(s, dict) and s.get("name") == name]

    if action == "remove":
        if not known:
            return "Error: no MCP server named '{}' is configured.".format(name)
        pid = _new_proposal("remove", name, None)
        return "OK: proposal {} to remove MCP server '{}' is waiting for the user to confirm in the chat. Nothing has changed yet.".format(pid, name)

    if action != "add":
        return "Error: action must be add or remove."
    if known:
        return "Error: an MCP server named '{}' already exists.".format(name)

    url = str(args.get("url", "")).strip()
    if not _URL_RE.match(url):
        return "Error: url must start with http:// or https://"
    entry: Dict[str, Any] = {"name": name, "url": url}

    oauth = args.get("oauth")
    if oauth is not None:
        if not isinstance(oauth, dict) or set(oauth) - set(_OAUTH_ALLOWED):
            return "Error: oauth may only contain {} (no client_secret from chat).".format(", ".join(_OAUTH_ALLOWED))
        entry["oauth"] = oauth

    # Validate exactly as the settings parser will when the user confirms.
    try:
        _parse_mcp_servers(json.dumps(servers + [entry]))
    except ValueError as exc:
        return "Error: invalid server config: {}".format(exc)

    pid = _new_proposal("add", name, entry)
    return "OK: proposal {} to add MCP server '{}' is waiting for the user to confirm in the chat. Nothing has changed yet.".format(pid, name)


def describe(pid: str) -> Optional[str]:
    """Plain-text summary of a pending proposal, or None if unknown."""
    with _plock:
        prop = _proposals.get(pid)
    if prop is None:
        return None
    if prop["action"] == "remove":
        return "Remove MCP server '{}'?".format(prop["name"])
    entry = prop["entry"]
    line = "Add MCP server '{}' at {}?".format(entry["name"], entry["url"])
    if "oauth" in entry:
        line += "\nIt uses OAuth: you will get a login link after confirming."
    return line


def confirm(pid: str) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """Apply a pending proposal. Returns (ok, message, added_entry_or_None)."""
    with _plock:
        prop = _proposals.pop(pid, None)
    if prop is None:
        return False, "This request has expired or was already handled.", None
    if prop["created"] + _PROPOSAL_TTL_SEC < time.time():
        return False, "This request has expired. Ask again.", None

    name = prop["name"]
    servers = _current_servers()
    if prop["action"] == "remove":
        kept = [s for s in servers if not (isinstance(s, dict) and s.get("name") == name)]
        if len(kept) == len(servers):
            return False, "MCP server '{}' is no longer configured.".format(name), None
        new_servers = kept
    else:
        if any(isinstance(s, dict) and s.get("name") == name for s in servers):
            return False, "An MCP server named '{}' already exists.".format(name), None
        new_servers = servers + [prop["entry"]]

    # actor="chat": the user confirmed in Telegram. Secrets stay web-only, and this
    # path never writes headers or client_secret.
    result = settings.update({"MCP_SERVERS": json.dumps(new_servers)}, actor="chat")
    if not result.ok:
        message = "; ".join("{}: {}".format(k, v) for k, v in result.errors.items()) or "update failed"
        return False, "Could not save: " + message, None
    if prop["action"] == "remove":
        return True, "Removed MCP server '{}'.".format(name), None
    return True, "Added MCP server '{}'.".format(name), prop["entry"]


def discard(pid: str) -> bool:
    with _plock:
        return _proposals.pop(pid, None) is not None
