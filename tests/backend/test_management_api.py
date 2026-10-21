"""Management API contracts over disposable profiles and in-memory HTTP streams."""

import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch
from uuid import uuid4

from control_everything.sessions.codex_index import unavailable
from control_everything.workspace import management
from control_everything.workspace.store import WorkspaceError, WorkspaceStore
from tests.backend import test_profile_http as fixtures
from tests.backend.test_project_management import project_body


class ManagementAPITests(unittest.TestCase):
    request = fixtures.ProfileHTTPTests.request

    def setUp(self):
        fixtures.ProfileHTTPTests.setUp(self)
        self.workspace.session_reader = Mock(return_value=unavailable(0))
        self.project_body = project_body(self.root / 'project')
        self.project = self.profile.store.save_project(self.project_body)

    def item(self, **fields):
        status, body = self.request('/api/workspace/items', 'POST',
                                    {'title': 'Review task', 'project_id': self.project['id'], **fields})
        self.assertEqual(status, 200, body)
        return body['item']

    def decision(self, **fields):
        status, body = self.request('/api/workspace/decisions', 'POST',
            {'project_id': self.project['id'], 'title': 'Local storage', 'confirmed': True, **fields})
        self.assertEqual(status, 200, body)
        return body['decision']

    @staticmethod
    def review_body(item, **fields):
        return {'task_id': item['id'], 'expected_revision': item['revision'], 'verdict': 'approved',
                'reason': 'Verified locally', 'confirmed': True, **fields}

    def update_decision(self, old, changes):
        return self.request('/api/workspace/decisions/update', 'POST',
            {'id': old['id'], 'expected_updated_at': old['updated_at'], 'changes': changes, 'confirmed': True})

    def test_focus_and_priority_write_filter_order_and_clear(self):
        high = self.item(title='High', priority=0, focus=1)
        self.item(title='Low', priority=2, focus=1)
        self.item(title='Unset', focus=0)
        self.item(title='Normal', priority=1, focus=1)
        status, page = self.request('/api/workspace/items?focus=1&sort=priority')
        self.assertEqual(status, 200)
        self.assertEqual([row['title'] for row in page['items']], ['High', 'Normal', 'Low'])
        self.assertEqual(self.request('/api/workspace/items?priority=0')[1]['total'], 1)
        self.assertEqual(self.request('/api/workspace/items?focus=0')[1]['total'], 1)
        status, changed = self.request('/api/workspace/items/update', 'POST',
            {'id': high['id'], 'expected_revision': high['revision'], 'changes': {'priority': None, 'focus': 0}})
        self.assertEqual((status, changed['item']['priority'], changed['item']['focus']), (200, None, 0))
        self.assertEqual(self.request('/api/workspace/items?sort=priority')[1]['items'][0]['title'], 'Normal')
        self.assertEqual(self.request('/api/workspace/items?focus=1')[1]['total'], 2)

    def test_flag_validation_precedes_noop_comparison_and_cas(self):
        item = self.item(priority=0, focus=0)
        for changes in ({'priority': False}, {'focus': False}, {'priority': 3}, {'priority': '0'},
                        {'focus': None}, {'focus': 2}, {'priority': []}):
            with self.subTest(changes=changes):
                self.assertEqual(self.request('/api/workspace/items/update', 'POST',
                    {'id': item['id'], 'expected_revision': 1, 'changes': changes})[0], 422)
                self.assertEqual(self.request('/api/workspace/items', 'POST', {'title': 'Bad', **changes})[0], 422)
        self.assertEqual(self.profile.store.get_item(item['id'])['revision'], 1)
        for query in ('focus=true', 'focus=', 'focus=2', 'priority=-1', 'priority=null', 'sort=bad',
                      'priority=1&priority=2'):
            self.assertEqual(self.request('/api/workspace/items?' + query)[0], 422, query)
        first = self.request('/api/workspace/items?focus=0&limit=1')[1]
        self.request('/api/workspace/items/update', 'POST',
            {'id': item['id'], 'expected_revision': 1, 'changes': {'focus': 1}})
        self.assertEqual(self.request('/api/workspace/items?focus=0&offset=1&list_revision='
                                     + str(first['list_revision']))[0], 409)

    def test_decision_draft_edit_accept_supersede_and_preserve_history(self):
        old = self.decision(links=['docs/architecture.md'], alternatives='Remote service')
        self.assertEqual(old['status'], 'open')
        status, body = self.update_decision(old, {'decision': 'SQLite', 'rationale': 'Local first'})
        self.assertEqual(status, 200, body)
        edited = body['decision']
        self.assertNotEqual(edited['updated_at'], old['updated_at'])
        self.assertEqual(self.update_decision(old, {'decision': 'Stale'})[0], 409)
        status, body = self.update_decision(edited, {'status': 'accepted'})
        self.assertEqual(status, 200, body)
        accepted = body['decision']
        self.assertEqual(self.update_decision(accepted, {'decision': 'Overwrite history'})[0], 409)
        self.assertEqual(self.update_decision(accepted, {'status': 'open'})[0], 409)
        status, body = self.update_decision(accepted, {'status': 'superseded'})
        self.assertEqual(status, 200, body)
        self.assertEqual(body['decision']['decision'], 'SQLite')
        self.assertEqual(self.update_decision(body['decision'], {'status': 'accepted'})[0], 409)
        replacement = self.decision(status='accepted', decision='Local files')
        page = self.request('/api/workspace/decisions?project_id=' + self.project['id'])[1]
        self.assertEqual(page['total'], 2)
        self.assertEqual({row['id'] for row in page['items']}, {old['id'], replacement['id']})
        self.assertEqual(self.request('/api/workspace/decisions?status=open')[1]['total'], 0)
        self.assertEqual(self.request('/api/workspace/decisions?status=accepted')[1]['total'], 1)

    def test_decision_noop_and_clock_rollback_keep_cas_sound(self):
        old = self.decision()
        self.assertEqual(self.update_decision(old, {'title': old['title']})[1]['decision'], old)
        with patch.object(management, 'now_utc', return_value='2000-01-01T00:00:00+00:00'):
            status, body = self.update_decision(old, {'rationale': 'New reason'})
        self.assertEqual(status, 200, body)
        self.assertGreater(body['decision']['updated_at'], old['updated_at'])
        self.assertEqual(self.update_decision(old, {'rationale': 'Stale'})[0], 409)

    def test_decision_validation_and_project_archive_race(self):
        valid = {'title': 'Choose', 'project_id': self.project['id'], 'confirmed': True}
        for changes in ({'confirmed': False}, {'confirmed': 1}, {'title': ''}, {'title': '\ud800'},
                        {'project_id': 'missing'}, {'links': [1]}, {'links': 'url'},
                        {'status': 'accepted'}, {'status': 'superseded'}, {'extra': True}):
            with self.subTest(changes=changes):
                raw = json.dumps({**valid, **changes}).encode('utf-8')
                self.assertEqual(self.request('/api/workspace/decisions', 'POST', raw)[0], 422)
        for query in ('status=bad', 'project_id=', 'limit=0', 'unknown=1', 'status=open&status=open'):
            self.assertEqual(self.request('/api/workspace/decisions?' + query)[0], 422, query)
        self.assertEqual(self.update_decision({'id': str(uuid4()), 'updated_at': 'missing'}, {'status': 'open'})[0], 404)
        actual = management.save_decision

        def archive_before_write(store, body, **kwargs):
            store.save_project({**self.project_body, 'id': self.project['id'], 'archived': True})
            return actual(store, body, **kwargs)

        with patch.object(management, 'save_decision', side_effect=archive_before_write):
            self.assertEqual(self.request('/api/workspace/decisions', 'POST', valid)[0], 422)
        self.assertEqual(self.request('/api/workspace/decisions')[1]['total'], 0)

    def test_review_outcomes_are_explicit_atomic_and_queryable(self):
        for verdict, state in (('approved', 'done'), ('changes_requested', 'doing'), ('rejected', 'todo')):
            with self.subTest(verdict=verdict):
                item = self.item(state='review')
                request = self.review_body(item, verdict=verdict, reviewer='Manual', run_id='external-reference',
                                           evidence=['Checked diff', 'Ran local smoke test'])
                status, body = self.request('/api/workspace/reviews', 'POST', request)
                self.assertEqual(status, 200, body)
                self.assertEqual((body['item']['state'], body['item']['revision']), (state, 2))
                self.assertEqual(body['review']['evidence'], request['evidence'])
                self.assertEqual(self.request('/api/workspace/reviews', 'POST', request)[0], 409)
                request['expected_revision'] = 2
                self.assertEqual(self.request('/api/workspace/reviews', 'POST', request)[0], 409)
                history = self.request('/api/workspace/reviews?task_id=' + item['id'])[1]
                self.assertEqual(history['items'], [body['review']])
        self.assertEqual(self.request('/api/workspace/reviews?project_id=' + self.project['id'])[1]['total'], 3)
        self.assertEqual(self.request('/api/workspace/reviews?verdict=approved')[1]['total'], 1)

    def test_review_cas_detects_changes_in_another_store(self):
        item = self.item(state='review')
        other = WorkspaceStore(self.profile.store.path, profile=True)
        other.save_item({'priority': 0}, item_id=item['id'], expected_revision=1)
        status, body = self.request('/api/workspace/reviews', 'POST', self.review_body(item))
        self.assertEqual((status, body['reason']), (409, 'item_changed'))
        self.assertEqual(self.request('/api/workspace/reviews')[1]['total'], 0)
        self.assertEqual(other.get_item(item['id'])['state'], 'review')

    def test_concurrent_review_submissions_only_commit_once(self):
        item = self.item(state='review')
        barrier = threading.Barrier(2)

        def submit():
            store = WorkspaceStore(self.profile.store.path, profile=True)
            barrier.wait(timeout=5)
            try:
                management.save_review(store, self.review_body(item))
                return 'approved'
            except WorkspaceError as error:
                return str(error)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: submit(), range(2)))
        self.assertCountEqual(results, ['approved', 'item_changed'])
        self.assertEqual(self.request('/api/workspace/reviews')[1]['total'], 1)
        self.assertEqual(self.profile.store.get_item(item['id'])['revision'], 2)

    def test_failed_review_insert_rolls_back_task_and_list_revision(self):
        item = self.item(state='review')
        before = self.profile.store.list_items()
        # Inject an insert failure on the same connection after the real update.
        actual = self.profile.store._update_item

        def fail_insert(connection, *args):
            result = actual(connection, *args)
            connection.execute("""CREATE TEMP TRIGGER fail_review BEFORE INSERT ON task_reviews
                                  BEGIN SELECT RAISE(ABORT,'synthetic failure'); END""")
            return result

        with patch.object(self.profile.store, '_update_item', side_effect=fail_insert):
            status, body = self.request('/api/workspace/reviews', 'POST', self.review_body(item))
        self.assertEqual((status, body['reason']), (500, 'storage_error'))
        self.assertEqual(self.profile.store.list_items(), before)
        self.assertEqual(self.request('/api/workspace/reviews')[1]['total'], 0)

    def test_review_validation_never_changes_task(self):
        item = self.item(state='review')
        valid = self.review_body(item)
        for changes in ({'confirmed': False}, {'confirmed': 1}, {'verdict': 'unknown'}, {'verdict': []},
                        {'reason': ''}, {'reason': '\ud800'}, {'expected_revision': True},
                        {'evidence': {}}, {'evidence': [1]}, {'run_id': ''}, {'reviewer': 1}, {'extra': True}):
            with self.subTest(changes=changes):
                raw = json.dumps({**valid, **changes}).encode('utf-8')
                self.assertEqual(self.request('/api/workspace/reviews', 'POST', raw)[0], 422)
        missing = {key: value for key, value in valid.items() if key != 'confirmed'}
        self.assertEqual(self.request('/api/workspace/reviews', 'POST', missing)[0], 422)
        self.assertEqual(self.request('/api/workspace/reviews', 'POST', {**valid, 'task_id': str(uuid4())})[0], 404)
        for query in ('task_id=bad', 'project_id=', 'verdict=bad', 'limit=101', 'unknown=1'):
            self.assertEqual(self.request('/api/workspace/reviews?' + query)[0], 422, query)
        self.assertEqual(self.profile.store.get_item(item['id'])['revision'], 1)
        self.assertEqual(self.request('/api/workspace/reviews')[1]['total'], 0)

    def test_history_paging_and_archive_are_readable(self):
        for _ in range(3):
            self.decision()
            self.request('/api/workspace/reviews', 'POST', self.review_body(self.item(state='review')))
        self.profile.store.save_project({**self.project_body, 'id': self.project['id'], 'archived': True})
        for route in ('decisions', 'reviews'):
            first = self.request('/api/workspace/' + route + '?limit=2')[1]
            second = self.request('/api/workspace/' + route + '?limit=2&offset=2')[1]
            self.assertEqual((first['total'], second['total'], len(second['items'])), (3, 3, 1))
            self.assertFalse({row['id'] for row in first['items']} & {row['id'] for row in second['items']})

    def test_new_write_routes_preserve_security_and_strict_body_contract(self):
        for route in ('decisions', 'decisions/update', 'reviews'):
            path = '/api/workspace/' + route
            for headers in ({'Host': 'bad'}, {'Origin': ''}, {'X-Session-Client': ''}, {'X-Session-CSRF': ''}):
                self.assertEqual(self.request(path, 'POST', {}, headers)[0], 403)
            for body in (b'{"confirmed":true,"confirmed":false}', b'{"value":NaN}', b'[]', b'\xff'):
                self.assertEqual(self.request(path, 'POST', body)[0], 400)
            self.assertEqual(self.request(path, 'POST', b' ' * 262145)[0], 413)
            self.assertEqual(self.request(path + '?extra=1', 'POST', {})[0], 400)
        self.server.profile = None
        for route in ('decisions', 'reviews'):
            self.assertEqual(self.request('/api/workspace/' + route)[0], 404)
            self.assertEqual(self.request('/api/workspace/' + route, 'POST', {})[0], 404)


if __name__ == '__main__':
    unittest.main()
