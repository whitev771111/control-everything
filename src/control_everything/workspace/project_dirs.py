"""Local project directory inspection, creation and native folder picker."""

import re
import threading
from pathlib import Path

from control_everything.input_validation import ProfileError, absolute_path

PICK_LOCK = threading.Lock()
INVALID_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class DirectoryError(Exception):
    def __init__(self, reason, status=422):
        self.reason, self.status = reason, status
        super().__init__(reason)


def _name(value, *, fallback=None):
    if value is None:
        value = fallback
    if (type(value) is not str or not 1 <= len(value.strip()) <= 80
            or value != value.strip() or value in (".", "..") or INVALID_NAME.search(value)
            or value.endswith((".", " "))):
        raise DirectoryError("invalid_directory_name")
    return value


def _directory(value, field="path"):
    if not absolute_path(value):
        raise DirectoryError("invalid_directory")
    try:
        path = Path(value).resolve()
    except (OSError, RuntimeError):
        raise DirectoryError("invalid_directory") from None
    if not path.is_dir():
        raise DirectoryError("directory_not_found", 404)
    return path


def inspect(body):
    if type(body) is not dict or set(body) != {"path"}:
        raise DirectoryError("invalid_request", 400)
    path = _directory(body["path"])
    try:
        empty = not any(path.iterdir())
    except OSError:
        raise DirectoryError("directory_unreadable", 403) from None
    return {"status": "ok", "path": str(path), "name": path.name, "empty": empty}


def preview(body):
    if (type(body) is not dict or set(body) != {"name", "parent_path"}):
        raise DirectoryError("invalid_request", 400)
    parent = _directory(body["parent_path"], "parent_path")
    name = _name(body["name"])
    target = parent / name
    try:
        target = target.resolve()
    except (OSError, RuntimeError):
        raise DirectoryError("invalid_directory") from None
    if target.parent != parent:
        raise DirectoryError("invalid_directory_name")
    return {"status": "preview", "name": name, "parent_path": str(parent), "path": str(target),
            "exists": target.exists(), "can_create": not target.exists()}


def register(store, body):
    if type(body) is not dict or set(body) != {"name", "path"}:
        raise DirectoryError("invalid_request", 400)
    path = _directory(body["path"])
    name = _name(body["name"], fallback=path.name)
    project = store.save_project({
        "id": None, "name": name, "archived": False,
        "directories": [{"id": "main", "path": str(path)}],
        "source": {"kind": "none", "directory_id": None, "relative_path": None},
    })
    return {"status": "registered", "path": str(path), "project": project}


def create(store, body):
    if type(body) is not dict or set(body) != {"name", "parent_path", "confirmed"}:
        raise DirectoryError("invalid_request", 400)
    if body["confirmed"] is not True:
        raise DirectoryError("confirmation_required", 422)
    parent = _directory(body["parent_path"], "parent_path")
    name = _name(body["name"])
    target = parent / name
    try:
        target = target.resolve()
    except (OSError, RuntimeError):
        raise DirectoryError("invalid_directory") from None
    if target.parent != parent:
        raise DirectoryError("invalid_directory_name")
    if target.exists():
        raise DirectoryError("directory_exists", 409)
    try:
        target.mkdir()
    except FileExistsError:
        raise DirectoryError("directory_exists", 409) from None
    except OSError:
        raise DirectoryError("directory_create_failed", 409) from None
    try:
        project = store.save_project({
            "id": None, "name": name, "archived": False,
            "directories": [{"id": "main", "path": str(target)}],
            "source": {"kind": "none", "directory_id": None, "relative_path": None},
        })
    except Exception as error:
        reason = getattr(error, "reason", "project_registration_failed")
        return {"status": "partial", "path": str(target), "directory_created": True,
                "reason": reason, "message": "目录已创建，但项目登记失败；请保留目录并在项目中重试登记。"}
    return {"status": "created", "path": str(target), "project": project}


def pick(body, *, ask=None):
    if body is None:
        body = {}
    if type(body) is not dict or set(body) - {"title", "initial_path"}:
        raise DirectoryError("invalid_request", 400)
    title = body.get("title", "选择项目文件夹")
    if type(title) is not str or not 1 <= len(title) <= 120:
        raise DirectoryError("invalid_request", 400)
    initial = None
    if body.get("initial_path") is not None:
        initial = str(_directory(body["initial_path"], "initial_path"))
    if not PICK_LOCK.acquire(blocking=False):
        raise DirectoryError("picker_busy", 409)
    try:
        if ask is None:
            import tkinter
            from tkinter import filedialog
            root = tkinter.Tk()
            root.withdraw()
            try:
                root.attributes("-topmost", True)
            except tkinter.TclError:
                pass
            try:
                selected = filedialog.askdirectory(parent=root, title=title, initialdir=initial, mustexist=True)
            finally:
                root.destroy()
        else:
            selected = ask(title, initial)
    except Exception:
        raise DirectoryError("picker_unavailable", 503) from None
    finally:
        PICK_LOCK.release()
    if not selected:
        return {"status": "cancelled"}
    return {"status": "selected", "path": str(_directory(selected))}
