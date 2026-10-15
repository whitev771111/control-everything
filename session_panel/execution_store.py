"""Durable facts for workspace-owned, single-turn Codex runs."""

import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from .workspace_store import WorkspaceError

ACTIVE_STATES = ("starting", "running", "waiting_approval", "cancel_requested")
TERMINAL_STATES = ("cancelled_before_send", "completed", "interrupted", "failed", "unknown")
EVENT_KINDS = {"phase", "agent_message", "command", "file_change", "approval", "error",
               "token_usage", "turn_finished", "cleanup"}
SUMMARY_FIELDS = ("id", "request_id", "task_id", "task_title", "project_id", "project_name", "cwd",
                  "mode", "state", "reason", "thread_id", "turn_id", "model", "cancel_requested",
                  "cleanup_state", "token_observed_at", "created_at", "updated_at", "ended_at")
RUN_UPDATES = {"state", "reason", "thread_id", "turn_id", "model", "effective_policy_json",
               "turn_send_started", "cancel_requested", "job_name", "cleanup_state", "token_json",
               "token_observed_at", "ended_at"}


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def overlapping_roots(left, right):
    left, right = os.path.normcase(left), os.path.normcase(right)
    try:
        return os.path.commonpath((left, right)) in (left, right)
    except ValueError:
        return False


def public_run(row, *, detail=False):
    result = {key: row[key] for key in SUMMARY_FIELDS}
    result["cancel_requested"] = bool(row["cancel_requested"])
    result["token_usage"] = json.loads(row["token_json"]) if row["token_json"] else None
    if detail:
        result["prompt"] = row["prompt"]
        result["effective_policy"] = json.loads(row["effective_policy_json"]) if row["effective_policy_json"] else None
    return result


def public_approval(row, *, can_act=True):
    return {"id": row["id"], "kind": "command" if row["method"] == "item/commandExecution/requestApproval" else "file_change",
            "detail": json.loads(row["detail_json"]), "detail_complete": bool(row["detail_complete"]),
            "status": row["status"], "decision": row["decision"],
            "can_accept": can_act and bool(row["detail_complete"]) and row["status"] == "pending"}


