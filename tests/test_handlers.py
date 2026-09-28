from __future__ import annotations

import os
import time
import unittest
from unittest import mock

from src.agent.settings import Result
from src.agent.telegram import handlers


def _fc_response(calls, finish_reason="STOP"):
    parts = [{"functionCall": {"name": name, "args": args}} for name, args in calls]
    return {
        "candidates": [
            {"content": {"role": "model", "parts": parts}, "finishReason": finish_reason}
        ]
    }


def _text_response(text, finish_reason="STOP"):
    return {
        "candidates": [
            {"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": finish_reason}
        ]
    }


def _blocked_response(reason="SAFETY"):
    return {"candidates": [], "promptFeedback": {"blockReason": reason}}


def _raise_system_exit(code=0):
    raise SystemExit(code)


def _row(
    key,
    value="v",
    source="default",
    telegram_editable=True,
    apply_timing="immediate",
    description="desc",
    constraint="",
    default="v",
    required=False,
    secret=False,
):
    return {
        "key": key,
        "value": value,
        "source": source,
        "telegram_editable": telegram_editable,
        "apply_timing": apply_timing,
        "description": description,
        "constraint": constraint,
        "default": default,
        "required": required,
        "secret": secret,
    }


class HandlersTestCase(unittest.TestCase):
    """Base fixture: resets handlers module state and mocks all collaborators."""

    def setUp(self) -> None:
        handlers._reset_history()
        handlers._last_activity = 0.0
        handlers._queue_active_item = None
        handlers._queue_cooldown_until = 0.0
        # Drain any leftover items from a previous test.
        while not handlers._task_queue.empty():
            try:
                handlers._task_queue.get_nowait()
                handlers._task_queue.task_done()
            except Exception:
                break

        patches = {
            "settings": mock.patch.object(handlers, "settings"),
            "send_message": mock.patch.object(handlers, "send_message"),
            "send_chat_action": mock.patch.object(handlers, "send_chat_action"),
            "delete_message": mock.patch.object(handlers, "delete_message"),
            "execute_shell": mock.patch.object(handlers, "execute_shell"),
            "read_memory": mock.patch.object(handlers, "read_memory"),
            "call_gemini": mock.patch.object(handlers, "call_gemini"),
            "run_tool": mock.patch.object(handlers, "run_tool"),
        }
        self.mocks = {}
        for name, patcher in patches.items():
            self.mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)

        self.mock_settings = self.mocks["settings"]
        self.mock_send_message = self.mocks["send_message"]
        self.mock_send_chat_action = self.mocks["send_chat_action"]
        self.mock_delete_message = self.mocks["delete_message"]
        self.mock_execute_shell = self.mocks["execute_shell"]
        self.mock_read_memory = self.mocks["read_memory"]
        self.mock_call_gemini = self.mocks["call_gemini"]
        self.mock_run_tool = self.mocks["run_tool"]

        self._settings_values = {
            "ALLOWED_USER_ID": 111,
            "FC_MAX_LOOPS": 5,
            "CONTEXT_TURNS": 10,
            "IDLE_RESET_MINUTES": 30,
            "WEB_PORT": 8321,
        }
        self.mock_settings.get.side_effect = lambda key: self._settings_values[key]

    def tearDown(self) -> None:
        handlers._reset_history()
        handlers._last_activity = 0.0
        handlers._queue_active_item = None
        handlers._queue_cooldown_until = 0.0

    def _drain_queue(self) -> None:
        """Process all queued LLM items synchronously by calling _handle_llm directly."""
        while not handlers._task_queue.empty():
            try:
                item = handlers._task_queue.get_nowait()
            except Exception:
                break
            try:
                handlers._handle_llm(item.chat_id, item.text)
            except Exception:  # noqa: BLE001 - intentional in test drain helper
                pass
            finally:
                handlers._task_queue.task_done()

    def _update(self, text, chat_id=1, user_id=111, message_id=42):
        return {
            "message": {
                "text": text,
                "chat": {"id": chat_id},
                "from": {"id": user_id},
                "message_id": message_id,
            }
        }

    def _base_rows(self):
        return [
            _row("SHELL_TIMEOUT_SEC", value="45", telegram_editable=True, required=False),
            _row(
                "TELEGRAM_BOT_TOKEN",
                value="1234567890:••••abcd",
                telegram_editable=False,
                secret=True,
                required=True,
            ),
            _row("ALLOWED_USER_ID", value="111", telegram_editable=False, required=True),
            _row("SYSTEM_PROMPT", value="prompt text", telegram_editable=True),
        ]


