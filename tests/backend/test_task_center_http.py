"""Socket-free task-center HTTP contracts over disposable profiles."""
import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from uuid import uuid4

from tests.backend import test_profile_http as fixtures
from tests.backend.test_project_management import project_body


class TaskCenterHTTPTests(unittest.TestCase):
    request = fixtures.ProfileHTTPTests.request

    def setUp(self):
        fixtures.ProfileHTTPTests.setUp(self)

    def test_create_update_cas_and_fixed_envelopes(self):
        status, body = self.request('/api/workspace/items', 'POST', {'title': 'Global 😀'})
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {'item'})
        item = body['item']
        status, changed = self.request('/api/workspace/items/update', 'POST',
            dict(id=item['id'], expected_revision=1, changes={'state': 'doing'}))
        self.assertEqual((status, changed['item']['state']), (200, 'doing'))
        self.assertEqual(self.request('/api/workspace/items/update', 'POST',
            dict(id=item['id'], expected_revision=1, changes={'state': 'done'}))[0], 409)
        self.assertEqual(self.request('/api/workspace/items/update', 'POST',
            dict(id=str(uuid4()), expected_revision=1, changes={'state': 'done'}))[0], 404)
        self.assertEqual(self.request('/api/workspace/items')[1]['total'], 1)

    def test_strict_json_framing_budget_and_security_stay_route_local(self):
        for route in ('/api/workspace/items', '/api/workspace/items/update'):
            for headers in ({'Host': 'bad'}, {'Origin': ''}, {'X-Session-Client': ''}, {'X-Session-CSRF': ''}):
                self.assertEqual(self.request(route, 'POST', {}, headers)[0], 403)
            for body in (b'{"title":"A","title":"B"}', b'{"title":NaN}', b'{"title":1e999}', b'[]', b'\xff'):
                self.assertEqual(self.request(route, 'POST', body)[0], 400)
            self.assertEqual(self.request(route, 'POST', b' ' * 262145)[0], 413)
            self.assertEqual(self.request(route, 'POST', b'{}', {'Content-Length': ['2', '2']})[0], 400)
        # Exact byte boundary is decoded; it must not be mistaken for overflow.
        raw = b'{"title":"Boundary"}'
        self.assertEqual(self.request('/api/workspace/items', 'POST', raw + b' ' * (262144-len(raw)))[0], 200)
        self.assertEqual(self.request('/api/workspace/tasks', 'POST', b' ' * 4097)[0], 400)
        for text in ('中' * 2000, '😀' * 2000, '\\"\n' * 600):
            self.assertEqual(self.request('/api/workspace/items', 'POST', {'title': text})[0], 200)

    def test_query_and_body_validation_422_and_no_profile_404(self):
        for route in ('?limit=0', '?limit=101', '?offset=-1', '?unknown=x', '?state=bad', '?provider_id=bad',
                      '?scope=bad', '?list_revision=true', '?limit=1&limit=2', '?project_id=', '?list_revision=0'):
            self.assertEqual(self.request('/api/workspace/items' + route)[0], 422, route)
        self.assertEqual(self.request('/api/workspace/items?offset=1')[0], 409)
        for body in ({}, {'title': 'X', 'origin': 'todo'}, {'title': 'X', 'provider_id': 'unknown'}):
            self.assertEqual(self.request('/api/workspace/items', 'POST', body)[0], 422)
        self.assertEqual(self.request('/api/workspace/items', 'POST', b'{"title":"\\ud800"}')[0], 422)
        for body in ({'id': 'bad', 'expected_revision': 1, 'changes': {'state': 'done'}},
                     {'id': str(uuid4()), 'expected_revision': True, 'changes': {'state': 'done'}}):
            self.assertEqual(self.request('/api/workspace/items/update', 'POST', body)[0], 422)
        self.server.profile = None
        for method in ('GET', 'POST'):
            self.assertEqual(self.request('/api/workspace/items', method, {} if method == 'POST' else None)[0], 404)

    def test_project_archive_unarchive_and_restore_invalidate_http_pages(self):
        project = self.profile.store.save_project(project_body(self.root / 'project'))
        for _ in range(2):
            self.request('/api/workspace/items', 'POST', {'title': 'Scoped', 'project_id': project['id']})
        for archived in (True, False):
            page = self.request('/api/workspace/items?scope=active&limit=1')[1]
            before = self.profile.store.list_items()['items']
            self.request('/api/workspace/projects/save', 'POST', project_body(self.root / 'project', id=project['id'], archived=archived))
            status, result = self.request('/api/workspace/items?scope=active&offset=1&list_revision=' + str(page['list_revision']))
            self.assertEqual((status, result['reason']), (409, 'list_changed'))
            self.assertEqual(self.request('/api/workspace/items?scope=active')[1]['total'], 0 if archived else 2)
            self.assertEqual(self.profile.store.list_items()['items'], before)
        with self.profile.store.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            self.profile.store._create_item(conn, dict(title='Orphan', project_id='orphan', provider_id=None,state='todo',session_key=None))
        page = self.request('/api/workspace/items?scope=orphan&limit=1')[1]
        self.profile.store.save_project(dict(project_id='orphan',name='Restored',directories=[{'id':'main','path':str(self.root/'restored')}],
            source={'kind':'none','directory_id':None,'relative_path':None}),restore=True)
        self.assertEqual(self.request('/api/workspace/items?scope=orphan&offset=1&list_revision='+str(page['list_revision']))[0],409)

    def test_profile_progress_redirect_and_static_url_preserve_legacy(self):
        # Redirect writes real HTTP headers through in-memory streams only.
        self.request('/api/bootstrap')
        handler = self.last_handler
        handler.path = '/progress'
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = io.BytesIO()
        handler.do_GET()
        handler.send_response.assert_called_once_with(302)
        self.assertIn(('Location', '/workspace#projects'), [c.args for c in handler.send_header.call_args_list])
        with TemporaryDirectory() as temporary:
            web = Path(temporary)
            (web/'workspace').mkdir()
            (web/'workspace'/'task-center.js').write_text('fixture',encoding='utf-8')
            with patch('control_everything.http.gateway.WEB', web):
                self.assertEqual(self.request('/task-center.js')[0],200)

    def test_legacy_empty_todo_id_and_history_raw_text_roundtrip(self):
        with self.profile.store.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            old = self.profile.store._create_item(conn, dict(title='  Old\r\n😀' * 300, project_id='',
                provider_id=None, state='review', session_key=None), origin='todo', raw_id='', source=None)
        self.assertEqual(self.request('/api/todos/toggle', 'POST', {'id': ''})[0], 200)
        self.assertEqual(self.request('/api/todos/toggle', 'POST', {'id': ''})[0], 200)
        item = self.request('/api/workspace/items?scope=global')[1]['items'][0]
        self.assertEqual((item['id'], item['origin_id'], item['state'], item['title']), (old['id'], '', 'review', old['title']))
        status, response = self.request('/api/workspace/items/update', 'POST',
            dict(id=item['id'], expected_revision=item['revision'], changes={'title': item['title']}))
        self.assertEqual(status, 200)
        self.assertEqual(response['item'], item)
        self.assertEqual(self.request('/api/todos/toggle', 'POST', {'id': 'missing'})[0], 404)
