import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from session_panel.codex_index import list_threads, unavailable


def thread_id(index):
    return f"a0000000-0000-4000-8000-{index:012d}"


class CodexIndexTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / 'synthetic state.sqlite'
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('CREATE TABLE threads(id TEXT,cwd,created_at,updated_at,archived,source TEXT,unread TEXT)')

    def tearDown(self):
        self.assertEqual(self.path.resolve().parent, Path(self.directory.name).resolve())
        self.directory.cleanup()

    def add(self, index, *, archived=0, source='cli', updated=1700000001):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?)',
                (thread_id(index), r'\\?\E:\same\<synthetic-project>', 1700000000, updated, archived, source, 'synthetic-not-readable'))

    def test_empty_is_valid_but_missing_and_bad_schema_are_unavailable(self):
        self.assertEqual((list_threads(db_path=self.path)['available'], list_threads(db_path=self.path)['total_visible']), (True, 0))
        missing = self.path.with_name('missing.sqlite')
        self.assertEqual(list_threads(db_path=missing), unavailable(0))
        self.assertFalse(missing.exists())
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('DROP TABLE threads'); connection.execute('CREATE TABLE threads(id TEXT)')
        self.assertEqual(list_threads(12, self.path), unavailable(12))

    def test_pages_counts_and_same_cwd_identity_are_preserved(self):
        for index in range(25): self.add(index, source=('cli', 'exec', 'vscode')[index % 3], updated=1700000001 + index)
        self.add(25, archived=1); self.add(26, source='{"subagent":{"id":"synthetic"}}')
        self.add(27, source='other'); self.add(28, source='{"Subagent":{}}')
        pages = [list_threads(offset, self.path) for offset in (0, 12, 24, 36)]
        self.assertEqual([len(page['items']) for page in pages], [12, 12, 1, 0])
        self.assertEqual([page['next_offset'] for page in pages], [12, 24, None, None])
        self.assertTrue(all(page['total_visible'] == 25 for page in pages))
        self.assertEqual(pages[0]['coverage'], {'total_count': 29, 'archived_count': 1, 'excluded_subagent_count': 1, 'excluded_other_source_count': 2})
        items = [item for page in pages for item in page['items']]
        self.assertEqual([item['thread_id'] for item in items], [thread_id(index) for index in reversed(range(25))])
        self.assertEqual(len({item['cwd'] for item in items}), 1)
        self.assertTrue(all(item['session_key'] == 'codex:' + item['thread_id'] and item['activity_state'] == item['task_completion'] == 'unknown' for item in items))
        self.assertNotIn('synthetic-not-readable', json.dumps(pages))

    def test_boundary_sizes_and_tie_order(self):
        for count in (0, 12, 13, 25):
            with self.subTest(count=count):
                with closing(sqlite3.connect(self.path)) as connection, connection: connection.execute('DELETE FROM threads')
                for index in reversed(range(count)): self.add(index)
                result = list_threads(db_path=self.path)
                self.assertEqual(result['total_visible'], count)
                self.assertEqual([item['thread_id'] for item in result['items']], [thread_id(index) for index in range(min(count, 12))])

    def test_invalid_rows_reject_whole_page_and_close(self):
        invalids = [('id', thread_id(0).upper()), ('id', 'invalid'), ('cwd', sqlite3.Binary(b'synthetic')),
                    ('created_at', -1), ('created_at', 1.5), ('created_at', '1700000000'),
                    ('updated_at', 9223372036854775807), ('updated_at', None), ('archived', None)]
        self.add(0); self.add(1)
        for column, value in invalids:
            with self.subTest(column=column, value=value):
                with closing(sqlite3.connect(self.path)) as connection, connection:
                    previous = connection.execute(f'SELECT {column} FROM threads WHERE id=?', (thread_id(0),)).fetchone()[0]
                    connection.execute(f'UPDATE threads SET {column}=? WHERE id=?', (value, thread_id(0)))
                self.assertEqual(list_threads(db_path=self.path), unavailable(0))
                with closing(sqlite3.connect(self.path)) as connection, connection:
                    connection.execute(f'UPDATE threads SET {column}=? WHERE rowid=1', (previous,))

    def test_duplicate_ids_reject_whole_page(self):
        self.add(0); self.add(0)
        self.assertEqual(list_threads(db_path=self.path), unavailable(0))

    def test_read_only_query_only_explicit_columns_and_connection_close(self):
        self.add(0)
        opened = []
        native_connect = sqlite3.connect
        class ObservedConnection(sqlite3.Connection):
            closed = False
            query_only = False
            def execute(inner, sql, *args):
                result = super(ObservedConnection, inner).execute(sql, *args)
                if sql == 'PRAGMA query_only=ON':
                    inner.query_only = inner.execute('PRAGMA query_only').fetchone()[0] == 1
                if 'SELECT COUNT(*)' in sql:
                    with self.assertRaises(sqlite3.OperationalError):
                        inner.execute('INSERT INTO threads(id) VALUES("cannot-write")')
                return result
            def close(inner):
                inner.closed = True
                super(ObservedConnection, inner).close()
        def connect(database_uri, **kwargs):
            self.assertTrue(database_uri.endswith('?mode=ro')); self.assertTrue(kwargs['uri']); self.assertEqual(kwargs['timeout'], 0.25)
            connection = native_connect(database_uri, factory=ObservedConnection, **kwargs)
            connection.set_authorizer(lambda action, _table, column, *_: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and column == 'unread' else sqlite3.SQLITE_OK)
            opened.append(connection)
            return connection
        before = self.path.read_bytes()
        with patch('session_panel.codex_index.sqlite3.connect', side_effect=connect): result = list_threads(db_path=self.path)
        self.assertTrue(result['available']); self.assertTrue(opened[0].query_only); self.assertTrue(opened[0].closed)
        self.assertEqual(self.path.read_bytes(), before)

    def test_read_snapshot_keeps_counts_and_page_consistent(self):
        self.add(0)
        native_connect = sqlite3.connect
        with closing(native_connect(self.path)) as connection, connection: connection.execute('PRAGMA journal_mode=WAL')
        class SnapshotConnection(sqlite3.Connection):
            def execute(inner, sql, *args):
                if 'SELECT id,cwd' in sql:
                    with closing(native_connect(self.path)) as writer, writer:
                        writer.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?)', (thread_id(1), '/synthetic', 1700000000, 1700000002, 0, 'cli', 'unread'))
                return super(SnapshotConnection, inner).execute(sql, *args)
        with patch('session_panel.codex_index.sqlite3.connect', side_effect=lambda database_uri, **kwargs: native_connect(database_uri, factory=SnapshotConnection, **kwargs)):
            result = list_threads(db_path=self.path)
        self.assertEqual((result['total_visible'], len(result['items'])), (1, 1))
        self.assertEqual(list_threads(db_path=self.path)['total_visible'], 2)

    def test_locked_and_connection_error_do_not_leak_private_errors(self):
        self.add(0)
        lock = sqlite3.connect(self.path)
        try:
            lock.execute('BEGIN EXCLUSIVE')
            self.assertEqual(list_threads(db_path=self.path), unavailable(0))
        finally: lock.close()
        with patch('session_panel.codex_index.sqlite3.connect', side_effect=sqlite3.OperationalError('synthetic-private-error')):
            result = list_threads(db_path=self.path)
        self.assertEqual(result, unavailable(0)); self.assertNotIn('synthetic-private-error', json.dumps(result))

    def test_invalid_internal_offsets_never_open_database(self):
        with patch('session_panel.codex_index.sqlite3.connect') as connect:
            for value in (-1, True, 1.2, '0', 1000000000):
                with self.subTest(value=value), self.assertRaises(ValueError): list_threads(value, self.path)
        connect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
