"""Agent client boundaries and real CLI lifecycle over disposable data."""

import contextlib
import io
import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from uuid import uuid4

from control_everything.http.gateway import make_server
from control_everything.integrations.agent_cli import main
from control_everything.integrations.agent_client import AgentClient, AgentClientError
from control_everything.profiles.profile import Profile
from control_everything.workspace import management
from control_everything.workspace.service import WorkspaceService
from tests.backend.test_project_management import project_body


class AgentClientTests(unittest.TestCase):
    def test_wait_payload_and_validation_without_http(self):
        client = AgentClient("http://127.0.0.1:1234", "synthetic")
        client._request = Mock(return_value={"status": "synthetic"})
        identity = str(uuid4())
        run_id = str(uuid4())
        client.report_progress(identity, 1, "waiting_user", message="Await tool permission",
                               wait_kind="tool_authorization", request_id="synthetic-request",
                               agent_hint="Return to the external Agent", run_id=run_id)
        body = client._request.call_args.args[1]
        self.assertEqual((body["stage"], body["wait_kind"], body["request_id"]),
                         ("waiting_user", "tool_authorization", "synthetic-request"))
        self.assertEqual(body["run_id"], run_id)
        client.report_progress(identity, 1, "waiting_user", message="Unknown wait", run_id=run_id)
        self.assertEqual(client._request.call_args.args[1]["wait_kind"], "unknown")
        client._request.reset_mock()
        for fields in ({}, {"message": " "}, {"message": "Wait", "wait_kind": "approve"},
                       {"message": "Wait", "request_id": "x" * 201},
                       {"message": "Wait", "agent_hint": "x" * 501},
                       {"message": "Wait", "request_id": True}):
            with self.assertRaisesRegex(AgentClientError, "invalid_agent_request"):
                client.report_progress(identity, 1, "waiting_user", run_id=run_id, **fields)
        with self.assertRaisesRegex(AgentClientError, "invalid_agent_request"):
            client.report_progress(identity, 1, "testing", wait_kind="user_input", run_id=run_id)
        for invalid in (None, "", "../another-run", True):
            with self.assertRaisesRegex(AgentClientError, "invalid_run_id"):
                client.report_progress(identity, 1, "testing", run_id=invalid)
            with self.assertRaisesRegex(AgentClientError, "invalid_run_id"):
                client.submit_for_review(identity, 1, "Result", run_id=invalid)
        client._request.assert_not_called()

    def test_cli_wait_forwards_only_a_progress_request(self):
        client = Mock()
        client.report_progress.return_value = {"status": "synthetic"}
        output = io.StringIO()
        with patch("control_everything.integrations.agent_cli.AgentClient.from_env", return_value=client), contextlib.redirect_stdout(output):
            self.assertEqual(main(["task", "wait", "synthetic-task", "--expected-revision", "1",
                                   "--message", "Await input", "--wait-kind", "user_input",
                                   "--request-id", "synthetic-request", "--agent-hint", "External Agent", "--run-id", "synthetic-run"]), 0)
        client.report_progress.assert_called_once_with("synthetic-task", 1, "waiting_user", None,
                                                       "Await input", "user_input", "synthetic-request", "External Agent", run_id="synthetic-run")
        self.assertEqual(json.loads(output.getvalue()), {"status": "synthetic"})

    def test_url_and_credentials_fail_before_opening_a_connection(self):
        for url in (None, "http://example.com:80", "https://127.0.0.1:1234", "http://localhost:1234",
                    "http://secret@127.0.0.1:1234", "http://127.0.0.1:1234/api", "http://127.0.0.1:1234?token=x",
                    "http://127.0.0.1:0", "http://127.0.0.1:999999", "http://127.0.0.1:1234#x"):
            with self.subTest(url=url), self.assertRaisesRegex(AgentClientError, "invalid_workbench_url"):
                AgentClient(url, "synthetic")
        for token in (None, "", "value\nheader", "含空格", "a b"):
            with self.assertRaisesRegex(AgentClientError, "missing_or_invalid_token"):
                AgentClient("http://127.0.0.1:1234", token)

    def test_strict_inputs_and_timeout_never_replay_a_write(self):
        client = AgentClient("http://127.0.0.1:1234", "synthetic")
        client._opener = Mock()
        for revision in (True, "1", 0, 1 << 53):
            with self.assertRaisesRegex(AgentClientError, "invalid_revision"):
                client.claim_task(str(uuid4()), revision)
        for percent in (True, "0", -1, 101):
            with self.assertRaisesRegex(AgentClientError, "invalid_agent_request"):
                client.report_progress(str(uuid4()), 1, "testing", percent, run_id=str(uuid4()))
        with self.assertRaisesRegex(AgentClientError, "invalid_task_id"):
            client.get_task("../reviews")
        client._opener.open.assert_not_called()
        client._opener.open.side_effect = TimeoutError("synthetic credential must not be printed")
        with self.assertRaises(AgentClientError) as failure:
            client.claim_task(str(uuid4()), 1)
        self.assertEqual(str(failure.exception), "workbench_unavailable")
        self.assertEqual(client._opener.open.call_count, 1)

    def test_redirect_does_not_forward_bearer_and_unknown_http_errors_are_redacted(self):
        paths = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                body = json.dumps({"reason": "must-not-echo-this-sensitive-value"}).encode()
                self.send_response(302)
                self.send_header("Location", "/unexpected")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            client = AgentClient(f"http://127.0.0.1:{server.server_port}", "synthetic")
            with patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1"}), self.assertRaises(AgentClientError) as failure:
                client.list_my_tasks()
            self.assertEqual((failure.exception.reason, failure.exception.status), ("http_error", 302))
            self.assertEqual(paths, ["/api/agent/tasks"])
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)

    def test_missing_token_and_invalid_arguments_do_not_echo_input(self):
        for arguments in (["task", "list"], ["task", "approve", "synthetic-secret"]):
            output, errors = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, {"WORKBENCH_URL": "http://127.0.0.1:1", "WORKBENCH_TOKEN": ""}), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                self.assertEqual(main(arguments), 1)
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(json.loads(errors.getvalue())["status"], "error")
            self.assertNotIn("synthetic-secret", errors.getvalue())
            self.assertNotIn("Traceback", errors.getvalue())


