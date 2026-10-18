"""Synthetic metadata and explicit consent boundaries."""

import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4
from contextlib import closing

from control_everything.sessions import project_sources as sources


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_disabled_never_opens_any_source(self):
        with patch.object(Path, 'open', side_effect=AssertionError('private read')):
            for provider in sources.PROVIDERS:
                self.assertEqual(sources.read_confirmed(provider, {'enabled': False, 'path': str(self.root / 'missing')})['status'], 'not_configured')

    def test_claude_projects_only_privacy_and_shapes(self):
        path = self.root / 'claude.json'
        document = {'token': 'fixture-secret-sentinel', 'projects': {'C:/example/a': {'history': 'fixture-body-sentinel'}, 'C:/example/b': {}}}
        path.write_text(json.dumps(document), encoding='utf-8')
        value = sources.read_confirmed('claude', {'enabled': True, 'path': str(path)})
        self.assertEqual((value['status'], len(value['projects']), value['complete']), ('available', 2, True))
        self.assertNotIn('sentinel', json.dumps(value))
        self.assertNotEqual(value['projects'][0]['id'], value['projects'][1]['id'])
        for raw, status in [('{}', 'unsupported_format'), ('{"projects":[]}', 'unsupported_format'),
                            ('{"projects":{},"projects":{}}', 'unsupported_format'),
                            ('{"projects":{}}', 'available'), ('{', 'unsupported_format')]:
            path.write_text(raw, encoding='utf-8')
            self.assertEqual(sources.read_confirmed('claude', {'enabled': True, 'path': str(path)})['status'], status)
        path.write_text(json.dumps({'projects': {'relative': {}}}), encoding='utf-8')
        self.assertFalse(sources.read_confirmed('claude', {'enabled': True, 'path': str(path)})['complete'])

    def test_opencode_only_project_columns_and_unsupported_view(self):
        path = self.root / 'opencode.db'
        db = sqlite3.connect(path)
        db.executescript('CREATE TABLE project(id TEXT,worktree TEXT,name TEXT,sandboxes TEXT); CREATE TABLE message(secret TEXT);')
        db.execute('INSERT INTO project VALUES(?,?,?,?)', ('one', 'C:/example/one', 'One', '["C:/example/sandbox"]'))
        db.commit()
        db.close()
        original = sqlite3.connect
        def guarded(*args, **kwargs):
            value = original(*args, **kwargs)
            value.set_authorizer(lambda action, table, column, *_: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table in ('message', 'session', 'part') else sqlite3.SQLITE_OK)
            return value
        with patch.object(sources.sqlite3, 'connect', side_effect=guarded):
            value = sources.read_confirmed('opencode', {'enabled': True, 'path': str(path)})
        self.assertTrue(value['complete'])
        self.assertEqual(len(value['projects'][0]['directories']), 2)
        with closing(original(path)) as db, db:
            db.execute('UPDATE project SET sandboxes=?', ('{}',))
        self.assertEqual(sources.read_confirmed('opencode', {'enabled': True, 'path': str(path)})['status'], 'limited')
        with closing(original(path)) as db, db:
            db.executescript('DROP TABLE project; CREATE VIEW project AS SELECT secret AS id,secret AS worktree FROM message;')
        self.assertEqual(sources.read_confirmed('opencode', {'enabled': True, 'path': str(path)})['status'], 'unsupported_format')

    def test_codex_candidates_never_query_threads(self):
        path = self.root / 'state_99.sqlite'
        identity = str(uuid4())
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript('CREATE TABLE projects(id TEXT,name TEXT,position INTEGER); CREATE TABLE project_roots(project_id TEXT,position INTEGER,path TEXT); CREATE TABLE threads(secret TEXT);')
            db.execute('INSERT INTO projects VALUES(?,?,?)', (identity, 'Codex Project', 0))
            db.execute('INSERT INTO project_roots VALUES(?,?,?)', (identity, 0, 'C:/example/codex'))
        original = sqlite3.connect
        def guarded(*args, **kwargs):
            db = original(*args, **kwargs)
            db.set_authorizer(lambda action, table, *_: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table == 'threads' else sqlite3.SQLITE_OK)
            return db
        with patch('control_everything.sessions.codex_desktop.sqlite3.connect', side_effect=guarded):
            value = sources.read_confirmed('codex', {'enabled': True, 'db_path': str(path)})
        self.assertEqual((value['status'], len(value['projects'])), ('available', 1))
        self.assertTrue(value['projects'][0]['id'].startswith('cx-'))
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript('DROP TABLE projects; CREATE VIEW projects AS SELECT secret AS id, secret AS name, 0 AS position FROM threads;')
        self.assertEqual(sources.read_confirmed('codex', {'enabled': True, 'db_path': str(path)})['status'], 'unsupported_format')

    def test_discovery_injected_home_environment_never_parses(self):
        (self.root / '.codex').mkdir()
        (self.root / '.codex/state_44.sqlite').touch()
        (self.root / '.claude.json').write_text('private sentinel', encoding='utf-8')
        with patch.object(Path, 'open', side_effect=AssertionError('must only stat')):
            codex = sources.discover_locations('codex', home=self.root, environ={'CODEX_HOME': 'relative'})
            claude = sources.discover_locations('claude', home=self.root, environ={'CLAUDE_CONFIG_DIR': str(self.root)})
        self.assertEqual(len(codex['items']), 1)
        self.assertEqual(codex['status'], 'limited')
        self.assertEqual(claude['directory_hint'], str(self.root))
        self.assertEqual(len(claude['items']), 1)

    def test_budgets_invalid_rows_missing_files_and_view_shapes_are_distinct(self):
        path = self.root / 'claude.json'
        path.write_text(json.dumps({'projects': {f'C:/example/{index}': {} for index in range(1001)}}), encoding='utf-8')
        self.assertEqual(sources.read_confirmed('claude', {'enabled': True, 'path': str(path)})['reason'], 'metadata_budget')
        path.write_text('{"projects":{},"other":' + '[' * 65 + '0' + ']' * 65 + '}', encoding='utf-8')
        self.assertEqual(sources.read_confirmed('claude', {'enabled': True, 'path': str(path)})['reason'], 'metadata_budget')
        path.write_bytes(b' ' * (8 * 1024 * 1024 + 1))
        self.assertEqual(sources.read_confirmed('claude', {'enabled': True, 'path': str(path)})['status'], 'limited')
        self.assertEqual(sources.read_confirmed('opencode', {'enabled': True, 'path': str(self.root / 'missing.db')})['status'], 'unavailable')
        database = self.root / 'invalid.db'
        with closing(sqlite3.connect(database)) as db, db:
            db.execute('CREATE TABLE project(id,worktree,name,sandboxes)')
            db.executemany('INSERT INTO project VALUES(?,?,?,?)', [('global', '/', None, None), ('bad', 123, None, None)])
        value = sources.read_confirmed('opencode', {'enabled': True, 'path': str(database)})
        self.assertEqual((value['status'], value['complete'], value['projects']), ('limited', False, []))


if __name__ == '__main__':
    unittest.main()
