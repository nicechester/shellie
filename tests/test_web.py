from __future__ import annotations

import http.client
import http.server
import os
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.parse import urlencode

from src.agent.settings import SettingsStore
from src.agent.web import server as web_server

# Distinct, non-repeating secret values so a "raw secret leaked" assertion
# cannot pass by accident because a masked fragment happens to look like
# the whole value (e.g. an all-"A" secret would still contain "AAAA").
_TOKEN_REST = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef"  # 32 chars, matches token regex
_GEMINI_KEY = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmn"  # 40 chars


class TestWebServer(unittest.TestCase):
    """Starts the real server in-process on 127.0.0.1:0 against a fresh,
    injected SettingsStore (never the real .env/settings.json) per 5.1."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        settings_path = os.path.join(self._tmpdir.name, "settings.json")

        self.token = "123456789:" + _TOKEN_REST
        self.gemini_key = _GEMINI_KEY
        self.allowed_user_id = 444444444

        self.store = SettingsStore()
        missing = self.store.load(
            env={
                "TELEGRAM_BOT_TOKEN": self.token,
                "GEMINI_API_KEY": self.gemini_key,
                "ALLOWED_USER_ID": str(self.allowed_user_id),
            },
            path=settings_path,
        )
        self.assertEqual(missing, [])

        # Wire the same on_change hook WebServerManager registers for
        # ALLOWED_USER_ID (old/new-account notification), without touching
        # WEB_PORT's precheck registration -- that mutates a SettingSpec
        # object shared process-wide via CATALOG, and would otherwise leak
        # into unrelated tests (see also test_settings.py TestPrecheck).
        self.store.on_change(
            "ALLOWED_USER_ID", web_server.manager._notify_allowed_user_change
        )

        self._settings_patcher = mock.patch.object(web_server, "settings", self.store)
        self._settings_patcher.start()
        self.addCleanup(self._settings_patcher.stop)

        get_me_patcher = mock.patch.object(
            web_server.telegram_client,
            "get_me",
            return_value={"ok": True, "result": {"username": "testbot", "id": 1}},
        )
        self.mock_get_me = get_me_patcher.start()
        self.addCleanup(get_me_patcher.stop)

        send_message_patcher = mock.patch.object(
            web_server.telegram_client, "send_message", return_value={"ok": True}
        )
        self.mock_send_message = send_message_patcher.start()
        self.addCleanup(send_message_patcher.stop)

        handler_cls = web_server._make_handler_class()
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.httpd.server_address[1]
        self.server_thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.server_thread.start()
        self.addCleanup(self._shutdown_server)
        self.addCleanup(self._reset_module_globals)

    def _shutdown_server(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.server_thread.join(timeout=2.0)

    def _reset_module_globals(self) -> None:
        # `_notice` and `_pending_old_values` are process-wide module
        # globals in server.py (not per-store state) -- reset them so one
        # test's POST cannot bleed a banner/notify-state into the next.
        web_server._notice = None
        web_server._pending_old_values.clear()

    # ---- request helpers ------------------------------------------------

    def _request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            data = resp.read()
            return resp, data
        finally:
            conn.close()

    def _post_settings(self, fields, headers=None):
        body = urlencode(fields)
        merged_headers = {"Content-Type": "application/x-www-form-urlencoded"}
        merged_headers.update(headers or {})
        return self._request("POST", "/settings", body=body, headers=merged_headers)

    def _post_unset(self, key, csrf=None):
        body = urlencode({"csrf": csrf if csrf is not None else web_server._CSRF, "key": key})
        return self._request(
            "POST",
            "/unset",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    def _wait_until(self, predicate, timeout=2.0, interval=0.05):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(interval)
        self.fail("condition was not met within timeout")

    # ---- GET / ------------------------------------------------------------

    def test_get_root_ok_with_masked_secrets_csrf_and_security_headers(self):
        resp, data = self._request("GET", "/")
        self.assertEqual(resp.status, 200)
        body = data.decode("utf-8")

        self.assertNotIn(self.token, body)
        self.assertNotIn(self.gemini_key, body)
        self.assertIn(web_server._CSRF, body)

        self.assertEqual(resp.getheader("X-Frame-Options"), "DENY")
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")
        csp = resp.getheader("Content-Security-Policy")
        self.assertIsNotNone(csp)
        self.assertIn("default-src 'none'", csp)

    def test_get_root_wrong_host_rejected(self):
        resp, _ = self._request(
            "GET", "/", headers={"Host": "evil.com:{}".format(self.port)}
        )
        self.assertEqual(resp.status, 403)

    def test_get_root_cross_site_origin_rejected(self):
        resp, _ = self._request(
            "GET", "/", headers={"Origin": "http://evil.example.com"}
        )
        self.assertEqual(resp.status, 403)

    def test_get_root_cross_site_sec_fetch_site_rejected(self):
        resp, _ = self._request(
            "GET", "/", headers={"Sec-Fetch-Site": "cross-site"}
        )
        self.assertEqual(resp.status, 403)

    # ---- POST /settings: CSRF / Host gates ---------------------------------

    def test_post_settings_without_csrf_rejected(self):
        body = urlencode({"v.SHELL_TIMEOUT_SEC": "100"})
        resp, _ = self._request(
            "POST",
            "/settings",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(resp.status, 403)
        self.assertEqual(self.store.get("SHELL_TIMEOUT_SEC"), 45)

    def test_post_settings_wrong_csrf_rejected(self):
        resp, _ = self._post_settings(
            {"csrf": "not-the-real-token", "v.SHELL_TIMEOUT_SEC": "100"}
        )
        self.assertEqual(resp.status, 403)
        self.assertEqual(self.store.get("SHELL_TIMEOUT_SEC"), 45)

    # ---- POST /settings: valid / partially invalid --------------------------

    def test_post_settings_valid_change_redirects_and_persists(self):
        resp, _ = self._post_settings(
            {"csrf": web_server._CSRF, "v.SHELL_TIMEOUT_SEC": "100"}
        )
        self.assertEqual(resp.status, 303)
        self.assertEqual(resp.getheader("Location"), "/?saved=1")
        self.assertEqual(self.store.get("SHELL_TIMEOUT_SEC"), 100)

    def test_post_settings_partial_invalid_applies_neither_field(self):
        resp, _ = self._post_settings(
            {
                "csrf": web_server._CSRF,
                "v.SHELL_TIMEOUT_SEC": "9999",  # out of range (1..600)
                "v.FC_MAX_LOOPS": "3",  # otherwise valid change
            }
        )
        self.assertEqual(resp.status, 400)
        self.assertEqual(self.store.get("SHELL_TIMEOUT_SEC"), 45)
        self.assertEqual(self.store.get("FC_MAX_LOOPS"), 5)

    # ---- POST /settings: body handling -------------------------------------

    def test_post_settings_wrong_content_type_rejected_415(self):
        body = urlencode({"csrf": web_server._CSRF, "v.SHELL_TIMEOUT_SEC": "100"})
        resp, _ = self._request(
            "POST", "/settings", body=body, headers={"Content-Type": "text/plain"}
        )
        self.assertEqual(resp.status, 415)

    def test_post_settings_missing_content_length_rejected_411(self):
        body = urlencode(
            {"csrf": web_server._CSRF, "v.SHELL_TIMEOUT_SEC": "100"}
        ).encode("utf-8")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            # HTTPConnection.request()/_send_request() always computes and
            # sends a Content-Length header for a str/bytes body, so a
            # normal request can never omit it. Drive the low-level
            # putrequest/putheader/endheaders API directly instead, which
            # does not add Content-Length on its own, so the server truly
            # receives none and must answer 411.
            conn.putrequest("POST", "/settings", skip_accept_encoding=True)
            conn.putheader("Content-Type", "application/x-www-form-urlencoded")
            conn.endheaders(message_body=body)
            resp = conn.getresponse()
            self.assertEqual(resp.status, 411)
            resp.read()
        finally:
            conn.close()

    def test_post_settings_body_too_large_rejected_413(self):
        body = urlencode({"csrf": web_server._CSRF, "junk": "x" * (70 * 1024)})
        resp, _ = self._request(
            "POST",
            "/settings",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(resp.status, 413)

    # ---- POST /settings: ALLOWED_USER_ID confirm gate ----------------------

    def test_post_allowed_user_id_without_confirm_rejected_and_unchanged(self):
        resp, _ = self._post_settings(
            {"csrf": web_server._CSRF, "v.ALLOWED_USER_ID": "999999999"}
        )
        self.assertEqual(resp.status, 400)
        self.assertEqual(self.store.get("ALLOWED_USER_ID"), self.allowed_user_id)
        self.mock_send_message.assert_not_called()

    def test_post_allowed_user_id_with_confirm_applies_and_notifies_both_accounts(self):
        new_id = 999999999
        resp, _ = self._post_settings(
            {
                "csrf": web_server._CSRF,
                "v.ALLOWED_USER_ID": str(new_id),
                "confirm.ALLOWED_USER_ID": "on",
            }
        )
        self.assertEqual(resp.status, 303)
        self.assertEqual(self.store.get("ALLOWED_USER_ID"), new_id)

        # Notification fires on a background thread; poll briefly instead
        # of a single long sleep.
        self._wait_until(lambda: self.mock_send_message.call_count >= 2)

        notified_ids = {call.args[0] for call in self.mock_send_message.call_args_list}
        self.assertEqual(notified_ids, {self.allowed_user_id, new_id})

    # ---- POST /unset --------------------------------------------------------

    def test_post_unset_removes_override_and_redirects(self):
        resp, _ = self._post_settings(
            {"csrf": web_server._CSRF, "v.SHELL_TIMEOUT_SEC": "111"}
        )
        self.assertEqual(resp.status, 303)
        self.assertEqual(self.store.get("SHELL_TIMEOUT_SEC"), 111)

        resp2, _ = self._post_unset("SHELL_TIMEOUT_SEC")
        self.assertEqual(resp2.status, 303)
        self.assertEqual(self.store.get("SHELL_TIMEOUT_SEC"), 45)


if __name__ == "__main__":
    unittest.main()
