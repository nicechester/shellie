from __future__ import annotations

import hmac
import html
import logging
import secrets
import socket
import threading
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlsplit

from src.agent.config import settings
from src.agent.telegram import client as telegram_client
from . import page as web_page

_LOGGER = logging.getLogger("shellie.web.server")

# Process-lifetime CSRF token. Regenerated only on restart (acceptable: the
# web UI has no login, so a fresh token per process is the whole point).
_CSRF = secrets.token_urlsafe(32)

# Bind address is a literal constant, never configurable (dev-plan §6.3).
_BIND_ADDR = "127.0.0.1"

_MAX_BODY_BYTES = 64 * 1024

_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}

# One-shot notice shown on the next GET / (e.g. "new bot @username" after a
# successful token change). Kept as simple module state per dev-plan §6.3.
_notice: Optional[str] = None

# Old values stashed by the POST handler right before calling settings.update()
# / settings.unset(), so the ALLOWED_USER_ID on_change hook (which only
# receives the *new* value, per SettingsStore._run_hooks) can still notify
# both the old and the new account (P3). Populated at the call site because
# server.py is the only actor allowed to change this key (P1).
_pending_old_values: Dict[str, Any] = {}


def _make_handler_class() -> type:
    """Build the request handler lazily: http.server is only imported once
    the web server actually starts (R39 RSS)."""
    import http.server

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "shellie"
        sys_version = ""
        timeout = 10

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            logging.getLogger("shellie.web.access").debug(
                "%s - " + format, self.address_string(), *args
            )

        # ---- low-level response helpers -------------------------------

        def _send_html(self, code: int, body_html: str) -> None:
            body = body_html.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            for name, value in _SECURITY_HEADERS.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except Exception:
                pass

        def _send_redirect(self, location: str) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            for name, value in _SECURITY_HEADERS.items():
                self.send_header(name, value)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _simple_page(self, message: str) -> str:
            return "<!doctype html><html><body><p>{}</p></body></html>".format(
                html.escape(message, quote=True)
            )

        def _reject(self, code: int, reason: str) -> None:
            _LOGGER.warning(
                "web request rejected code=%s reason=%s path=%s", code, reason, self.path
            )
            self._send_html(code, self._simple_page(reason))

        # ---- security gate (Host / Origin / Sec-Fetch-Site) -----------

        def _security_gate(self) -> bool:
            port = self.server.server_address[1]
            host = self.headers.get("Host")
            allowed_hosts = ("127.0.0.1:{}".format(port), "localhost:{}".format(port))
            if host not in allowed_hosts:
                self._reject(403, "Host header not allowed")
                return False

            origin = self.headers.get("Origin")
            if origin is not None and origin != "null":
                allowed_origins = (
                    "http://127.0.0.1:{}".format(port),
                    "http://localhost:{}".format(port),
                )
                if origin not in allowed_origins:
                    _LOGGER.warning(
                        "web request rejected code=403 reason=Origin not allowed "
                        "path=%s origin=%r allowed=%r",
                        self.path, origin, allowed_origins,
                    )
                    self._reject(403, "Origin not allowed")
                    return False

            sec_fetch_site = self.headers.get("Sec-Fetch-Site")
            if sec_fetch_site is not None and sec_fetch_site not in ("same-origin", "none"):
                self._reject(403, "Request not allowed")
                return False

            return True

        # ---- body parsing (POST only) ----------------------------------

        def _read_form(self) -> Optional[Dict[str, List[str]]]:
            content_type = self.headers.get("Content-Type", "")
            media_type = content_type.split(";", 1)[0].strip().lower()
            if media_type != "application/x-www-form-urlencoded":
                self._send_html(415, self._simple_page("Unsupported Content-Type"))
                return None

            length_header = self.headers.get("Content-Length")
            if length_header is None:
                self._send_html(411, self._simple_page("Content-Length required"))
                return None
            try:
                length = int(length_header)
            except ValueError:
                self._send_html(400, self._simple_page("Bad request"))
                return None
            if length < 0:
                self._send_html(400, self._simple_page("Bad request"))
                return None
            if length > _MAX_BODY_BYTES:
                self.close_connection = True
                self._send_html(413, self._simple_page("Request body too large"))
                return None

            raw = self.rfile.read(length)
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                self._send_html(400, self._simple_page("UTF-8 decoding failed"))
                return None

            try:
                form = parse_qs(text, keep_blank_values=True, max_num_fields=100)
            except ValueError:
                self._send_html(400, self._simple_page("Bad request"))
                return None
            return form

        def _check_csrf(self, form: Dict[str, List[str]]) -> bool:
            submitted = form.get("csrf", [""])[0]
            if not hmac.compare_digest(submitted, _CSRF):
                self._reject(403, "Please refresh the page and try again")
                return False
            return True

        # ---- shared page rendering --------------------------------------

        def _status_dict(self) -> Dict[str, Any]:
            try:
                info = dict(manager.status_provider() or {})
            except Exception:
                _LOGGER.exception("status_provider call failed")
                info = {}
            info["revision"] = settings.revision
            info.setdefault("started_at", manager.started_at)
            return info

        def _render(
            self,
            code: int,
            saved: Optional[int] = None,
            errors: Optional[Dict[str, str]] = None,
            submitted: Optional[Dict[str, str]] = None,
        ) -> None:
            global _notice
            notice = _notice
            if code == 200:
                _notice = None
            body = web_page.render(
                settings.rows(mask=True),
                _CSRF,
                saved=saved,
                errors=errors,
                submitted=submitted,
                notice=notice,
                status=self._status_dict(),
            )
            self._send_html(code, body)

        # ---- routes -------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802
            if not self._security_gate():
                return
            try:
                parsed = urlsplit(self.path)
                if parsed.path != "/":
                    self._send_html(404, self._simple_page("404 Not Found"))
                    return
                query = parse_qs(parsed.query, keep_blank_values=True)
                saved = None
                if "saved" in query:
                    try:
                        saved = int(query["saved"][0])
                    except (ValueError, IndexError):
                        saved = None
                self._render(200, saved=saved)
            except Exception:
                _LOGGER.exception("Exception in GET /")
                self._send_html(500, self._simple_page("500 Internal Server Error"))

        def do_POST(self) -> None:  # noqa: N802
            if not self._security_gate():
                return
            try:
                parsed = urlsplit(self.path)
                if parsed.path == "/settings":
                    self._handle_settings_post()
                elif parsed.path == "/unset":
                    self._handle_unset_post()
                else:
                    self._send_html(404, self._simple_page("404 Not Found"))
            except Exception:
                _LOGGER.exception("Exception in POST handler")
                self._send_html(500, self._simple_page("500 Internal Server Error"))

        def _method_not_allowed(self) -> None:
            if not self._security_gate():
                return
            self._send_html(405, self._simple_page("Method not allowed"))

        do_HEAD = _method_not_allowed  # noqa: N815
        do_PUT = _method_not_allowed  # noqa: N815
        do_DELETE = _method_not_allowed  # noqa: N815
        do_PATCH = _method_not_allowed  # noqa: N815
        do_OPTIONS = _method_not_allowed  # noqa: N815

        # ---- POST /settings -----------------------------------------------

        def _handle_settings_post(self) -> None:
            form = self._read_form()
            if form is None:
                return
            if not self._check_csrf(form):
                return

            current_rows = {row["key"]: row for row in settings.rows(mask=False)}
            changes: Dict[str, str] = {}
            submitted: Dict[str, str] = {}
            errors: Dict[str, str] = {}

            for key, row in current_rows.items():
                field = "v." + key
                if field not in form:
                    continue
                raw_value = form[field][0]
                if not row["secret"]:
                    submitted[key] = raw_value
                if row["secret"]:
                    if raw_value == "":
                        continue  # empty secret field means "no change"
                    changes[key] = raw_value
                    continue
                if raw_value.strip() == (row["value"] or "").strip():
                    continue  # unchanged, drop before it ever reaches the store
                changes[key] = raw_value

            if "ALLOWED_USER_ID" in changes:
                confirm_raw = form.get("confirm.ALLOWED_USER_ID", [""])[0].strip().lower()
                if confirm_raw not in ("on", "1", "true", "yes"):
                    errors["ALLOWED_USER_ID"] = "You must check the confirmation checkbox to change this"

            if errors:
                self._render(400, errors=errors, submitted=submitted)
                return

            if "ALLOWED_USER_ID" in changes:
                _pending_old_values["ALLOWED_USER_ID"] = settings.get("ALLOWED_USER_ID")

            result = settings.update(changes, actor="web")
            if not result.ok:
                self._render(400, errors=dict(result.errors), submitted=submitted)
                return

            global _notice
            if "TELEGRAM_BOT_TOKEN" in result.applied:
                info = telegram_client.get_me()
                if info.get("ok"):
                    username = info.get("result", {}).get("username") or "?"
                    _notice = "New bot: @{} — if this is a different bot, you may need to /start a conversation".format(username)

            self._send_redirect("/?saved={}".format(len(result.applied)))

        # ---- POST /unset ----------------------------------------------------

        def _handle_unset_post(self) -> None:
            form = self._read_form()
            if form is None:
                return
            if not self._check_csrf(form):
                return

            key = form.get("key", [""])[0].strip().upper()
            if not key:
                self._send_html(400, self._simple_page("key is required"))
                return

            if key == "ALLOWED_USER_ID":
                _pending_old_values["ALLOWED_USER_ID"] = settings.get("ALLOWED_USER_ID")

            result = settings.unset(key, actor="web")
            if not result.ok:
                self._render(400, errors=dict(result.errors))
                return

            self._send_redirect("/?saved={}".format(len(result.applied)))

    return Handler


