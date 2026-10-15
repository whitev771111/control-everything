"""Known local work entrances. The panel starts only these ids."""

import os
import subprocess
from pathlib import Path
from urllib.request import urlopen

CATALOG = (
    {"id": "workbench", "group": "desk", "name": "打开终端", "blurb": "接回 WezTerm、顶栏和平铺", "kind": "cmd", "target": r"C:/example/private-c3dbd7b5.cmd", "probe": "wezterm.exe"},
    {"id": "grok-gateway", "group": "code", "name": "先开网关", "blurb": "Claude 走 Grok 之前，这个要在跑", "kind": "task", "target": "Claude-Grok-LiteLLM", "probe": "http://127.0.0.1:4001/health/liveliness"},
    {"id": "claude-200k", "group": "code", "name": "短上下文", "blurb": "用 Grok 模型开 Claude，大约 20 万", "kind": "cmd", "target": r"C:/example/private-9079af5e.cmd", "probe": ""},
    {"id": "claude-500k", "group": "code", "name": "长上下文", "blurb": "同样走 Grok，大约 50 万", "kind": "cmd", "target": r"C:/example/private-4399bfd8.cmd", "probe": ""},
    {"id": "experiment-progress", "group": "lab", "name": "只看进度", "blurb": "打开实验网页，不会启动实验", "kind": "url", "target": "http://127.0.0.1:8768/", "probe": "http://127.0.0.1:8768/"},
    {"id": "experiment-menu", "group": "lab", "name": "打开菜单", "blurb": "继续实验要在菜单里选，这里不会自动跑", "kind": "py", "target": r"C:/example/private-8d0c95e9.py", "probe": ""},
    {"id": "campus", "group": "net", "name": "连学校", "blurb": "校园服务器，不是日常写代码", "kind": "cmd", "target": r"C:/example/private-97233da4.cmd", "probe": ""},
)
GROUPS = (
    ("desk", "工作台", "你平时写代码的那套终端"),
    ("code", "用 Grok 写", "网关开着之后，再选短或长上下文"),
    ("lab", "原油实验", "看进度和打开菜单是两件事"),
    ("net", "网络", "偶尔才用"),
)


def reachable(url):
    try:
        with urlopen(url, timeout=0.6) as response:
            return 200 <= response.status < 500
    except Exception:
        return False


def process_up(name):
    try:
        completed = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"], capture_output=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return False
    output = (completed.stdout or b"").decode("oem", "replace")
    return name.lower() in output.lower()


def status_of(item):
    target = item["target"]
    if item["kind"] in {"cmd", "py"} and not Path(target).is_file():
        return "missing"
    probe = item["probe"]
    if probe.startswith("http"):
        return "up" if reachable(probe) else "ready"
    if probe.endswith(".exe"):
        return "up" if process_up(probe) else "ready"
    return "ready"


def catalog():
    return [{"id": item["id"], "group": item["group"], "name": item["name"], "blurb": item["blurb"], "status": status_of(item)} for item in CATALOG]


def start(launcher_id, spawn=subprocess.Popen):
    item = next((entry for entry in CATALOG if entry["id"] == launcher_id), None)
    if item is None or status_of(item) == "missing":
        return False
    if item["kind"] == "cmd":
        spawn(["cmd", "/c", "start", "", item["target"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif item["kind"] == "py":
        spawn(["python", item["target"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif item["kind"] == "task":
        spawn(["schtasks", "/Run", "/TN", item["target"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif item["kind"] == "url":
        os.startfile(item["target"])
    else:
        return False
    return True
