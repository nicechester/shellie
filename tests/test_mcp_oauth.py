from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import time
import unittest
import urllib.parse
from unittest import mock

from src.agent.core import mcp_oauth
from src.agent.settings import _valid_mcp_oauth

_OAUTH = {
    "authorization_url": "https://auth.example.com/authorize",
    "token_url": "https://auth.example.com/token",
    "redirect_uri": "http://localhost:8787/callback",
    "client_id": "shellie-client",
}


def _entry(oauth=None, url="https://mcp.example.com/"):
    entry = {"name": "bank", "url": url}
    entry["oauth"] = dict(_OAUTH) if oauth is None else oauth
    return entry


class _StoreMixin:
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._patches = [
            mock.patch.object(mcp_oauth, "SHELLIE_HOME", self._tmp),
            mock.patch.object(mcp_oauth, "TOKENS_FILE", os.path.join(self._tmp, "mcp_oauth.json")),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write_record(self, record, entry=None):
        entry = entry or _entry()
        record = dict(record, fingerprint=mcp_oauth._config_fingerprint(entry))
        with open(mcp_oauth.TOKENS_FILE, "w", encoding="utf-8") as fh:
            json.dump({"bank": record}, fh)


class PkceTests(unittest.TestCase):
    def test_s256_matches_rfc7636_vector(self):
        # RFC 7636 appendix B test vector.
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        expected = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
        challenge = mcp_oauth._b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        self.assertEqual(challenge, expected)

    def test_make_pkce_returns_fresh_pair(self):
        v1, c1 = mcp_oauth.make_pkce()
        v2, _ = mcp_oauth.make_pkce()
        self.assertNotEqual(v1, v2)
        self.assertGreaterEqual(len(v1), 43)
        self.assertNotIn("=", c1)


class BeginTests(_StoreMixin, unittest.TestCase):
    def test_begin_builds_authorize_url_with_pkce_and_state(self):
        ok, url = mcp_oauth.begin(_entry())
        self.assertTrue(ok)
        parts = urllib.parse.urlsplit(url)
        self.assertEqual(parts.netloc, "auth.example.com")
        query = dict(urllib.parse.parse_qsl(parts.query))
        self.assertEqual(query["response_type"], "code")
        self.assertEqual(query["client_id"], "shellie-client")
        self.assertEqual(query["redirect_uri"], "http://localhost:8787/callback")
        self.assertEqual(query["code_challenge_method"], "S256")
        self.assertEqual(query["access_type"], "offline")
        self.assertEqual(query["prompt"], "consent")
        self.assertIn("state", query)
        self.assertIn("code_challenge", query)
        self.assertNotIn("scope", query)

    def test_begin_without_oauth_config_fails(self):
        entry = {"name": "bank", "url": "https://mcp.example.com/"}
        ok, _ = mcp_oauth.begin(entry)
        self.assertFalse(ok)


class FinishTests(_StoreMixin, unittest.TestCase):
    def _start(self):
        _, url = mcp_oauth.begin(_entry())
        return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))

    def test_finish_exchanges_code_and_stores_tokens(self):
        params = self._start()
        pasted = "http://localhost:8787/callback?code=AUTHCODE&state=" + params["state"]
        token_reply = {"access_token": "AT-1", "refresh_token": "RT-1", "expires_in": 3600}
        with mock.patch.object(mcp_oauth, "_post_form", return_value=(token_reply, "", "")) as post:
            ok, msg = mcp_oauth.finish(_entry(), pasted)
        self.assertTrue(ok, msg)
        fields = post.call_args[0][1]
        self.assertEqual(fields["grant_type"], "authorization_code")
        self.assertEqual(fields["code"], "AUTHCODE")
        self.assertIn("code_verifier", fields)
        self.assertTrue(mcp_oauth.is_connected(_entry()))
        with open(mcp_oauth.TOKENS_FILE, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["bank"]["refresh_token"], "RT-1")

    def test_state_mismatch_is_rejected_and_keeps_pending_login(self):
        params = self._start()
        forged = "http://localhost:8787/callback?code=AUTHCODE&state=forged"
        with mock.patch.object(mcp_oauth, "_post_form") as post:
            ok, _ = mcp_oauth.finish(_entry(), forged)
        self.assertFalse(ok)
        post.assert_not_called()
        # The correct paste still works afterwards.
        good = "http://localhost:8787/callback?code=AUTHCODE&state=" + params["state"]
        with mock.patch.object(mcp_oauth, "_post_form", return_value=({"access_token": "AT"}, "", "")):
            ok, msg = mcp_oauth.finish(_entry(), good)
        self.assertTrue(ok, msg)

    def test_pending_login_is_single_use(self):
        params = self._start()
        pasted = "http://localhost:8787/callback?code=AUTHCODE&state=" + params["state"]
        with mock.patch.object(mcp_oauth, "_post_form", return_value=({"access_token": "AT"}, "", "")):
            self.assertTrue(mcp_oauth.finish(_entry(), pasted)[0])
        ok, msg = mcp_oauth.finish(_entry(), pasted)
        self.assertFalse(ok)
        self.assertIn("No pending login", msg)

    def test_finish_without_pending_login_fails(self):
        ok, msg = mcp_oauth.finish(_entry(), "http://localhost:8787/callback?code=x&state=y")
        self.assertFalse(ok)
        self.assertIn("No pending login", msg)

    def test_provider_error_is_reported(self):
        params = self._start()
        pasted = "http://localhost:8787/callback?error=access_denied&state=" + params["state"]
        ok, msg = mcp_oauth.finish(_entry(), pasted)
        self.assertFalse(ok)
        self.assertIn("access_denied", msg)

    def test_stored_file_is_0600(self):
        params = self._start()
        pasted = "http://localhost:8787/callback?code=AUTHCODE&state=" + params["state"]
        with mock.patch.object(mcp_oauth, "_post_form", return_value=({"access_token": "AT"}, "", "")):
            mcp_oauth.finish(_entry(), pasted)
        mode = stat.S_IMODE(os.stat(mcp_oauth.TOKENS_FILE).st_mode)
        self.assertEqual(mode, 0o600)


