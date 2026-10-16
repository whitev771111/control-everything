"""A small per-main-thread artifact/review ledger. No agent execution or terminal control."""

import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from session_panel.paths import REPOSITORY_ROOT

from session_panel.garden.native import GardenError, resolve_actor
from session_panel.sessions.source import canonical_uuid

WORKSPACE = REPOSITORY_ROOT
CAPABILITIES = {"coordinator": ["garden_register_actor", "garden_assign_task", "garden_request_review", "garden_accept_artifact"],
                "worker": ["garden_submit_artifact"], "reviewer": ["garden_claim_review", "garden_record_review"]}


def text(value, maximum=400):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise GardenError("invalid_text")
    return value


def relative_path(value):
    text(value, 240)
    path = Path(value)
    if path.is_absolute() or path.drive or ".." in path.parts:
        raise GardenError("artifact_path_rejected")
    return path.as_posix()


def artifact_digest(workspace, value):
    try:
        relative = relative_path(value)
        path = (workspace / relative).resolve(strict=True)
        if not path.is_relative_to(workspace) or not path.is_file():
            raise GardenError("artifact_path_rejected")
        before = path.stat()
        if before.st_size > 2 * 1024 * 1024:
            raise GardenError("artifact_too_large")
        with path.open("rb") as handle:
            payload = handle.read(2 * 1024 * 1024 + 1)
        after = path.stat()
        if len(payload) > 2 * 1024 * 1024 or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise GardenError("artifact_changed")
        return hashlib.sha256(payload).hexdigest()
    except GardenError:
        raise
    except (OSError, ValueError):
        raise GardenError("artifact_unavailable") from None


