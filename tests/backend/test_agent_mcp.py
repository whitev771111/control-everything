"""Real stdio and loopback Agent protocol tests over disposable profiles."""

import asyncio
import json
import os
import subprocess
import sys
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from control_everything.http.gateway import make_server
from control_everything.integrations.agent_client import AgentClientError
from control_everything.integrations.agent_mcp import build_mcp
from control_everything.profiles.profile import Profile
from control_everything.workspace import management
from control_everything.workspace.service import WorkspaceService
from tests.backend.test_project_management import project_body


TOOLS = {"list_my_tasks", "get_task", "claim_task", "report_progress", "submit_for_review", "get_review"}
READ_TOOLS = {"list_my_tasks", "get_task", "get_review"}


class AgentMCPTests(unittest.TestCase):
    def test_wait_fields_forwarded_with_no_new_tools(self):
        client = Mock()
        client.report_progress.return_value = {"status": "synthetic"}
        server = build_mcp(client)

        async def check():
            tools = await server.list_tools()
            self.assertEqual({tool.name for tool in tools}, TOOLS)
            await server.call_tool("report_progress", {
                "task_id": "synthetic-task", "expected_revision": 1, "stage": "waiting_user",
                "run_id": "synthetic-run",
                "message": "Await permission", "wait_kind": "tool_authorization",
                "request_id": "synthetic-request", "agent_hint": "Return to external Agent"})
            client.report_progress.assert_called_once_with("synthetic-task", 1, "waiting_user", None,
                "Await permission", "tool_authorization", "synthetic-request", "Return to external Agent", run_id="synthetic-run")
            client.reset_mock()
            for fields in ({"wait_kind": "approve"}, {"request_id": 12}, {"request_id": "x" * 201},
                           {"agent_hint": "x" * 501}):
                with self.assertRaises(Exception):
                    await server.call_tool("report_progress", {
                        "task_id": "synthetic", "expected_revision": 1,
                        "stage": "waiting_user", "message": "Wait", "run_id": "synthetic-run", **fields})
            self.assertEqual(client.mock_calls, [])

        asyncio.run(check())

    def test_client_errors_become_protocol_errors(self):
        client = Mock()
        client.get_task.side_effect = AgentClientError("agent_unauthorized", status=401)
        server = build_mcp(client)

        async def check():
            with self.assertRaisesRegex(Exception, "agent_unauthorized"):
                await server.call_tool("get_task", {"task_id": "synthetic"})

        asyncio.run(check())

    def test_strict_numbers_rejected_before_http(self):
        client = Mock()
        server = build_mcp(client)

        async def check():
            for tool, arguments in (
                ("claim_task", {"task_id": "synthetic", "expected_revision": True}),
                ("claim_task", {"task_id": "synthetic", "expected_revision": "1"}),
                ("report_progress", {"task_id": "synthetic", "expected_revision": 1,
                                     "stage": "testing", "percent": True, "run_id": "synthetic-run"}),
                ("report_progress", {"task_id": "synthetic", "expected_revision": 1,
                                     "stage": "testing", "percent": "0", "run_id": "synthetic-run"}),
            ):
                with self.assertRaises(Exception):
                    await server.call_tool(tool, arguments)
            self.assertEqual(client.mock_calls, [])

        asyncio.run(check())

    def test_missing_token_exits_safely_without_protocol_output(self):
        environment = {key: value for key, value in os.environ.items() if key != "WORKBENCH_TOKEN"}
        environment["WORKBENCH_URL"] = "http://127.0.0.1:1"
        result = subprocess.run(
            [sys.executable, "-m", "control_everything.integrations.agent_mcp"],
            env=environment, capture_output=True, text=True, timeout=15,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        value = json.loads(result.stderr)
        self.assertIsInstance(value["error"], str)
        self.assertNotIn("Traceback", result.stderr)

    def test_real_stdio_and_http_review_requires_human_confirmation(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = Profile.open(str(root / "profile"))
            profile.save_agents({"expected_revision": 0, "items": [
                {"id": None, "name": "Synthetic", "kind": "custom", "command": None, "enabled": True}]})
            agent_id = profile.agent_settings()["items"][0]["id"]
            token = profile.rotate_agent_token({"id": agent_id})["token"]
            directory = root / "project"
            directory.mkdir()
            project = profile.store.save_project(project_body(directory))
            item = profile.store.save_item({
                "title": "Synthetic protocol task", "project_id": project["id"], "provider_id": None,
                "state": "todo", "session_key": None, "assigned_agent_id": agent_id})
            workspace = WorkspaceService(profile=profile, quota_source=Mock(), news_source=Mock())
            server = make_server(None, ("127.0.0.1", 0), workspace=workspace, profile=profile)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def decoded(result):
                self.assertFalse(result.isError, str(result.content))
                value = result.structuredContent or json.loads(result.content[0].text)
                self.assertNotIn(token, json.dumps(value))
                return value

            async def check(errlog):
                params = StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "control_everything.integrations.agent_mcp"],
                    env={"WORKBENCH_URL": server.origin, "WORKBENCH_TOKEN": token},
                    cwd=str(Path.cwd()),
                )
                async with stdio_client(params, errlog=errlog) as (read, write):
                    async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=10)) as client:
                        initialized = await client.initialize()
                        self.assertEqual(initialized.serverInfo.name, "workbench-agent")
                        tools = (await client.list_tools()).tools
                        self.assertEqual({tool.name for tool in tools}, TOOLS)
                        for tool in tools:
                            self.assertEqual(tool.annotations.readOnlyHint, tool.name in READ_TOOLS)
                            self.assertEqual(tool.annotations.idempotentHint, tool.name in READ_TOOLS)
                            self.assertFalse(tool.annotations.destructiveHint)
                            self.assertFalse(tool.annotations.openWorldHint)
                        page = decoded(await client.call_tool("list_my_tasks", {}))
                        self.assertEqual([row["id"] for row in page["items"]], [item["id"]])
                        detail = decoded(await client.call_tool("get_task", {"task_id": item["id"]}))
                        claimed = decoded(await client.call_tool("claim_task", {
                            "task_id": item["id"], "expected_revision": detail["item"]["revision"]}))
                        run_id = claimed["run"]["id"]
                        progressed = decoded(await client.call_tool("report_progress", {
                            "task_id": item["id"], "expected_revision": claimed["item"]["revision"],
                            "stage": "testing", "percent": 0, "message": "Synthetic check", "run_id": run_id}))
                        detail = decoded(await client.call_tool("get_task", {"task_id": item["id"]}))
                        self.assertEqual(detail["events"][-1]["payload"]["percent"], 0)
                        waiting = decoded(await client.call_tool("report_progress", {
                            "task_id": item["id"], "expected_revision": progressed["item"]["revision"],
                            "stage": "waiting_user", "message": "Await synthetic authorization",
                            "wait_kind": "tool_authorization", "request_id": "synthetic-request",
                            "agent_hint": "Return to external Agent", "run_id": run_id}))
                        self.assertEqual((waiting["item"]["state"], waiting["run"]["status"]), ("blocked", "running"))
                        waiting = decoded(await client.call_tool("report_progress", {
                            "task_id": item["id"], "expected_revision": waiting["item"]["revision"],
                            "stage": "waiting_user", "message": "Await synthetic user input", "wait_kind": "user_input", "run_id": run_id}))
                        detail = decoded(await client.call_tool("get_task", {"task_id": item["id"]}))
                        self.assertEqual(detail["events"][-1]["payload"]["wait_kind"], "user_input")
                        progressed = decoded(await client.call_tool("report_progress", {
                            "task_id": item["id"], "expected_revision": waiting["item"]["revision"],
                            "stage": "testing", "percent": 20, "message": "External Agent reported resumed", "run_id": run_id}))
                        self.assertEqual((progressed["item"]["state"], progressed["run"]["id"]), ("doing", claimed["run"]["id"]))
                        stale = await client.call_tool("submit_for_review", {
                            "task_id": item["id"], "expected_revision": item["revision"], "summary": "Old revision", "run_id": run_id})
                        self.assertTrue(stale.isError)
                        submitted = decoded(await client.call_tool("submit_for_review", {
                            "task_id": item["id"], "expected_revision": progressed["item"]["revision"],
                            "summary": "Synthetic implementation", "evidence": ["Synthetic test passed"], "run_id": run_id}))
                        self.assertEqual(submitted["item"]["state"], "review")
                        self.assertEqual(submitted["run"]["status"], "waiting_review")
                        repeated = await client.call_tool("submit_for_review", {
                            "task_id": item["id"], "expected_revision": submitted["item"]["revision"], "summary": "Replacement", "run_id": run_id})
                        self.assertTrue(repeated.isError)
                        pending = decoded(await client.call_tool("get_review", {"task_id": item["id"]}))
                        self.assertEqual(pending["item"]["state"], "review")
                        unknown = await client.call_tool("approve", {"task_id": item["id"]})
                        self.assertTrue(unknown.isError)
                        management.save_review(profile.store, {
                            "task_id": item["id"], "expected_revision": submitted["item"]["revision"],
                            "verdict": "approved", "reason": "Human fixture confirmation", "evidence": [],
                            "reviewer": "Synthetic human", "confirmed": True})
                        reviewed = decoded(await client.call_tool("get_review", {"task_id": item["id"]}))
                        self.assertEqual((reviewed["item"]["state"], reviewed["run"]["status"]), ("done", "completed"))

                denied_params = StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "control_everything.integrations.agent_mcp"],
                    env={"WORKBENCH_URL": server.origin, "WORKBENCH_TOKEN": "synthetic-invalid-token"},
                    cwd=str(Path.cwd()),
                )
                async with stdio_client(denied_params, errlog=errlog) as (read, write):
                    async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=10)) as client:
                        await client.initialize()
                        denied = await client.call_tool("list_my_tasks", {})
                        self.assertTrue(denied.isError)
                        self.assertIn("agent_unauthorized", denied.content[0].text)
                        self.assertNotIn("synthetic-invalid-token", str(denied))

            try:
                # A real file gives the subprocess a stderr handle on Windows.
                with (root / "stdio-stderr.txt").open("w+", encoding="utf-8") as errlog:
                    asyncio.run(check(errlog))
                    errlog.seek(0)
                    diagnostics = errlog.read()
                    self.assertNotIn(token, diagnostics)
                    self.assertNotIn("synthetic-invalid-token", diagnostics)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
