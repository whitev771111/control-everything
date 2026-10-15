import asyncio
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from session_panel.garden_mcp import build_mcp
from tests.test_garden import Fixture, ROOT, WORKER, REVIEWER, TASK

TOOLS = {"garden_context", "garden_register_actor", "garden_assign_task", "garden_submit_artifact",
         "garden_request_review", "garden_claim_review", "garden_record_review", "garden_accept_artifact"}


class GardenMCPTests(unittest.TestCase):
    def test_raw_unknown_identity_and_version_types_never_enter_business(self):
        with TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            root, version, digest = fixture.delivered()
            before = root.path.read_bytes()
            async def check():
                mcp = build_mcp(root)
                for args in ({"task_id": TASK, "version": True, "artifact_sha256": digest, "reviewer_id": REVIEWER},
                             {"task_id": TASK, "version": "1", "artifact_sha256": digest, "reviewer_id": REVIEWER},
                             {"task_id": TASK, "version": version, "artifact_sha256": digest, "reviewer_id": REVIEWER, "root": ROOT}):
                    with self.assertRaises(Exception):
                        await mcp.call_tool("garden_request_review", args)
                with self.assertRaises(Exception):
                    await mcp.call_tool("garden_submit_artifact", {"task_id": TASK, "summary": "Bad", "caller": WORKER})
            asyncio.run(check())
            self.assertEqual(before, root.path.read_bytes())

    def test_real_stdio_discovery_and_caller_bound_submission(self):
        with TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            root = fixture.assign()
            script = ("from pathlib import Path; from session_panel.garden import GardenService; "
                      "from session_panel.garden_native import resolve_actor; from session_panel.garden_mcp import build_mcp; "
                      f"p=Path({directory!r}); n=p/'native.sqlite'; "
                      f"build_mcp(GardenService(resolve_actor({WORKER!r},n),p,n)).run(transport='stdio')")
            async def check():
                parameters = StdioServerParameters(command=sys.executable, args=["-B", "-c", script], cwd=str(Path.cwd()))
                async with stdio_client(parameters) as (read, write):
                    async with ClientSession(read, write) as client:
                        initialized = await client.initialize()
                        self.assertEqual(initialized.serverInfo.name, "subagent-garden")
                        listing = await client.list_tools()
                        self.assertEqual({tool.name for tool in listing.tools}, TOOLS)
                        for tool in listing.tools:
                            self.assertFalse(tool.inputSchema["additionalProperties"])
                            self.assertEqual(tool.annotations.readOnlyHint, tool.name == "garden_context")
                        invalid = await client.call_tool("garden_submit_artifact", {"task_id": TASK, "summary": "Bad", "caller": ROOT})
                        self.assertTrue(invalid.isError)
                        delivered = await client.call_tool("garden_submit_artifact", {"task_id": TASK, "summary": "Protocol delivery"})
                        self.assertFalse(delivered.isError)
                        receipt = json.loads(delivered.content[0].text)["receipt"]
                        self.assertEqual((receipt["actor_id"], receipt["root_thread_id"], receipt["result"]), (WORKER, ROOT, "delivered"))
                        rejected = await client.call_tool("garden_accept_artifact", {"task_id": TASK, "version": receipt["version"],
                                                          "artifact_sha256": receipt["artifact_sha256"], "summary": "Worker cannot accept"})
                        self.assertTrue(rejected.isError)
            asyncio.run(check())
            self.assertEqual(root.snapshot()["tasks"][TASK]["state"], "delivered")


if __name__ == "__main__":
    unittest.main()
