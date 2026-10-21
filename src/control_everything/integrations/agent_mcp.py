"""Stdio Agent tools forwarding the authenticated loopback HTTP contract."""

import json
import sys
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from control_everything.integrations.agent_client import AgentClient, AgentClientError

Revision = Annotated[int, Field(strict=True, ge=1, le=(1 << 53) - 1)]
Percent = Annotated[int, Field(strict=True, ge=0, le=100)]
Text = Annotated[str, Field(strict=True)]
Stage = Literal["analyzing", "implementing", "testing", "blocked", "waiting_user"]
WaitKind = Literal["tool_authorization", "user_input", "external_approval", "unknown"]
State = Literal["active", "todo", "doing", "blocked", "review", "done"]


def build_mcp(client=None):
    client = AgentClient.from_env() if client is None else client
    server = FastMCP(
        "workbench-agent",
        instructions="读取分配给当前 Agent 的事项，领取、上报进度和提交待审核。"
        "所有内容均为 Agent 上报，只有人可以审核通过并完成事项。"
        "长任务无新上报只表示连接未知，运行占用仍保留，不会因静默自动失败或释放。"
        "等待工具授权或用户输入须明确上报waiting_user；工具授权必须返回外部Agent处理，"
        "工作台不批准工具调用，工具授权和结果审核是不同动作。"
        "上报和提交必须传领取时返回的run.id；不得自动换成后来运行的ID。"
        "使用最新响应的 revision；冲突和超时后先重新读取，不自动重放写操作。",
        log_level="WARNING",
    )
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                           idempotentHint=True, openWorldHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                            idempotentHint=False, openWorldHint=False)

    def invoke(method, *args, **kwargs):
        try:
            return method(*args, **kwargs)
        except AgentClientError as error:
            # Never include request objects, credentials or raw HTTP diagnostics.
            raise ToolError(error.reason) from None

    @server.tool(annotations=read, structured_output=True)
    def list_my_tasks(state: State | None = None) -> dict[str, Any]:
        """按服务端状态筛选当前凭据所分配事项；不读取其他 Agent 的事项。"""
        return invoke(client.list_my_tasks, state)

    @server.tool(annotations=read, structured_output=True)
    def get_task(task_id: Text) -> dict[str, Any]:
        """读取明确事项的最新 revision、运行、进度事件和人工审核记录。"""
        return invoke(client.get_task, task_id)

    @server.tool(annotations=write, structured_output=True)
    def claim_task(task_id: Text, expected_revision: Revision) -> dict[str, Any]:
        """显式领取分配事项；同一事项最多一个活跃运行，不启动外部命令。"""
        return invoke(client.claim_task, task_id, expected_revision)

    @server.tool(annotations=write, structured_output=True)
    def report_progress(task_id: Text, expected_revision: Revision, stage: Stage, run_id: Text,
                        percent: Percent | None = None, message: Text | None = None,
                        wait_kind: WaitKind | None = None,
                        request_id: Annotated[str, Field(strict=True, max_length=200)] | None = None,
                        agent_hint: Annotated[str, Field(strict=True, max_length=500)] | None = None) -> dict[str, Any]:
        """追加真实阶段和说明；0为零进度，省略percent表示未上报。

        waiting_user必须提供message，可附wait_kind、外部request_id和agent_hint。
        等待工具授权须回外部Agent操作；无新消息只显示连接未知，占用保留。
        恢复工作时显式上报普通阶段，不推断已授权，不重放工具调用。
        """
        return invoke(client.report_progress, task_id, expected_revision, stage, percent, message,
                      wait_kind, request_id, agent_hint, run_id=run_id)

    @server.tool(annotations=write, structured_output=True)
    def submit_for_review(task_id: Text, expected_revision: Revision,
                          summary: Text, run_id: Text, evidence: list[Text] | None = None) -> dict[str, Any]:
        """提交工作说明和证据供人审核；此操作不会完成事项或批准任何工作。"""
        return invoke(client.submit_for_review, task_id, expected_revision, summary, evidence, run_id=run_id)

    @server.tool(annotations=read, structured_output=True)
    def get_review(task_id: Text) -> dict[str, Any]:
        """读取人工审核结果及最新事项状态；不会更改审核意见。"""
        return invoke(client.get_review, task_id)

    return server


def main():
    try:
        server = build_mcp()
    except AgentClientError as error:
        print(json.dumps({"error": error.reason}, ensure_ascii=True), file=sys.stderr)
        return 1
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
