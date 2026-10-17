import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from control_everything.execution.codex import (CodexRun, COMMAND_APPROVAL, FILE_APPROVAL, FEATURES,
    WORKSPACE_CONFIG, check_config, check_thread, encoded, launch_arguments, sandbox_policy)
from control_everything.execution.store import ExecutionStore
from control_everything.execution.owned_process import ProcessError
from control_everything.workspace.store import WorkspaceError, WorkspaceStore


def config():
    return {"config": {"features": dict.fromkeys(FEATURES, False), "notify": [], "web_search": "disabled",
                       "sandbox_workspace_write": copy.deepcopy(WORKSPACE_CONFIG)}}


def thread_result(cwd, mode="workspace-write"):
    return {"cwd": cwd, "approvalPolicy": "on-request", "approvalsReviewer": "user",
            "sandbox": sandbox_policy(mode), "model": "fixture-model", "modelProvider": "fixture",
            "thread": {"id": "thread-a"}}


def notice(method, **params):
    return {"method": method, "params": {"threadId": "thread-a", "turnId": "turn-a", **params}}


def approval(identifier=1, method=COMMAND_APPROVAL, **values):
    return {"id": identifier, "method": method,
            "params": {"threadId": "thread-a", "turnId": "turn-a", "itemId": "item-a",
                       "startedAtMs": 1, "command": "echo fixture", "cwd": ".", **values}}


class MemoryTransport:
    def __init__(self, cwd, mode="workspace-write", changes=None, turn_messages=None):
        self.cwd, self.mode = cwd, mode
        self.changes, self.turn_messages = changes or {}, turn_messages
        self.messages, self.sent = [], []
        self.closed = False
        self.on_read = self.on_write = None

    def start(self):
        return self

    def write(self, value):
        self.sent.append(copy.deepcopy(value))
        if self.on_write:
            self.on_write(value)
        method = value.get("method")
        results = {"initialize": {"userAgent": "codex/0.162.0-alpha.17.2 fixture"}, "config/read": config(),
                   "thread/start": thread_result(self.cwd, self.mode), "turn/start": {"turn": {"id": "turn-a"}},
                   "turn/interrupt": {}}
        if method in results:
            result = self.changes.get(method, results[method])
            if result is not None:
                self.messages.append({"id": value["id"], "result": result})
        if method == "turn/start":
            self.messages.extend(self.turn_messages if self.turn_messages is not None else [
                notice("item/agentMessage/delta", itemId="a", delta="hel"),
                notice("item/agentMessage/delta", itemId="a", delta="lo"),
                notice("item/completed", item={"id": "a", "type": "agentMessage", "text": "hello"}),
                notice("turn/completed", turn={"id": "turn-a", "status": "completed"})])

    def read(self, timeout=.1):
        if self.on_read:
            self.on_read()
        if not self.messages:
            raise ProcessError("process_eof")
        return self.messages.pop(0)

    def close(self, grace=2):
        self.closed = True
        return True


class CodexExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = ExecutionStore(WorkspaceStore(self.root / "fixture.sqlite"))
        self.owner = str(uuid4())

    def tearDown(self):
        self.temp.cleanup()

    def worker(self, *, transport=None, mode="workspace-write", manual=False, directory="project", root_ok=True):
        cwd = self.root / directory
        cwd.mkdir(exist_ok=True)
        record, _ = self.store.admit({"request_id": str(uuid4()), "task_id": str(uuid4()), "prompt": "fixture prompt"},
            str(uuid4()), self.owner, lambda _: {"task_title": "fixture", "project_id": directory,
            "project_name": "fixture", "cwd": str(cwd), "root_identity": "{}", "mode": mode})
        transport = transport or MemoryTransport(str(cwd), mode)
        worker = CodexRun(self.store, record, self.owner, lambda _: root_ok, {"mode": mode},
                          process_factory=lambda *_: transport, resolver=lambda: sys.executable)
        if manual:
            worker.process, worker.thread_id, worker.turn_id, worker.turn_sent = transport, "thread-a", "turn-a", True
            worker._update(thread_id="thread-a", turn_id="turn-a", state="running", turn_send_started=True)
        return worker, transport

    def data(self, worker):
        return self.store.snapshot(worker.id)

    def test_new_thread_one_turn_completion_and_item_replacement(self):
        worker, transport = self.worker()
        worker.run()
        data = self.data(worker)
        self.assertEqual((data["run"]["state"], data["run"]["cleanup_state"]), ("completed", "verified"))
        methods = [row.get("method") for row in transport.sent]
        self.assertEqual(methods, ["initialize", "initialized", "config/read", "thread/start", "turn/start"])
        self.assertEqual(transport.sent[-1]["params"]["sandboxPolicy"], sandbox_policy("workspace-write"))
        self.assertTrue(transport.sent[-2]["params"]["ephemeral"])
        messages = [e["payload"] for e in data["events"] if e["kind"] == "agent_message"]
        self.assertEqual([m["text"] for m in messages], ["hel", "hello", "hello"])
        self.assertTrue(messages[-1]["final"])
        self.assertTrue(transport.closed)

    def test_read_only_parameters_and_no_workspace_fields(self):
        worker, transport = self.worker(mode="read-only")
        worker.run()
        self.assertEqual(self.data(worker)["run"]["state"], "completed")
        self.assertEqual(transport.sent[-1]["params"]["sandboxPolicy"], {"type": "readOnly", "networkAccess": False})
        self.assertNotIn("config", transport.sent[-2]["params"])

    def test_config_missing_overrides_fail_before_thread(self):
        worker, transport = self.worker()
        transport.changes["config/read"] = {"config": {"private_secret": "never persist"}}
        worker.run()
        data = self.data(worker)
        self.assertEqual(data["run"]["state"], "failed")
        self.assertEqual(data["run"]["reason"], "effective_policy_mismatch")
        self.assertNotIn("thread/start", [row.get("method") for row in transport.sent])
        self.assertNotIn("private_secret", encoded(data))

    def test_effective_policy_rejects_extra_root_network_profile_and_missing(self):
        cwd = self.root
        cases = [("approvalPolicy", "never"), ("approvalsReviewer", "auto_review"),
                 ("cwd", str(cwd / "missing")), ("sandbox", {"type": "dangerFullAccess"}),
                 ("activePermissionProfile", {"id": "unknown"})]
        for key, value in cases:
            result = thread_result(str(cwd))
            result[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ProcessError, "effective_policy_mismatch"):
                check_thread(result, str(cwd), "workspace-write")
        for key, value in (("networkAccess", True), ("excludeSlashTmp", False),
                           ("excludeTmpdirEnvVar", False), ("writableRoots", [str(cwd.parent)])):
            result = thread_result(str(cwd))
            result["sandbox"][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ProcessError, "effective_policy_mismatch"):
                check_thread(result, str(cwd), "workspace-write")
        result = thread_result(str(cwd))
        del result["sandbox"]["networkAccess"]
        with self.assertRaises(ProcessError):
            check_thread(result, str(cwd), "workspace-write")

    def test_root_changed_and_cancel_before_send_never_turn(self):
        worker, transport = self.worker(root_ok=False)
        worker.run()
        self.assertFalse(transport.sent)
        self.assertEqual(self.data(worker)["run"]["reason"], "context_changed")
        worker, transport = self.worker(directory="second")
        worker.interrupt()
        worker.run()
        self.assertEqual(self.data(worker)["run"]["state"], "cancelled_before_send")
        self.assertFalse(transport.sent)

    def test_eof_or_partial_turn_write_means_unknown_without_retry(self):
        for failure in ("eof", "partial"):
            with self.subTest(failure=failure):
                worker, transport = self.worker(directory=failure)
                transport.turn_messages = []
                if failure == "partial":
                    def fail(value):
                        if value.get("method") == "turn/start":
                            raise ProcessError("partial_write")
                    transport.on_write = fail
                worker.run()
                self.assertEqual(self.data(worker)["run"]["state"], "unknown")
                self.assertEqual(sum(row.get("method") == "turn/start" for row in transport.sent), 1)

    def test_early_turn_started_binds_and_rpc_must_match(self):
        worker, transport = self.worker()
        transport.changes["turn/start"] = None
        transport.turn_messages = [notice("turn/started", turn={"id": "turn-a"}),
            notice("item/agentMessage/delta", itemId="a", delta="early"),
            {"id": 4, "result": {"turn": {"id": "turn-b"}}}]
        worker.run()
        self.assertEqual(self.data(worker)["run"]["reason"], "turn_identity_mismatch")
        self.assertEqual(self.data(worker)["run"]["state"], "unknown")

    def test_response_direction_and_integer_string_ids_are_separate(self):
        worker, transport = self.worker(manual=True)
        worker.pending["1"] = "fixture"
        worker.handle_message(approval(1))
        worker.handle_message(approval("1"))
        worker.handle_message({"id": 1, "result": {}})
        data = self.data(worker)
        self.assertEqual(len(data["approvals"]), 2)
        self.assertIn("1", worker.responses)
        self.assertNotEqual(worker.approval_ids["1"], worker.approval_ids['"1"'])
        for identifier in (True, None, {}, []):
            with self.subTest(identifier=identifier), self.assertRaisesRegex(ProcessError, "protocol_error"):
                worker.handle_message({"id": identifier, "result": {}})

    def test_duplicate_responses_and_requests_are_idempotent_conflicts_fail(self):
        worker, _ = self.worker(manual=True)
        worker.pending["1"] = "fixture"
        worker.handle_message({"id": 1, "result": {}})
        worker.handle_message({"id": 1, "result": {}})
        with self.assertRaisesRegex(ProcessError, "conflicting_rpc_response"):
            worker.handle_message({"id": 1, "result": {"changed": True}})
        worker.handle_message(approval(1))
        worker.handle_message(approval(1))
        self.assertEqual(len(self.data(worker)["approvals"]), 1)
        with self.assertRaisesRegex(ProcessError, "conflicting_server_request"):
            worker.handle_message(approval(1, command="changed"))
        worker.handle_message(approval(2))
        self.assertEqual(len(self.data(worker)["approvals"]), 2)

    def test_two_browser_decisions_only_one_claim_and_one_send(self):
        worker, transport = self.worker(manual=True)
        worker.handle_message(approval())
        identifier = self.data(worker)["approvals"][0]["id"]
        outcomes = []
        barrier = threading.Barrier(3)
        def decide(decision):
            barrier.wait()
            try:
                outcomes.append(worker.answer(identifier, decision))
            except WorkspaceError as error:
                outcomes.append(str(error))
        threads = [threading.Thread(target=decide, args=(value,)) for value in ("accept", "decline")]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(2)
        self.assertIn("decision_conflict", outcomes)
        worker._tick()
        replies = [row for row in transport.sent if "result" in row]
        self.assertEqual(len(replies), 1)
        decision = replies[0]["result"]["decision"]
        self.assertTrue(worker.answer(identifier, decision)[1])
        worker._tick()
        self.assertEqual(len([row for row in transport.sent if "result" in row]), 1)

    def test_cancel_claim_before_queued_accept_prevents_send(self):
        worker, transport = self.worker(manual=True)
        worker.handle_message(approval())
        identifier = self.data(worker)["approvals"][0]["id"]
        worker.answer(identifier, "accept")
        self.store.request_cancel(worker.id, self.owner)
        worker.interrupt()
        worker._tick()
        worker._tick()
        self.assertFalse(any("result" in row for row in transport.sent))
        self.assertEqual(sum(row.get("method") == "turn/interrupt" for row in transport.sent), 1)
        self.assertEqual(self.store.get_approval(worker.id, identifier)["status"], "expired")

    def test_accept_before_cancel_keeps_sent_fact(self):
        worker, transport = self.worker(manual=True)
        worker.handle_message(approval())
        identifier = self.data(worker)["approvals"][0]["id"]
        worker.answer(identifier, "accept")
        worker._tick()
        self.store.request_cancel(worker.id, self.owner)
        worker.interrupt()
        worker._tick()
        self.assertEqual(self.store.get_approval(worker.id, identifier)["status"], "sent")
        self.assertEqual([row.get("result", {}).get("decision") for row in transport.sent].count("accept"), 1)

    def test_answer_partial_write_is_unknown_and_not_retried(self):
        worker, transport = self.worker(manual=True)
        worker.handle_message(approval())
        identifier = self.data(worker)["approvals"][0]["id"]
        worker.answer(identifier, "accept")
        transport.on_write = lambda _: (_ for _ in ()).throw(ProcessError("partial_write"))
        with self.assertRaisesRegex(ProcessError, "partial_write"):
            worker._tick()
        self.assertEqual(self.store.get_approval(worker.id, identifier)["status"], "unknown")
        self.assertTrue(worker.answer(identifier, "accept")[1])
        self.assertFalse(worker.actions)

    def test_missing_or_oversized_approval_detail_cannot_accept(self):
        worker, _ = self.worker(manual=True)
        for index, values in enumerate(({"command": None}, {"command": "x" * 65536})):
            worker.handle_message(approval(index, **values))
            identifier = worker.approval_ids[str(index)]
            self.assertFalse(self.store.get_approval(worker.id, identifier)["detail_complete"])
            with self.assertRaisesRegex(WorkspaceError, "approval_expired"):
                worker.answer(identifier, "accept")
            worker.answer(identifier, "decline")

    def test_file_approval_requires_same_item_diff_and_preserves_full_diff(self):
        worker, _ = self.worker(manual=True)
        worker.handle_message(approval(1, FILE_APPROVAL))
        self.assertFalse(self.data(worker)["approvals"][0]["can_accept"])
        worker.handle_message(notice("item/started", item={"id": "item-b", "type": "fileChange", "status": "inProgress",
            "changes": [{"path": "marker.txt", "kind": {"type": "add"}, "diff": "+" + "x" * 5000}]}))
        worker.handle_message(approval(2, FILE_APPROVAL, itemId="item-b"))
        details = self.store.get_approval(worker.id, worker.approval_ids["2"])
        self.assertTrue(details["detail_complete"])
        self.assertEqual(len(json.loads(details["detail_json"])["changes"][0]["diff"]), 5001)

    def test_unsupported_request_during_initialize_visible_error_no_turn(self):
        worker, transport = self.worker()
        original = transport.write
        def write(message):
            original(message)
            if message.get("method") == "initialize":
                transport.messages.insert(0, {"id": 1, "method": "item/tool/requestUserInput", "params": {}})
        transport.write = write
        worker.run()
        self.assertEqual(self.data(worker)["run"]["state"], "cancelled_before_send")
        self.assertFalse(any(row.get("method") == "turn/start" for row in transport.sent))
        self.assertTrue(any(row.get("error", {}).get("message") == "unsupported_request" for row in transport.sent))

    def test_unrelated_turns_reasoning_idle_and_retry_error_cannot_finish(self):
        worker, _ = self.worker(manual=True)
        worker.handle_message(notice("turn/completed", threadId="other", turn={"id": "turn-a", "status": "completed"}))
        worker.handle_message(notice("turn/completed", turn={"id": "other", "status": "completed"}))
        worker.handle_message(notice("thread/status/changed", status={"type": "idle"}))
        worker.handle_message(notice("item/reasoning/summaryTextDelta", delta="do not store"))
        worker.handle_message(notice("error", error={"message": "private native error"}, willRetry=True))
        self.assertEqual(self.data(worker)["run"]["state"], "running")
        self.assertNotIn("private", encoded(self.data(worker)))
        self.assertNotIn("do not store", encoded(self.data(worker)))

    def test_cancelled_late_completion_retains_native_result_and_duplicate_final(self):
        worker, _ = self.worker(manual=True)
        self.store.request_cancel(worker.id, self.owner)
        worker.interrupt()
        complete = notice("turn/completed", turn={"id": "turn-a", "status": "completed"})
        worker.handle_message(complete)
        worker.handle_message(complete)
        self.assertEqual(self.data(worker)["run"]["state"], "completed")
        self.assertTrue(self.data(worker)["run"]["cancel_requested"])
        with self.assertRaisesRegex(ProcessError, "conflicting_terminal"):
            worker.handle_message(notice("turn/completed", turn={"id": "turn-a", "status": "interrupted"}))
        self.assertEqual(self.data(worker)["run"]["state"], "completed")

    def test_resolved_before_sending_expires_queued_action(self):
        worker, transport = self.worker(manual=True)
        worker.handle_message(approval())
        identifier = worker.approval_ids["1"]
        worker.answer(identifier, "accept")
        worker.handle_message(notice("serverRequest/resolved", requestId=1))
        worker._tick()
        self.assertFalse(any("result" in row for row in transport.sent))
        self.assertEqual(self.store.get_approval(worker.id, identifier)["status"], "resolved")

    def test_two_runs_same_native_ids_do_not_share_approval_or_events(self):
        first, a = self.worker(manual=True, directory="a")
        second, b = self.worker(manual=True, directory="b")
        for worker in (first, second):
            worker.handle_message(approval())
        first.answer(first.approval_ids["1"], "accept")
        first._tick()
        self.assertTrue(any("result" in row for row in a.sent))
        self.assertFalse(b.sent)
        self.assertEqual(self.data(second)["approvals"][0]["status"], "pending")

    def test_usage_snapshot_zero_null_duplicate_and_regression(self):
        worker, _ = self.worker(manual=True)
        for amount in (0, 10, 10, 8):
            worker.handle_message(notice("thread/tokenUsage/updated", tokenUsage={"total": {"totalTokens": amount}, "last": None}))
        usage = self.data(worker)["run"]["token_usage"]
        self.assertEqual(usage["total"]["totalTokens"], 10)
        self.assertIsNone(usage["total"]["inputTokens"])
        self.assertIsNone(usage["last"])
        self.assertTrue(usage["usage_inconsistent"])
        worker.handle_message(notice("thread/tokenUsage/updated", tokenUsage={"total": None, "last": None}))
        self.assertIsNone(self.data(worker)["run"]["token_usage"]["total"])

    def test_buffer_limit_unknown_response_and_invalid_envelopes(self):
        worker, _ = self.worker()
        for _ in range(32):
            worker.handle_message(notice("thread/status/changed", status={"type": "idle"}))
        with self.assertRaisesRegex(ProcessError, "protocol_buffer_full"):
            worker.handle_message(notice("thread/status/changed", status={"type": "idle"}))
        for message in ({}, {"id": 1, "result": {}, "error": {}}, {"method": 5}, {"method": "x", "params": []}):
            with self.subTest(message=message), self.assertRaises(ProcessError):
                worker.handle_message(message)
        with self.assertRaisesRegex(ProcessError, "unexpected_rpc_response"):
            worker.handle_message({"id": 100, "result": {}})

    def test_fixed_launch_overrides_and_internal_mcp_check(self):
        args = launch_arguments(["fixture-mcp"])
        self.assertNotIn("--strict-config", args)
        self.assertIn("mcp_servers.fixture-mcp.enabled=false", args)
        with self.assertRaises(ProcessError):
            launch_arguments(["x.invalid"])
        values = config()
        values["config"]["mcp_servers"] = {"fixture-mcp": {"enabled": False, "private_header": "never return"}}
        self.assertTrue(check_config(values, ["fixture-mcp"])["test_mcp_disabled"])
        values["config"]["mcp_servers"]["unexpected"] = {"enabled": True}
        with self.assertRaises(ProcessError):
            check_config(values, ["fixture-mcp"])

    def test_rpc_timeout_never_resends_and_cleanup_unverified_remains_separate(self):
        worker, transport = self.worker()
        transport.changes["initialize"] = None
        transport.read = lambda timeout: None
        transport.close = lambda grace: False
        with patch("control_everything.execution.codex.time.monotonic", side_effect=[0, 21]):
            worker.run()
        data = self.data(worker)
        self.assertEqual((data["run"]["state"], data["run"]["reason"], data["run"]["cleanup_state"]),
                         ("failed", "rpc_timeout", "unverified"))
        self.assertEqual(sum(row.get("method") == "initialize" for row in transport.sent), 1)

    def test_cancel_grace_starts_at_interrupt_and_no_terminal_is_unknown(self):
        worker, transport = self.worker(manual=True)
        worker.turn_id = None
        self.store.request_cancel(worker.id, self.owner)
        worker.interrupt()
        self.assertIsNone(worker.cancel_deadline)
        with patch("control_everything.execution.codex.time.monotonic", return_value=100):
            worker._tick()
            self.assertFalse(transport.sent)
            worker._bind_turn("turn-a")
            worker._tick()
        self.assertEqual(worker.cancel_deadline, 105)
        with patch("control_everything.execution.codex.time.monotonic", return_value=106):
            with self.assertRaisesRegex(ProcessError, "cancel_timeout"):
                worker._tick()
        self.assertIsNone(worker.terminal)
        self.assertEqual(sum(row.get("method") == "turn/interrupt" for row in transport.sent), 1)

    def test_early_approval_and_completed_before_turn_response(self):
        worker, transport = self.worker()
        transport.changes["turn/start"] = None
        transport.turn_messages = [notice("turn/started", turn={"id": "turn-a"}), approval(),
            notice("turn/completed", turn={"id": "turn-a", "status": "completed"}),
            {"id": 4, "result": {"turn": {"id": "turn-a"}}}]
        worker.run()
        data = self.data(worker)
        self.assertEqual(data["run"]["state"], "completed")
        self.assertEqual(data["approvals"][0]["status"], "expired")

    def test_root_rechecked_before_thread_and_turn(self):
        worker, transport = self.worker()
        checks = iter([True, True, False])
        worker.validate_root = lambda _: next(checks)
        worker.run()
        methods = [row.get("method") for row in transport.sent]
        self.assertIn("thread/start", methods)
        self.assertNotIn("turn/start", methods)
        self.assertEqual(self.data(worker)["run"]["reason"], "context_changed")

    def test_cancel_new_approval_does_not_end_early_or_accept(self):
        worker, transport = self.worker(manual=True)
        self.store.request_cancel(worker.id, self.owner)
        worker.interrupt()
        worker.handle_message(approval())
        worker._tick()
        self.assertFalse(self.data(worker)["approvals"])
        self.assertEqual(self.data(worker)["run"]["state"], "cancel_requested")
        self.assertFalse(any("result" in row for row in transport.sent))

    def test_final_message_text_and_command_output_truncation_are_explicit(self):
        worker, _ = self.worker(manual=True)
        worker.handle_message(notice("item/agentMessage/delta", itemId="large", delta="中" * 5000))
        worker.handle_message(notice("item/completed", item={"id": "large", "type": "agentMessage", "text": "x" * 40000}))
        worker.handle_message(notice("item/commandExecution/outputDelta", itemId="cmd", delta="x" * 6000))
        events = self.data(worker)["events"]
        final = [row["payload"] for row in events if row["kind"] == "agent_message"][-1]
        command = [row["payload"] for row in events if row["kind"] == "command"][-1]
        self.assertEqual(len(final["text"].encode()), 32768)
        self.assertEqual(len(command["output"].encode()), 4096)
        self.assertTrue(final["truncated"] and command["truncated"])

    def test_run_reports_queued_conflicting_terminal_without_overwriting_fact(self):
        worker, transport = self.worker()
        transport.turn_messages = [notice("turn/completed", turn={"id": "turn-a", "status": "completed"}),
                                   notice("turn/completed", turn={"id": "turn-a", "status": "interrupted"})]
        worker.run()
        data = self.data(worker)
        self.assertEqual(data["run"]["state"], "completed")
        self.assertTrue(any(row["payload"].get("reason") == "conflicting_terminal" for row in data["events"]))


if __name__ == "__main__":
    unittest.main()
