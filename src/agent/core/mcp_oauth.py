from __future__ import annotations

# OAuth 2.0 authorization-code flow with PKCE for MCP servers (issue #25).
#
# Shellie runs headless (Raspberry Pi, Mac mini), so the redirect cannot land
# inside the app. The flow is therefore copy-paste based:
#   1. `/mcp auth <server>` builds an authorization URL (with a PKCE challenge
#      and a random state) and sends it to the chat.
#   2. The user opens it on their own device and approves access. The browser
#      then lands on `redirect_uri`, which usually fails to load -- expected.
#   3. The user copies the full address from the browser and sends
#      `/mcp finish <server> <url>`. We check the state, then exchange the code
#      at `token_url` with the PKCE verifier.
# Tokens are kept in SHELLIE_HOME/mcp_oauth.json (0600) and refreshed on demand.
# Each stored login is bound to the server's url/token_url/client_id; if those
# change in MCP_SERVERS the stored login no longer applies.
#
# Secrets (verifier, tokens, client_secret) are never logged or returned to chat.
# Locks are never held across a network call.

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

from src.agent.config import SHELLIE_HOME

_LOGGER = logging.getLogger("shellie.mcp.oauth")

TOKENS_FILE = os.path.join(SHELLIE_HOME, "mcp_oauth.json")

_PENDING_TTL_SEC = 900          # an authorization link is valid for 15 minutes
_REQUEST_TIMEOUT_SEC = 30
_REFRESH_MARGIN_SEC = 60        # refresh this long before the access token expires
_INVALID_GRANT = "invalid_grant"