class WebServerManager:
    """Owns the single-thread web listener lifecycle. One instance per
    process (module-level singleton `manager`, below)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._httpd: Any = None
        self._thread: Optional[threading.Thread] = None
        self._port: Optional[int] = None
        self._started_at: Optional[float] = None
        self._prechecks_registered = False
        # Set by __main__; returns e.g. {"polling": bool, "setup_required": bool}.
        self.status_provider: Callable[[], Dict[str, Any]] = lambda: {}

    @property
    def started_at(self) -> Optional[float]:
        return self._started_at

    @property
    def port(self) -> Optional[int]:
        return self._port

    def start(self) -> None:
        """Called once by __main__: wires prechecks/hooks, then reconciles
        the listener against current settings."""
        if not self._prechecks_registered:
            self._register_prechecks_and_hooks()
            self._prechecks_registered = True
        self.reconcile()

    def _register_prechecks_and_hooks(self) -> None:
        def check_port(new_port: Any) -> None:
            try:
                candidate = int(new_port)
            except (TypeError, ValueError):
                raise ValueError("Invalid port value")
            if self._port is not None and candidate == self._port:
                # Re-affirming the port we are already bound to (e.g. the
                # system-actor rollback after a failed port switch, below) —
                # skip the bind test, since binding a second socket while our
                # own listener is still up on that port would always fail.
                return
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((_BIND_ADDR, candidate))
            except OSError:
                raise ValueError("Port in use: {}".format(candidate))
            finally:
                probe.close()

        # Only WEB_PORT's precheck is registered here. TELEGRAM_BOT_TOKEN's
        # getMe precheck and ALLOWED_USER_ID's bot-self-id precheck are
        # registered by telegram.client.register_prechecks(); register_precheck
        # overwrites a single slot per key, so this module must never touch
        # those two keys to avoid clobbering that registration (order-independent).
        settings.register_precheck("WEB_PORT", check_port)

        settings.on_change("WEB_ENABLED", lambda _new_value: self.reconcile())
        settings.on_change("WEB_PORT", self._on_port_change)
        settings.on_change("ALLOWED_USER_ID", self._notify_allowed_user_change)

    def _notify_allowed_user_change(self, new_id: Any) -> None:
        old_id = _pending_old_values.pop("ALLOWED_USER_ID", None)

        def worker() -> None:
            if old_id is not None:
                try:
                    telegram_client.send_message(
                        old_id,
                        "⚠️ Allowed user was changed via the local web UI. Commands from this account will now be rejected.",
                        parse_mode=None,
                    )
                except Exception:
                    _LOGGER.exception("Failed to notify old ALLOWED_USER_ID account")
            if new_id is not None:
                try:
                    telegram_client.send_message(
                        new_id,
                        "✅ This account has been set as the allowed user.",
                        parse_mode=None,
                    )
                except Exception:
                    _LOGGER.exception("Failed to notify new ALLOWED_USER_ID account")

        threading.Thread(target=worker, name="web-notify-allowed-user", daemon=True).start()

    def reconcile(self) -> None:
        """Idempotent: makes the running listener match (WEB_ENABLED, WEB_PORT)."""
        with self._lock:
            enabled = bool(settings.get("WEB_ENABLED"))
            port = settings.get("WEB_PORT")
            if enabled and self._httpd is None:
                self._start_locked(port)
            elif not enabled and self._httpd is not None:
                self._stop_locked()

    def _start_locked(self, port: int) -> None:
        import http.server

        handler_cls = _make_handler_class()
        try:
            httpd = http.server.HTTPServer((_BIND_ADDR, port), handler_cls)
        except OSError as exc:
            _LOGGER.error("Web server failed to start port=%s error=%s", port, exc)
            return

        thread = threading.Thread(
            target=httpd.serve_forever,
            kwargs={"poll_interval": 0.5},
            name="web",
            daemon=True,
        )
        thread.start()
        self._httpd = httpd
        self._thread = thread
        self._port = port
        self._started_at = time.time()
        _LOGGER.info("Web server started: http://127.0.0.1:%s/", port)

    def _stop_locked(self) -> Optional[threading.Thread]:
        httpd = self._httpd
        if httpd is None:
            return None
        self._httpd = None
        self._thread = None
        self._port = None

        def shutdown_worker() -> None:
            # Delay lets an in-flight response (e.g. the redirect right
            # after a WEB_ENABLED=off save) finish sending first, and keeps
            # shutdown() off the server's own single serving thread.
            time.sleep(0.5)
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:
                _LOGGER.exception("Exception shutting down web server")

        worker_thread = threading.Thread(target=shutdown_worker, name="web-shutdown", daemon=True)
        worker_thread.start()
        _LOGGER.info("Web server shutdown requested")
        return worker_thread

    def _on_port_change(self, new_port: Any) -> None:
        if self._httpd is None:
            return  # not running; reconcile() will pick up the new port later
        try:
            new_port = int(new_port)
        except (TypeError, ValueError):
            return
        if new_port == self._port:
            # Already serving here (e.g. this is the hook re-firing for the
            # system-actor rollback after a failed switch) — nothing to do.
            return

        old_httpd = self._httpd
        old_port = self._port

        def worker() -> None:
            time.sleep(0.5)
            import http.server

            handler_cls = _make_handler_class()
            try:
                new_httpd = http.server.HTTPServer((_BIND_ADDR, new_port), handler_cls)
            except OSError as exc:
                _LOGGER.error("Failed to bind new port, rolling back: port=%s error=%s", new_port, exc)
                if old_port is not None:
                    settings.update({"WEB_PORT": old_port}, actor="system")
                return

            new_thread = threading.Thread(
                target=new_httpd.serve_forever,
                kwargs={"poll_interval": 0.5},
                name="web",
                daemon=True,
            )
            new_thread.start()
            with self._lock:
                self._httpd = new_httpd
                self._thread = new_thread
                self._port = new_port
                self._started_at = time.time()
            _LOGGER.info("Web server port switch complete: %s", new_port)

            if old_httpd is not None:
                try:
                    old_httpd.shutdown()
                    old_httpd.server_close()
                except Exception:
                    _LOGGER.exception("Exception shutting down old web server")

        threading.Thread(target=worker, name="web-port-change", daemon=True).start()

    def stop(self) -> None:
        """SIGTERM path: stop and best-effort wait for a clean shutdown."""
        with self._lock:
            worker_thread = self._stop_locked()
        if worker_thread is not None:
            worker_thread.join(timeout=3.0)


manager = WebServerManager()
