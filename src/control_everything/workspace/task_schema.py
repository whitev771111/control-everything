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

SCHEMA_VERSION = 9
STATES = ("todo", "doing", "blocked", "review", "done")
MAX_REVISION = (1 << 53) - 1
MAX_BODY_BYTES = 262144
KEPT_TABLES = tuple(name for name in SCHEMA1_TABLES if name not in ("tasks", "todos"))
SCHEMA2_TABLES = (*KEPT_TABLES, "task_items", "task_center_meta")
SCHEMA3_TABLES = (*SCHEMA2_TABLES, "task_reviews", "project_decisions")
SCHEMA4_TABLES = (*SCHEMA3_TABLES, "agent_runs", "task_events")
SCHEMA5_TABLES = (*SCHEMA4_TABLES, "task_briefs", "task_dispatches")
SCHEMA6_TABLES = (*SCHEMA5_TABLES, "task_sessions")
SCHEMA7_TABLES = SCHEMA6_TABLES
SCHEMA8_TABLES = (*SCHEMA7_TABLES, "task_messages")
SCHEMA9_TABLES = (*SCHEMA8_TABLES, "execution_runs", "execution_events", "execution_approvals")
ITEM_COLUMNS = ("id", "origin", "origin_id", "title", "project_id", "provider_id", "state", "session_key",
                "source", "created_at", "updated_at", "todo_position", "sort_index", "resume_state", "revision",
                "priority", "focus", "assigned_agent_id")
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
REVIEW_DDL = """CREATE TABLE task_reviews(
 id TEXT PRIMARY KEY NOT NULL, task_id TEXT NOT NULL REFERENCES task_items(id),
 run_id TEXT, verdict TEXT NOT NULL CHECK(verdict IN ('approved','changes_requested','rejected')),
 reason TEXT NOT NULL, evidence_json TEXT, reviewer TEXT, created_at TEXT NOT NULL)"""
