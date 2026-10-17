"""Registered projects and the progress the user sets on the page."""

import json
import os
import re
import sqlite3
from pathlib import Path

from session_panel.paths import legacy_data_root
from urllib.parse import unquote

REGISTRY = legacy_data_root() / "progress-registry.json"
MANUAL = REGISTRY.with_name("progress-manual.json")
STATES = ("未开始", "在做", "等我", "完成")
ID_TEXT = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
STAGE = re.compile(r"当前阶段(?:\*\*)?\s*[:：]\s*([^\n]+)")
HEADING = re.compile(r"(?m)^#{1,6}\s+当前阶段\s*$")
DATED = re.compile(r"(?m)^(.*20\d{2}-\d{2}-\d{2}.*)$")
CLOSE_MARKERS = ("完成", "PASS", "Archive", "已收口")
OPEN_MARKERS = ("其余", "保持", "未完成", "BLOCKED", "待确认", "USER_CONFIRMATION")
NOTE_LIMIT = 24
TODOS = REGISTRY.with_name("progress-todos.json")
TEXT_LIMIT = 72


def load_registry(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, list) or not projects:
        raise ValueError("invalid_registry")
    seen, cleaned = set(), []
    for item in projects:
        if not isinstance(item, dict) or set(item) != {"id", "name", "root"}:
            raise ValueError("invalid_registry")
        project_id, name, root = item["id"], item["name"], item["root"]
        if (not isinstance(project_id, str) or not ID_TEXT.fullmatch(project_id) or project_id in seen
                or not isinstance(name, str) or not name.strip() or len(name) > 80 or any(ord(char) < 32 for char in name)
                or not isinstance(root, str) or not Path(root).is_absolute()):
            raise ValueError("invalid_registry")
        seen.add(project_id)
        cleaned.append({"id": project_id, "name": name.strip(), "root": root})
    return cleaned


def closed(text):
    if any(marker in text for marker in OPEN_MARKERS):
        return False
    return any(marker in text for marker in CLOSE_MARKERS)


def clip(text):
    text = " ".join(text.replace("**", "").split())
    if len(text) <= TEXT_LIMIT:
        return text
    return text[:TEXT_LIMIT].rstrip() + "…"


def read_text(path):
    try:
        if not path.is_file() or path.stat().st_size > 2_000_000:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def record_text(folder):
    readme, progress = folder / "README.md", folder / "progress.md"
    body = read_text(readme)
    heading = HEADING.search(body)
    if heading:
        paragraph = body[heading.end():].strip().split("\n\n", 1)[0]
        paragraph = " ".join(paragraph.split())
        if paragraph:
            return paragraph, "当前阶段", readme
    match = STAGE.search(body)
    if match and match.group(1).strip():
        return match.group(1).strip(), "当前阶段", readme
    dated = DATED.findall(read_text(progress))
    if dated:
        return dated[-1].strip(), "最近一条", progress
    return None


def norm(value):
    return str(value).replace("\\", "/").casefold().rstrip("/")


def belongs(cwd, root):
    left, right = norm(cwd), norm(root)
    return left == right or left.startswith(right + "/")


def codex_cwds(db_path=None):
    if db_path is None:
        return []
    path = Path(db_path).resolve()
    if not path.is_file():
        return []
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.25)
        try:
            rows = connection.execute(
                "SELECT id,cwd FROM threads WHERE archived=0 AND cwd IS NOT NULL ORDER BY updated_at DESC LIMIT 40")
            return [{"thread_id": row[0], "cwd": row[1]} for row in rows
                    if isinstance(row[0], str) and isinstance(row[1], str)]
        finally:
            connection.close()
    except sqlite3.Error:
        return []


def grok_cwds():
    root = Path.home() / ".grok" / "sessions"
    try:
        return [unquote(entry.name) for entry in root.iterdir() if entry.is_dir()]
    except OSError:
        return []


