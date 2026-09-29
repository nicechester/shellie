from __future__ import annotations

import io
import os
import re
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from src.agent.telegram import client
from src.agent.telegram import files
from src.agent.utils import http as http_utils


class FilesTestBase(unittest.TestCase):
    """Isolates the files module from the real workspace/config dirs."""

    def setUp(self) -> None:
        self.workspace_dir = tempfile.mkdtemp()
        self.base_dir = tempfile.mkdtemp()
        self.settings_file = os.path.join(self.base_dir, "settings.json")
        self.addCleanup(shutil.rmtree, self.workspace_dir, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.base_dir, ignore_errors=True)

        for name, value in (
            ("SHELLIE_WORKSPACE", self.workspace_dir),
            ("BASE_DIR", self.base_dir),
            ("SETTINGS_FILE", self.settings_file),
        ):
            patcher = mock.patch.object(files, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _write(self, rel_path: str, content: bytes = b"data") -> str:
        path = os.path.join(self.workspace_dir, rel_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(content)
        return path


class SanitizeFilenameTests(FilesTestBase):
    def test_path_traversal_is_stripped_to_basename(self) -> None:
        self.assertEqual(files.sanitize_filename("../../x", "fallback"), "x")

    def test_backslash_is_treated_as_separator(self) -> None:
        self.assertEqual(files.sanitize_filename("a\\b", "fallback"), "b")

    def test_leading_dots_stripped(self) -> None:
        self.assertEqual(files.sanitize_filename("...env", "fallback"), "env")
        self.assertEqual(files.sanitize_filename("..", "fallback"), "fallback")

    def test_control_chars_and_nul_removed(self) -> None:
        name = "a\x00b\x01c\x7f.txt"
        self.assertEqual(files.sanitize_filename(name, "fallback"), "abc.txt")

    def test_empty_result_falls_back(self) -> None:
        self.assertEqual(files.sanitize_filename("", "fallback.txt"), "fallback.txt")
        self.assertEqual(files.sanitize_filename("   ", "fallback.txt"), "fallback.txt")
        self.assertEqual(files.sanitize_filename("...", "fallback.txt"), "fallback.txt")

    def test_long_name_truncated_keeps_extension(self) -> None:
        long_name = ("a" * 300) + ".txt"
        result = files.sanitize_filename(long_name, "fallback")
        self.assertTrue(result.endswith(".txt"))
        self.assertLessEqual(len(result.encode("utf-8")), files._MAX_NAME_BYTES)

    def test_korean_name_is_kept(self) -> None:
        name = "한글파일명.pdf"
        self.assertEqual(files.sanitize_filename(name, "fallback"), name)


class ClaimUniquePathTests(FilesTestBase):
    def test_collisions_get_numbered_suffix(self) -> None:
        first = files.claim_unique_path(self.workspace_dir, "a.txt")
        second = files.claim_unique_path(self.workspace_dir, "a.txt")
        third = files.claim_unique_path(self.workspace_dir, "a.txt")

        self.assertEqual(first, os.path.join(self.workspace_dir, "a.txt"))
        self.assertEqual(second, os.path.join(self.workspace_dir, "a (1).txt"))
        self.assertEqual(third, os.path.join(self.workspace_dir, "a (2).txt"))

    def test_name_without_extension(self) -> None:
        first = files.claim_unique_path(self.workspace_dir, "README")
        second = files.claim_unique_path(self.workspace_dir, "README")

        self.assertEqual(first, os.path.join(self.workspace_dir, "README"))
        self.assertEqual(second, os.path.join(self.workspace_dir, "README (1)"))


class ExtractAttachmentTests(unittest.TestCase):
    def test_largest_photo_is_chosen(self) -> None:
        message = {
            "photo": [
                {"file_id": "a", "file_unique_id": "ua", "file_size": 100},
                {"file_id": "b", "file_unique_id": "ub", "file_size": 500},
                {"file_id": "c", "file_unique_id": "uc", "file_size": 300},
            ]
        }
        att = files.extract_attachment(message)
        self.assertIsNotNone(att)
        self.assertEqual(att["file_id"], "b")
        self.assertEqual(att["kind"], "photo")

    def test_photo_without_sizes_takes_last(self) -> None:
        message = {"photo": [{"file_id": "a"}, {"file_id": "b"}]}
        att = files.extract_attachment(message)
        self.assertEqual(att["file_id"], "b")

    def test_default_name_per_kind(self) -> None:
        cases = {
            "document": ("document", r"^document_\d{8}-\d{6}$"),
            "video": ("video", r"^video_\d{8}-\d{6}\.mp4$"),
            "audio": ("audio", r"^audio_\d{8}-\d{6}\.mp3$"),
            "voice": ("voice", r"^voice_\d{8}-\d{6}\.ogg$"),
        }

        for key, (kind, pattern) in cases.items():
            message = {key: {"file_id": "id-{}".format(key)}}
            att = files.extract_attachment(message)
            self.assertEqual(att["kind"], kind)
            self.assertRegex(att["file_name"], pattern)

    def test_photo_default_name(self) -> None:
        message = {"photo": [{"file_id": "a", "file_size": 10}]}
        att = files.extract_attachment(message)
        self.assertRegex(att["file_name"], r"^photo_\d{8}-\d{6}\.jpg$")

    def test_text_only_returns_none(self) -> None:
        self.assertIsNone(files.extract_attachment({"text": "hello"}))

    def test_no_file_id_returns_none(self) -> None:
        self.assertIsNone(files.extract_attachment({"document": {}}))


class SaveIncomingTests(FilesTestBase):
    def _att(self, **overrides):
        base = {
            "kind": "document",
            "file_id": "abc",
            "file_unique_id": "uabc",
            "file_size": 10,
            "file_name": "report.csv",
            "mime_type": "text/csv",
        }
        base.update(overrides)
        return base

    def test_too_big_up_front_skips_get_file(self) -> None:
        att = self._att(file_size=files.TG_DOWNLOAD_MAX_BYTES + 1)
        with mock.patch.object(files, "get_file") as mock_get_file:
            path, err = files.save_incoming(att)

        mock_get_file.assert_not_called()
        self.assertIsNone(path)
        self.assertIn("too large", err)

    def test_get_file_error_is_passed_through(self) -> None:
        att = self._att()
        with mock.patch.object(files, "get_file", return_value={"_status": 400, "_error": "file is too big"}):
            path, err = files.save_incoming(att)

        self.assertIsNone(path)
        self.assertEqual(err, "file is too big")

    def test_download_error_leaves_no_leftover_files(self) -> None:
        att = self._att()
        get_file_result = {"result": {"file_path": "documents/file_1", "file_size": 10}}
        with mock.patch.object(files, "get_file", return_value=get_file_result), \
                mock.patch.object(files, "download_file", return_value="boom"):
            path, err = files.save_incoming(att)

        self.assertIsNone(path)
        self.assertEqual(err, "boom")
        self.assertEqual(os.listdir(self.workspace_dir), [])

    def test_success_saves_file_with_mode_0600_and_collision_suffix(self) -> None:
        att = self._att()
        get_file_result = {"result": {"file_path": "documents/file_1", "file_size": 5}}

        def _fake_download(file_path, fileobj, max_bytes):
            fileobj.write(b"hello")
            return None

        with mock.patch.object(files, "get_file", return_value=get_file_result), \
                mock.patch.object(files, "download_file", side_effect=_fake_download):
            path1, err1 = files.save_incoming(att)
            path2, err2 = files.save_incoming(att)

        self.assertIsNone(err1)
        self.assertIsNone(err2)
        self.assertEqual(os.path.dirname(path1), self.workspace_dir)
        self.assertEqual(os.path.basename(path1), "report.csv")
        self.assertEqual(os.path.basename(path2), "report (1).csv")
        mode1 = stat.S_IMODE(os.stat(path1).st_mode)
        self.assertEqual(mode1, 0o600)
        with open(path1, "rb") as f:
            self.assertEqual(f.read(), b"hello")


class ResolveSendPathTests(FilesTestBase):
    def test_relative_path_resolves_to_workspace(self) -> None:
        path = self._write("sub/data.txt")
        resolved, err = files.resolve_send_path("sub/data.txt")
        self.assertIsNone(err)
        self.assertEqual(resolved, os.path.realpath(path))

    def test_tilde_expands_to_home(self) -> None:
        home_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home_dir, ignore_errors=True)
        home_file = os.path.join(home_dir, "x.txt")
        with open(home_file, "wb") as f:
            f.write(b"hi")

        with mock.patch.dict(os.environ, {"HOME": home_dir}):
            resolved, err = files.resolve_send_path("~/x.txt")

        self.assertIsNone(err)
        self.assertEqual(resolved, os.path.realpath(home_file))

    def test_missing_file(self) -> None:
        resolved, err = files.resolve_send_path("nope.txt")
        self.assertIsNone(resolved)
        self.assertIn("not found", err.lower())

    def test_directory_rejected(self) -> None:
        os.makedirs(os.path.join(self.workspace_dir, "adir"))
        resolved, err = files.resolve_send_path("adir")
        self.assertIsNone(resolved)
        self.assertIn("directory", err.lower())

    def test_empty_file_rejected(self) -> None:
        self._write("empty.txt", b"")
        resolved, err = files.resolve_send_path("empty.txt")
        self.assertIsNone(resolved)
        self.assertIn("empty", err.lower())

    def test_over_size_limit_rejected(self) -> None:
        self._write("big.bin", b"x" * 20)
        with mock.patch.object(files, "TG_UPLOAD_MAX_BYTES", 10):
            resolved, err = files.resolve_send_path("big.bin")
        self.assertIsNone(resolved)
        self.assertIn("too large", err.lower())

    def test_env_file_blocked(self) -> None:
        env_path = os.path.join(self.base_dir, ".env")
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("SECRET=1")
        resolved, err = files.resolve_send_path(env_path)
        self.assertIsNone(resolved)
        self.assertIsNotNone(err)
        self.assertNotIn("not found", err.lower())
        self.assertNotIn("directory", err.lower())

    def test_settings_file_blocked(self) -> None:
        with open(self.settings_file, "w", encoding="utf-8") as f:
            f.write("{}")
        resolved, err = files.resolve_send_path(self.settings_file)
        self.assertIsNone(resolved)
        self.assertIsNotNone(err)
        self.assertNotIn("not found", err.lower())
        self.assertNotIn("directory", err.lower())

    def test_env_file_blocked_via_symlink(self) -> None:
        env_path = os.path.join(self.base_dir, ".env")
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("SECRET=1")
        link_path = os.path.join(self.workspace_dir, "link_env")
        os.symlink(env_path, link_path)

        resolved, err = files.resolve_send_path("link_env")
        self.assertIsNone(resolved)
        self.assertIsNotNone(err)


class NormalizePathTests(FilesTestBase):
    def test_quotes_stripped(self) -> None:
        path, err = files._normalize_path('"data.txt"')
        self.assertIsNone(err)
        self.assertEqual(path, os.path.realpath(os.path.join(self.workspace_dir, "data.txt")))

    def test_empty_raises_error(self) -> None:
        path, err = files._normalize_path("   ")
        self.assertIsNone(path)
        self.assertIsNotNone(err)


class ListDirTests(FilesTestBase):
    def test_dirs_first_case_insensitive_sort(self) -> None:
        os.makedirs(os.path.join(self.workspace_dir, "Zdir"))
        os.makedirs(os.path.join(self.workspace_dir, "adir"))
        self._write("Bfile.txt")
        self._write("afile.txt")

        entries, total, err = files.list_dir(self.workspace_dir)

        self.assertIsNone(err)
        self.assertEqual(total, 4)
        names = [(e["name"], e["is_dir"]) for e in entries]
        self.assertEqual(
            names, [("adir", True), ("Zdir", True), ("afile.txt", False), ("Bfile.txt", False)]
        )

    def test_hidden_names_excluded(self) -> None:
        self._write("visible.txt")
        self._write(".hidden.txt")
        self._write(".shellie-upload-abc.part")

        entries, total, err = files.list_dir(self.workspace_dir)

        self.assertIsNone(err)
        self.assertEqual([e["name"] for e in entries], ["visible.txt"])
        self.assertEqual(total, 1)

    def test_limit_caps_entries_but_total_is_full_count(self) -> None:
        for i in range(10):
            self._write("f{:02d}.txt".format(i))

        entries, total, err = files.list_dir(self.workspace_dir, limit=3)

        self.assertIsNone(err)
        self.assertEqual(len(entries), 3)
        self.assertEqual(total, 10)

    def test_broken_symlink_reports_none_size_and_not_dir(self) -> None:
        target = os.path.join(self.workspace_dir, "gone.txt")
        with open(target, "wb") as f:
            f.write(b"x")
        link = os.path.join(self.workspace_dir, "broken_link")
        os.symlink(target, link)
        os.unlink(target)

        entries, total, err = files.list_dir(self.workspace_dir)

        self.assertIsNone(err)
        entry = next(e for e in entries if e["name"] == "broken_link")
        self.assertIsNone(entry["size"])
        self.assertFalse(entry["is_dir"])

    def test_missing_directory_returns_error(self) -> None:
        entries, total, err = files.list_dir(os.path.join(self.workspace_dir, "nope"))

        self.assertEqual(entries, [])
        self.assertEqual(total, 0)
        self.assertIsNotNone(err)

    def test_scandir_error_returns_error(self) -> None:
        with mock.patch.object(files.os, "scandir", side_effect=PermissionError("denied")):
            entries, total, err = files.list_dir(self.workspace_dir)

        self.assertEqual(entries, [])
        self.assertEqual(total, 0)
        self.assertIn("denied", err)


class ResolveDirTests(FilesTestBase):
    def test_none_defaults_to_workspace(self) -> None:
        path, err = files.resolve_dir(None)
        self.assertIsNone(err)
        self.assertEqual(path, os.path.realpath(self.workspace_dir))

    def test_empty_string_defaults_to_workspace(self) -> None:
        path, err = files.resolve_dir("")
        self.assertIsNone(err)
        self.assertEqual(path, os.path.realpath(self.workspace_dir))

    def test_relative_path_resolves_inside_workspace(self) -> None:
        os.makedirs(os.path.join(self.workspace_dir, "sub"))
        path, err = files.resolve_dir("sub")
        self.assertIsNone(err)
        self.assertEqual(path, os.path.realpath(os.path.join(self.workspace_dir, "sub")))

    def test_tilde_expands_to_home(self) -> None:
        home_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home_dir, ignore_errors=True)
        with mock.patch.dict(os.environ, {"HOME": home_dir}):
            path, err = files.resolve_dir("~")
        self.assertIsNone(err)
        self.assertEqual(path, os.path.realpath(home_dir))

    def test_missing_path_reports_not_found(self) -> None:
        path, err = files.resolve_dir("nope")
        self.assertIsNone(path)
        self.assertIn("Not found", err)

    def test_file_path_reports_not_a_directory(self) -> None:
        self._write("afile.txt")
        path, err = files.resolve_dir("afile.txt")
        self.assertIsNone(path)
        self.assertIn("Not a directory", err)
        self.assertIn("/file", err)


