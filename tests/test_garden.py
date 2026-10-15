import concurrent.futures
import hashlib
import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from session_panel.garden import GardenService, artifact_digest
from session_panel.garden_native import GardenError, actor_from_environment, resolve_actor

IDS = [f"a1000000-0000-4000-8000-{number:012d}" for number in range(1, 8)]
ROOT, WORKER, REVIEWER, OTHER_ROOT, OTHER_WORKER, OTHER_REVIEWER, TASK = IDS


class Fixture:
    def __init__(self, directory):
        self.workspace = Path(directory).resolve()
        self.native = self.workspace / "native.sqlite"
        with closing(sqlite3.connect(self.native)) as db:
            db.execute("CREATE TABLE threads(id TEXT,source TEXT)")
            for thread_id in (ROOT, OTHER_ROOT):
                db.execute("INSERT INTO threads VALUES(?,?)", (thread_id, "vscode"))
            for actor, parent in ((WORKER, ROOT), (REVIEWER, ROOT), (OTHER_WORKER, OTHER_ROOT), (OTHER_REVIEWER, OTHER_ROOT)):
                source = {"subagent": {"thread_spawn": {"parent_thread_id": parent, "depth": 1,
                          "agent_path": "/root/shared-path", "agent_nickname": "Fixture"}}, "sessionId": "shared-runtime"}
                db.execute("INSERT INTO threads VALUES(?,?)", (actor, json.dumps(source)))
            db.commit()
        (self.workspace / "artifact.md").write_text("Synthetic artifact", encoding="utf-8")
        (self.workspace / "review.md").write_text("Independent synthetic review", encoding="utf-8")

    def service(self, actor):
        return GardenService(resolve_actor(actor, self.native), self.workspace, self.native)

    def assign(self):
        root = self.service(ROOT)
        root.register_actor(WORKER, "worker")
        root.register_actor(REVIEWER, "reviewer")
        root.assign_task(TASK, WORKER, "Synthetic task", "artifact.md")
        return root

    def delivered(self):
        root = self.assign()
        receipt = self.service(WORKER).submit_artifact(TASK, "Artifact delivered")["receipt"]
        return root, receipt["version"], receipt["artifact_sha256"]

    def reviewed(self):
        root, version, digest = self.delivered()
        request = root.request_review(TASK, version, digest, REVIEWER)["receipt"]
        reviewer = self.service(REVIEWER)
        reviewer.claim_review(TASK, version, digest, request["id"])
        reviewer.record_review(TASK, version, digest, "pass", "Reviewed", "review.md")
        return root, version, digest


class GardenTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = Fixture(self.temporary.name)

    def test_only_explicit_parent_chain_binds_same_path_and_runtime(self):
        self.assertEqual(resolve_actor(WORKER, self.fixture.native).root_thread_id, ROOT)
        self.assertEqual(resolve_actor(OTHER_WORKER, self.fixture.native).root_thread_id, OTHER_ROOT)
        with patch.dict("os.environ", {}, clear=True), self.assertRaises(GardenError):
            actor_from_environment()

    def test_missing_parent_cycle_bad_schema_fail_closed_without_db_write(self):
        before = self.fixture.native.read_bytes()
        for thread_id in (ROOT.upper(), "not-a-uuid", IDS[-1]):
            with self.assertRaises(GardenError):
                resolve_actor(thread_id, self.fixture.native)
        self.assertEqual(before, self.fixture.native.read_bytes())
        for source in ("{}", "not-json", json.dumps({"subagent": {"thread_spawn": {"parent_thread_id": WORKER, "depth": 1,
                      "agent_path": "/root/shared-path", "agent_nickname": "Fixture"}}})):
            with closing(sqlite3.connect(self.fixture.native)) as db:
                db.execute("UPDATE threads SET source=? WHERE id=?", (source, WORKER))
                db.commit()
            with self.assertRaises(GardenError):
                resolve_actor(WORKER, self.fixture.native)

    def test_root_cannot_submit_or_review_for_child(self):
        root = self.fixture.assign()
        before = root.snapshot()["revision"]
        for action in (lambda: root.submit_artifact(TASK, "Fake author"), lambda: root.record_review(TASK, 1, "bad", "pass", "Fake review", "review.md")):
            with self.assertRaises(GardenError):
                action()
        self.assertEqual(root.snapshot()["revision"], before)

    def test_request_is_pending_claim_is_received_and_accept_requires_pass(self):
        root, version, digest = self.fixture.delivered()
        with self.assertRaises(GardenError):
            root.accept_artifact(TASK, version, digest, "Premature")
        request = root.request_review(TASK, version, digest, REVIEWER)["receipt"]
        self.assertEqual(root.snapshot()["tasks"][TASK]["state"], "review_pending")
        reviewer = self.fixture.service(REVIEWER)
        with self.assertRaises(GardenError):
            reviewer.record_review(TASK, version, digest, "pass", "No claim", "review.md")
        reviewer.claim_review(TASK, version, digest, request["id"])
        self.assertEqual(root.snapshot()["tasks"][TASK]["state"], "reviewing")
        reviewer.record_review(TASK, version, digest, "changes_requested", "Needs correction", "review.md")
        with self.assertRaises(GardenError):
            root.accept_artifact(TASK, version, digest, "Still premature")

    def test_accepted_binds_independent_actor_artifact_and_review(self):
        root, version, digest = self.fixture.reviewed()
        result = root.accept_artifact(TASK, version, digest, "Verified delivery")
        task = root.snapshot()["tasks"][TASK]
        self.assertEqual(task["state"], "accepted")
        self.assertNotEqual(task["assignee_id"], task["reviewer_id"])
        self.assertEqual(result["receipt"]["actor_id"], ROOT)
        self.assertEqual(task["review"]["evidence_sha256"], hashlib.sha256((self.fixture.workspace / "review.md").read_bytes()).hexdigest())

    def test_cross_root_registration_task_and_review_are_rejected_without_leaks(self):
        root, version, digest = self.fixture.delivered()
        other = self.fixture.service(OTHER_ROOT)
        self.assertEqual(other.snapshot()["tasks"], {})
        with self.assertRaises(GardenError):
            root.register_actor(OTHER_REVIEWER, "reviewer")
        with self.assertRaises(GardenError):
            other.accept_artifact(TASK, version, digest, "Cross root")
        with self.assertRaises(GardenError):
            root.request_review(TASK, version, digest, OTHER_REVIEWER)
        self.assertEqual(root.snapshot()["tasks"][TASK]["state"], "delivered")
        self.assertNotEqual(root.path, other.path)

    def test_current_hash_invalidates_result_without_snapshot_writing(self):
        root, version, digest = self.fixture.reviewed()
        root.accept_artifact(TASK, version, digest, "Accepted")
        before = root.path.read_bytes()
        (self.fixture.workspace / "artifact.md").write_text("Changed artifact", encoding="utf-8")
        self.assertEqual(root.snapshot()["tasks"][TASK]["state"], "artifact_changed")
        self.assertEqual(before, root.path.read_bytes())
        with self.assertRaises(GardenError):
            root.accept_artifact(TASK, version, digest, "Accepted")
        next_receipt = self.fixture.service(WORKER).submit_artifact(TASK, "New artifact")["receipt"]
        self.assertEqual(next_receipt["version"], version + 1)
        self.assertIsNone(root.snapshot()["tasks"][TASK]["review"])

    def test_changed_review_same_artifact_recovers_once_then_can_reaccept(self):
        root, version, digest = self.fixture.reviewed()
        root.accept_artifact(TASK, version, digest, "Accepted")
        (self.fixture.workspace / "review.md").write_text("Changed review evidence", encoding="utf-8")
        self.assertEqual(root.snapshot()["tasks"][TASK]["state"], "artifact_changed")
        with self.assertRaises(GardenError):
            root.accept_artifact(TASK, version, digest, "Accepted")
        worker = self.fixture.service(WORKER)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: worker.submit_artifact(TASK, "Artifact delivered"), range(4)))
        self.assertEqual({value["receipt"]["version"] for value in results}, {version + 1})
        self.assertEqual(len({value["receipt"]["id"] for value in results}), 1)
        version += 1
        request = root.request_review(TASK, version, digest, REVIEWER)["receipt"]
        reviewer = self.fixture.service(REVIEWER)
        reviewer.claim_review(TASK, version, digest, request["id"])
        reviewer.record_review(TASK, version, digest, "pass", "New valid review", "review.md")
        root.accept_artifact(TASK, version, digest, "Reaccepted")
        self.assertEqual(root.snapshot()["tasks"][TASK]["state"], "accepted")

    def test_retries_are_idempotent_conflicting_intents_do_not_overwrite(self):
        root, version, digest = self.fixture.reviewed()
        first = root.accept_artifact(TASK, version, digest, "Accepted")
        second = root.accept_artifact(TASK, version, digest, "Accepted")
        self.assertEqual(first, second)
        with self.assertRaises(GardenError):
            root.accept_artifact(TASK, version, digest, "Other conclusion")
        with self.assertRaises(GardenError):
            self.fixture.service(REVIEWER).record_review(TASK, version, digest, "changes_requested", "Conflict", "review.md")
        self.assertEqual(root.snapshot()["tasks"][TASK]["state"], "accepted")

    def test_path_directory_escape_budget_and_stale_version_rejected(self):
        root, version, digest = self.fixture.delivered()
        for path in ("../artifact.md", str(self.fixture.workspace / "artifact.md"), "."):
            with self.assertRaises(GardenError):
                artifact_digest(self.fixture.workspace, path)
        (self.fixture.workspace / "big.md").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
        with self.assertRaises(GardenError):
            artifact_digest(self.fixture.workspace, "big.md")
        for bad_version in (True, "1", 0, version + 1):
            with self.assertRaises(GardenError):
                root.request_review(TASK, bad_version, digest, REVIEWER)
        self.assertEqual(root.snapshot()["tasks"][TASK]["state"], "delivered")

    def test_corrupt_ledger_is_not_reset_and_locked_ledger_is_not_success(self):
        root = self.fixture.assign()
        with closing(sqlite3.connect(root.path)) as db:
            db.execute("UPDATE document SET payload='not-json'")
            db.commit()
        before = root.path.read_bytes()
        with self.assertRaises(GardenError):
            root.snapshot()
        with self.assertRaises(GardenError):
            root.register_actor(WORKER, "worker")
        self.assertEqual(before, root.path.read_bytes())

    def test_busy_transaction_does_not_record_success(self):
        root = self.fixture.assign()
        before = root.snapshot()["revision"]
        with closing(sqlite3.connect(root.path)) as lock:
            lock.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(GardenError, "ledger_busy"):
                self.fixture.service(WORKER).submit_artifact(TASK, "Blocked by lock")
            lock.rollback()
        self.assertEqual(root.snapshot()["revision"], before)
        self.assertEqual(root.snapshot()["tasks"][TASK]["state"], "assigned")


if __name__ == "__main__":
    unittest.main()
