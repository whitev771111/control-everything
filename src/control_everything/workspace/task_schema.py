"""Fixed management schemas and lossless task identity mapping."""

import hashlib
import json
import sqlite3
import re
from uuid import uuid4

MANAGEMENT_SCHEMA = """CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT NOT NULL, archived INTEGER NOT NULL,
 created_at TEXT, updated_at TEXT, position INTEGER NOT NULL);
CREATE TABLE project_directories(project_id TEXT NOT NULL REFERENCES projects(id), binding_id TEXT NOT NULL,
 path TEXT NOT NULL, path_key TEXT NOT NULL, is_primary INTEGER NOT NULL, PRIMARY KEY(project_id,binding_id));
CREATE TABLE project_sources(project_id TEXT PRIMARY KEY REFERENCES projects(id), kind TEXT NOT NULL,
 directory_id TEXT, relative_path TEXT);
CREATE TABLE project_manual(project_id TEXT PRIMARY KEY, state TEXT NOT NULL, percent NUMERIC, note TEXT NOT NULL, updated_at TEXT);
CREATE TABLE project_manual_history(id TEXT PRIMARY KEY, project_id TEXT NOT NULL, state TEXT NOT NULL,
 percent NUMERIC, note TEXT NOT NULL, recorded_at TEXT, origin TEXT NOT NULL);
CREATE TABLE todos(id TEXT PRIMARY KEY, project_id TEXT, text TEXT NOT NULL, done INTEGER NOT NULL,
 created_at TEXT, updated_at TEXT, position INTEGER NOT NULL);
CREATE TABLE profile_imports(id TEXT PRIMARY KEY, imported_at TEXT NOT NULL, source_path TEXT NOT NULL,
 source_fingerprints_json TEXT NOT NULL, counts_json TEXT NOT NULL, backup_path TEXT NOT NULL);
"""
BASE_SCHEMA = """
CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, project_id TEXT NOT NULL,
 provider_id TEXT, state TEXT NOT NULL, session_key TEXT, source TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE manual_records (id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, product TEXT NOT NULL,
 day TEXT NOT NULL, metric TEXT NOT NULL, value NUMERIC NOT NULL, currency TEXT, window_label TEXT,
 note TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
"""
SCHEMA1_TABLES = ("projects", "project_directories", "project_sources", "project_manual", "project_manual_history",
                     "todos", "tasks", "manual_records", "profile_imports")

SCHEMA_VERSION = 2
STATES = ("todo", "doing", "blocked", "review", "done")
MAX_REVISION = (1 << 53) - 1
MAX_BODY_BYTES = 262144
KEPT_TABLES = tuple(name for name in SCHEMA1_TABLES if name not in ("tasks", "todos"))
SCHEMA2_TABLES = (*KEPT_TABLES, "task_items", "task_center_meta")
ITEM_COLUMNS = ("id", "origin", "origin_id", "title", "project_id", "provider_id", "state", "session_key",
                "source", "created_at", "updated_at", "todo_position", "sort_index", "resume_state", "revision")
ITEM_DDL = """CREATE TABLE task_items(
 id TEXT PRIMARY KEY NOT NULL, origin TEXT NOT NULL CHECK(origin IN ('task','todo')),
 origin_id TEXT NOT NULL, title TEXT NOT NULL, project_id TEXT, provider_id TEXT,
 state TEXT NOT NULL CHECK(state IN ('todo','doing','blocked','review','done')),
 session_key TEXT, source TEXT, created_at TEXT, updated_at TEXT, todo_position INTEGER,
 sort_index INTEGER NOT NULL UNIQUE,
 resume_state TEXT NOT NULL CHECK(resume_state IN ('todo','doing','blocked','review')),
 revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 9007199254740991), UNIQUE(origin,origin_id))"""
