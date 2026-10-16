"""Dedicated per-caller stdio tools; independent of the legacy read-only MCP."""

import copy

import jsonschema
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from session_panel.garden.service import GardenService
from session_panel.garden.native import GardenError, actor_from_environment


class GardenMCP(FastMCP):
    async def list_tools(self):
        result = await super().list_tools()
        for tool in result:
            tool.inputSchema = {**copy.deepcopy(tool.inputSchema), "additionalProperties": False}
        return result

    async def call_tool(self, name, arguments):
        listing = await self.list_tools()
        tool = next((tool for tool in listing if tool.name == name), None)
        if tool is None:
            raise GardenError("operation_rejected")
        jsonschema.validate(arguments, tool.inputSchema)
        return await super().call_tool(name, arguments)


def build_mcp(garden=None):
    service = garden or GardenService(actor_from_environment())
    mcp = GardenMCP("subagent-garden", instructions="同主 Thread 子agent产物/复核。当前进程CODEX_THREAD_ID绑定主体；每个actor独立启动。请求只是pending，claim才是接收，root验收才完成。不派发agent，不操作终端，不跨主聊天。可信本地进程边界，不是OS用户认证。")
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    @mcp.tool(annotations=read)
    def garden_context() -> dict:
        """读取本人主会话域的角色、任务和有效收据；不读聊天正文。"""
        return service.snapshot()

    @mcp.tool(annotations=write)
    def garden_register_actor(target_thread_id: str, role: str) -> dict:
        """仅协调者登记有明确同域父链的worker/reviewer。"""
        return service.register_actor(target_thread_id, role)

    @mcp.tool(annotations=write)
    def garden_assign_task(task_id: str, assignee_id: str, title: str, artifact_path: str) -> dict:
        """仅协调者分配同域worker任务与固定workspace相对产物路径。"""
        return service.assign_task(task_id, assignee_id, title, artifact_path)

    @mcp.tool(annotations=write)
    def garden_submit_artifact(task_id: str, summary: str) -> dict:
        """仅实际作者提交已分任务的产物；绑定当前文件hash/version，不表示验收。"""
        return service.submit_artifact(task_id, summary)

    @mcp.tool(annotations=write)
    def garden_request_review(task_id: str, version: int, artifact_sha256: str, reviewer_id: str) -> dict:
        """仅协调者请求不同作者的同域reviewer复核；收据仅pending，不派发。"""
        return service.request_review(task_id, version, artifact_sha256, reviewer_id)

    @mcp.tool(annotations=write)
    def garden_claim_review(task_id: str, version: int, artifact_sha256: str, request_receipt_id: str) -> dict:
        """仅已指派reviewer接收精确版本/请求；成功收据才证明接受。"""
        return service.claim_review(task_id, version, artifact_sha256, request_receipt_id)

    @mcp.tool(annotations=write)
    def garden_record_review(task_id: str, version: int, artifact_sha256: str, outcome: str, summary: str, evidence_path: str) -> dict:
        """实际reviewer返回pass/changes_requested及可验审查文件；尚待协调者验收。"""
        return service.record_review(task_id, version, artifact_sha256, outcome, summary, evidence_path)

    @mcp.tool(annotations=write)
    def garden_accept_artifact(task_id: str, version: int, artifact_sha256: str, summary: str) -> dict:
        """仅协调者核验当前产物/独立复核证据后验收；生成当前版本完成收据。"""
        return service.accept_artifact(task_id, version, artifact_sha256, summary)

    return mcp


if __name__ == "__main__":
    build_mcp().run(transport="stdio")