class GardenService:
    def __init__(self, context, workspace=WORKSPACE, native_db=None):
        self.context = context
        self.workspace = Path(workspace).resolve()
        self.native_db = native_db
        self.path = self.workspace / "work/garden-state" / (context.root_thread_id + ".sqlite")

    def native(self, actor_id):
        return resolve_actor(actor_id, self.native_db) if self.native_db is not None else resolve_actor(actor_id)

    def binding(self):
        if self.native(self.context.actor_id) != self.context:
            raise GardenError("native_binding_changed")

    def empty(self):
        actor = {**self.context.public(), "actor_id": self.context.root_thread_id, "parent_thread_id": None,
                 "role": "coordinator", "nickname": "协调者", "agent_path": "", "capabilities": CAPABILITIES["coordinator"]}
        return {"protocol_version": 1, "root_thread_id": self.context.root_thread_id, "revision": 0,
                "actors": {actor["actor_id"]: actor}, "tasks": {}, "receipts": []}

    def read(self, db):
        row = db.execute("SELECT payload FROM document WHERE id=1").fetchone()
        try:
            doc = json.loads(row[0]) if row else self.empty()
        except (ValueError, TypeError):
            raise GardenError("ledger_invalid") from None
        if not isinstance(doc, dict):
            raise GardenError("ledger_invalid")
        if doc.get("protocol_version") != 1 or doc.get("root_thread_id") != self.context.root_thread_id:
            raise GardenError("ledger_invalid")
        if not isinstance(doc.get("actors"), dict) or not isinstance(doc.get("tasks"), dict) or not isinstance(doc.get("receipts"), list):
            raise GardenError("ledger_invalid")
        return doc

    def validity(self, task):
        if task["version"] == 0:
            return True
        try:
            if artifact_digest(self.workspace, task["artifact_path"]) != task["artifact_sha256"]:
                return False
            if task.get("review"):
                review = task["review"]
                if artifact_digest(self.workspace, review["evidence_path"]) != review["evidence_sha256"]:
                    return False
            return True
        except GardenError:
            return False

    def snapshot(self):
        self.binding()
        try:
            if self.path.exists():
                with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
                    db.execute("PRAGMA query_only=ON")
                    doc = self.read(db)
            else:
                doc = self.empty()
            for actor in doc["actors"].values():
                bound = self.native(actor["actor_id"])
                if bound.root_thread_id != self.context.root_thread_id:
                    raise GardenError("native_binding_changed")
            for task in doc["tasks"].values():
                task["current_valid"] = self.validity(task)
                if not task["current_valid"]:
                    task["state"] = "artifact_changed"
            doc.update(available=True, caller=self.context.public(), source="local_garden_ledger")
            return doc
        except GardenError:
            raise
        except (sqlite3.Error, ValueError, KeyError, TypeError):
            raise GardenError("ledger_unavailable") from None

    def mutate(self, operation, payload, source):
        self.binding()
        if source not in ("mcp_stdio", "panel_web"):
            raise GardenError("invalid_source")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            initialize = not self.path.exists()
            with closing(sqlite3.connect(self.path, timeout=1)) as db:
                if initialize:
                    db.execute("CREATE TABLE IF NOT EXISTS document(id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)")
                db.execute("BEGIN IMMEDIATE")
                doc = self.read(db)
                receipt = self.apply(doc, operation, payload, source)
                encoded = json.dumps(doc, ensure_ascii=False, allow_nan=False)
                if len(encoded.encode("utf-8")) > 512 * 1024:
                    raise GardenError("ledger_budget_exceeded")
                db.execute("INSERT OR REPLACE INTO document VALUES(1,?)", (encoded,))
                db.commit()
            return {"available": True, "root_thread_id": self.context.root_thread_id,
                    "caller": self.context.public(), "receipt": receipt, "revision": doc["revision"]}
        except GardenError:
            raise
        except sqlite3.OperationalError:
            raise GardenError("ledger_busy") from None
        except ValueError:
            raise GardenError("invalid_request") from None
        except (sqlite3.Error, KeyError, TypeError):
            raise GardenError("ledger_invalid") from None

    def apply(self, doc, operation, payload, source):
        caller = self.context.actor_id
        actor = doc["actors"].get(caller)
        if not actor or operation not in CAPABILITIES[actor["role"]]:
            raise GardenError("role_rejected")
        task = None
        if "task_id" in payload:
            task_id = canonical_uuid(payload["task_id"])
            task = doc["tasks"].get(task_id)
            if operation != "garden_assign_task" and task is None:
                raise GardenError("task_not_in_domain")
        if operation == "garden_register_actor":
            target = self.native(canonical_uuid(payload["target_thread_id"]))
            role = payload["role"]
            if target.root_thread_id != self.context.root_thread_id or target.actor_id == target.root_thread_id:
                raise GardenError("actor_not_in_domain")
            if role not in ("worker", "reviewer"):
                raise GardenError("role_rejected")
            existing = doc["actors"].get(target.actor_id)
            if existing:
                if existing["role"] != role:
                    raise GardenError("role_conflict")
                return existing["registration_receipt"]
            if len(doc["actors"]) >= 64:
                raise GardenError("ledger_budget_exceeded")
            doc["actors"][target.actor_id] = {**target.public(), "role": role, "capabilities": CAPABILITIES[role]}
        elif operation == "garden_assign_task":
            assignee = doc["actors"].get(canonical_uuid(payload["assignee_id"]))
            if not assignee or assignee["role"] != "worker":
                raise GardenError("role_rejected")
            if self.native(assignee["actor_id"]).root_thread_id != self.context.root_thread_id:
                raise GardenError("actor_not_in_domain")
            title, path = text(payload["title"], 160), relative_path(payload["artifact_path"])
            if task:
                if (task["assignee_id"], task["title"], task["artifact_path"]) != (assignee["actor_id"], title, path):
                    raise GardenError("task_conflict")
                return task["assign_receipt"]
            if len(doc["tasks"]) >= 128:
                raise GardenError("ledger_budget_exceeded")
            task = {"task_id": task_id, "assignee_id": assignee["actor_id"], "title": title, "artifact_path": path,
                    "version": 0, "artifact_sha256": None, "state": "assigned", "reviewer_id": None,
                    "review_request": None, "claim_receipt": None, "review": None, "accept_receipt": None}
            doc["tasks"][task_id] = task
        elif operation == "garden_submit_artifact":
            if task["assignee_id"] != caller:
                raise GardenError("assignee_rejected")
            summary = text(payload["summary"])
            digest = artifact_digest(self.workspace, task["artifact_path"])
            if task["version"] and task["artifact_sha256"] == digest and self.validity(task):
                if task["submit_receipt"]["summary"] != summary:
                    raise GardenError("submission_conflict")
                return task["submit_receipt"]
            task.update(version=task["version"] + 1, artifact_sha256=digest, state="delivered",
                        reviewer_id=None, review_request=None, claim_receipt=None, review=None, accept_receipt=None)
        else:
            version, digest = payload["version"], payload["artifact_sha256"]
            if type(version) is not int or version < 1 or version != task["version"] or digest != task["artifact_sha256"]:
                raise GardenError("version_mismatch")
            if not self.validity(task):
                raise GardenError("artifact_changed")
            if operation == "garden_request_review":
                reviewer_id = canonical_uuid(payload["reviewer_id"])
                reviewer = doc["actors"].get(reviewer_id)
                if not reviewer or reviewer["role"] != "reviewer" or reviewer_id == task["assignee_id"]:
                    raise GardenError("reviewer_rejected")
                if self.native(reviewer_id).root_thread_id != self.context.root_thread_id:
                    raise GardenError("actor_not_in_domain")
                if task["review_request"]:
                    if reviewer_id != task["reviewer_id"]:
                        raise GardenError("review_request_conflict")
                    return task["review_request"]
                if task["state"] != "delivered":
                    raise GardenError("state_rejected")
                task.update(reviewer_id=reviewer_id, state="review_pending")
            elif operation == "garden_claim_review":
                if not task["review_request"] or caller != task["reviewer_id"] or payload["request_receipt_id"] != task["review_request"]["id"]:
                    raise GardenError("reviewer_rejected")
                if task["claim_receipt"]:
                    return task["claim_receipt"]
                if task["state"] != "review_pending":
                    raise GardenError("state_rejected")
                task["state"] = "reviewing"
            elif operation == "garden_record_review":
                if caller != task["reviewer_id"] or not task["claim_receipt"]:
                    raise GardenError("reviewer_rejected")
                outcome, summary = payload["outcome"], text(payload["summary"])
                if outcome not in ("pass", "changes_requested"):
                    raise GardenError("invalid_outcome")
                path = relative_path(payload["evidence_path"])
                evidence = artifact_digest(self.workspace, path)
                value = {"outcome": outcome, "summary": summary, "evidence_path": path, "evidence_sha256": evidence}
                if task["review"]:
                    if value != {key: task["review"][key] for key in value}:
                        raise GardenError("review_result_conflict")
                    return task["review"]["receipt"]
                if task["state"] != "reviewing":
                    raise GardenError("state_rejected")
                task["review"] = value
                task["state"] = "reviewed_pass" if outcome == "pass" else "changes_requested"
            elif operation == "garden_accept_artifact":
                summary = text(payload["summary"])
                if task["accept_receipt"]:
                    if summary != task["accept_receipt"]["summary"]:
                        raise GardenError("accept_conflict")
                    return task["accept_receipt"]
                if task["state"] != "reviewed_pass" or not task["claim_receipt"] or task["assignee_id"] == task["reviewer_id"]:
                    raise GardenError("state_rejected")
                task["state"] = "accepted"
            else:
                raise GardenError("operation_rejected")
        if len(doc["receipts"]) >= 1024:
            raise GardenError("ledger_budget_exceeded")
        receipt = {"id": str(uuid.uuid4()), "root_thread_id": self.context.root_thread_id, "actor_id": caller,
                   "operation": operation, "source": source, "created_at": datetime.now(timezone.utc).isoformat(),
                   "task_id": task["task_id"] if task else None, "version": task["version"] if task else None,
                   "artifact_sha256": task["artifact_sha256"] if task else None,
                   "result": task["state"] if task else "registered", "summary": payload.get("summary", "")}
        doc["receipts"].append(receipt)
        doc["revision"] += 1
        if operation == "garden_register_actor":
            doc["actors"][target.actor_id]["registration_receipt"] = receipt
        elif operation == "garden_assign_task":
            task["assign_receipt"] = receipt
        elif operation == "garden_submit_artifact":
            task["submit_receipt"] = receipt
        elif operation == "garden_request_review":
            task["review_request"] = receipt
        elif operation == "garden_claim_review":
            task["claim_receipt"] = receipt
        elif operation == "garden_record_review":
            task["review"]["receipt"] = receipt
        elif operation == "garden_accept_artifact":
            task["accept_receipt"] = receipt
        return receipt

    def register_actor(self, target_thread_id, role, *, source="mcp_stdio"):
        return self.mutate("garden_register_actor", {"target_thread_id": target_thread_id, "role": role}, source)

    def assign_task(self, task_id, assignee_id, title, artifact_path, *, source="mcp_stdio"):
        return self.mutate("garden_assign_task", {"task_id": task_id, "assignee_id": assignee_id, "title": title, "artifact_path": artifact_path}, source)

    def submit_artifact(self, task_id, summary, *, source="mcp_stdio"):
        return self.mutate("garden_submit_artifact", {"task_id": task_id, "summary": summary}, source)

    def request_review(self, task_id, version, artifact_sha256, reviewer_id, *, source="mcp_stdio"):
        return self.mutate("garden_request_review", {"task_id": task_id, "version": version, "artifact_sha256": artifact_sha256, "reviewer_id": reviewer_id}, source)

    def claim_review(self, task_id, version, artifact_sha256, request_receipt_id, *, source="mcp_stdio"):
        return self.mutate("garden_claim_review", {"task_id": task_id, "version": version, "artifact_sha256": artifact_sha256, "request_receipt_id": request_receipt_id}, source)

    def record_review(self, task_id, version, artifact_sha256, outcome, summary, evidence_path, *, source="mcp_stdio"):
        return self.mutate("garden_record_review", {"task_id": task_id, "version": version, "artifact_sha256": artifact_sha256, "outcome": outcome, "summary": summary, "evidence_path": evidence_path}, source)

    def accept_artifact(self, task_id, version, artifact_sha256, summary, *, source="mcp_stdio"):
        return self.mutate("garden_accept_artifact", {"task_id": task_id, "version": version, "artifact_sha256": artifact_sha256, "summary": summary}, source)