class ExecutionStore:
    def __init__(self, workspace_store):
        self.workspace = workspace_store
        self.path = workspace_store.path
        self._schema_lock = threading.Lock()
        self._initialized = False

    @contextmanager
    def connection(self, *, immediate=False):
        with self.workspace.connection() as connection:
            with self._schema_lock:
                if not self._initialized:
                    connection.executescript("""
                        CREATE TABLE IF NOT EXISTS execution_runs (
                            id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, request_hash TEXT NOT NULL,
                            owner_id TEXT NOT NULL, task_id TEXT NOT NULL, task_title TEXT NOT NULL,
                            project_id TEXT NOT NULL, project_name TEXT NOT NULL, cwd TEXT NOT NULL,
                            root_identity TEXT NOT NULL, mode TEXT NOT NULL, prompt TEXT NOT NULL,
                            state TEXT NOT NULL, reason TEXT, thread_id TEXT, turn_id TEXT, model TEXT,
                            effective_policy_json TEXT, turn_send_started INTEGER NOT NULL DEFAULT 0,
                            cancel_requested INTEGER NOT NULL DEFAULT 0, job_name TEXT,
                            cleanup_state TEXT NOT NULL, token_json TEXT, token_observed_at TEXT,
                            dropped_cursor INTEGER NOT NULL DEFAULT 0,
                            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, ended_at TEXT
                        );
                        CREATE TABLE IF NOT EXISTS execution_events (
                            seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, kind TEXT NOT NULL,
                            payload_json TEXT NOT NULL, observed_at TEXT NOT NULL
                        );
                        CREATE INDEX IF NOT EXISTS execution_event_run ON execution_events(run_id,seq);
                        CREATE TABLE IF NOT EXISTS execution_approvals (
                            id TEXT PRIMARY KEY, run_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                            native_id_json TEXT NOT NULL, method TEXT NOT NULL, thread_id TEXT NOT NULL,
                            turn_id TEXT NOT NULL, item_id TEXT NOT NULL, detail_json TEXT NOT NULL,
                            detail_complete INTEGER NOT NULL, status TEXT NOT NULL, decision TEXT,
                            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(run_id,native_id_json)
                        );
                    """)
                    self._initialized = True
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection

    @staticmethod
    def _run(connection, run_id):
        row = connection.execute("SELECT * FROM execution_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise WorkspaceError("run_not_found")
        return dict(row)

    @staticmethod
    def _approval(connection, run_id, approval_id):
        row = connection.execute("SELECT * FROM execution_approvals WHERE run_id=? AND id=?",
                                 (run_id, approval_id)).fetchone()
        if row is None:
            raise WorkspaceError("approval_not_found")
        return dict(row)

    def get_run(self, run_id):
        with self.connection() as connection:
            return self._run(connection, run_id)

    def find_request(self, request_id):
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM execution_runs WHERE request_id=?", (request_id,)).fetchone()
            return dict(row) if row else None

    def admit(self, body, request_hash, owner_id, prepare):
        # Replays precede all mutable context and availability checks.
        with self.connection(immediate=True) as connection:
            old = connection.execute("SELECT * FROM execution_runs WHERE request_id=?", (body["request_id"],)).fetchone()
            if old:
                if old["request_hash"] != request_hash:
                    raise WorkspaceError("idempotency_conflict")
                return dict(old), True
            context = prepare(connection)
            busy = connection.execute("SELECT cwd FROM execution_runs WHERE state IN (?,?,?,?) OR cleanup_state!='verified'",
                                      ACTIVE_STATES).fetchall()
            if any(overlapping_roots(row["cwd"], context["cwd"]) for row in busy):
                raise WorkspaceError("project_busy")
            if len(busy) >= 2:
                raise WorkspaceError("capacity_busy")
            now, run_id = timestamp(), str(uuid4())
            connection.execute("""INSERT INTO execution_runs
                (id,request_id,request_hash,owner_id,task_id,task_title,project_id,project_name,cwd,
                 root_identity,mode,prompt,state,cleanup_state,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'starting','pending',?,?)""",
                (run_id, body["request_id"], request_hash, owner_id, body["task_id"], context["task_title"],
                 context["project_id"], context["project_name"], context["cwd"], context["root_identity"],
                 context["mode"], body["prompt"], now, now))
            return self._run(connection, run_id), False

    def listing(self):
        with self.connection() as connection:
            total = connection.execute("SELECT COUNT(*) FROM execution_runs").fetchone()[0]
            rows = connection.execute("SELECT * FROM execution_runs ORDER BY created_at DESC,id LIMIT 50").fetchall()
            active = connection.execute("SELECT * FROM execution_runs WHERE state IN (?,?,?,?) ORDER BY created_at,id",
                                        ACTIVE_STATES).fetchall()
            return {"runs": [public_run(row) for row in rows], "active": [public_run(row) for row in active],
                    "total": total, "limit": 50}

    def snapshot(self, run_id, after=0, *, can_act=True):
        with self.connection() as connection:
            run = self._run(connection, run_id)
            approvals = connection.execute("SELECT * FROM execution_approvals WHERE run_id=? ORDER BY created_at,id",
                                           (run_id,)).fetchall()
            first = connection.execute("SELECT MIN(seq) FROM execution_events WHERE run_id=?", (run_id,)).fetchone()[0]
            rows = connection.execute("SELECT * FROM execution_events WHERE run_id=? AND seq>? ORDER BY seq LIMIT 201",
                                      (run_id, after)).fetchall()
            page = rows[:200]
            # A global sequence gap from another run is not lost history.
            gap = after < run["dropped_cursor"]
            return {"run": public_run(run, detail=True),
                    "approvals": [public_approval(row, can_act=can_act and not run["cancel_requested"] and
                                                   run["state"] in ACTIVE_STATES) for row in approvals],
                    "events": [{"seq": row["seq"], "kind": row["kind"], "payload": json.loads(row["payload_json"]),
                                "observed_at": row["observed_at"]} for row in page],
                    "next_cursor": page[-1]["seq"] if page else after, "first_cursor": first or 0,
                    "gap": gap, "has_more": len(rows) > 200}

    def update_run(self, run_id, **fields):
        if not fields or set(fields) - RUN_UPDATES:
            raise ValueError("invalid_run_fields")
        if "state" in fields and fields["state"] not in (*ACTIVE_STATES, *TERMINAL_STATES):
            raise ValueError("invalid_run_state")
        if "cleanup_state" in fields and fields["cleanup_state"] not in ("pending", "verified", "unverified"):
            raise ValueError("invalid_cleanup_state")
        with self.connection(immediate=True) as connection:
            old = self._run(connection, run_id)
            if old["state"] in TERMINAL_STATES and fields.get("state", old["state"]) != old["state"]:
                raise WorkspaceError("protocol_error")
            fields["updated_at"] = timestamp()
            if fields.get("state") in TERMINAL_STATES and not old["ended_at"]:
                fields.setdefault("ended_at", fields["updated_at"])
            connection.execute("UPDATE execution_runs SET " + ",".join(key + "=?" for key in fields) + " WHERE id=?",
                               (*fields.values(), run_id))
            return self._run(connection, run_id)

    def append_event(self, run_id, kind, payload):
        if kind not in EVENT_KINDS or not isinstance(payload, dict):
            raise ValueError("invalid_event")
        limit = 32768 if kind == "agent_message" and payload.get("final") is True else 4096
        truncated = False

        def bound(value):
            nonlocal truncated
            if isinstance(value, str):
                data = value.encode("utf-8")
                if len(data) > limit:
                    truncated = True
                    return data[:limit].decode("utf-8", errors="ignore")
            if isinstance(value, dict):
                return {key: bound(item) for key, item in value.items()}
            if isinstance(value, list):
                return [bound(item) for item in value]
            return value

        payload = bound(payload)
        if truncated:
            payload["truncated"] = True
        with self.connection(immediate=True) as connection:
            self._run(connection, run_id)
            cursor = connection.execute("INSERT INTO execution_events(run_id,kind,payload_json,observed_at) VALUES (?,?,?,?)",
                                        (run_id, kind, encode(payload), timestamp()))
            dropped = connection.execute("SELECT seq FROM execution_events WHERE run_id=? ORDER BY seq DESC LIMIT 1 OFFSET 2000",
                                         (run_id,)).fetchone()
            if dropped:
                connection.execute("DELETE FROM execution_events WHERE run_id=? AND seq<=?", (run_id, dropped[0]))
                connection.execute("UPDATE execution_runs SET dropped_cursor=? WHERE id=?", (dropped[0], run_id))
            return cursor.lastrowid

    def get_approval(self, run_id, approval_id):
        with self.connection() as connection:
            return self._approval(connection, run_id, approval_id)

    def add_approval(self, run_id, owner_id, native_id_json, method, thread_id, turn_id, item_id, detail, detail_complete):
        detail_json = encode(detail)
        if len(detail_json.encode("utf-8")) > 65536:
            detail_json = encode({"text": detail_json.encode("utf-8")[:16000].decode("utf-8", errors="ignore"), "truncated": True})
            detail_complete = False
        with self.connection(immediate=True) as connection:
            run = self._run(connection, run_id)
            if run["owner_id"] != owner_id:
                raise WorkspaceError("other_instance")
            old = connection.execute("SELECT * FROM execution_approvals WHERE run_id=? AND native_id_json=?",
                                     (run_id, native_id_json)).fetchone()
            values = (method, thread_id, turn_id, item_id, detail_json, int(detail_complete))
            if old:
                if tuple(old[key] for key in ("method", "thread_id", "turn_id", "item_id", "detail_json", "detail_complete")) != values:
                    raise WorkspaceError("protocol_error")
                return dict(old)
            if run["cancel_requested"] or run["state"] not in ACTIVE_STATES or run["thread_id"] != thread_id or run["turn_id"] != turn_id:
                raise WorkspaceError("approval_expired")
            now, approval_id = timestamp(), str(uuid4())
            connection.execute("""INSERT INTO execution_approvals
                (id,run_id,owner_id,native_id_json,method,thread_id,turn_id,item_id,detail_json,detail_complete,
                 status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?,?)""",
                (approval_id, run_id, owner_id, native_id_json, *values, now, now))
            return self._approval(connection, run_id, approval_id)

    def claim_approval(self, run_id, approval_id, owner_id, decision):
        if decision not in ("accept", "decline", "cancel"):
            raise ValueError("invalid_request")
        with self.connection(immediate=True) as connection:
            run, row = self._run(connection, run_id), self._approval(connection, run_id, approval_id)
            if run["owner_id"] != owner_id or row["owner_id"] != owner_id:
                raise WorkspaceError("other_instance")
            if row["decision"] is not None:
                if row["decision"] != decision:
                    raise WorkspaceError("decision_conflict")
                return row, True
            if (row["status"] != "pending" or run["state"] not in ACTIVE_STATES or run["cancel_requested"]
                    or row["thread_id"] != run["thread_id"] or row["turn_id"] != run["turn_id"]
                    or decision == "accept" and not row["detail_complete"]):
                raise WorkspaceError("approval_expired")
            connection.execute("UPDATE execution_approvals SET status='queued',decision=?,updated_at=? WHERE id=?",
                               (decision, timestamp(), approval_id))
            return self._approval(connection, run_id, approval_id), False

    def update_approval(self, run_id, approval_id, **fields):
        if not fields or set(fields) - {"status", "decision"}:
            raise ValueError("invalid_approval_fields")
        if "status" in fields and fields["status"] not in ("pending", "queued", "sending", "sent", "resolved", "expired", "unknown"):
            raise ValueError("invalid_approval_status")
        with self.connection(immediate=True) as connection:
            self._approval(connection, run_id, approval_id)
            fields["updated_at"] = timestamp()
            connection.execute("UPDATE execution_approvals SET " + ",".join(key + "=?" for key in fields) + " WHERE run_id=? AND id=?",
                               (*fields.values(), run_id, approval_id))
            return self._approval(connection, run_id, approval_id)

    def expire_approvals(self, run_id):
        with self.connection(immediate=True) as connection:
            connection.execute("UPDATE execution_approvals SET status='expired',updated_at=? WHERE run_id=? AND status IN ('pending','queued','sending','sent')",
                               (timestamp(), run_id))

    def request_cancel(self, run_id, owner_id):
        with self.connection(immediate=True) as connection:
            row = self._run(connection, run_id)
            if row["owner_id"] != owner_id:
                raise WorkspaceError("other_instance")
            if row["cancel_requested"] or row["state"] in TERMINAL_STATES:
                return row, True
            connection.execute("UPDATE execution_runs SET cancel_requested=1,state='cancel_requested',updated_at=? WHERE id=?",
                               (timestamp(), run_id))
            return self._run(connection, run_id), False

    def recover(self, owner_id, job_query):
        # Only the newly acquired OS owner may invoke recovery; never resend.
        with self.connection(immediate=True) as connection:
            rows = connection.execute("SELECT * FROM execution_runs WHERE owner_id!=? AND (state IN (?,?,?,?) OR cleanup_state!='verified')",
                                      (owner_id, *ACTIVE_STATES)).fetchall()
            for row in rows:
                try:
                    verified = not row["job_name"] or job_query(row["job_name"])
                except Exception:
                    verified = False
                active = row["state"] in ACTIVE_STATES
                connection.execute("UPDATE execution_runs SET state=?,reason=?,cleanup_state=?,updated_at=?,ended_at=? WHERE id=?",
                                   ("unknown" if active else row["state"], "service_restarted" if active else row["reason"],
                                    "verified" if verified else "unverified", timestamp(),
                                    row["ended_at"] or timestamp(), row["id"]))
                connection.execute("UPDATE execution_approvals SET status='expired',updated_at=? WHERE run_id=? AND status IN ('pending','queued','sending','sent')",
                                   (timestamp(), row["id"]))