class SendFileTests(FilesTestBase):
    def test_caption_cut_to_max(self) -> None:
        path = self._write("report.csv", b"1,2,3")
        long_caption = "x" * 2000
        with mock.patch.object(files, "send_chat_action"), \
                mock.patch.object(files, "send_document", return_value={"ok": True}) as mock_send_doc:
            ok, msg = files.send_file(1, "report.csv", caption=long_caption)

        self.assertTrue(ok)
        sent_caption = mock_send_doc.call_args[0][3]
        self.assertEqual(len(sent_caption), files.CAPTION_MAX)
        self.assertEqual(sent_caption, long_caption[: files.CAPTION_MAX])

    def test_send_document_error_message_contains_no_token(self) -> None:
        path = self._write("report.csv", b"1,2,3")
        fake_token = "123456789:AAFakeTokenAAAAAAAAAAAAAAAAAAAAAAAAA"

        with mock.patch.object(client, "settings") as mock_settings, \
                mock.patch.object(client, "http_post_multipart") as mock_multipart, \
                mock.patch.object(files, "send_chat_action"):
            mock_settings.get.return_value = fake_token
            mock_multipart.return_value = (
                {"ok": False, "description": "Bad Request: token {} invalid".format(fake_token)},
                400,
            )
            ok, msg = files.send_file(1, "report.csv")

        self.assertFalse(ok)
        self.assertNotIn(fake_token, msg)


