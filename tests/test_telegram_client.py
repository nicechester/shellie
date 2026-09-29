from __future__ import annotations

import unittest
from unittest import mock

from src.agent.telegram import client


class GetUpdatesTests(unittest.TestCase):
    def test_allowed_updates_includes_callback_query(self) -> None:
        with mock.patch.object(client, "settings") as mock_settings, \
                mock.patch.object(client, "http_post") as mock_post:
            mock_settings.get.side_effect = lambda key: {
                "TELEGRAM_BOT_TOKEN": "123:tok", "POLL_TIMEOUT_SEC": 30
            }[key]
            mock_post.return_value = ({"ok": True, "result": []}, 200)

            client.get_updates()

        payload = mock_post.call_args[0][1]
        self.assertIn("callback_query", payload["allowed_updates"])
        self.assertIn("message", payload["allowed_updates"])


class SendMessageReplyMarkupTests(unittest.TestCase):
    def test_reply_markup_only_on_last_chunk(self) -> None:
        with mock.patch.object(client, "settings") as mock_settings, \
                mock.patch.object(client, "split_message", return_value=["c1", "c2"]), \
                mock.patch.object(client, "http_post") as mock_post:
            mock_settings.get.return_value = "123:tok"
            mock_post.return_value = ({"ok": True}, 200)

            client.send_message(1, "text", reply_markup={"inline_keyboard": []})

        self.assertEqual(mock_post.call_count, 2)
        first_payload = mock_post.call_args_list[0][0][1]
        second_payload = mock_post.call_args_list[1][0][1]
        self.assertNotIn("reply_markup", first_payload)
        self.assertIn("reply_markup", second_payload)
        self.assertEqual(second_payload["reply_markup"], {"inline_keyboard": []})

    def test_reply_markup_kept_on_plain_text_retry(self) -> None:
        with mock.patch.object(client, "settings") as mock_settings, \
                mock.patch.object(client, "split_message", return_value=["only"]), \
                mock.patch.object(client, "http_post") as mock_post:
            mock_settings.get.return_value = "123:tok"
            mock_post.side_effect = [
                ({"ok": False, "error": "bad html"}, 400),
                ({"ok": True}, 200),
            ]

            client.send_message(1, "only", reply_markup={"inline_keyboard": []})

        self.assertEqual(mock_post.call_count, 2)
        retry_payload = mock_post.call_args_list[1][0][1]
        self.assertIn("reply_markup", retry_payload)
        self.assertNotIn("parse_mode", retry_payload)


class EditMessageTextTests(unittest.TestCase):
    def test_ok_returns_true(self) -> None:
        with mock.patch.object(client, "settings") as mock_settings, \
                mock.patch.object(client, "http_post") as mock_post:
            mock_settings.get.return_value = "123:tok"
            mock_post.return_value = ({"ok": True}, 200)

            result = client.edit_message_text(1, 2, "text")

        self.assertTrue(result)

    def test_message_not_modified_returns_true(self) -> None:
        with mock.patch.object(client, "settings") as mock_settings, \
                mock.patch.object(client, "http_post") as mock_post:
            mock_settings.get.return_value = "123:tok"
            mock_post.return_value = (
                {"ok": False, "description": "Bad Request: message is not modified"}, 400
            )

            result = client.edit_message_text(1, 2, "text")

        self.assertTrue(result)

    def test_other_400_returns_false_and_masks_token(self) -> None:
        fake_token = "123456789:AAFakeTokenAAAAAAAAAAAAAAAAAAAAAAAAA"
        with mock.patch.object(client, "settings") as mock_settings, \
                mock.patch.object(client, "http_post") as mock_post, \
                self.assertLogs(client._LOGGER, level="WARNING") as log_ctx:
            mock_settings.get.return_value = fake_token
            mock_post.return_value = (
                {"ok": False, "description": "Bad Request: chat not found {}".format(fake_token)},
                400,
            )

            result = client.edit_message_text(1, 2, "text")

        self.assertFalse(result)
        combined_log = "\n".join(log_ctx.output)
        self.assertNotIn(fake_token, combined_log)

    def test_no_token_returns_false(self) -> None:
        with mock.patch.object(client, "settings") as mock_settings, \
                mock.patch.object(client, "http_post") as mock_post:
            mock_settings.get.return_value = None

            result = client.edit_message_text(1, 2, "text")

        mock_post.assert_not_called()
        self.assertFalse(result)


class AnswerCallbackQueryTests(unittest.TestCase):
    def test_payload_shape(self) -> None:
        with mock.patch.object(client, "settings") as mock_settings, \
                mock.patch.object(client, "http_post") as mock_post:
            mock_settings.get.return_value = "123:tok"
            mock_post.return_value = ({"ok": True}, 200)

            result = client.answer_callback_query("cq1", "hi", show_alert=True)

        payload = mock_post.call_args[0][1]
        self.assertEqual(payload["callback_query_id"], "cq1")
        self.assertEqual(payload["text"], "hi")
        self.assertTrue(payload["show_alert"])
        self.assertTrue(result)

    def test_text_cut_to_200_chars(self) -> None:
        with mock.patch.object(client, "settings") as mock_settings, \
                mock.patch.object(client, "http_post") as mock_post:
            mock_settings.get.return_value = "123:tok"
            mock_post.return_value = ({"ok": True}, 200)
            long_text = "x" * 500

            client.answer_callback_query("cq1", long_text)

        payload = mock_post.call_args[0][1]
        self.assertEqual(len(payload["text"]), 200)
        self.assertEqual(payload["text"], "x" * 200)

    def test_never_raises_on_transport_error(self) -> None:
        with mock.patch.object(client, "settings") as mock_settings, \
                mock.patch.object(client, "http_post", side_effect=Exception("boom")):
            mock_settings.get.return_value = "123:tok"

            try:
                result = client.answer_callback_query("cq1", "hi")
            except Exception:  # noqa: BLE001 - the point of this test is that it must not raise
                self.fail("answer_callback_query raised an exception")

        self.assertIsInstance(result, bool)
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
