import asyncio
import json
import sys
import threading
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from control_everything.http.gateway import make_server
from control_everything.integrations.mcp_stdio import build_mcp, call_gateway
from tests.backend.test_gateway import PAGE, SESSION_ID, fake_service


class MCPTests(unittest.TestCase):
    def test_http_results_and_unavailable_reasons_are_shared(self):
        service = fake_service(); server = make_server(service, ("127.0.0.1", 0))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with patch("control_everything.integrations.mcp_stdio.GATEWAY", server.origin):
                self.assertEqual(call_gateway("/api/sessions"), service.list_sessions.return_value)
                self.assertEqual(call_gateway(f"/api/sessions/{SESSION_ID}"), service.get_session_status.return_value)
                self.assertEqual(call_gateway(f"/api/sessions/{SESSION_ID}/messages"), PAGE)
                for reason in ("reader_empty_unverified", "reader_scope_unverified"):
                    value = {"available": False, "reason": reason, "items": [], "total_visible": None, "next_offset": None, "read_at": PAGE["read_at"], "source": PAGE["source"]}
                    service.get_session_messages.return_value = value
                    self.assertEqual(call_gateway(f"/api/sessions/{SESSION_ID}/messages"), value)
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_gateway_connection_failure_is_not_empty_success(self):
        with patch("control_everything.integrations.mcp_stdio.build_opener") as factory:
            factory.return_value.open.side_effect = OSError("synthetic-connection-error")
            result = call_gateway("/api/sessions")
        self.assertEqual(result["reason"], "gateway_unavailable"); self.assertFalse(result["available"])
        self.assertNotIn("sessions", result)
        self.assertIsNone(result["source"])

    def test_example_config_is_stdio_three_tools_and_correct_workspace(self):
        value = tomllib.loads(Path("guide/examples/integrations/codex.toml.example").read_text(encoding="utf-8"))
        server = value["mcp_servers"]["remote_claude_sessions"]
        self.assertEqual(server["args"], ["-m", "control_everything.mcp_stdio"])
        self.assertEqual(set(server["enabled_tools"]), {"list_sessions", "get_session_status", "get_session_messages"})
        self.assertEqual(server["cwd"], "C:/example/control-everything")

    def test_initialize_list_and_calls_over_stdio_protocol(self):
        # Only synthetic results enter this child process; no gateway or SSH is used.
        script = "from unittest.mock import patch; from control_everything.integrations.mcp_stdio import build_mcp; from tests.backend.test_gateway import PAGE; p=patch('control_everything.integrations.mcp_stdio.call_gateway',return_value=PAGE); p.start(); build_mcp().run(transport='stdio')"

        async def check():
            params = StdioServerParameters(command=sys.executable, args=["-c", script], cwd=str(Path.cwd()))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as client:
                    initialized = await client.initialize()
                    self.assertEqual(initialized.serverInfo.name, "remote-claude-sessions")
                    listing = await client.list_tools()
                    self.assertEqual({tool.name for tool in listing.tools}, {"list_sessions", "get_session_status", "get_session_messages"})
                    for tool in listing.tools:
                        self.assertTrue(tool.annotations.readOnlyHint); self.assertTrue(tool.annotations.idempotentHint)
                        self.assertFalse(tool.annotations.destructiveHint)
                    for name, args in [("list_sessions", {}), ("get_session_status", {"session_id": SESSION_ID}), ("get_session_messages", {"session_id": SESSION_ID, "limit": 20})]:
                        result = await client.call_tool(name, args)
                        self.assertFalse(result.isError)
                        content = json.loads(result.content[0].text)
                        self.assertEqual(content, PAGE)
                        self.assertNotIn("timestamp", content["items"][0])
                    invalid = await client.call_tool("get_session_status", {"session_id": "../other"})
                    self.assertTrue(invalid.isError)
            return True

        self.assertTrue(asyncio.run(check()))

    def test_invalid_tool_input_is_rejected_before_gateway(self):
        async def check():
            mcp = build_mcp()
            with patch("control_everything.integrations.mcp_stdio.call_gateway") as gateway:
                for kwargs in ({"session_id": SESSION_ID, "limit": 51}, {"session_id": SESSION_ID, "offset": -1}, {"session_id": SESSION_ID.upper()}):
                    with self.assertRaises(Exception):
                        await mcp.call_tool("get_session_messages", kwargs)
                gateway.assert_not_called()
        asyncio.run(check())




class McpStdioEntryTests(unittest.TestCase):
    def test_legacy_cli_contract(self):
        import runpy
        from unittest.mock import patch

        with patch("control_everything.integrations.mcp_stdio.build_mcp") as build:
            runpy.run_module("control_everything.mcp_stdio", run_name="__main__")
        build.assert_called_once_with()
        build.return_value.run.assert_called_once_with(transport="stdio")


if __name__ == "__main__":
    unittest.main()
