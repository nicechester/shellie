from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from src.agent.core import tasks
from src.agent.settings import Result
from src.agent.telegram import handlers


def _fc_response(calls, finish_reason="STOP", text=None):
    parts = []
    if text is not None:
        parts.append({"text": text})
    parts.extend({"functionCall": {"name": name, "args": args}} for name, args in calls)
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
        handlers._task_epoch = 0
        handlers._active_continuation = 0
        # Drain any leftover items from a previous test.
        while not handlers._task_queue.empty():
            try:
                handlers._task_queue.get_nowait()
                handlers._task_queue.task_done()
            except Exception:
                break

        # Isolate task persistence from the real ~/.shellie/tasks directory.
        self._tasks_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tasks_dir, ignore_errors=True)
        tasks_dir_patcher = mock.patch.object(tasks, "TASKS_DIR", self._tasks_dir)
        tasks_dir_patcher.start()
        self.addCleanup(tasks_dir_patcher.stop)

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
            "FC_MAX_CONTINUATIONS": 2,
        }
        self.mock_settings.get.side_effect = lambda key: self._settings_values[key]

    def tearDown(self) -> None:
        handlers._reset_history()
        handlers._last_activity = 0.0
        handlers._queue_active_item = None
        handlers._task_epoch = 0
        handlers._active_continuation = 0

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

    def _drain_queue_full(self) -> None:
        """Process all queued items synchronously through the real worker
        entry point (_process_llm_item), including task persistence and
        auto-continuation."""
        while not handlers._task_queue.empty():
            try:
                item = handlers._task_queue.get_nowait()
            except Exception:
                break
            try:
                handlers._process_llm_item(item)
            except Exception:  # noqa: BLE001 - intentional in test drain helper
                pass
            finally:
                handlers._task_queue.task_done()

    def _sent_texts(self):
        return [call_args[0][1] for call_args in self.mock_send_message.call_args_list]

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

    def test_limit_reached_triggers_tools_disabled_wrapup_and_preserves_history(self) -> None:
        self._settings_values["FC_MAX_LOOPS"] = 2
        fc_ls = _fc_response([("execute_shell", {"command": "ls"})])
        fc_pwd = _fc_response([("execute_shell", {"command": "pwd"})])
        fc_whoami = _fc_response([("execute_shell", {"command": "whoami"})])
        summary_resp = _text_response("summary")
        self.mock_call_gemini.side_effect = [
            (fc_ls, "m1"), (fc_pwd, "m1"), (fc_whoami, "m1"), (summary_resp, "m1"),
        ]
        self.mock_run_tool.side_effect = ["ls-output", "pwd-output"]

        handlers.process_update(self._update("loop forever"))
        self._drain_queue()

        self.assertEqual(self.mock_call_gemini.call_count, 4)
        self.assertEqual(self.mock_run_tool.call_count, 2)  # pending whoami never runs

        last_call_kwargs = self.mock_call_gemini.call_args_list[-1][1]
        self.assertIs(last_call_kwargs["allow_tools"], False)

        reply = self.mock_send_message.call_args[0][1]
        self.assertIn("도구 호출 한도(2회)", reply)
        self.assertIn("summary", reply)

        self.assertEqual(len(handlers._history), 2)
        self.assertEqual(handlers._history[1]["parts"], [{"text": "summary"}])

        last_call_contents = self.mock_call_gemini.call_args_list[-1][0][0]
        final_turn = last_call_contents[-1]
        self.assertEqual(final_turn["role"], "user")
        self.assertTrue(final_turn["parts"][-1]["text"].startswith("[system]"))

    def test_repeated_identical_calls_and_results_trigger_loop_detection(self) -> None:
        self._settings_values["FC_MAX_LOOPS"] = 15
        fc_ls = _fc_response([("execute_shell", {"command": "ls"})])
        wrap_resp = _text_response("wrap")
        self.mock_call_gemini.side_effect = [
            (fc_ls, "m1"), (fc_ls, "m1"), (fc_ls, "m1"), (wrap_resp, "m1"),
        ]
        self.mock_run_tool.return_value = "same"

        handlers.process_update(self._update("loop identical"))
        self._drain_queue()

        self.assertEqual(self.mock_run_tool.call_count, 3)
        self.assertEqual(self.mock_call_gemini.call_count, 4)

        last_call_kwargs = self.mock_call_gemini.call_args_list[-1][1]
        self.assertIs(last_call_kwargs["allow_tools"], False)

        reply = self.mock_send_message.call_args[0][1]
        self.assertIn("3회 연속 반복", reply)
        self.assertIn("wrap", reply)
        self.assertEqual(len(handlers._history), 2)

    def test_identical_calls_with_changing_results_are_not_flagged(self) -> None:
        fc_ls = _fc_response([("execute_shell", {"command": "ls"})])
        done_resp = _text_response("done")
        self.mock_call_gemini.side_effect = [
            (fc_ls, "m1"), (fc_ls, "m1"), (fc_ls, "m1"), (done_resp, "m1"),
        ]
        self.mock_run_tool.side_effect = ["a", "b", "c"]

        handlers.process_update(self._update("poll status"))
        self._drain_queue()

        reply = self.mock_send_message.call_args[0][1]
        self.assertEqual(reply, "done")
        for call in self.mock_call_gemini.call_args_list:
            self.assertNotIn("allow_tools", call[1])

    def test_reordered_parallel_batch_counts_as_repeat(self) -> None:
        batch_a = _fc_response([("execute_shell", {"command": "ls"}), ("execute_shell", {"command": "pwd"})])
        batch_b = _fc_response([("execute_shell", {"command": "pwd"}), ("execute_shell", {"command": "ls"})])
        wrap_resp = _text_response("wrap")
        self.mock_call_gemini.side_effect = [
            (batch_a, "m1"), (batch_b, "m1"), (batch_a, "m1"), (wrap_resp, "m1"),
        ]

        def _run_tool_side_effect(name, args):
            return "ls-output" if args.get("command") == "ls" else "pwd-output"

        self.mock_run_tool.side_effect = _run_tool_side_effect

        handlers.process_update(self._update("parallel loop"))
        self._drain_queue()

        # 3 iterations executed before the loop detector fires on the 3rd.
        self.assertEqual(self.mock_call_gemini.call_count, 4)
        reply = self.mock_send_message.call_args[0][1]
        self.assertIn("3회 연속 반복", reply)

    def test_wrapup_failure_falls_back_to_last_text(self) -> None:
        self._settings_values["FC_MAX_LOOPS"] = 1
        fc_with_text = _fc_response([("execute_shell", {"command": "ls"})], text="partial")
        fc_pending = _fc_response([("execute_shell", {"command": "pwd"})])
        self.mock_call_gemini.side_effect = [
            (fc_with_text, "m1"),
            (fc_pending, "m1"),
            Exception("boom"),
        ]
        self.mock_run_tool.return_value = "ls-output"

        handlers.process_update(self._update("cap with partial text"))
        self._drain_queue()

        reply = self.mock_send_message.call_args[0][1]
        self.assertIn("도구 호출 한도(1회)", reply)
        self.assertIn("partial", reply)
        self.assertNotIn("boom", reply)

        self.assertEqual(len(handlers._history), 2)
        self.assertEqual(handlers._history[1]["parts"], [{"text": "partial"}])

    def test_wrapup_without_any_text_sends_notice_and_keeps_no_history(self) -> None:
        self._settings_values["FC_MAX_LOOPS"] = 1
        fc_pending_1 = _fc_response([("execute_shell", {"command": "ls"})])
        fc_pending_2 = _fc_response([("execute_shell", {"command": "pwd"})])
        empty_wrapup = _text_response("")
        self.mock_call_gemini.side_effect = [
            (fc_pending_1, "m1"),
            (fc_pending_2, "m1"),
            (empty_wrapup, "m1"),
        ]
        self.mock_run_tool.return_value = "ls-output"

        handlers.process_update(self._update("cap with no text at all"))
        self._drain_queue()

        reply = self.mock_send_message.call_args[0][1]
        self.assertIn("도구 호출 한도(1회)", reply)
        self.assertIn("(요약 응답을 생성하지 못했습니다.)", reply)
        self.assertEqual(handlers._history, [])

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

    def test_process_llm_item_delegates_to_handle_llm(self) -> None:
        """_process_llm_item calls _handle_llm; retry/cooldown now lives in call_gemini."""
        sleep_patcher = mock.patch.object(handlers.time, "sleep")
        handle_llm_patcher = mock.patch.object(handlers, "_handle_llm")
        with sleep_patcher as mock_sleep, handle_llm_patcher as mock_handle_llm:
            mock_handle_llm.return_value = ("done", "")
            item = handlers._QueueItem(chat_id=1, text="test")
            handlers._process_llm_item(item)

        mock_handle_llm.assert_called_once_with(1, "test")
        mock_sleep.assert_not_called()

    def test_process_llm_item_propagates_exceptions(self) -> None:
        """_process_llm_item exceptions propagate to the worker, which notifies the user."""
        def _fail(chat_id, text):
            raise ValueError("bad input")

        with mock.patch.object(handlers, "_handle_llm", side_effect=_fail):
            item = handlers._QueueItem(chat_id=1, text="test")
            with self.assertRaises(ValueError):
                handlers._process_llm_item(item)

    def test_queue_status_retry_idle_adds_no_retry_lines(self) -> None:
        """Idle retry status adds no emoji lines."""
        with mock.patch.object(handlers, "get_retry_status", return_value={"phase": "idle"}):
            handlers.process_update(self._update("/queue"))
        reply = self.mock_send_message.call_args[0][1]
        self.assertNotIn("📡", reply)
        self.assertNotIn("⏳", reply)

    def test_queue_status_retry_calling_shows_model(self) -> None:
        """Calling phase with model shows emoji and model name."""
        with mock.patch.object(handlers, "get_retry_status",
                              return_value={"phase": "calling", "model": "gemini-x"}):
            handlers.process_update(self._update("/queue"))
        reply = self.mock_send_message.call_args[0][1]
        self.assertIn("📡 모델 호출 중: <code>gemini-x</code>", reply)

    def test_queue_status_retry_rpm_cooldown_shows_remaining(self) -> None:
        """RPM cooldown shows remaining seconds and attempt counter."""
        with mock.patch.object(handlers.time, "time", return_value=1000.0), \
             mock.patch.object(handlers, "get_retry_status",
                              return_value={
                                  "phase": "cooldown",
                                  "reason": "429_rpm",
                                  "until": 1030.0,
                                  "attempt": 1,
                                  "max_attempts": 1,
                                  "model": "m1"
                              }):
            handlers.process_update(self._update("/queue"))
        reply = self.mock_send_message.call_args[0][1]
        self.assertIn("429(RPM) 쿨다운: 30초 남음", reply)
        self.assertIn("재시도 1/1", reply)

    def test_queue_status_retry_5xx_shows_attempt(self) -> None:
        """5xx cooldown shows remaining seconds and attempt counter with 회차."""
        with mock.patch.object(handlers.time, "time", return_value=1000.0), \
             mock.patch.object(handlers, "get_retry_status",
                              return_value={
                                  "phase": "cooldown",
                                  "reason": "5xx",
                                  "until": 1020.0,
                                  "attempt": 2,
                                  "max_attempts": 3,
                                  "model": "m1"
                              }):
            handlers.process_update(self._update("/queue"))
        reply = self.mock_send_message.call_args[0][1]
        self.assertIn("서버 오류(5xx) 재시도 대기: 20초 남음", reply)
        self.assertIn("2/3회차", reply)

    def test_queue_status_retry_transport_shows_network_error(self) -> None:
        """Transport error cooldown shows network error message."""
        with mock.patch.object(handlers.time, "time", return_value=1000.0), \
             mock.patch.object(handlers, "get_retry_status",
                              return_value={
                                  "phase": "cooldown",
                                  "reason": "transport",
                                  "until": 1015.0,
                                  "attempt": 1,
                                  "max_attempts": 3,
                                  "model": "m1"
                              }):
            handlers.process_update(self._update("/queue"))
        reply = self.mock_send_message.call_args[0][1]
        self.assertIn("네트워크 오류 재시도 대기", reply)

    def test_queue_status_retry_chain_cooldown_shows_pass_counter(self) -> None:
        """Chain cooldown shows pass counter without model name."""
        with mock.patch.object(handlers.time, "time", return_value=1000.0), \
             mock.patch.object(handlers, "get_retry_status",
                              return_value={
                                  "phase": "cooldown",
                                  "reason": "chain_cooldown",
                                  "until": 1120.0,
                                  "attempt": 1,
                                  "max_attempts": 2,
                                  "model": None
                              }):
            handlers.process_update(self._update("/queue"))
        reply = self.mock_send_message.call_args[0][1]
        self.assertIn("모든 모델 실패, 전체 재시도 대기: 120초 남음 (1/2)", reply)

    def test_queue_status_retry_remaining_clamped_at_zero(self) -> None:
        """Remaining seconds clamped at zero when until is in the past."""
        with mock.patch.object(handlers.time, "time", return_value=1000.0), \
             mock.patch.object(handlers, "get_retry_status",
                              return_value={
                                  "phase": "cooldown",
                                  "reason": "transport",
                                  "until": 990.0,
                                  "attempt": 1,
                                  "max_attempts": 1,
                                  "model": "m1"
                              }):
            handlers.process_update(self._update("/queue"))
        reply = self.mock_send_message.call_args[0][1]
        self.assertIn("0초 남음", reply)

    def test_queue_status_retry_model_name_escaped(self) -> None:
        """Model name with HTML special chars is escaped."""
        with mock.patch.object(handlers, "get_retry_status",
                              return_value={
                                  "phase": "calling",
                                  "model": "<b>x"
                              }):
            handlers.process_update(self._update("/queue"))
        reply = self.mock_send_message.call_args[0][1]
        self.assertIn("&lt;b&gt;x", reply)
        self.assertNotIn("<b>x", reply)

    def test_queue_status_retry_malformed_status_does_not_crash(self) -> None:
        """Malformed status (e.g. until as string) does not crash, reply is still sent."""
        with mock.patch.object(handlers, "get_retry_status",
                              return_value={"phase": "cooldown", "until": "bad"}):
            handlers.process_update(self._update("/queue"))
        reply = self.mock_send_message.call_args[0][1]
        self.assertIn("대기 항목", reply)


