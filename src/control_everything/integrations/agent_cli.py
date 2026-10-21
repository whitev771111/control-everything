"""JSON command-line interface for external Agents; no process execution."""

import argparse
import json
import sys

from control_everything.integrations.agent_client import AgentClient, AgentClientError


class _Parser(argparse.ArgumentParser):
    def error(self, _message):
        # argparse's normal error message can echo user input, including secrets.
        raise AgentClientError("invalid_arguments")


def parser():
    root = _Parser(prog="workbench", description="本地 Agent 任务客户端；通过 WORKBENCH_URL/WORKBENCH_TOKEN 连接。")
    task = root.add_subparsers(dest="resource", required=True).add_parser("task")
    commands = task.add_subparsers(dest="action", required=True)
    listing = commands.add_parser("list"); listing.add_argument("--state", choices=["active", "todo", "doing", "blocked", "review", "done"])
    for action in ("get", "claim", "progress", "wait", "submit", "review"):
        command = commands.add_parser(action); command.add_argument("task_id")
        if action in {"claim", "progress", "wait", "submit"}:
            command.add_argument("--expected-revision", type=int, required=True)
        if action in {"progress", "wait", "submit"}:
            command.add_argument("--run-id", required=True, help="领取时返回的run.id，不可替换为其他运行。")
        if action in {"progress", "wait"}:
            if action == "progress":
                command.add_argument("--stage", choices=["analyzing", "implementing", "testing", "blocked", "waiting_user"], required=True)
            command.add_argument("--percent", type=int)
            command.add_argument("--message", required=action == "wait")
            command.add_argument("--wait-kind", choices=["tool_authorization", "user_input", "external_approval", "unknown"])
            command.add_argument("--request-id")
            command.add_argument("--agent-hint", help="说明应返回哪个外部Agent处理；工作台不批准工具授权。")
        if action == "submit":
            command.add_argument("--summary", required=True); command.add_argument("--evidence", action="append", default=[])
    return root


def main(argv=None):
    try:
        args = parser().parse_args(argv)
        client = AgentClient.from_env()
        if args.action == "list":
            result = client.list_my_tasks(args.state)
        elif args.action == "get":
            result = client.get_task(args.task_id)
        elif args.action == "review":
            result = client.get_review(args.task_id)
        elif args.action == "claim":
            result = client.claim_task(args.task_id, args.expected_revision)
        elif args.action in {"progress", "wait"}:
            stage = "waiting_user" if args.action == "wait" else args.stage
            result = client.report_progress(args.task_id, args.expected_revision, stage, args.percent, args.message,
                                            args.wait_kind, args.request_id, args.agent_hint, run_id=args.run_id)
        else:
            result = client.submit_for_review(args.task_id, args.expected_revision, args.summary, args.evidence, run_id=args.run_id)
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
        return 0
    except AgentClientError as error:
        print(json.dumps({"status": "error", "reason": error.reason, "http_status": error.status}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
