import json
import sqlite3
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from uuid import uuid4

from session_panel.execution.service import ExecutionService, resolve_root
from session_panel.execution.store import ExecutionStore, overlapping_roots
from session_panel.workspace.service import WorkspaceService
from session_panel.workspace.store import WorkspaceError


class FakeOwner:
    def __init__(self, _path, acquired=True):
        self.acquired, self.is_owner, self.closed = acquired, False, False

    def acquire(self):
        self.is_owner = self.acquired
        return self.is_owner

    def close(self):
        self.closed, self.is_owner = True, False


class HoldingWorker:
    def __init__(self, **kwargs):
        self.store, self.row, self.owner_id = kwargs["store"], kwargs["run"], kwargs["owner_id"]
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.interrupts = 0

    def run(self):
        self.stop.wait()

    def interrupt(self):
        self.interrupts += 1
        self.stop.set()

    def answer(self, approval_id, decision):
        with self.lock:
            return self.store.claim_approval(self.row["id"], approval_id, self.owner_id, decision)

    def close(self):
        self.stop.set()


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.roots = {name: self.root / name for name in ("one", "two", "three")}
        self.roots["child"] = self.roots["one"] / "child"
        for path in self.roots.values():
            path.mkdir(parents=True, exist_ok=True)
        self.registry = self.root / "registry.json"
        self.write_registry()
        self.workspace = WorkspaceService(self.root / "state.sqlite3", registry_path=self.registry)
        self.tasks = {name: self.workspace.store.save_task({"title": name, "project_id": name,
                      "provider_id": "codex", "state": "todo", "session_key": None}) for name in self.roots}
        self.workers = []
        self.owner = FakeOwner(None)
        self.service = self.make_service(owner=self.owner)
        self.store = self.service.store

    def write_registry(self):
        self.registry.write_text(json.dumps({"projects": [{"id": name, "name": name, "root": str(path)}
                                                          for name, path in self.roots.items()]}), encoding="utf-8")

    def worker_factory(self, **kwargs):
        worker = HoldingWorker(**kwargs)
        self.workers.append(worker)
        return worker

    def make_service(self, owner=None, **kwargs):
        service = ExecutionService(self.workspace, owner_factory=lambda _: owner or FakeOwner(None),
                                   resolver=lambda: Path("fixture.exe"), job_query=lambda _: True,
                                   worker_factory=self.worker_factory, platform="win32", **kwargs)
        self.addCleanup(service.close)
        return service

    def body(self, name="one", **changes):
        task_id = self.tasks[name]["id"]
        return {"request_id": str(uuid4()), "task_id": task_id,
                "context_hash": self.service.context(task_id)["context_hash"], "prompt": "执行测试", **changes}

    def admit(self, name="one"):
        return self.service.submit(self.body(name))["run"]

    def approval(self, run):
        self.store.update_run(run["id"], thread_id="thread", turn_id="turn", state="waiting_approval")
        return self.store.add_approval(run["id"], self.service.owner_id, "1", "item/commandExecution/requestApproval",
                                       "thread", "turn", "item", {"command": "echo test", "cwd": run["cwd"]}, True)

    def test_same_request_is_admitted_once_even_after_context_and_owner_change(self):
        body = self.body()
        first = self.service.submit(body)
        self.workspace.store.save_task({**self.tasks["one"], "provider_id": "claude", "state": "done"}, self.tasks["one"]["id"])
        self.service._reason = "other_instance"
        replay = self.service.submit(body)
        self.assertEqual(first["run"]["id"], replay["run"]["id"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(len(self.workers), 1)
        with self.assertRaisesRegex(WorkspaceError, "idempotency_conflict"):
            self.service.submit({**body, "prompt": "different"})

    def test_concurrent_same_request_creates_one_worker(self):
        body = self.body()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.service.submit(body), range(16)))
        self.assertEqual(len({row["run"]["id"] for row in results}), 1)
        self.assertEqual(sum(not row["replayed"] for row in results), 1)
        self.assertEqual(len(self.workers), 1)

    def test_two_roots_capacity_overlap_and_cleanup_reservation(self):
        one = self.admit()
        with self.assertRaisesRegex(WorkspaceError, "project_busy"):
            self.admit("child")
        two = self.admit("two")
        with self.assertRaisesRegex(WorkspaceError, "capacity_busy"):
            self.admit("three")
        self.store.update_run(one["id"], state="completed", cleanup_state="unverified")
        with self.assertRaisesRegex(WorkspaceError, "project_busy"):
            self.admit()
        self.store.update_run(one["id"], cleanup_state="verified")
        three = self.admit("three")
        self.assertEqual({row["id"] for row in self.service.snapshot()["active"]}, {two["id"], three["id"]})

    def test_context_changes_reject_and_run_keeps_original_task_snapshot(self):
        body = self.body()
        updated = {**self.tasks["one"], "title": "renamed", "project_id": "two"}
        self.workspace.store.save_task(updated, updated["id"])
        with self.assertRaisesRegex(WorkspaceError, "context_changed"):
            self.service.submit(body)
        run = self.admit()
        self.workspace.store.save_task({**updated, "title": "later", "provider_id": "claude"}, updated["id"])
        persisted = self.store.get_run(run["id"])
        self.assertEqual((persisted["task_title"], persisted["project_id"]), ("renamed", "two"))
        self.assertTrue(self.service.validate_root(persisted))
        self.roots["two"] = self.roots["three"]
        self.write_registry()
        self.assertFalse(self.service.validate_root(persisted))

    def test_invalid_exact_fields_uuid_prompt_and_provider_do_not_admit(self):
        good = self.body()
        for change in ({"cwd": str(self.root)}, {"mode": "danger-full-access"}, {"request_id": "bad"},
                       {"prompt": " "}, {"prompt": "x" * 4001}, {"prompt": "\ud800"}, {"context_hash": "bad"}):
            with self.subTest(change=repr(change)[:100]), self.assertRaises(ValueError):
                self.service.submit({**good, **change})
        self.assertEqual(self.store.listing()["total"], 0)
        accepted = self.service.submit({**good, "prompt": "😀" * 4000})
        self.assertFalse(accepted["replayed"])
        self.assertEqual(self.workspace.store.get_task(good["task_id"])["state"], "todo")

    def test_path_identity_and_parent_boundaries(self):
        cwd, identity = resolve_root(str(self.roots["one"]))
        self.assertEqual(json.loads(identity)["cwd"], cwd)
        self.assertTrue(overlapping_roots(cwd, str(self.roots["child"])))
        self.assertFalse(overlapping_roots(cwd, cwd + "-other"))
        for value in (str(self.root / "missing"), str(self.registry), self.root.anchor, r"\\server\share\project"):
            with self.subTest(value=value), self.assertRaisesRegex(WorkspaceError, "context_changed"):
                resolve_root(value)
        self.admit()
        actual_resolve = resolve_root
        with patch("session_panel.execution.service.resolve_root", side_effect=lambda value:
                   actual_resolve(str(self.roots["one"])) if value == str(self.roots["two"]) else actual_resolve(value)):
            with self.assertRaisesRegex(WorkspaceError, "project_busy"):
                self.admit("two")

    def test_other_owner_can_read_but_cannot_change_or_recover(self):
        run = self.admit()
        other = self.make_service(owner=FakeOwner(None, acquired=False))
        self.assertEqual(other.capability()["reason"], "other_instance")
        self.assertEqual(other.snapshot(run["id"])["run"]["state"], "starting")
        with self.assertRaisesRegex(WorkspaceError, "other_instance"):
            other.submit(self.body("two"))
        with self.assertRaisesRegex(WorkspaceError, "other_instance"):
            other.act(run["id"], {})
        self.assertEqual(self.store.get_run(run["id"])["state"], "starting")

    def test_restart_expires_requests_and_never_replays(self):
        run = self.admit()
        approval = self.approval(run)
        self.store.update_run(run["id"], job_name="old-job", turn_send_started=1)
        self.service.close()
        query = Mock(return_value=False)
        restarted = ExecutionService(self.workspace, owner_factory=FakeOwner, resolver=lambda: Path("fixture.exe"),
                                     job_query=query, worker_factory=self.worker_factory, platform="win32")
        self.addCleanup(restarted.close)
        row = restarted.store.get_run(run["id"])
        self.assertEqual((row["state"], row["cleanup_state"]), ("unknown", "unverified"))
        self.assertEqual(restarted.store.get_approval(run["id"], approval["id"])["status"], "expired")
        self.assertEqual(len(self.workers), 1)
        query.assert_called_once_with("old-job")

    def test_approval_claim_atomic_replays_and_conflicts(self):
        run = self.admit()
        approval = self.approval(run)
        with ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(lambda _: self.service.act(run["id"], {"decision": "accept"}, approval_id=approval["id"]), range(8)))
        self.assertEqual(sum(not row["replayed"] for row in replies), 1)
        with self.assertRaisesRegex(WorkspaceError, "decision_conflict"):
            self.service.act(run["id"], {"decision": "decline"}, approval_id=approval["id"])
        self.assertNotIn("native_id_json", replies[0]["approval"])

    def test_cancel_prevents_accept_and_duplicate_cancellation(self):
        run = self.admit()
        approval = self.approval(run)
        self.assertFalse(self.service.act(run["id"], {})["replayed"])
        self.assertTrue(self.service.act(run["id"], {})["replayed"])
        with self.assertRaisesRegex(WorkspaceError, "approval_expired"):
            self.store.claim_approval(run["id"], approval["id"], self.service.owner_id, "accept")
        self.assertEqual(self.workers[0].interrupts, 1)

    def test_native_id_types_and_truncated_approvals(self):
        run = self.admit()
        first = self.approval(run)
        second = self.store.add_approval(run["id"], self.service.owner_id, '"1"', first["method"], "thread", "turn", "item",
                                         {"command": "x" * 70000}, True)
        self.assertNotEqual(first["id"], second["id"])
        self.assertFalse(second["detail_complete"])
        with self.assertRaisesRegex(WorkspaceError, "approval_expired"):
            self.store.claim_approval(run["id"], second["id"], self.service.owner_id, "accept")
        self.assertFalse(self.store.claim_approval(run["id"], second["id"], self.service.owner_id, "decline")[1])
        with self.assertRaisesRegex(WorkspaceError, "protocol_error"):
            self.store.add_approval(run["id"], self.service.owner_id, "1", first["method"], "thread", "turn", "item", {"command": "different"}, True)

    def test_events_are_isolated_bounded_and_report_real_pruning(self):
        one, two = self.admit(), self.admit("two")
        self.store.append_event(two["id"], "phase", {"text": "other"})
        self.store.append_event(one["id"], "agent_message", {"text": "😀" * 3000})
        first = self.store.snapshot(one["id"])
        self.assertFalse(first["gap"])
        self.assertLessEqual(len(first["events"][0]["payload"]["text"].encode("utf-8")), 4096)
        self.assertTrue(first["events"][0]["payload"]["truncated"])
        # Bulk seeding uses the same real table; the append being tested performs retention.
        with self.store.connection(immediate=True) as connection:
            connection.executemany("INSERT INTO execution_events(run_id,kind,payload_json,observed_at) VALUES (?,'phase','{}','now')",
                                   [(one["id"],)] * 2000)
        self.store.append_event(one["id"], "phase", {"text": "last"})
        page = self.store.snapshot(one["id"], first["next_cursor"])
        self.assertTrue(page["gap"])
        self.assertTrue(page["has_more"])
        self.assertEqual(len(page["events"]), 200)
        self.assertFalse(self.store.snapshot(one["id"], page["next_cursor"])["gap"])
        self.assertEqual(self.store.snapshot(two["id"])["events"][0]["payload"]["text"], "other")
        with self.assertRaises(ValueError):
            self.store.append_event(one["id"], "reasoning", {})

    def test_terminal_facts_are_not_overwritten(self):
        run = self.admit()
        self.store.update_run(run["id"], state="completed")
        self.store.update_run(run["id"], state="completed", cleanup_state="verified")
        with self.assertRaisesRegex(WorkspaceError, "protocol_error"):
            self.store.update_run(run["id"], state="interrupted")
        self.assertEqual(self.store.get_run(run["id"])["state"], "completed")

    def test_missing_and_busy_storage_remain_fixed_failures(self):
        with self.assertRaisesRegex(WorkspaceError, "request_not_found"):
            self.service.snapshot(request_id=str(uuid4()))
        with self.assertRaisesRegex(WorkspaceError, "run_not_found"):
            self.service.snapshot(str(uuid4()))
        body = self.body()
        blocker = sqlite3.connect(self.store.path)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(WorkspaceError, "storage_unavailable"):
                self.service.submit(body)
        finally:
            blocker.close()
        self.assertIsNone(self.store.find_request(body["request_id"]))

    def test_failed_initialization_releases_owner_and_nonwindows_is_readonly(self):
        owner = FakeOwner(None)
        unavailable = ExecutionService(self.workspace, owner_factory=lambda _: owner,
                                        resolver=Mock(side_effect=OSError("private")), job_query=lambda _: True, platform="win32")
        self.assertTrue(owner.closed)
        self.assertEqual(unavailable.capability()["reason"], "execution_unavailable")
        nonwindows = ExecutionService(self.workspace, platform="linux")
        self.assertEqual(nonwindows.snapshot()["capability"]["reason"], "platform_unsupported")

    def test_worker_start_failure_is_queryable_and_never_retried(self):
        self.service._worker_factory = Mock(side_effect=RuntimeError("private startup detail"))
        body = self.body()
        self.service.submit(body)
        run = self.service.snapshot(request_id=body["request_id"])["run"]
        self.assertEqual((run["state"], run["reason"], run["cleanup_state"]), ("failed", "worker_start_failed", "verified"))
        self.assertTrue(self.service.submit(body)["replayed"])
        self.service._worker_factory.assert_called_once()

    def test_close_keeps_owner_until_last_worker_finishes(self):
        worker, thread = Mock(), Mock()
        worker.lock = threading.RLock()
        self.service._workers["late"] = (worker, thread)
        self.service.close()
        self.assertTrue(self.owner.is_owner)
        self.assertFalse(self.service.capability()["available"])
        self.service._run_worker("late", worker)
        self.assertTrue(self.owner.closed)

    def test_server_close_records_cancellation_before_interrupt(self):
        run = self.admit()
        self.service.close()
        row = self.store.get_run(run["id"])
        self.assertEqual((row["cancel_requested"], row["state"]), (1, "cancel_requested"))
        self.assertTrue(self.owner.closed)

    def test_concurrent_distinct_roots_never_admit_a_third(self):
        bodies = [self.body(name) for name in ("one", "two", "three")]

        def submit(body):
            try:
                return self.service.submit(body)
            except WorkspaceError as error:
                return str(error)

        with ThreadPoolExecutor(max_workers=3) as pool:
            result = list(pool.map(submit, bodies))
        self.assertEqual(sum(isinstance(row, dict) for row in result), 2)
        self.assertEqual(result.count("capacity_busy"), 1)
        self.assertEqual(len(self.workers), 2)

    def test_completed_run_expires_sent_approval_without_losing_decision(self):
        run = self.admit()
        approval = self.approval(run)
        self.store.claim_approval(run["id"], approval["id"], self.service.owner_id, "accept")
        self.store.update_approval(run["id"], approval["id"], status="sent")
        self.store.update_run(run["id"], state="completed")
        self.store.expire_approvals(run["id"])
        row, replayed = self.store.claim_approval(run["id"], approval["id"], self.service.owner_id, "accept")
        self.assertEqual((row["status"], row["decision"], replayed), ("expired", "accept", True))


if __name__ == "__main__":
    unittest.main()