class AgentCLITests(unittest.TestCase):
    def test_real_cli_claim_zero_progress_review_and_human_approval(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = Profile.open(str(root / "profile"))
            profile.save_agents({"expected_revision": 0, "items": [
                {"id": None, "name": "Synthetic CLI", "kind": "custom", "command": None, "enabled": True}]})
            agent_id = profile.agent_settings()["items"][0]["id"]
            token = profile.rotate_agent_token({"id": agent_id})["token"]
            directory = root / "project"; directory.mkdir()
            project = profile.store.save_project(project_body(directory))
            item = profile.store.save_item({"title": "Synthetic CLI task", "project_id": project["id"],
                "state": "todo", "provider_id": None, "session_key": None, "assigned_agent_id": agent_id})
            workspace = WorkspaceService(profile=profile, news_source=Mock(), quota_source=Mock())
            server = make_server(None, ("127.0.0.1", 0), profile=profile, workspace=workspace)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            environment = {**os.environ, "WORKBENCH_URL": server.origin, "WORKBENCH_TOKEN": token, "PYTHONIOENCODING": "utf-8"}

            def command(*arguments, code=0):
                result = subprocess.run([sys.executable, "-m", "control_everything.integrations.agent_cli", "task", *arguments],
                    env=environment, capture_output=True, text=True, encoding="utf-8", timeout=15)
                self.assertNotIn(token, result.stdout + result.stderr)
                self.assertEqual(result.returncode, code, result.stderr)
                return json.loads(result.stdout if code == 0 else result.stderr)

            try:
                self.assertEqual(command("list")["items"][0]["id"], item["id"])
                def scope(project_ids):
                    settings = profile.agent_settings()
                    profile.save_agents({"expected_revision": settings["revision"], "items": [
                        {"id": agent_id, "name": "Synthetic CLI", "kind": "custom", "command": None,
                         "enabled": True, "project_ids": project_ids}]})

                scope(["another-synthetic-project"])
                self.assertEqual(command("list")["items"], [])
                self.assertEqual(command("get", item["id"], code=1)["reason"], "project_forbidden")
                scope([project["id"]])
                claimed = command("claim", item["id"], "--expected-revision", str(item["revision"]))
                run_id = claimed["run"]["id"]
                revision = str(claimed["item"]["revision"])
                self.assertEqual(command("claim", item["id"], "--expected-revision", revision, code=1)["reason"], "run_conflict")
                progressed = command("progress", item["id"], "--expected-revision", revision, "--run-id", run_id, "--stage", "testing", "--percent", "0")
                detail = command("get", item["id"])
                self.assertEqual(detail["events"][-1]["payload"]["percent"], 0)
                waiting = command("wait", item["id"], "--expected-revision", str(progressed["item"]["revision"]),
                                  "--run-id", run_id, "--message", "Await synthetic tool authorization", "--wait-kind", "tool_authorization",
                                  "--request-id", "synthetic-tool-request", "--agent-hint", "Return to external Agent")
                self.assertEqual((waiting["item"]["state"], waiting["run"]["status"]), ("blocked", "running"))
                self.assertEqual(command("get", item["id"])["events"][-1]["payload"]["request_id"], "synthetic-tool-request")
                waiting = command("wait", item["id"], "--expected-revision", str(waiting["item"]["revision"]),
                                  "--run-id", run_id, "--message", "Await synthetic user input", "--wait-kind", "user_input")
                self.assertEqual(waiting["run"]["id"], claimed["run"]["id"])
                progressed = command("progress", item["id"], "--expected-revision", str(waiting["item"]["revision"]),
                                     "--run-id", run_id, "--stage", "testing", "--percent", "20", "--message", "External Agent reported resumed")
                self.assertEqual((progressed["item"]["state"], progressed["run"]["id"]), ("doing", claimed["run"]["id"]))
                released = workspace.cancel_agent_run(item["id"], progressed["item"]["revision"],
                                                      "Synthetic human released old execution")
                reclaimed = command("claim", item["id"], "--expected-revision", str(released["item"]["revision"]))
                self.assertNotEqual(reclaimed["run"]["id"], run_id)
                latest_revision = str(reclaimed["item"]["revision"])
                for arguments in (
                    ("progress", item["id"], "--expected-revision", latest_revision, "--run-id", run_id, "--stage", "testing"),
                    ("submit", item["id"], "--expected-revision", latest_revision, "--run-id", run_id, "--summary", "Old tool result"),
                ):
                    error = command(*arguments, code=1)
                    self.assertEqual((error["reason"], error["http_status"]), ("run_mismatch", 409))
                run_id = reclaimed["run"]["id"]
                progressed = command("progress", item["id"], "--expected-revision", latest_revision,
                                     "--run-id", run_id, "--stage", "testing", "--percent", "40")
                submitted = command("submit", item["id"], "--expected-revision", str(progressed["item"]["revision"]),
                                    "--run-id", run_id, "--summary", "Synthetic result awaiting a human", "--evidence", "Synthetic test record")
                self.assertEqual(submitted["item"]["state"], "review")
                self.assertEqual(command("review", item["id"])["run"]["status"], "waiting_review")
                self.assertEqual(command("approve", item["id"], code=1)["reason"], "invalid_arguments")
                management.save_review(profile.store, {"task_id": item["id"], "expected_revision": submitted["item"]["revision"],
                    "verdict": "approved", "reason": "Synthetic human checked result", "confirmed": True})
                finished = command("review", item["id"])
                self.assertEqual((finished["item"]["state"], finished["run"]["status"]), ("done", "completed"))
                environment["WORKBENCH_TOKEN"] = "synthetic-invalid-token"
                self.assertEqual(command("list", code=1)["reason"], "agent_unauthorized")
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=5)
