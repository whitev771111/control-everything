"""Three read-only MCP tools forwarding the existing loopback gateway."""

import json
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from control_everything.sessions.source import canonical_uuid

GATEWAY = "http://127.0.0.1:8766"
INSTRUCTIONS = "查询当前网关已配置的 Claude 会话来源，与网页共用索引。unknown 表示证据不足，不表示完成。消息仅按明确选中的 UUID 读取私人对话，最多50条、512KiB；不会发送、审批、resume、终止或定位终端。网关须单独运行。"


def unavailable(reason):
    return {"status": "unknown", "available": False, "reason": reason,
            "source": None, "observed_at": datetime.now(timezone.utc).isoformat()}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def call_gateway(path):
    request = Request(GATEWAY + path, headers={"X-Session-Client": "stdio"})
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        response = opener.open(request, timeout=25)
    except HTTPError as exc:
        response = exc
    except (URLError, TimeoutError, OSError):
        return unavailable("gateway_unavailable")
    try:
        with response:
            body = response.read(512 * 1024 + 1)
            if len(body) > 512 * 1024:
                return unavailable("response_too_large")
            value = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError("invalid_gateway_response")
            return value
    except (ValueError, UnicodeDecodeError, OSError):
        return unavailable("gateway_response_invalid")


def build_mcp():
    mcp = FastMCP("remote-claude-sessions", instructions=INSTRUCTIONS)
    annotations = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    @mcp.tool(annotations=annotations)
    def list_sessions() -> dict:
        """读取固定远端来源的会话元数据；不读取消息正文。"""
        return call_gateway("/api/sessions")

    @mcp.tool(annotations=annotations)
    def get_session_status(session_id: str) -> dict:
        """按真实 UUID 查询身份、进程与终端证据；unknown 不表示任务完成。"""
        return call_gateway(f"/api/sessions/{canonical_uuid(session_id)}")

    @mcp.tool(annotations=annotations)
    def get_session_messages(session_id: str, offset: int | None = None, limit: int = 20, revision: str | None = None) -> dict:
        """按明确选中的 UUID 读取私人对话；默认最近20条，页读取时间不等于单条消息时间。"""
        session_id = canonical_uuid(session_id)
        if type(limit) is not int or not 1 <= limit <= 50 or (offset is not None and (type(offset) is not int or offset < 0)):
            raise ValueError("invalid_query")
        params = {"limit": limit}
        if offset is not None:
            params["offset"] = offset
        if revision is not None:
            if not revision or len(revision) > 160 or any(ord(c) < 32 for c in revision):
                raise ValueError("invalid_query")
            params["revision"] = revision
        return call_gateway(f"/api/sessions/{session_id}/messages?{urlencode(params)}")

    return mcp


if __name__ == "__main__":
    build_mcp().run(transport="stdio")