META_DDL = """CREATE TABLE task_center_meta(id INTEGER PRIMARY KEY CHECK(id=1),
 list_revision INTEGER NOT NULL CHECK(list_revision BETWEEN 1 AND 9007199254740991), migration_json TEXT)"""
VIEWS = (
    """CREATE VIEW tasks AS SELECT origin_id AS id,title,project_id,provider_id,state,session_key,
 source,created_at,updated_at FROM task_items WHERE origin='task'""",
    """CREATE VIEW todos AS SELECT origin_id AS id,project_id,title AS text,(state='done') AS done,
 created_at,updated_at,todo_position AS position FROM task_items WHERE origin='todo'""",
)
GUARDS = tuple(f"""CREATE TRIGGER guard_{table}_{action.lower()} BEFORE {action} ON {table}
 BEGIN SELECT CASE WHEN task_center_writer_version() IS NOT 2
 THEN RAISE(ABORT,'writer_version_mismatch') END; END"""
               for table in SCHEMA2_TABLES for action in ("INSERT", "UPDATE", "DELETE"))
OLD_STATEMENTS = tuple(part.strip() for part in (BASE_SCHEMA + MANAGEMENT_SCHEMA).split(';') if part.strip())
KEPT_DDL = tuple(statement for statement in OLD_STATEMENTS
                 if statement.split('(', 1)[0].split()[-1] in KEPT_TABLES)


def fail(reason):
    # Delayed import keeps the schema constants independent from the store class.
    from control_everything.workspace.store import WorkspaceError
    raise WorkspaceError(reason)


def register_writer(connection):
    # A compatibility marker, not authentication; no external side effects.
    connection.create_function("task_center_writer_version", 0, lambda: SCHEMA_VERSION)


def install_schema2(connection, *, fresh=False):
    for statement in (*KEPT_DDL,) if fresh else ():
        connection.execute(statement)
    for statement in (ITEM_DDL, META_DDL):
        connection.execute(statement)
    if fresh:
        finish_schema2(connection, None)


def finish_schema2(connection, receipt):
    register_writer(connection)
    for statement in (*VIEWS, *GUARDS):
        connection.execute(statement)
    connection.execute("INSERT INTO task_center_meta VALUES(1,1,?)",
                       (json.dumps(receipt, sort_keys=True) if receipt is not None else None,))
    connection.execute("PRAGMA user_version=2")


