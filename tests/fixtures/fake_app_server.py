"""Coordinator-only JSONL fixture. Never imported or started by unit discovery.

Required: --fixture --isolated <directory under work/agent-execution-20261013>.
It simulates protocol facts, not Codex inference or a security sandbox.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", action="store_true", required=True)
    parser.add_argument("--isolated", required=True)
    parser.add_argument("--scenario", default="complete", choices=("complete", "command", "file", "wait", "early",
                        "unsupported", "eof-after-turn", "bad-policy", "fragmented", "token-regression"))
    parser.add_argument("--audit")
    parser.add_argument("--spawn-child", action="store_true")
    args = parser.parse_args()
    allowed = Path(__file__).resolve().parents[2] / "work" / "agent-execution-20261013"
    isolated = Path(args.isolated).resolve(strict=True)
    if not isolated.is_dir() or isolated == allowed.resolve() or not isolated.is_relative_to(allowed.resolve()):
        parser.error("isolated must be an existing child directory of this feature's work directory")
    audit = Path(args.audit).resolve() if args.audit else isolated / "fixture-audit.jsonl"
    if not audit.is_relative_to(isolated):
        parser.error("audit must remain inside isolated")

    def record(**values):
        with audit.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"pid": os.getpid(), **values}) + "\n")

    def send(value):
        data = (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")
        if args.scenario == "fragmented":
            for offset in range(0, len(data), 7):
                sys.stdout.buffer.write(data[offset:offset + 7])
                sys.stdout.buffer.flush()
        else:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

    thread_id, turn_id = "fixture-thread-" + str(uuid4()), "fixture-turn-" + str(uuid4())
    cwd, count, ended = isolated, 0, False

    def notify(method, **params):
        send({"method": method, "params": {"threadId": thread_id, "turnId": turn_id, **params}})

    def finish(state="completed", text="fixture protocol result; not model inference"):
        nonlocal ended
        if ended:
            return
        ended = True
        notify("item/completed", item={"id": "fixture-message", "type": "agentMessage", "text": text})
        notify("turn/completed", turn={"id": turn_id, "status": state, "items": []})
        record(event="terminal", state=state)

    if args.spawn_child:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        (isolated / "fixture-child.pid").write_text(str(child.pid), encoding="ascii")
        record(event="child", child_pid=child.pid)
    record(event="started", scenario=args.scenario)
    for raw in sys.stdin.buffer:
        message = json.loads(raw)
        method, params = message.get("method"), message.get("params", {})
        identifier = message.get("id")
        record(direction="client", method=method, id=identifier, id_type=type(identifier).__name__,
               decision=message.get("result", {}).get("decision"))
        if method == "initialize":
            send({"id": identifier, "result": {"userAgent": "fixture/0.162.0-test"}})
        elif method == "config/read":
            send({"id": identifier, "result": {"config": {
                "features": dict.fromkeys(("hooks", "plugins", "apps", "multi_agent", "goals"), False),
                "notify": [], "web_search": "disabled", "sandbox_workspace_write": {"writable_roots": [],
                "network_access": False, "exclude_tmpdir_env_var": True, "exclude_slash_tmp": True}}}})
        elif method == "thread/start":
            cwd = Path(params["cwd"]).resolve(strict=True)
            if not cwd.is_relative_to(isolated):
                raise ValueError("fixture cwd outside isolated directory")
            sandbox = {"type": "readOnly", "networkAccess": False} if params["sandbox"] == "read-only" else {
                "type": "workspaceWrite", "writableRoots": [], "networkAccess": False,
                "excludeTmpdirEnvVar": True, "excludeSlashTmp": True}
            if args.scenario == "bad-policy":
                sandbox["networkAccess"] = True
            send({"id": identifier, "result": {"thread": {"id": thread_id}, "cwd": str(cwd),
                "model": "fixture-model", "modelProvider": "fixture", "sandbox": sandbox,
                "approvalPolicy": "on-request", "approvalsReviewer": "user"}})
        elif method == "turn/start":
            count += 1
            record(event="turn_received", count=count)
            if args.scenario == "eof-after-turn":
                return 0
            if args.scenario == "early":
                notify("turn/started", turn={"id": turn_id, "status": "inProgress", "items": []})
                notify("item/agentMessage/delta", itemId="fixture-message", delta="早到通知")
            send({"id": identifier, "result": {"turn": {"id": turn_id, "status": "inProgress", "items": []}}})
            if args.scenario in ("command", "file"):
                item_id = "fixture-operation"
                if args.scenario == "file":
                    notify("item/started", item={"id": item_id, "type": "fileChange", "status": "inProgress",
                           "changes": [{"path": str(cwd / "fixture-marker.txt"), "kind": {"type": "add"},
                                        "diff": "+fixture-only\n"}]})
                send({"id": 1, "method": "item/commandExecution/requestApproval" if args.scenario == "command"
                      else "item/fileChange/requestApproval", "params": {"threadId": thread_id, "turnId": turn_id,
                      "itemId": item_id, "startedAtMs": 1, "cwd": str(cwd),
                      "command": "fixture-only: write fixture-marker.txt", "reason": "isolated protocol fixture"}})
            elif args.scenario == "unsupported":
                send({"id": "unsupported", "method": "item/tool/requestUserInput", "params": {"threadId": thread_id,
                      "turnId": turn_id, "itemId": "fixture-question", "questions": []}})
            elif args.scenario != "wait":
                for total in ([10, 8] if args.scenario == "token-regression" else [10]):
                    notify("thread/tokenUsage/updated", tokenUsage={"last": {"totalTokens": total},
                           "total": {"totalTokens": total}, "modelContextWindow": None})
                finish()
        elif method == "turn/interrupt":
            send({"id": identifier, "result": {}})
            finish("interrupted", "fixture interrupted")
        elif "result" in message and identifier == 1:
            decision = message["result"].get("decision")
            if decision == "accept":
                (cwd / "fixture-marker.txt").write_text("fixture-only\n", encoding="utf-8")
            notify("serverRequest/resolved", requestId=1)
            finish("interrupted" if decision == "cancel" else "completed", "fixture decision: " + str(decision))
    record(event="stdin_eof")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