class HttpDownloadTests(unittest.TestCase):
    class _ChunkedResponse:
        def __init__(self, status, chunks):
            self.status = status
            self._chunks = list(chunks)

        def read(self, _n=-1):
            if self._chunks:
                return self._chunks.pop(0)
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def test_stops_at_max_bytes(self) -> None:
        chunks = [b"a" * 70000, b"b" * 70000]
        fake_res = self._ChunkedResponse(200, chunks)
        buf = io.BytesIO()

        with mock.patch.object(http_utils.urllib.request, "urlopen", return_value=fake_res):
            status, n, err = http_utils.http_download("http://example.com/f", buf, max_bytes=100000)

        self.assertEqual(status, 200)
        self.assertEqual(err, "too large")
        self.assertEqual(n, 140000)
        self.assertEqual(len(buf.getvalue()), 70000)

    def test_success_returns_no_error(self) -> None:
        fake_res = self._ChunkedResponse(200, [b"hello"])
        buf = io.BytesIO()

        with mock.patch.object(http_utils.urllib.request, "urlopen", return_value=fake_res):
            status, n, err = http_utils.http_download("http://example.com/f", buf, max_bytes=100000)

        self.assertEqual(status, 200)
        self.assertIsNone(err)
        self.assertEqual(n, 5)
        self.assertEqual(buf.getvalue(), b"hello")