class ContinuationTests(HandlersTestCase):
    """Auto-continuation chain (issue #4), driven through the real worker
    entry point (_process_llm_item) so task persistence is exercised."""

    def setUp(self) -> None:
        super().setUp()
        self._settings_values["FC_MAX_LOOPS"] = 1

    def test_limit_then_continuation_finishes(self) -> None:
        fc_pending_1 = _fc_response([("execute_shell", {"command": "ls"})])
        fc_pending_2 = _fc_response([("execute_shell", {"command": "pwd"})])
        wrapup_resp = _text_response("W1")
        done_resp = _text_response("all finished")
        self.mock_call_gemini.side_effect = [
            (fc_pending_1, "m1"), (fc_pending_2, "m1"), (wrapup_resp, "m1"), (done_resp, "m1"),
        ]
        self.mock_run_tool.return_value = "ls-output"

        handlers.process_update(self._update("do the big task"))
        self._drain_queue_full()

        self.assertEqual(self.mock_call_gemini.call_count, 4)
        continuation_contents = self.mock_call_gemini.call_args_list[3][0][0]
        last_turn = continuation_contents[-1]
        self.assertEqual(last_turn["role"], "user")
        cont_text = last_turn["parts"][-1]["text"]
        self.assertIn("Original request", cont_text)
        self.assertIn("do the big task", cont_text)
        self.assertIn("W1", cont_text)

        self.assertTrue(
            any("Continuing the remaining work automatically (1/2)" in t for t in self._sent_texts())
        )
        self.assertIsNone(tasks.load_task())

    def test_three_limits_exhausts_continuations(self) -> None:
        fc1 = _fc_response([("execute_shell", {"command": "ls"})])
        fc2 = _fc_response([("execute_shell", {"command": "pwd"})])
        wrap1 = _text_response("W1")
        wrap2 = _text_response("W2")
        wrap3 = _text_response("W3")
        self.mock_call_gemini.side_effect = [
            (fc1, "m1"), (fc2, "m1"), (wrap1, "m1"),
            (fc1, "m1"), (fc2, "m1"), (wrap2, "m1"),
            (fc1, "m1"), (fc2, "m1"), (wrap3, "m1"),
        ]
        self.mock_run_tool.return_value = "out"

        handlers.process_update(self._update("long task"))
        self._drain_queue_full()

        self.assertEqual(self.mock_call_gemini.call_count, 9)
        texts = self._sent_texts()
        self.assertTrue(any("(1/2)" in t for t in texts))
        self.assertTrue(any("(2/2)" in t for t in texts))
        self.assertTrue(any("Auto-continuation limit" in t for t in texts))

        record = tasks.load_task()
        self.assertIsNotNone(record)
        self.assertEqual(record["stop_reason"], "exhausted")
        self.assertEqual(record["continuations"], 2)
        self.assertEqual(record["last_wrapup"], "W3")

    def test_fc_max_continuations_zero_no_continuation(self) -> None:
        self._settings_values["FC_MAX_CONTINUATIONS"] = 0
        fc1 = _fc_response([("execute_shell", {"command": "ls"})])
        fc2 = _fc_response([("execute_shell", {"command": "pwd"})])
        wrap1 = _text_response("W1")
        self.mock_call_gemini.side_effect = [(fc1, "m1"), (fc2, "m1"), (wrap1, "m1")]
        self.mock_run_tool.return_value = "out"

        handlers.process_update(self._update("task"))
        self._drain_queue_full()

        self.assertEqual(self.mock_call_gemini.call_count, 3)
        record = tasks.load_task()
        self.assertIsNotNone(record)
        self.assertEqual(record["stop_reason"], "limit")
        self.assertEqual(record["continuations"], 0)
        self.assertTrue(any("not finished yet" in t for t in self._sent_texts()))

    def test_repeat_loop_outcome_no_continuation(self) -> None:
        self._settings_values["FC_MAX_LOOPS"] = 15
        fc_ls = _fc_response([("execute_shell", {"command": "ls"})])
        wrap_resp = _text_response("wrap")
        self.mock_call_gemini.side_effect = [
            (fc_ls, "m1"), (fc_ls, "m1"), (fc_ls, "m1"), (wrap_resp, "m1"),
        ]
        self.mock_run_tool.return_value = "same"

        handlers.process_update(self._update("loop identical"))
        self._drain_queue_full()

        record = tasks.load_task()
        self.assertIsNotNone(record)
        self.assertEqual(record["stop_reason"], "loop")
        self.assertEqual(record["continuations"], 0)

    def test_identical_wrapup_twice_stops_no_progress(self) -> None:
        fc1 = _fc_response([("execute_shell", {"command": "ls"})])
        fc2 = _fc_response([("execute_shell", {"command": "pwd"})])
        wrap_w1 = _text_response("W1")
        self.mock_call_gemini.side_effect = [
            (fc1, "m1"), (fc2, "m1"), (wrap_w1, "m1"),
            (fc1, "m1"), (fc2, "m1"), (wrap_w1, "m1"),
        ]
        self.mock_run_tool.return_value = "out"

        handlers.process_update(self._update("stuck task"))
        self._drain_queue_full()

        record = tasks.load_task()
        self.assertIsNotNone(record)
        self.assertEqual(record["stop_reason"], "no_progress")
        self.assertEqual(record["continuations"], 1)

    def test_wrapup_no_text_no_continuation(self) -> None:
        fc1 = _fc_response([("execute_shell", {"command": "ls"})])
        fc2 = _fc_response([("execute_shell", {"command": "pwd"})])
        self.mock_call_gemini.side_effect = [(fc1, "m1"), (fc2, "m1"), Exception("boom")]
        self.mock_run_tool.return_value = "out"

        handlers.process_update(self._update("silent task"))
        self._drain_queue_full()

        self.assertEqual(self.mock_call_gemini.call_count, 3)
        record = tasks.load_task()
        self.assertIsNotNone(record)
        self.assertEqual(record["continuations"], 0)
        self.assertEqual(record["stop_reason"], "limit")

    def test_blocked_response_deletes_file_no_hint(self) -> None:
        self.mock_call_gemini.return_value = (_blocked_response("SAFETY"), "m1")

        handlers.process_update(self._update("blocked task"))
        self._drain_queue_full()

        self.assertIsNone(tasks.load_task())
        self.assertEqual(self.mock_send_message.call_count, 1)

    def test_handle_llm_error_path_keeps_file(self) -> None:
        self.mock_call_gemini.side_effect = Exception("kaboom")

        handlers.process_update(self._update("error task"))
        self._drain_queue_full()

        record = tasks.load_task()
        self.assertIsNotNone(record)
        self.assertEqual(record["stop_reason"], "error")

    def test_abandon_mid_chain_stops_continuation(self) -> None:
        fc1 = _fc_response([("execute_shell", {"command": "ls"})])
        fc2 = _fc_response([("execute_shell", {"command": "pwd"})])
        wrap1 = _text_response("W1")
        call_state = {"count": 0}

        def _side_effect(contents, **kwargs):
            call_state["count"] += 1
            if call_state["count"] == 1:
                handlers._abandon_task("kill")
                return fc1, "m1"
            if call_state["count"] == 2:
                return fc2, "m1"
            return wrap1, "m1"

        self.mock_call_gemini.side_effect = _side_effect
        self.mock_run_tool.return_value = "out"

        handlers.process_update(self._update("abandon me"))
        self._drain_queue_full()

        self.assertEqual(self.mock_call_gemini.call_count, 3)
        self.assertIsNone(tasks.load_task())
        texts = self._sent_texts()
        self.assertFalse(any("Continuing the remaining work automatically" in t for t in texts))
        self.assertFalse(any("not finished yet" in t for t in texts))