class AccessTokenTests(_StoreMixin, unittest.TestCase):
    def test_returns_unexpired_token_without_network(self):
        self._write_record({"access_token": "AT", "refresh_token": "RT", "expires_at": time.time() + 3600})
        with mock.patch.object(mcp_oauth, "_post_form") as post:
            self.assertEqual(mcp_oauth.access_token(_entry()), "AT")
        post.assert_not_called()

    def test_refreshes_expired_token_and_keeps_refresh_token(self):
        self._write_record({"access_token": "OLD", "refresh_token": "RT", "expires_at": time.time() - 5})
        with mock.patch.object(mcp_oauth, "_post_form", return_value=({"access_token": "NEW"}, "", "")) as post:
            self.assertEqual(mcp_oauth.access_token(_entry()), "NEW")
        self.assertEqual(post.call_args[0][1]["grant_type"], "refresh_token")
        with open(mcp_oauth.TOKENS_FILE, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["bank"]["refresh_token"], "RT")

    def test_invalid_grant_forgets_the_login(self):
        self._write_record({"access_token": "OLD", "refresh_token": "RT", "expires_at": time.time() - 5})
        err = ("", "token endpoint returned HTTP 400 (invalid_grant)")
        with mock.patch.object(mcp_oauth, "_post_form", return_value=(None, "invalid_grant", err[1])):
            self.assertIsNone(mcp_oauth.access_token(_entry()))
        self.assertFalse(mcp_oauth.is_connected(_entry()))

    def test_other_refresh_errors_keep_the_login(self):
        self._write_record({"access_token": "OLD", "refresh_token": "RT", "expires_at": time.time() - 5})
        with mock.patch.object(mcp_oauth, "_post_form", return_value=(None, "", "token endpoint unreachable")):
            self.assertIsNone(mcp_oauth.access_token(_entry()))
        self.assertTrue(mcp_oauth.is_connected(_entry()))

    def test_login_for_changed_server_config_is_not_used(self):
        self._write_record({"access_token": "AT", "refresh_token": "RT", "expires_at": time.time() + 3600})
        moved = _entry(url="https://evil.example.com/")
        with mock.patch.object(mcp_oauth, "_post_form") as post:
            self.assertIsNone(mcp_oauth.access_token(moved))
        post.assert_not_called()
        self.assertFalse(mcp_oauth.is_connected(moved))

    def test_no_stored_login_returns_none(self):
        self.assertIsNone(mcp_oauth.access_token(_entry()))

    def test_entry_without_oauth_returns_none(self):
        entry = {"name": "bank", "url": "https://mcp.example.com/"}
        self.assertIsNone(mcp_oauth.access_token(entry))

    def test_logout_removes_login(self):
        self._write_record({"access_token": "AT", "refresh_token": None, "expires_at": None})
        self.assertTrue(mcp_oauth.logout("bank"))
        self.assertFalse(mcp_oauth.is_connected(_entry()))
        self.assertFalse(mcp_oauth.logout("bank"))


class CorruptStoreTests(_StoreMixin, unittest.TestCase):
    def test_corrupt_file_is_quarantined_not_overwritten_silently(self):
        with open(mcp_oauth.TOKENS_FILE, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertFalse(mcp_oauth.is_connected(_entry()))
        leftovers = [f for f in os.listdir(self._tmp) if ".corrupt-" in f]
        self.assertEqual(len(leftovers), 1)


class OauthConfigValidationTests(unittest.TestCase):
    def test_valid_block_accepted(self):
        self.assertTrue(_valid_mcp_oauth(dict(_OAUTH, client_secret="s", scope="a b")))

    def test_loopback_http_endpoint_accepted(self):
        self.assertTrue(_valid_mcp_oauth(dict(_OAUTH, token_url="http://127.0.0.1:9000/token")))

    def test_missing_required_key_rejected(self):
        bad = dict(_OAUTH)
        del bad["token_url"]
        self.assertFalse(_valid_mcp_oauth(bad))

    def test_unknown_key_rejected(self):
        self.assertFalse(_valid_mcp_oauth(dict(_OAUTH, clint_secret="typo")))

    def test_plain_http_token_endpoint_rejected(self):
        self.assertFalse(_valid_mcp_oauth(dict(_OAUTH, token_url="http://auth.example.com/token")))

    def test_plain_http_authorization_endpoint_rejected(self):
        self.assertFalse(_valid_mcp_oauth(dict(_OAUTH, authorization_url="http://auth.example.com/authorize")))

    def test_non_http_redirect_rejected(self):
        self.assertFalse(_valid_mcp_oauth(dict(_OAUTH, redirect_uri="myapp://cb")))


if __name__ == "__main__":
    unittest.main()
