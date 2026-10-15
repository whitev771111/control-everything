"""Version-specific, read-only Codex parent metadata; trusted local caller context."""

import json
import os
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path

from .source import canonical_uuid

NATIVE_DB = Path("C:/example/private-69202ba3.sqlite")


class GardenError(ValueError):
    pass


@dataclass(frozen=True)
class Actor:
    actor_id: str
    root_thread_id: str
    parent_thread_id: str | None
    agent_path: str
    nickname: str

    def public(self):
        return asdict(self)


def resolve_actor(thread_id, database=NATIVE_DB):
    try:
        caller = canonical_uuid(thread_id)
        current, seen, first = caller, set(), None
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            db.execute("PRAGMA query_only=ON")
            for _ in range(16):
                if current in seen:
                    raise GardenError("parent_cycle")
                seen.add(current)
                rows = db.execute("SELECT id,source FROM threads WHERE id=?", (current,)).fetchall()
                if len(rows) != 1:
                    raise GardenError("native_identity_unavailable")
                source = rows[0][1]
                if source in ("cli", "exec", "vscode"):
                    return Actor(caller, current, first["parent"] if first else None,
                                 first["path"] if first else "", first["name"] if first else "协调者")
                if not isinstance(source, str) or len(source) > 8192:
                    raise GardenError("native_schema_unavailable")
                value = json.loads(source)
                spawn = value["subagent"]["thread_spawn"]
                parent = canonical_uuid(spawn["parent_thread_id"])
                if type(spawn.get("depth")) is not int or spawn["depth"] < 1:
                    raise GardenError("native_schema_unavailable")
                path, name = spawn.get("agent_path"), spawn.get("agent_nickname")
                if not isinstance(path, str) or not isinstance(name, str):
                    raise GardenError("native_schema_unavailable")
                if first is None:
                    first = {"parent": parent, "path": path, "name": name}
                current = parent
            raise GardenError("parent_chain_unavailable")
    except GardenError:
        raise
    except (ValueError, TypeError, KeyError, sqlite3.Error, OSError, AttributeError):
        raise GardenError("native_binding_unavailable") from None


def actor_from_environment():
    return resolve_actor(os.environ.get("CODEX_THREAD_ID", ""))