class TaskCommandTests(HandlersTestCase):
    """/continue, /discard, /reset, /kill task-file interactions and
    notify_pending_task (issue #4)."""

    def test_continue_no_file(self) -> None:
        handlers.process_update(self._update("/continue"))
        reply = self.mock_send_message.call_args[0][1]
        self.assertEqual(reply, "No unfinished task to continue.")
        self.assertEqual(handlers._task_queue.qsize(), 0)

    def test_continue_different_chat_id(self) -> None:
        record = tasks.new_task(chat_id=999, user_id=111, prompt="other chat task")
        tasks.save_task(record)

        handlers.process_update(self._update("/continue", chat_id=1))

        reply = self.mock_send_message.call_args[0][1]
        self.assertEqual(reply, "No unfinished task to continue.")
        self.assertEqual(handlers._task_queue.qsize(), 0)

    def test_continue_with_file_enqueues_and_processes(self) -> None:
        record = tasks.new_task(chat_id=1, user_id=111, prompt="orig task")
        record["continuations"] = 2
        record["stop_reason"] = "exhausted"
        tasks.save_task(record)

        handlers.process_update(self._update("/continue", chat_id=1))

        self.assertEqual(handlers._task_queue.qsize(), 1)
        item = list(handlers._task_queue.queue)[0]
        self.assertEqual(item.kind, "continue")
        self.assertEqual(item.task_id, record["task_id"])

        self.mock_call_gemini.return_value = (_text_response("all done"), "m1")
        self._drain_queue_full()

        contents = self.mock_call_gemini.call_args_list[0][0][0]
        last_text = contents[-1]["parts"][-1]["text"]
        self.assertIn("manual", last_text)
        self.assertIsNone(tasks.load_task())

    def test_continue_item_stale_task_id(self) -> None:
        item = handlers._QueueItem(1, "/continue", user_id=None, kind="continue", task_id="nonexistent")
        handlers._task_queue.put(item)

        self._drain_queue_full()

        reply = self.mock_send_message.call_args[0][1]
        self.assertIn("already finished or discarded", reply)
        self.mock_call_gemini.assert_not_called()

    def test_continue_while_active_already_in_progress(self) -> None:
        record = tasks.new_task(chat_id=1, user_id=111, prompt="active task")
        tasks.save_task(record)
        handlers._queue_active_item = handlers._QueueItem(1, "working", task_id=record["task_id"])

        handlers.process_update(self._update("/continue", chat_id=1))

        reply = self.mock_send_message.call_args[0][1]
        self.assertEqual(reply, "That task is already in progress.")
        self.assertEqual(handlers._task_queue.qsize(), 0)

    def test_discard_deletes_file_and_bumps_epoch(self) -> None:
        record = tasks.new_task(chat_id=1, user_id=111, prompt="discard me")
        tasks.save_task(record)
        epoch_before = handlers._task_epoch

        handlers.process_update(self._update("/discard", chat_id=1))

        self.assertIsNone(tasks.load_task())
        self.assertEqual(handlers._task_epoch, epoch_before + 1)
        reply = self.mock_send_message.call_args[0][1]
        self.assertIn("Unfinished task discarded", reply)
        self.assertIn("ago", reply)

    def test_discard_wrong_chat_is_noop(self) -> None:
        record = tasks.new_task(chat_id=999, user_id=111, prompt="other")
        tasks.save_task(record)
        epoch_before = handlers._task_epoch

        handlers.process_update(self._update("/discard", chat_id=1))

        reply = self.mock_send_message.call_args[0][1]
        self.assertEqual(reply, "No unfinished task to discard.")
        self.assertEqual(handlers._task_epoch, epoch_before)
        self.assertIsNotNone(tasks.load_task())

    def test_discard_no_file_is_noop(self) -> None:
        epoch_before = handlers._task_epoch

        handlers.process_update(self._update("/discard", chat_id=1))

        reply = self.mock_send_message.call_args[0][1]
        self.assertEqual(reply, "No unfinished task to discard.")
        self.assertEqual(handlers._task_epoch, epoch_before)

    def test_reset_discards_existing_task(self) -> None:
        record = tasks.new_task(chat_id=1, user_id=111, prompt="reset target")
        tasks.save_task(record)

        handlers.process_update(self._update("/reset", chat_id=1))

        reply = self.mock_send_message.call_args[0][1]
        self.assertIn("Unfinished task discarded.", reply)
        self.assertIsNone(tasks.load_task())

    def test_kill_discards_existing_task(self) -> None:
        record = tasks.new_task(chat_id=1, user_id=111, prompt="kill target")
        tasks.save_task(record)

        handlers.process_update(self._update("/kill", chat_id=1))

        reply = self.mock_send_message.call_args[0][1]
        self.assertIn("Unfinished task discarded.", reply)
        self.assertIsNone(tasks.load_task())

    def test_help_includes_continue_and_discard(self) -> None:
        handlers.process_update(self._update("/help"))
        reply = self.mock_send_message.call_args[0][1]
        self.assertIn("/continue", reply)
        self.assertIn("/discard", reply)

    def test_notify_pending_task_valid_file(self) -> None:
        record = tasks.new_task(chat_id=1, user_id=111, prompt="pending work")
        record["stop_reason"] = "limit"
        tasks.save_task(record)

        handlers.notify_pending_task()

        self.assertEqual(self.mock_send_message.call_count, 1)
        call_args = self.mock_send_message.call_args
        self.assertEqual(call_args[0][0], 1)
        reply = call_args[0][1]
        self.assertIn("An unfinished task exists", reply)
        self.assertIn("/continue", reply)
        self.assertEqual(handlers._task_queue.qsize(), 0)

    def test_notify_pending_task_stale_file(self) -> None:
        record = tasks.new_task(chat_id=1, user_id=111, prompt="stale work")
        stale_time = time.time() - tasks.TASK_MAX_AGE_SEC - 10
        with mock.patch.object(tasks.time, "time", return_value=stale_time):
            tasks.save_task(record)

        handlers.notify_pending_task()

        self.mock_send_message.assert_not_called()
        self.assertIsNone(tasks.load_task())

    def test_notify_pending_task_user_mismatch(self) -> None:
        record = tasks.new_task(chat_id=1, user_id=222, prompt="not yours")
        tasks.save_task(record)

        handlers.notify_pending_task()

        self.mock_send_message.assert_not_called()
        self.assertIsNone(tasks.load_task())

    def test_notify_pending_task_corrupt_file(self) -> None:
        path = os.path.join(self._tasks_dir, "current.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not valid json")

        handlers.notify_pending_task()

        self.mock_send_message.assert_not_called()
        corrupt_names = [
            name for name in os.listdir(self._tasks_dir) if name.startswith("current.json.corrupt-")
        ]
        self.assertEqual(len(corrupt_names), 1)

    def test_notify_pending_task_stop_reason_none_includes_interrupted_line(self) -> None:
        record = tasks.new_task(chat_id=1, user_id=111, prompt="interrupted work")
        tasks.save_task(record)

        handlers.notify_pending_task()

        reply = self.mock_send_message.call_args[0][1]
        self.assertIn("interrupted", reply.lower())


if __name__ == "__main__":
    unittest.main()