class UnauthorizedAccessTests(HandlersTestCase):
    def test_wrong_user_id_is_silent(self) -> None:
        handlers.process_update(self._update("hello", user_id=222))
        self.mock_send_message.assert_not_called()

    def test_allowed_user_id_none_is_silent(self) -> None:
        self._settings_values["ALLOWED_USER_ID"] = None
        handlers.process_update(self._update("hello", user_id=111))
        self.mock_send_message.assert_not_called()


class BypassCommandTests(HandlersTestCase):
    def test_bang_prefix_runs_shell_and_skips_llm(self) -> None:
        self.mock_execute_shell.return_value = "hi"
        handlers.process_update(self._update("!echo hi"))
        self.mock_execute_shell.assert_called_once_with("echo hi")
        self.mock_call_gemini.assert_not_called()
        reply = self.mock_send_message.call_args[0][1]
        self.assertEqual(reply, "<pre>hi</pre>")

    def test_sh_command_runs_shell_and_skips_llm(self) -> None:
        self.mock_execute_shell.return_value = "hi"
        handlers.process_update(self._update("/sh echo hi"))
        self.mock_execute_shell.assert_called_once_with("echo hi")
        self.mock_call_gemini.assert_not_called()
        reply = self.mock_send_message.call_args[0][1]
        self.assertEqual(reply, "<pre>hi</pre>")

    def test_mem_command_reads_memory_without_llm(self) -> None:
        self.mock_read_memory.return_value = "remembered fact"
        handlers.process_update(self._update("/mem"))
        self.mock_read_memory.assert_called_once()
        self.mock_call_gemini.assert_not_called()
        reply = self.mock_send_message.call_args[0][1]
        self.assertIn("remembered fact", reply)

    def test_reset_command_clears_history(self) -> None:
        handlers._history.append({"role": "user", "parts": [{"text": "old"}]})
        handlers.process_update(self._update("/reset"))
        self.assertEqual(handlers._history, [])
        self.mock_call_gemini.assert_not_called()


class RestartCommandTests(HandlersTestCase):
    def test_restart_sends_message_then_exits(self) -> None:
        order = []
        self.mock_send_message.side_effect = lambda *a, **k: order.append("send")

        def _exit(code=0):
            order.append("exit")
            raise SystemExit(code)

        with mock.patch.object(handlers.sys, "exit", side_effect=_exit):
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(SystemExit):
                    handlers.process_update(self._update("/restart"))

        self.assertEqual(order, ["send", "exit"])

    def test_restart_warns_when_no_service_manager_present(self) -> None:
        with mock.patch.object(handlers.sys, "exit", side_effect=_raise_system_exit):
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(SystemExit):
                    handlers.process_update(self._update("/restart"))
        reply = self.mock_send_message.call_args[0][1]
        self.assertIn("without a service manager", reply)

    def test_restart_no_warning_when_systemd_present(self) -> None:
        with mock.patch.object(handlers.sys, "exit", side_effect=_raise_system_exit):
            with mock.patch.dict(os.environ, {"INVOCATION_ID": "abc"}, clear=True):
                with self.assertRaises(SystemExit):
                    handlers.process_update(self._update("/restart"))
        reply = self.mock_send_message.call_args[0][1]
        self.assertNotIn("without a service manager", reply)

    def test_restart_no_warning_when_launchd_present(self) -> None:
        with mock.patch.object(handlers.sys, "exit", side_effect=_raise_system_exit):
            with mock.patch.dict(os.environ, {"XPC_SERVICE_NAME": "com.example.shellie"}, clear=True):
                with self.assertRaises(SystemExit):
                    handlers.process_update(self._update("/restart"))
        reply = self.mock_send_message.call_args[0][1]
        self.assertNotIn("without a service manager", reply)