_lock = threading.Lock()          # guards _pending and the token store file
_refresh_lock = threading.Lock()  # serializes token-endpoint calls for refresh
# server name -> {"state", "verifier", "expires"}; in memory only, one per server.
_pending: Dict[str, Dict[str, Any]] = {}


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def make_pkce() -> Tuple[str, str]:
    """Return (code_verifier, code_challenge) using the S256 method (RFC 7636)."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _config_fingerprint(entry: Dict[str, Any]) -> str:
    """Identify the server config a stored login was obtained for."""
    oauth = entry.get("oauth") or {}
    material = {
        "url": entry.get("url"),
        "token_url": oauth.get("token_url"),
        "client_id": oauth.get("client_id"),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()


def _post_form(url: str, fields: Dict[str, str]) -> Tuple[Optional[Dict[str, Any]], str, str]:
    """POST form-encoded fields to a token endpoint.

    Returns (data, error_code, error_text). Only the OAuth error code is ever
    surfaced, never the response body.
    """
    body = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT_SEC) as res:
            raw = res.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        code = ""
        try:
            err_data = json.loads(exc.read().decode("utf-8", errors="replace") or "{}")
            if isinstance(err_data, dict) and isinstance(err_data.get("error"), str):
                code = err_data["error"]
        except (OSError, ValueError):
            pass
        text = "token endpoint returned HTTP {}".format(exc.code)
        if code:
            text += " ({})".format(code)
        return None, code, text
    except (urllib.error.URLError, OSError, ValueError):
        return None, "", "token endpoint unreachable"

    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return None, "", "token endpoint returned invalid JSON"
    if not isinstance(data, dict) or not isinstance(data.get("access_token"), str):
        return None, "", "token endpoint response has no access_token"
    return data, "", ""


def _quarantine_store(reason: str) -> None:
    """Move a corrupt token file aside so the next save does not destroy it."""
    target = "{}.corrupt-{}".format(TOKENS_FILE, int(time.time()))
    try:
        os.replace(TOKENS_FILE, target)
        _LOGGER.warning("MCP OAuth token store was %s; moved aside to %s", reason, target)
    except OSError:
        _LOGGER.warning("MCP OAuth token store was %s; could not move it aside", reason)


def _load_store() -> Dict[str, Dict[str, Any]]:
    """Read the token store. Caller must hold _lock."""
    try:
        with open(TOKENS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except ValueError:
        _quarantine_store("corrupt")
        return {}
    except OSError:
        return {}
    if not isinstance(data, dict):
        _quarantine_store("not an object")
        return {}
    return data


def _save_store(store: Dict[str, Dict[str, Any]]) -> None:
    """Atomically write the token store with 0600 permissions. Caller must hold _lock."""
    os.makedirs(SHELLIE_HOME, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=SHELLIE_HOME, prefix=".mcp_oauth.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(store, fh)
        os.replace(tmp_path, TOKENS_FILE)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _store_tokens(
    name: str, fingerprint: str, data: Dict[str, Any], previous: Optional[Dict[str, Any]]
) -> str:
    """Persist a token response and return the access token.

    Keeps the old refresh_token if the server does not return a new one.
    Caller must hold _lock.
    """
    refresh = data.get("refresh_token")
    if not isinstance(refresh, str) and previous is not None:
        refresh = previous.get("refresh_token")
    expires_in = data.get("expires_in")
    record: Dict[str, Any] = {
        "access_token": data["access_token"],
        "refresh_token": refresh if isinstance(refresh, str) else None,
        "expires_at": (time.time() + float(expires_in))
        if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool)
        else None,
        "fingerprint": fingerprint,
    }
    store = _load_store()
    store[name] = record
    _save_store(store)
    return data["access_token"]


def _client_fields(oauth: Dict[str, Any]) -> Dict[str, str]:
    fields = {"client_id": oauth["client_id"]}
    secret = oauth.get("client_secret")
    if isinstance(secret, str) and secret:
        fields["client_secret"] = secret
    return fields


def _authorize_url(oauth: Dict[str, Any], state: str, challenge: str) -> str:
    params: Dict[str, str] = {
        "response_type": "code",
        "client_id": oauth["client_id"],
        "redirect_uri": oauth["redirect_uri"],
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        # Google issues a refresh token only for offline access, and only when
        # consent is shown. Other providers ignore these parameters.
        "access_type": "offline",
        "prompt": "consent",
    }
    scope = oauth.get("scope")
    if isinstance(scope, str) and scope:
        params["scope"] = scope
    parts = urllib.parse.urlsplit(oauth["authorization_url"])
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query.extend(params.items())
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def begin(entry: Dict[str, Any]) -> Tuple[bool, str]:
    """Start a login for an MCP server. Returns (ok, authorization_url or error)."""
    name = entry["name"]
    oauth = entry.get("oauth")
    if not isinstance(oauth, dict):
        return False, "server {} has no oauth config".format(name)
    verifier, challenge = make_pkce()
    state = _b64url(secrets.token_bytes(16))
    now = time.time()
    with _lock:
        for key in [k for k, v in _pending.items() if v["expires"] < now]:
            del _pending[key]
        _pending[name] = {"state": state, "verifier": verifier, "expires": now + _PENDING_TTL_SEC}
    return True, _authorize_url(oauth, state, challenge)


def finish(entry: Dict[str, Any], pasted_url: str) -> Tuple[bool, str]:
    """Complete a login from the redirect URL the user pasted back."""
    name = entry["name"]
    oauth = entry.get("oauth")
    if not isinstance(oauth, dict):
        return False, "server {} has no oauth config".format(name)

    with _lock:
        pending = _pending.get(name)
    if pending is None or pending["expires"] < time.time():
        return False, "No pending login for {}. Start one with /mcp auth {}.".format(name, name)

    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(pasted_url.strip()).query))
    if "error" in query:
        return False, "Authorization was denied ({}).".format(query["error"])
    # A mistyped or stale paste keeps the pending login, so the user can retry.
    if not hmac.compare_digest(query.get("state", ""), pending["state"]):
        return False, "State does not match this login. Paste the address from the approval page, or start again with /mcp auth {}.".format(name)
    code = query.get("code", "")
    if not code:
        return False, "No authorization code found in the pasted URL."

    # Authorization codes are single-use: consume the pending login before the exchange.
    with _lock:
        _pending.pop(name, None)

    fields = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": oauth["redirect_uri"],
        "code_verifier": pending["verifier"],
    }
    fields.update(_client_fields(oauth))
    data, _code, err_text = _post_form(oauth["token_url"], fields)
    if data is None:
        return False, "Token exchange failed: {}.".format(err_text)

    with _lock:
        _store_tokens(name, _config_fingerprint(entry), data, None)
    return True, "Connected to {}.".format(name)


def _valid_record(record: Any, fingerprint: str) -> Optional[Dict[str, Any]]:
    if not isinstance(record, dict):
        return None
    if record.get("fingerprint") != fingerprint:
        return None
    return record


def access_token(entry: Dict[str, Any]) -> Optional[str]:
    """Return a valid access token for the server, refreshing it if needed.

    Returns None when the server has no oauth config, no stored login, or the
    stored login belongs to a different server config. Never raises.
    """
    oauth = entry.get("oauth")
    if not isinstance(oauth, dict):
        return None
    name = entry["name"]
    fingerprint = _config_fingerprint(entry)
    try:
        with _lock:
            record = _valid_record(_load_store().get(name), fingerprint)
        if record is None or not isinstance(record.get("access_token"), str):
            return None
        expires_at = record.get("expires_at")
        if not isinstance(expires_at, (int, float)) or expires_at - _REFRESH_MARGIN_SEC > time.time():
            return record["access_token"]

        refresh = record.get("refresh_token")
        if not isinstance(refresh, str):
            _LOGGER.warning("MCP server %s: access token expired and no refresh token; run /mcp auth", name)
            return None

        # Only one refresh at a time, and no _lock during the HTTP call.
        with _refresh_lock:
            # Another thread may have refreshed while we waited.
            with _lock:
                current = _valid_record(_load_store().get(name), fingerprint)
            if current is not None and current.get("access_token") != record["access_token"]:
                return current.get("access_token")

            fields = {"grant_type": "refresh_token", "refresh_token": refresh}
            fields.update(_client_fields(oauth))
            data, code, err_text = _post_form(oauth["token_url"], fields)
            if data is None:
                if code == _INVALID_GRANT:
                    # The refresh token is dead; forget it so the user is asked to log in again.
                    with _lock:
                        store = _load_store()
                        store.pop(name, None)
                        _save_store(store)
                    _LOGGER.warning("MCP server %s: login expired; run /mcp auth %s", name, name)
                else:
                    _LOGGER.warning("MCP server %s: token refresh failed: %s", name, err_text)
                return None
            with _lock:
                return _store_tokens(name, fingerprint, data, record)
    except Exception:
        _LOGGER.exception("MCP server %s: unexpected error reading OAuth token", name)
        return None


def is_connected(entry: Dict[str, Any]) -> bool:
    """True if a login is stored for this server config (may still need a refresh)."""
    with _lock:
        record = _valid_record(_load_store().get(entry["name"]), _config_fingerprint(entry))
    return record is not None and (
        isinstance(record.get("access_token"), str) or isinstance(record.get("refresh_token"), str)
    )


def logout(name: str) -> bool:
    """Forget the stored login for a server. Returns True if one existed."""
    with _lock:
        _pending.pop(name, None)
        store = _load_store()
        if name not in store:
            return False
        del store[name]
        _save_store(store)
    return True