class HttpPostMultipartTests(unittest.TestCase):
    class _FakeResponse:
        def __init__(self, status, body: bytes):
            self.status = status
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def test_boundary_layout_and_content_length(self) -> None:
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.write(b"filedata123")
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)

        captured = {}

        def _fake_urlopen(req, timeout=None):
            captured["req"] = req
            captured["body"] = b"".join(req.data)
            return self._FakeResponse(200, b'{"ok": true}')

        with mock.patch.object(http_utils.urllib.request, "urlopen", side_effect=_fake_urlopen):
            data, status = http_utils.http_post_multipart(
                "https://example.com/x", {"chat_id": "1"}, "document", tmp.name, "hello.txt"
            )

        self.assertEqual(status, 200)
        self.assertEqual(data, {"ok": True})

        body = captured["body"]
        content_length = int(captured["req"].get_header("Content-length"))
        self.assertEqual(content_length, len(body))
        self.assertIn(b'name="chat_id"', body)
        self.assertIn(b'filename="hello.txt"', body)
        self.assertIn(b"filedata123", body)

    def test_file_shrinking_mid_upload_reports_status_zero(self) -> None:
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.write(b"x" * 100)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)

        # Lie about the file size so the body generator detects a short read
        # partway through and raises IOError, without touching the network.
        def _fake_urlopen(req, timeout=None):
            list(req.data)
            return self._FakeResponse(200, b"{}")

        with mock.patch.object(http_utils.os.path, "getsize", return_value=1000), \
                mock.patch.object(http_utils.urllib.request, "urlopen", side_effect=_fake_urlopen):
            data, status = http_utils.http_post_multipart(
                "https://example.com/x", {}, "document", tmp.name, "f.bin"
            )

        self.assertEqual(status, 0)
        self.assertIn("error", data)


if __name__ == "__main__":
    unittest.main()