class SettingsCommandTests(HandlersTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.mock_settings.rows.return_value = self._base_rows()

    def test_set_updates_via_store_with_telegram_actor(self) -> None:
        self.mock_settings.update.return_value = Result(
            ok=True, applied=["SHELL_TIMEOUT_SEC"], errors={}, changes={"SHELL_TIMEOUT_SEC": ("45", "60")}
        )
        handlers.process_update(self._update("/set SHELL_TIMEOUT_SEC 60"))
        self.mock_settings.update.assert_called_once_with({"SHELL_TIMEOUT_SEC": "60"}, actor="telegram")
        reply = self.mock_send_message.call_args[0][1]
        self.assertIn("SHELL_TIMEOUT_SEC", reply)
        self.mock_call_gemini.assert_not_called()
        self.assertEqual(handlers._history, [])

    def test_set_secret_key_is_blocked_and_message_deleted(self) -> None:
        handlers.process_update(self._update("/set TELEGRAM_BOT_TOKEN xxx", message_id=99))
        self.mock_delete_message.assert_called_once_with(1, 99)
        self.mock_settings.update.assert_not_called()
        reply = self.mock_send_message.call_args[0][1]
        self.assertIn("security", reply)

    def test_set_non_secret_blocked_key_not_deleted(self) -> None:
        handlers.process_update(self._update("/set ALLOWED_USER_ID 5", message_id=99))
        self.mock_delete_message.assert_not_called()
        self.mock_settings.update.assert_not_called()
        reply = self.mock_send_message.call_args[0][1]
        self.assertIn("security", reply)

    def test_set_unknown_key_suggests_close_match(self) -> None:
        handlers.process_update(self._update("/set SHELL_TIMEOUT_SE 60"))
        reply = self.mock_send_message.call_args[0][1]
        self.assertIn("Unknown key", reply)
        self.assertIn("SHELL_TIMEOUT_SEC", reply)

    def test_set_multiline_value_rejected_for_non_system_prompt(self) -> None:
        handlers.process_update(self._update("/set SHELL_TIMEOUT_SEC 60\nextra"))
        self.mock_settings.update.assert_not_called()
        reply = self.mock_send_message.call_args[0][1]
        self.assertIn("newlines", reply)

    def test_settings_listing_is_pre_wrapped_and_masked(self) -> None:
        handlers.process_update(self._update("/settings"))
        reply = self.mock_send_message.call_args[0][1]
        self.assertTrue(reply.startswith("<pre>"))
        self.assertTrue(reply.endswith("</pre>"))
        self.assertIn("••••", reply)
        self.mock_call_gemini.assert_not_called()
        self.assertEqual(handlers._history, [])


class FunctionCallLoopTests(HandlersTestCase):
    def test_run_tool_called_for_each_call_in_multi_call_batch(self) -> None:
        calls_batch = [("execute_shell", {"command": "ls"}), ("append_memory", {"content": "note"})]
        fc_resp = _fc_response(calls_batch)
        final_resp = _text_response("done")
        self.mock_call_gemini.side_effect = [(fc_resp, "m1"), (final_resp, "m1")]
        self.mock_run_tool.side_effect = ["ls-output", "mem-output"]

        handlers.process_update(self._update("do things"))
        self._drain_queue()

        self.assertEqual(self.mock_run_tool.call_count, 2)
        self.mock_run_tool.assert_any_call("execute_shell", {"command": "ls"})
        self.mock_run_tool.assert_any_call("append_memory", {"content": "note"})

        second_call_contents = self.mock_call_gemini.call_args_list[1][0][0]
        function_response_turn = second_call_contents[-1]
        self.assertEqual(function_response_turn["role"], "user")
        self.assertEqual(len(function_response_turn["parts"]), 2)
        names = [part["functionResponse"]["name"] for part in function_response_turn["parts"]]
        self.assertEqual(names, ["execute_shell", "append_memory"])

    def test_loop_stops_at_fc_max_loops_with_limit_message(self) -> None:
        self._settings_values["FC_MAX_LOOPS"] = 2
        fc_resp = _fc_response([("execute_shell", {"command": "ls"})])
        self.mock_call_gemini.return_value = (fc_resp, "m1")
        self.mock_run_tool.return_value = "output"

        handlers.process_update(self._update("loop forever"))
        self._drain_queue()

        reply = self.mock_send_message.call_args[0][1]
        self.assertIn("Function call limit", reply)
        self.assertIn("2", reply)
        self.assertEqual(self.mock_call_gemini.call_count, 3)  # fc_max_loops + 1
        self.assertEqual(self.mock_run_tool.call_count, 2)

    def test_blocked_response_sends_blocked_message_and_history_not_retained(self) -> None:
        self.mock_call_gemini.return_value = (_blocked_response("SAFETY"), "m1")
        handlers.process_update(self._update("something"))
        self._drain_queue()
        reply = self.mock_send_message.call_args[0][1]
        self.assertIn("blocked", reply)
        self.assertEqual(handlers._history, [])


class HistoryManagementTests(HandlersTestCase):
    def test_context_turns_one_retains_only_last_pair(self) -> None:
        self._settings_values["CONTEXT_TURNS"] = 1

        self.mock_call_gemini.return_value = (_text_response("reply-1"), "m1")
        handlers.process_update(self._update("msg1"))
        self._drain_queue()
        self.assertEqual(len(handlers._history), 2)

        self.mock_call_gemini.return_value = (_text_response("reply-2"), "m1")
        handlers.process_update(self._update("msg2"))
        self._drain_queue()
        self.assertEqual(len(handlers._history), 2)
        self.assertEqual(handlers._history[0]["parts"][0]["text"], "msg2")

    def test_context_turns_zero_retains_nothing_and_sends_empty_history(self) -> None:
        self._settings_values["CONTEXT_TURNS"] = 0
        self.mock_call_gemini.return_value = (_text_response("reply"), "m1")

        handlers.process_update(self._update("msg1"))
        self._drain_queue()

        self.assertEqual(handlers._history, [])
        first_call_contents = self.mock_call_gemini.call_args_list[0][0][0]
        self.assertEqual(len(first_call_contents), 1)
        self.assertEqual(first_call_contents[0]["parts"][0]["text"], "msg1")

    def test_idle_reset_clears_history_before_processing(self) -> None:
        handlers._history.append({"role": "user", "parts": [{"text": "old"}]})
        handlers._history.append({"role": "model", "parts": [{"text": "old-reply"}]})
        handlers._last_activity = 1.0  # far in the past
        self._settings_values["IDLE_RESET_MINUTES"] = 1
        self.mock_call_gemini.return_value = (_text_response("reply"), "m1")

        handlers.process_update(self._update("new message"))
        self._drain_queue()

        # Only the newest turn pair should remain; the stale history was reset first.
        self.assertEqual(len(handlers._history), 2)
        self.assertEqual(handlers._history[0]["parts"][0]["text"], "new message")


class QueueTests(HandlersTestCase):
    def test_llm_message_is_enqueued_not_called_directly(self) -> None:
        handlers.process_update(self._update("hello"))
        self.mock_call_gemini.assert_not_called()
        self.assertEqual(handlers._task_queue.qsize(), 1)

    def test_queue_status_idle(self) -> None:
        handlers.process_update(self._update("/queue"))
        reply = self.mock_send_message.call_args[0][1]
        self.assertIn("idle", reply)
        self.assertIn("0", reply)

    def test_queue_status_shows_depth(self) -> None:
        handlers.process_update(self._update("msg1"))
        handlers.process_update(self._update("msg2"))
        handlers.process_update(self._update("/queue"))
        reply = self.mock_send_message.call_args[0][1]
        self.assertIn("2", reply)

    def test_queue_status_shows_active_item_preview(self) -> None:
        handlers._queue_active_item = handlers._QueueItem(chat_id=1, text="what is the weather today")
        handlers.process_update(self._update("/queue"))
        reply = self.mock_send_message.call_args[0][1]
        self.assertIn("what is the weather today", reply)

    def test_queue_status_shows_cooldown_remaining(self) -> None:
        handlers._queue_cooldown_until = time.time() + 45
        handlers.process_update(self._update("/queue"))
        reply = self.mock_send_message.call_args[0][1]
        self.assertIn("429", reply)
        self.assertIn("쿨다운", reply)

    def test_process_llm_item_retries_on_all_models_failed(self) -> None:
        """_process_llm_item retries after RPM cooldown when call_gemini raises."""
        ok_resp = _text_response("done")
        call_count = {"n": 0}

        def _handle_llm_side_effect(chat_id, text):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise Exception("All Gemini models failed (rate limit or transient error).")
            # Second call: simulate success by calling real _run_llm_turn path
            # via mock_call_gemini returning ok_resp.

        sleep_patcher = mock.patch.object(handlers.time, "sleep")
        handle_llm_patcher = mock.patch.object(handlers, "_handle_llm", side_effect=_handle_llm_side_effect)
        with sleep_patcher as mock_sleep, handle_llm_patcher:
            item = handlers._QueueItem(chat_id=1, text="test")
            # Run _process_llm_item; it will retry once then succeed (no exception on 2nd call)
            handlers._process_llm_item(item)

        self.assertEqual(call_count["n"], 2)
        mock_sleep.assert_called_once_with(handlers._QUEUE_RPM_COOLDOWN_SEC)

    def test_process_llm_item_raises_on_non_rpm_exception(self) -> None:
        """_process_llm_item re-raises non-RPM exceptions immediately."""
        def _fail(chat_id, text):
            raise ValueError("bad input")

        with mock.patch.object(handlers, "_handle_llm", side_effect=_fail):
            item = handlers._QueueItem(chat_id=1, text="test")
            with self.assertRaises(ValueError):
                handlers._process_llm_item(item)


if __name__ == "__main__":
    unittest.main()
