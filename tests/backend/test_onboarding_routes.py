"""Socket-free route authorization and selection contracts."""

import json
from unittest.mock import patch

from tests.backend import test_profile_http as http_fixtures


class OnboardingRoutesTests(http_fixtures.ProfileHTTPTests):
    def test_consent_candidates_selection_and_security(self):
        path = self.root / 'claude.json'
        path.write_text(json.dumps({'projects': {'C:/example/a': {}, 'C:/example/b': {}}}), encoding='utf-8')
        with patch('control_everything.sessions.project_sources.Path.home', return_value=self.root):
            overview = self.request('/api/workspace/onboarding')[1]
        self.assertEqual(len(overview['tools']), 3)
        self.assertEqual(self.request('/api/workspace/project-candidates?provider=claude')[1]['status'], 'not_configured')
        body = {'expected_revision': 0, 'provider': 'claude', 'enabled': True, 'path': str(path)}
        self.assertEqual(self.request('/api/workspace/sources', 'POST', body, {'X-Session-CSRF': 'wrong'})[0], 403)
        self.assertEqual(self.request('/api/workspace/sources', 'POST', body)[0], 200)
        self.assertEqual(self.request('/api/workspace/sources', 'POST', body)[0], 409)
        page = self.request('/api/workspace/project-candidates?provider=claude')[1]
        self.assertEqual(len(page['items']), 2)
        self.assertEqual(self.workspace.registered_projects(), [])
        item = page['items'][0]
        join = {'action': 'join', 'provider': 'claude', 'candidate_id': item['candidate_id'],
                'snapshot_token': page['snapshot_token'], 'selection_token': page['selection_token']}
        status, value = self.request('/api/workspace/project-selection', 'POST', join)
        self.assertEqual((status, value['selected']), (200, True))
        self.assertEqual(len(self.workspace.registered_projects()), 1)
        self.assertEqual(self.request('/api/workspace/project-selection', 'POST', {**join, 'path': str(path)})[0], 422)
        self.assertEqual(self.request('/api/workspace/project-candidates?provider=other')[0], 422)
        self.assertEqual(self.request('/api/workspace/project-candidates?provider=claude&provider=codex')[0], 422)