def schema_signature(connection):
    return [(kind, name, table, ' '.join(sql.split()) if sql is not None else None)
            for kind, name, table, sql in connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")]


def validate_schema(connection, version):
    if type(version) is not int or version not in (1, 2):
        fail("schema_error")
    if connection.execute("PRAGMA user_version").fetchone()[0] != version:
        fail("schema_error")
    expected = sqlite3.connect(":memory:")
    try:
        if version == 1:
            for statement in OLD_STATEMENTS:
                expected.execute(statement)
        else:
            install_schema2(expected, fresh=True)
        if schema_signature(connection) != schema_signature(expected):
            fail("schema_error")
        if version == 2:
            meta = connection.execute("SELECT * FROM task_center_meta").fetchall()
            if len(meta) != 1 or meta[0][0] != 1 or type(meta[0][1]) is not int or not 1 <= meta[0][1] <= MAX_REVISION:
                fail("schema_error")
            if meta[0][2] is not None:
                receipt = json.loads(meta[0][2])
                if (not isinstance(receipt, dict) or set(receipt) != {
                        "source_schema", "source_facts", "backup", "backup_sha256", "config_sha256", "migrated_at"}
                        or type(receipt["source_schema"]) is not int or receipt["source_schema"] != 1
                        or not isinstance(receipt["source_facts"], dict)
                        or set(receipt['source_facts']) != set(SCHEMA1_TABLES)
                        or not isinstance(receipt['backup'], str) or not re.fullmatch(r'backups[/\\][A-Za-z0-9-]+', receipt['backup'])
                        or not isinstance(receipt['migrated_at'], str)):
                    fail("schema_error")
                for value in (receipt['backup_sha256'], receipt['config_sha256']):
                    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
                        fail('schema_error')
                for value in receipt['source_facts'].values():
                    if (not isinstance(value, dict) or set(value) != {'count', 'sha256'}
                            or type(value['count']) is not int or value['count'] < 0
                            or not isinstance(value['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', value['sha256'])):
                        fail('schema_error')
    except (ValueError, TypeError, KeyError):
        fail("schema_error")
    finally:
        expected.close()


def bump_list_revision(connection):
    updated = connection.execute("UPDATE task_center_meta SET list_revision=list_revision+1 WHERE id=1 AND list_revision<?",
                                 (MAX_REVISION,))
    if updated.rowcount != 1:
        fail("revision_exhausted")


def check_text(value):
    if not isinstance(value, str):
        fail("migration_invalid_row")
    try:
        units = len(value.encode('utf-16-le')) // 2
        value.encode('utf-8')
    except UnicodeError:
        fail("migration_invalid_text")
    if 6 * units + 4096 > MAX_BODY_BYTES:
        fail("migration_edit_budget")


def mapped_item(origin, row, order):
    if not isinstance(row['id'], str):
        fail("migration_invalid_row")
    title = row['title'] if origin == 'task' else row['text']
    check_text(title)
    if origin == 'todo' and (type(row['done']) is not int or row['done'] not in (0, 1)
                             or type(row['position']) is not int):
        fail("migration_invalid_row")
    state = row['state'] if origin == 'task' else 'done' if row['done'] else 'todo'
    if state not in STATES or row['project_id'] is not None and not isinstance(row['project_id'], str):
        fail("migration_invalid_row")
    for key in ('created_at', 'updated_at'):
        if row[key] is not None and not isinstance(row[key], str):
            fail("migration_invalid_row")
    if origin == 'task' and any(row[key] is not None and not isinstance(row[key], str)
                                for key in ('provider_id', 'session_key', 'source')):
        fail("migration_invalid_row")
    return dict(zip(ITEM_COLUMNS, (str(uuid4()), origin, row['id'], title, row['project_id'],
                row['provider_id'] if origin == 'task' else None, state,
                row['session_key'] if origin == 'task' else None, row['source'] if origin == 'task' else None,
                row['created_at'], row['updated_at'], row['position'] if origin == 'todo' else None,
                order, state if state != 'done' else 'todo', 1)))


def insert_item(connection, item):
    check_text(item['title'])
    connection.execute(f"INSERT INTO task_items({','.join(ITEM_COLUMNS)}) VALUES({','.join('?' for _ in ITEM_COLUMNS)})",
                       tuple(item[key] for key in ITEM_COLUMNS))


def table_facts(connection, version):
    result = {}
    for table in SCHEMA1_TABLES if version == 1 else SCHEMA2_TABLES:
        declared = list(connection.execute(f'PRAGMA table_info({table})'))
        columns = [row[1] for row in declared]
        primary = [row[1] for row in sorted(declared, key=lambda row: row[5]) if row[5]]
        order = primary + [name for name in columns if name not in primary]
        def typed(value):
            if value is None:
                return ['null', None]
            if isinstance(value, bytes):
                return ['blob', value.hex()]
            if isinstance(value, float):
                return ['float', value.hex()]
            return ['int' if type(value) is int else 'text', value]
        # Primary-key order, with value ties for nullable legacy keys, streams all rows.
        digest = hashlib.sha256(json.dumps(columns).encode('ascii'))
        count = 0
        for raw in connection.execute(f"SELECT * FROM {table} ORDER BY {','.join(order)}"):
            row = json.dumps([typed(value) for value in raw], ensure_ascii=True, separators=(',', ':')).encode('ascii')
            digest.update(len(row).to_bytes(8, 'little'))
            digest.update(row)
            count += 1
        result[table] = {'count': count, 'sha256': digest.hexdigest()}
    return result
