"""Fixed local MCP client. Caller identity is inherited, never supplied in arguments."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from session_panel.garden.service import WORKSPACE, artifact_digest, relative_path
from session_panel.sessions.source import canonical_uuid


async def invoke(tool=None, arguments=None):
    caller = canonical_uuid(os.environ.get("CODEX_THREAD_ID", ""))
    params = StdioServerParameters(command=sys.executable, args=["-B", "-m", "session_panel.garden_mcp"],
                                   cwd=str(WORKSPACE), env={"CODEX_THREAD_ID": caller})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            initialized = await client.initialize()
            listing = await client.list_tools()
            discovery = {"server": initialized.serverInfo.name, "tools": [item.model_dump(mode="json") for item in listing.tools]}
            if tool is None:
                return discovery
            if tool not in [item.name for item in listing.tools]:
                raise ValueError("operation_rejected")
            result = await client.call_tool(tool, arguments or {})
            if result.isError:
                return {"available": False, "tool": tool, "reason": result.content[0].text}
            return {"server": discovery["server"], "tools": [item.name for item in listing.tools],
                    "tool": tool, "result": json.loads(result.content[0].text)}


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true")
    group.add_argument("--tool")
    parser.add_argument("--arguments-file")
    args = parser.parse_args()
    try:
        payload = None
        if args.tool:
            if not args.arguments_file:
                raise ValueError("arguments_file_required")
            relative = relative_path(args.arguments_file)
            artifact_digest(WORKSPACE, relative)
            path = (WORKSPACE / relative).resolve(strict=True)
            if path.stat().st_size > 4096:
                raise ValueError("arguments_too_large")
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(payload, dict):
                raise ValueError("invalid_arguments")
        elif args.arguments_file:
            raise ValueError("invalid_arguments")
        result = asyncio.run(invoke(args.tool, payload))
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result.get("available") is False else 0
    except (ValueError, OSError) as error:
        print(json.dumps({"available": False, "reason": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