DECISION_DDL = """CREATE TABLE project_decisions(
 id TEXT PRIMARY KEY NOT NULL, project_id TEXT NOT NULL REFERENCES projects(id),
 title TEXT NOT NULL, decision TEXT NOT NULL, rationale TEXT NOT NULL,
 alternatives TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('open','accepted','superseded')),
 links_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
AGENT_RUN_DDL = """CREATE TABLE agent_runs(
 id TEXT PRIMARY KEY NOT NULL, task_id TEXT NOT NULL REFERENCES task_items(id),
 agent_id TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('claimed','running','waiting_review','changes_requested','completed','failed','cancelled')),
 started_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL, ended_at TEXT,
 summary TEXT NOT NULL DEFAULT '', evidence_json TEXT NOT NULL DEFAULT '[]')"""
TASK_EVENT_DDL = """CREATE TABLE task_events(
 id TEXT PRIMARY KEY NOT NULL, task_id TEXT NOT NULL REFERENCES task_items(id),
 run_id TEXT REFERENCES agent_runs(id),
 kind TEXT NOT NULL CHECK(kind IN ('assigned','claimed','progress','waiting_review','changes_requested','completed','failed','blocked')),
 payload_json TEXT NOT NULL, created_at TEXT NOT NULL)"""
BRIEF_DDL = """CREATE TABLE task_briefs(
 task_id TEXT PRIMARY KEY NOT NULL REFERENCES task_items(id),
 request_text TEXT NOT NULL DEFAULT '', acceptance_text TEXT NOT NULL DEFAULT '',
 constraints_text TEXT NOT NULL DEFAULT '', references_json TEXT NOT NULL DEFAULT '[]',
 execution_directory_id TEXT,
 revision INTEGER NOT NULL CHECK(revision BETWEEN 0 AND 9007199254740991),
 updated_at TEXT NOT NULL)"""
DISPATCH_DDL = """CREATE TABLE task_dispatches(
 id TEXT PRIMARY KEY NOT NULL, task_id TEXT NOT NULL REFERENCES task_items(id),
 agent_id TEXT NOT NULL, project_id TEXT, directory_id TEXT, directory_path TEXT NOT NULL,
 brief_json TEXT NOT NULL, brief_sha256 TEXT NOT NULL, content_text TEXT NOT NULL,
 delivery_mode TEXT NOT NULL CHECK(delivery_mode IN ('auto','manual')),
 delivery_state TEXT NOT NULL CHECK(delivery_state IN ('previewed','sent','delivered','unknown','failed','cancelled')),
 external_ref TEXT, session_id TEXT, run_id TEXT REFERENCES agent_runs(id),
 created_at TEXT NOT NULL, sent_at TEXT, updated_at TEXT NOT NULL)"""
SESSION_DDL = """CREATE TABLE task_sessions(
 id TEXT PRIMARY KEY NOT NULL, task_id TEXT REFERENCES task_items(id), project_id TEXT,
 agent_id TEXT, source TEXT NOT NULL, external_id TEXT NOT NULL, title TEXT, url TEXT,
 last_seen_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
MESSAGE_DDL = """CREATE TABLE task_messages(
 id TEXT PRIMARY KEY NOT NULL, task_id TEXT NOT NULL REFERENCES task_items(id),
 run_id TEXT REFERENCES agent_runs(id), agent_id TEXT, client_token TEXT,
 direction TEXT NOT NULL CHECK(direction IN ('human_to_agent','agent_to_human')),
 kind TEXT NOT NULL CHECK(kind IN ('supplement','return','user_input','note')),
 body_text TEXT NOT NULL, delivery_mode TEXT NOT NULL CHECK(delivery_mode='manual'),
 delivery_state TEXT NOT NULL CHECK(delivery_state IN ('previewed','sent','cancelled','unknown','failed')),
 created_at TEXT NOT NULL, sent_at TEXT, updated_at TEXT NOT NULL)"""
VIEWS = (
    """CREATE VIEW tasks AS SELECT origin_id AS id,title,project_id,provider_id,state,session_key,
 source,created_at,updated_at FROM task_items WHERE origin='task'""",
    """CREATE VIEW todos AS SELECT origin_id AS id,project_id,title AS text,(state='done') AS done,
 created_at,updated_at,todo_position AS position FROM task_items WHERE origin='todo'""",
)
_SCHEMA_TABLES = {1: SCHEMA1_TABLES, 2: SCHEMA2_TABLES, 3: SCHEMA3_TABLES, 4: SCHEMA4_TABLES,
                  5: SCHEMA5_TABLES, 6: SCHEMA6_TABLES, 7: SCHEMA7_TABLES, 8: SCHEMA8_TABLES, 9: SCHEMA9_TABLES}


def tables_for(version):
    return _SCHEMA_TABLES[version]


def guard_statements(version):
    return tuple(f"""CREATE TRIGGER guard_{table}_{action.lower()} BEFORE {action} ON {table}
 BEGIN SELECT CASE WHEN task_center_writer_version() IS NOT {version}
 THEN RAISE(ABORT,'writer_version_mismatch') END; END"""
                 for table in tables_for(version) for action in ("INSERT", "UPDATE", "DELETE"))


GUARDS = guard_statements(2)
OLD_STATEMENTS = tuple(part.strip() for part in (BASE_SCHEMA + MANAGEMENT_SCHEMA).split(';') if part.strip())
KEPT_DDL = tuple(statement for statement in OLD_STATEMENTS
                 if statement.split('(', 1)[0].split()[-1] in KEPT_TABLES)
SCHEMA3_DDL = (REVIEW_DDL, DECISION_DDL,
               "CREATE INDEX idx_task_reviews_task ON task_reviews(task_id,created_at)",
               "CREATE INDEX idx_project_decisions_project ON project_decisions(project_id,created_at)")
SCHEMA4_DDL = (AGENT_RUN_DDL, TASK_EVENT_DDL,
               "CREATE INDEX idx_agent_runs_task ON agent_runs(task_id,started_at)",
               "CREATE UNIQUE INDEX idx_agent_runs_active ON agent_runs(task_id) "
               "WHERE status IN ('claimed','running','waiting_review')",
               "CREATE INDEX idx_task_events_task ON task_events(task_id,created_at)")
SCHEMA5_DDL = (BRIEF_DDL, DISPATCH_DDL,
               "CREATE INDEX idx_task_dispatches_task ON task_dispatches(task_id,created_at)",
               "CREATE INDEX idx_task_dispatches_run ON task_dispatches(run_id)")
SCHEMA6_DDL = (SESSION_DDL,
               "CREATE UNIQUE INDEX idx_task_sessions_external ON task_sessions(source,external_id)",
               "CREATE INDEX idx_task_sessions_task ON task_sessions(task_id,updated_at)",
               "CREATE INDEX idx_task_sessions_project ON task_sessions(project_id,updated_at)")
SCHEMA8_DDL = (MESSAGE_DDL,
               "CREATE UNIQUE INDEX idx_task_messages_client ON task_messages(task_id,client_token) "
               "WHERE client_token IS NOT NULL",
               "CREATE INDEX idx_task_messages_task ON task_messages(task_id,created_at)",
               "CREATE INDEX idx_task_messages_run ON task_messages(run_id)")
EXECUTION_RUN_DDL = """CREATE TABLE execution_runs (
 id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, request_hash TEXT NOT NULL,
 owner_id TEXT NOT NULL, task_id TEXT NOT NULL, task_title TEXT NOT NULL,
 project_id TEXT NOT NULL, project_name TEXT NOT NULL, cwd TEXT NOT NULL,
 root_identity TEXT NOT NULL, mode TEXT NOT NULL, prompt TEXT NOT NULL,
 state TEXT NOT NULL, reason TEXT, thread_id TEXT, turn_id TEXT, model TEXT,
 effective_policy_json TEXT, turn_send_started INTEGER NOT NULL DEFAULT 0,
 cancel_requested INTEGER NOT NULL DEFAULT 0, job_name TEXT,
 cleanup_state TEXT NOT NULL, token_json TEXT, token_observed_at TEXT,
 dropped_cursor INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, ended_at TEXT)"""
EXECUTION_EVENT_DDL = """CREATE TABLE execution_events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, kind TEXT NOT NULL,
 payload_json TEXT NOT NULL, observed_at TEXT NOT NULL)"""
EXECUTION_APPROVAL_DDL = """CREATE TABLE execution_approvals (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL, owner_id TEXT NOT NULL,
 native_id_json TEXT NOT NULL, method TEXT NOT NULL, thread_id TEXT NOT NULL,
 turn_id TEXT NOT NULL, item_id TEXT NOT NULL, detail_json TEXT NOT NULL,
 detail_complete INTEGER NOT NULL, status TEXT NOT NULL, decision TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(run_id,native_id_json))"""
SCHEMA9_DDL = (EXECUTION_RUN_DDL, EXECUTION_EVENT_DDL, EXECUTION_APPROVAL_DDL,
               "CREATE INDEX execution_event_run ON execution_events(run_id,seq)")


def fail(reason):
    # Delayed import keeps the schema constants independent from the store class.
    from control_everything.workspace.store import WorkspaceError
    raise WorkspaceError(reason)


def register_writer(connection, version=SCHEMA_VERSION):
    # A compatibility marker, not authentication; no external side effects.
    connection.create_function("task_center_writer_version", 0, lambda: version)


def create_schema2_tables(connection, *, fresh=False):
    for statement in (*KEPT_DDL,) if fresh else ():
        connection.execute(statement)
    for statement in (ITEM_DDL, META_DDL):
        connection.execute(statement)


def add_schema3(connection):
    connection.execute("ALTER TABLE task_items ADD COLUMN priority INTEGER "
                       "CHECK(priority IS NULL OR priority BETWEEN 0 AND 2)")
    connection.execute("ALTER TABLE task_items ADD COLUMN focus INTEGER NOT NULL DEFAULT 0 "
                       "CHECK(focus IN (0,1))")
    for statement in SCHEMA3_DDL:
        connection.execute(statement)


def install_schema2(connection, *, fresh=False):
    create_schema2_tables(connection, fresh=fresh)
    if fresh:
        finish_schema2(connection, None)


def install_schema3(connection, *, fresh=False):
    if fresh:
        create_schema2_tables(connection, fresh=True)
        add_schema3(connection)
        finish_schema3(connection, None)
    else:
        add_schema3(connection)


def add_schema4(connection):
    connection.execute("ALTER TABLE task_items ADD COLUMN assigned_agent_id TEXT")
    for statement in SCHEMA4_DDL:
        connection.execute(statement)


def install_schema4(connection, *, fresh=False):
    if fresh:
        install_schema3(connection, fresh=True)
        add_schema4(connection)
        finish_schema4(connection, None)
    else:
        add_schema4(connection)


def add_schema5(connection):
    connection.execute("ALTER TABLE agent_runs ADD COLUMN dispatch_id TEXT")
    for statement in SCHEMA5_DDL:
        connection.execute(statement)


def install_schema5(connection, *, fresh=False):
    if fresh:
        install_schema4(connection, fresh=True)
        add_schema5(connection)
        finish_schema5(connection, None)
    else:
        add_schema5(connection)


def add_schema6(connection):
    for statement in SCHEMA6_DDL:
        connection.execute(statement)


def install_schema6(connection, *, fresh=False):
    if fresh:
        install_schema5(connection, fresh=True)
        add_schema6(connection)
        finish_schema6(connection, None)
    else:
        add_schema6(connection)


def add_schema7(connection):
    connection.execute("ALTER TABLE task_sessions ADD COLUMN source_instance TEXT NOT NULL DEFAULT ''")
    connection.execute("DROP INDEX idx_task_sessions_external")
    connection.execute("CREATE UNIQUE INDEX idx_task_sessions_external "
                       "ON task_sessions(source,source_instance,external_id)")


def install_schema7(connection, *, fresh=False):
    if fresh:
        install_schema6(connection, fresh=True)
        add_schema7(connection)
        finish_schema7(connection, None)
    else:
        add_schema7(connection)


def add_schema8(connection):
    for statement in SCHEMA8_DDL:
        connection.execute(statement)


def install_schema8(connection, *, fresh=False):
    if fresh:
        install_schema7(connection, fresh=True)
        add_schema8(connection)
        finish_schema8(connection, None)
    else:
        add_schema8(connection)


def add_schema9(connection):
    for statement in SCHEMA9_DDL:
        connection.execute(statement)


def install_schema9(connection, *, fresh=False):
    if fresh:
        install_schema8(connection, fresh=True)
        add_schema9(connection)
        finish_schema9(connection, None)
    else:
        add_schema9(connection)


def finish_schema2(connection, receipt):
    register_writer(connection, 2)
    for statement in (*VIEWS, *guard_statements(2)):
        connection.execute(statement)
    connection.execute("INSERT INTO task_center_meta VALUES(1,1,?)",
                       (json.dumps(receipt, sort_keys=True) if receipt is not None else None,))
    connection.execute("PRAGMA user_version=2")


def finish_schema3(connection, receipt=None):
    register_writer(connection, 3)
    for view in ("tasks", "todos"):
        connection.execute(f"DROP VIEW IF EXISTS {view}")
    for table in SCHEMA3_TABLES:
        for action in ("insert", "update", "delete"):
            connection.execute(f"DROP TRIGGER IF EXISTS guard_{table}_{action}")
    for statement in (*VIEWS, *guard_statements(3)):
        connection.execute(statement)
    row = connection.execute("SELECT migration_json FROM task_center_meta WHERE id=1").fetchone()
    if row is None:
        connection.execute("INSERT INTO task_center_meta VALUES(1,1,?)",
                           (json.dumps(receipt, sort_keys=True) if receipt is not None else None,))
    elif receipt is not None:
        connection.execute("UPDATE task_center_meta SET migration_json=? WHERE id=1",
                           (json.dumps(receipt, sort_keys=True),))
    connection.execute("PRAGMA user_version=3")


def finish_schema4(connection, receipt=None):
    register_writer(connection, 4)
    for view in ("tasks", "todos"):
        connection.execute(f"DROP VIEW IF EXISTS {view}")
    for table in SCHEMA4_TABLES:
        for action in ("insert", "update", "delete"):
            connection.execute(f"DROP TRIGGER IF EXISTS guard_{table}_{action}")
    for statement in (*VIEWS, *guard_statements(4)):
        connection.execute(statement)
    row = connection.execute("SELECT migration_json FROM task_center_meta WHERE id=1").fetchone()
    if row is None:
        connection.execute("INSERT INTO task_center_meta VALUES(1,1,?)",
                           (json.dumps(receipt, sort_keys=True) if receipt is not None else None,))
    elif receipt is not None:
        connection.execute("UPDATE task_center_meta SET migration_json=? WHERE id=1",
                           (json.dumps(receipt, sort_keys=True),))
    connection.execute("PRAGMA user_version=4")


def finish_schema5(connection, receipt=None):
    register_writer(connection, 5)
    for view in ("tasks", "todos"):
        connection.execute(f"DROP VIEW IF EXISTS {view}")
    for table in SCHEMA5_TABLES:
        for action in ("insert", "update", "delete"):
            connection.execute(f"DROP TRIGGER IF EXISTS guard_{table}_{action}")
    for statement in (*VIEWS, *guard_statements(5)):
        connection.execute(statement)
    row = connection.execute("SELECT migration_json FROM task_center_meta WHERE id=1").fetchone()
    if row is None:
        connection.execute("INSERT INTO task_center_meta VALUES(1,1,?)",
                           (json.dumps(receipt, sort_keys=True) if receipt is not None else None,))
    elif receipt is not None:
        connection.execute("UPDATE task_center_meta SET migration_json=? WHERE id=1",
                           (json.dumps(receipt, sort_keys=True),))
    connection.execute("PRAGMA user_version=5")


def finish_schema6(connection, receipt=None):
    register_writer(connection, 6)
    for view in ("tasks", "todos"):
        connection.execute(f"DROP VIEW IF EXISTS {view}")
    for table in SCHEMA6_TABLES:
        for action in ("insert", "update", "delete"):
            connection.execute(f"DROP TRIGGER IF EXISTS guard_{table}_{action}")
    for statement in (*VIEWS, *guard_statements(6)):
        connection.execute(statement)
    row = connection.execute("SELECT migration_json FROM task_center_meta WHERE id=1").fetchone()
    if row is None:
        connection.execute("INSERT INTO task_center_meta VALUES(1,1,?)",
                           (json.dumps(receipt, sort_keys=True) if receipt is not None else None,))
    elif receipt is not None:
        connection.execute("UPDATE task_center_meta SET migration_json=? WHERE id=1",
                           (json.dumps(receipt, sort_keys=True),))
    connection.execute("PRAGMA user_version=6")


def finish_schema7(connection, receipt=None):
    register_writer(connection, 7)
    for view in ("tasks", "todos"):
        connection.execute(f"DROP VIEW IF EXISTS {view}")
    for table in SCHEMA7_TABLES:
        for action in ("insert", "update", "delete"):
            connection.execute(f"DROP TRIGGER IF EXISTS guard_{table}_{action}")
    for statement in (*VIEWS, *guard_statements(7)):
        connection.execute(statement)
    row = connection.execute("SELECT migration_json FROM task_center_meta WHERE id=1").fetchone()
    if row is None:
        connection.execute("INSERT INTO task_center_meta VALUES(1,1,?)",
                           (json.dumps(receipt, sort_keys=True) if receipt is not None else None,))
    elif receipt is not None:
        connection.execute("UPDATE task_center_meta SET migration_json=? WHERE id=1",
                           (json.dumps(receipt, sort_keys=True),))
    connection.execute("PRAGMA user_version=7")


def finish_schema8(connection, receipt=None):
    register_writer(connection, 8)
    for view in ("tasks", "todos"):
        connection.execute(f"DROP VIEW IF EXISTS {view}")
    for table in SCHEMA8_TABLES:
        for action in ("insert", "update", "delete"):
            connection.execute(f"DROP TRIGGER IF EXISTS guard_{table}_{action}")
    for statement in (*VIEWS, *guard_statements(8)):
        connection.execute(statement)
    row = connection.execute("SELECT migration_json FROM task_center_meta WHERE id=1").fetchone()
    if row is None:
        connection.execute("INSERT INTO task_center_meta VALUES(1,1,?)",
                           (json.dumps(receipt, sort_keys=True) if receipt is not None else None,))
    elif receipt is not None:
        connection.execute("UPDATE task_center_meta SET migration_json=? WHERE id=1",
                           (json.dumps(receipt, sort_keys=True),))
    connection.execute("PRAGMA user_version=8")


def finish_schema9(connection, receipt=None):
    register_writer(connection, 9)
    for view in ("tasks", "todos"):
        connection.execute(f"DROP VIEW IF EXISTS {view}")
    for table in SCHEMA9_TABLES:
        for action in ("insert", "update", "delete"):
            connection.execute(f"DROP TRIGGER IF EXISTS guard_{table}_{action}")
    for statement in (*VIEWS, *guard_statements(9)):
        connection.execute(statement)
    row = connection.execute("SELECT migration_json FROM task_center_meta WHERE id=1").fetchone()
    if row is None:
        connection.execute("INSERT INTO task_center_meta VALUES(1,1,?)",
                           (json.dumps(receipt, sort_keys=True) if receipt is not None else None,))
    elif receipt is not None:
        connection.execute("UPDATE task_center_meta SET migration_json=? WHERE id=1",
                           (json.dumps(receipt, sort_keys=True),))
    connection.execute("PRAGMA user_version=9")


def schema_signature(connection):
    return [(kind, name, table, ' '.join(sql.split()) if sql is not None else None)
            for kind, name, table, sql in connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")]


def validate_schema(connection, version):
    if type(version) is not int or version not in (1, 2, 3, 4, 5, 6, 7, 8, 9):
        fail("schema_error")
    if connection.execute("PRAGMA user_version").fetchone()[0] != version:
        fail("schema_error")
    expected = sqlite3.connect(":memory:")
    try:
        if version == 1:
            for statement in OLD_STATEMENTS:
                expected.execute(statement)
        elif version == 2:
            install_schema2(expected, fresh=True)
        elif version == 3:
            install_schema3(expected, fresh=True)
        elif version == 4:
            install_schema4(expected, fresh=True)
        elif version == 5:
            install_schema5(expected, fresh=True)
        elif version == 6:
            install_schema6(expected, fresh=True)
        elif version == 7:
            install_schema7(expected, fresh=True)
        elif version == 8:
            install_schema8(expected, fresh=True)
        else:
            install_schema9(expected, fresh=True)
        if schema_signature(connection) != schema_signature(expected):
            fail("schema_error")
        if version in (2, 3, 4, 5, 6, 7, 8, 9):
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
                order, state if state != 'done' else 'todo', 1, None, 0, None)))


def insert_item(connection, item):
    check_text(item['title'])
    connection.execute(f"INSERT INTO task_items({','.join(ITEM_COLUMNS)}) VALUES({','.join('?' for _ in ITEM_COLUMNS)})",
                       tuple(item[key] for key in ITEM_COLUMNS))


def table_facts(connection, version):
    result = {}
    for table in tables_for(version):
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