def session_hits(root, codex, grok):
    hits = []
    for item in codex:
        if belongs(item["cwd"], root):
            hits.append({"tool": "Codex", "place": Path(item["cwd"]).name, "href": "/?codex=" + item["thread_id"]})
    for cwd in grok:
        if belongs(cwd, root):
            hits.append({"tool": "Grok", "place": Path(cwd).name or cwd, "href": ""})
    seen, unique = set(), []
    for hit in hits:
        key = (hit["tool"], hit["place"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(hit)
        if len(unique) == 3:
            break
    return unique


def latest_focus(root):
    current = Path(root) / "docs" / "current"
    try:
        children = [child for child in current.iterdir() if child.is_dir() and not child.is_symlink()]
    except OSError:
        return None
    found = []
    for child in children:
        picked = record_text(child)
        if not picked or closed(picked[0]):
            continue
        text, label, source = picked
        try:
            found.append((source.stat().st_mtime, child.name, label, clip(text)))
        except OSError:
            continue
    if not found:
        return []
    found.sort(reverse=True)
    return [{"feature": feature, "label": label, "text": text} for _, feature, label, text in found[:5]]


def load_manual(path):
    file = Path(path)
    if not file.exists():
        return {}
    data = json.loads(file.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("invalid_manual")
    return data


def board(registry_path=REGISTRY, manual_path=MANUAL, *, codex_db_path=None):
    saved = load_manual(manual_path)
    codex, grok = codex_cwds(codex_db_path), grok_cwds()
    projects = []
    for project in load_registry(registry_path):
        item = saved.get(project["id"], {})
        if not isinstance(item, dict):
            item = {}
        state = item.get("state")
        note = item.get("note")
        percent = item.get("percent")
        if isinstance(percent, bool) or not isinstance(percent, (int, float)) or not 0 <= percent <= 100:
            percent = 0
        projects.append({
            "id": project["id"],
            "name": project["name"],
            "state": state if state in STATES else "未开始",
            "note": note.strip() if isinstance(note, str) else "",
            "percent": int(percent),
            "records": latest_focus(project["root"]),
            "sessions": session_hits(project["root"], codex, grok),
        })
    return projects


def load_todos(path=TODOS):
    file = Path(path)
    if not file.exists():
        return []
    data = json.loads(file.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("invalid_todos")
    items = []
    for item in data:
        if not isinstance(item, dict):
            continue
        text, todo_id = item.get("text"), item.get("id")
        project_id = item.get("project_id") if isinstance(item.get("project_id"), str) else ""
        if isinstance(text, str) and isinstance(todo_id, str) and text.strip() and isinstance(item.get("done"), bool):
            items.append({"id": todo_id, "text": text.strip(), "project_id": project_id, "done": item["done"]})
    return items


def save_todos(items, path=TODOS):
    file = Path(path)
    temporary = file.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, file)


def add_todo(text, project_id="", path=TODOS, registry_path=REGISTRY):
    if not isinstance(text, str) or not isinstance(project_id, str):
        raise ValueError("invalid_todo")
    text = " ".join(text.split())
    if not text or len(text) > 40:
        raise ValueError("invalid_todo")
    known = [project["id"] for project in load_registry(registry_path)]
    if project_id and project_id not in known:
        return None
    items = load_todos(path)
    item = {"id": os.urandom(4).hex(), "text": text, "project_id": project_id, "done": False}
    items.insert(0, item)
    save_todos(items[:30], path)
    return items


def toggle_todo(todo_id, path=TODOS):
    if not isinstance(todo_id, str):
        raise ValueError("invalid_todo")
    items = load_todos(path)
    found = False
    for item in items:
        if item["id"] == todo_id:
            item["done"] = not item["done"]
            found = True
    if not found:
        return None
    save_todos(items, path)
    return items


def set_progress(project_id, state, note, percent, registry_path=REGISTRY, manual_path=MANUAL):
    if not isinstance(project_id, str) or not isinstance(state, str) or not isinstance(note, str):
        raise ValueError("invalid_progress")
    if isinstance(percent, bool) or not isinstance(percent, (int, float)) or not 0 <= percent <= 100:
        raise ValueError("invalid_progress")
    if state not in STATES:
        raise ValueError("invalid_progress")
    note = " ".join(note.split())
    if len(note) > NOTE_LIMIT:
        raise ValueError("invalid_progress")
    percent = int(percent)
    known = [project["id"] for project in load_registry(registry_path)]
    if project_id not in known:
        return False
    saved = load_manual(manual_path)
    saved[project_id] = {"state": state, "note": note, "percent": percent}
    kept = {key: saved[key] for key in known if key in saved}
    file = Path(manual_path)
    temporary = file.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(kept, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, file)
    return True
