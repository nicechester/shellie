from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from src.agent.core import chat_admin
from src.agent.settings import Result

_OAUTH = {
    "authorization_url": "https://auth.example.com/authorize",
    "token_url": "https://auth.example.com/token",
    "redirect_uri": "http://localhost:8787/callback",
    "client_id": "shellie-client",
}


class SkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self.user_dir = os.path.join(self._tmp, "skills")
        self.repo_dir = os.path.join(self._tmp, "repo_skills")
        os.makedirs(self.repo_dir)
        with open(os.path.join(self.repo_dir, "builtin.md"), "w", encoding="utf-8") as fh:
            fh.write("# builtin\n")
        patches = [
            mock.patch.object(chat_admin, "SKILLS_DIR", self.user_dir),
            mock.patch.object(chat_admin, "REPO_SKILLS_DIR", self.repo_dir),
        ]
        for p in patches:
            p.start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(shutil.rmtree, self._tmp, True)

    def test_create_update_remove_cycle(self) -> None:
        self.assertTrue(chat_admin.skill_run({"action": "create", "name": "weather.md", "content": "# w\n"}).startswith("OK:"))
        self.assertTrue(os.path.isfile(os.path.join(self.user_dir, "weather.md")))
        self.assertTrue(chat_admin.skill_run({"action": "update", "name": "weather.md", "content": "# v2\n"}).startswith("OK:"))
        with open(os.path.join(self.user_dir, "weather.md"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "# v2\n")
        self.assertTrue(chat_admin.skill_run({"action": "remove", "name": "weather.md"}).startswith("OK:"))
        self.assertFalse(os.path.exists(os.path.join(self.user_dir, "weather.md")))

    def test_create_refuses_existing_and_update_refuses_missing(self) -> None:
        chat_admin.skill_run({"action": "create", "name": "a.md", "content": "x"})
        self.assertTrue(chat_admin.skill_run({"action": "create", "name": "a.md", "content": "y"}).startswith("Error"))
        self.assertTrue(chat_admin.skill_run({"action": "update", "name": "nope.md", "content": "y"}).startswith("Error"))

    def test_builtin_names_cannot_be_changed(self) -> None:
        for action in ("create", "update", "remove"):
            result = chat_admin.skill_run({"action": action, "name": "builtin.md", "content": "x"})
            self.assertIn("built-in", result)

    def test_bad_names_and_paths_are_rejected(self) -> None:
        for bad in ("../evil.py", "a/b.md", ".hidden.md", "noext", "x.sh", "_private.py"):
            result = chat_admin.skill_run({"action": "create", "name": bad, "content": "x"})
            self.assertTrue(result.startswith("Error"), bad)

    def test_names_are_normalized_to_lowercase(self) -> None:
        self.assertTrue(chat_admin.skill_run({"action": "create", "name": "Upper.md", "content": "x"}).startswith("OK:"))
        self.assertTrue(os.path.isfile(os.path.join(self.user_dir, "upper.md")))

    def test_content_limits(self) -> None:
        self.assertTrue(chat_admin.skill_run({"action": "create", "name": "b.md", "content": "   "}).startswith("Error"))
        big = "x" * (chat_admin._SKILL_MAX_CHARS + 1)
        self.assertTrue(chat_admin.skill_run({"action": "create", "name": "b.md", "content": big}).startswith("Error"))

    def test_list_shows_only_user_skills(self) -> None:
        chat_admin.skill_run({"action": "create", "name": "mine.md", "content": "x"})
        self.assertEqual(chat_admin.skill_run({"action": "list"}), "OK: user skills: mine.md")


class McpProposalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings_get = mock.patch.object(chat_admin.settings, "get").start()
        self.settings_get.side_effect = lambda key: ""
        self.update = mock.patch.object(chat_admin.settings, "update").start()
        self.update.return_value = Result(True, ["MCP_SERVERS"], {}, {})
        self.addCleanup(mock.patch.stopall)

    def _pid(self, result: str) -> str:
        self.assertTrue(result.startswith("OK:"), result)
        return result.split("proposal ", 1)[1].split(" ", 1)[0]

    def test_add_is_only_proposed_not_applied(self) -> None:
        result = chat_admin.mcp_run({"action": "add", "name": "svc", "url": "https://mcp.example.com/"})
        self._pid(result)
        self.update.assert_not_called()

    def test_confirm_applies_with_chat_actor(self) -> None:
        pid = self._pid(chat_admin.mcp_run({"action": "add", "name": "svc", "url": "https://mcp.example.com/"}))
        ok, msg, entry = chat_admin.confirm(pid)
        self.assertTrue(ok, msg)
        self.assertEqual(entry, {"name": "svc", "url": "https://mcp.example.com/"})
        changes, actor = self.update.call_args.args[0], self.update.call_args.kwargs["actor"]
        self.assertEqual(actor, "chat")
        self.assertEqual(json.loads(changes["MCP_SERVERS"]), [{"name": "svc", "url": "https://mcp.example.com/"}])

    def test_confirmed_proposal_cannot_be_used_twice(self) -> None:
        pid = self._pid(chat_admin.mcp_run({"action": "add", "name": "svc", "url": "https://mcp.example.com/"}))
        self.assertTrue(chat_admin.confirm(pid)[0])
        ok, msg, _ = chat_admin.confirm(pid)
        self.assertFalse(ok)

    def test_headers_and_client_secret_are_refused(self) -> None:
        # Top-level headers are never read from the tool args.
        pid = self._pid(chat_admin.mcp_run({
            "action": "add", "name": "svc", "url": "https://mcp.example.com/",
            "headers": {"Authorization": "Bearer secret"},
        }))
        ok, _, entry = chat_admin.confirm(pid)
        self.assertTrue(ok)
        self.assertNotIn("headers", entry)
        result = chat_admin.mcp_run({
            "action": "add", "name": "svc2", "url": "https://mcp.example.com/",
            "oauth": dict(_OAUTH, client_secret="s3cret"),
        })
        self.assertTrue(result.startswith("Error"))
        self.assertNotIn("s3cret", result)

    def test_oauth_block_is_validated_like_settings(self) -> None:
        result = chat_admin.mcp_run({
            "action": "add", "name": "svc", "url": "https://mcp.example.com/",
            "oauth": dict(_OAUTH, token_url="http://insecure.example.com/token"),
        })
        self.assertTrue(result.startswith("Error"))

    def test_duplicate_name_and_bad_url_rejected(self) -> None:
        self.settings_get.side_effect = lambda key: json.dumps([{"name": "svc", "url": "https://a.example.com/"}])
        self.assertTrue(chat_admin.mcp_run({"action": "add", "name": "svc", "url": "https://b.example.com/"}).startswith("Error"))
        self.assertTrue(chat_admin.mcp_run({"action": "add", "name": "x", "url": "ftp://x"}).startswith("Error"))

    def test_remove_requires_existing_server_and_confirms(self) -> None:
        self.assertTrue(chat_admin.mcp_run({"action": "remove", "name": "svc"}).startswith("Error"))
        self.settings_get.side_effect = lambda key: json.dumps([{"name": "svc", "url": "https://a.example.com/"}])
        pid = self._pid(chat_admin.mcp_run({"action": "remove", "name": "svc"}))
        self.assertTrue(chat_admin.confirm(pid)[0])
        self.assertEqual(json.loads(self.update.call_args[0][0]["MCP_SERVERS"]), [])

    def test_failed_settings_update_is_reported(self) -> None:
        self.update.return_value = Result(False, [], {"MCP_SERVERS": "bad"}, {})
        pid = self._pid(chat_admin.mcp_run({"action": "add", "name": "svc", "url": "https://mcp.example.com/"}))
        ok, msg, entry = chat_admin.confirm(pid)
        self.assertFalse(ok)
        self.assertIsNone(entry)

    def test_discard_drops_proposal(self) -> None:
        pid = self._pid(chat_admin.mcp_run({"action": "add", "name": "svc", "url": "https://mcp.example.com/"}))
        self.assertIsNotNone(chat_admin.describe(pid))
        self.assertTrue(chat_admin.discard(pid))
        self.assertIsNone(chat_admin.describe(pid))


if __name__ == "__main__":
    unittest.main()
