"""Socket-free public news routes and lifecycle contracts."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from control_everything.http.gateway import PanelServer
from tests.backend import test_execution_http


class DailyNewsHTTPTests(unittest.TestCase):
    request = test_execution_http.ExecutionHTTPTests.request

    def setUp(self):
        self.news = Mock()
        self.workspace = Mock()
        self.workspace.news.return_value = {'schema': 1, 'status': 'pending'}
        self.news.get.return_value = {'schema': 1, 'stream': 'hn', 'status': 'pending'}
        self.server = SimpleNamespace(workspace=self.workspace, news_source=self.news, profile=None, execution=None,
                                      expected_host='127.0.0.1:1234', origin='http://127.0.0.1:1234', csrf_token='token')

    def test_default_and_explicit_streams_legacy_route_and_missing_workspace(self):
        self.assertEqual(self.request('/api/workspace/news')[0], 200)
        self.workspace.news.assert_called_with()
        self.assertEqual(self.request('/api/workspace/news?stream=hn')[0], 200)
        self.workspace.news.assert_called_with('hn')
        self.assertEqual(self.request('/api/desk/news')[1]['stream'], 'hn')
        self.news.get.assert_called_with('hn')
        self.server.workspace = None
        self.assertEqual(self.request('/api/workspace/news')[0], 503)
        self.assertEqual(self.request('/api/desk/news')[0], 200)

    def test_exact_queries_and_refresh_body_budget(self):
        for query in ('stream=ai&stream=hn', 'stream=', 'stream=unknown', 'url=https://example.com', 'stream=ai&x=1'):
            self.assertEqual(self.request('/api/workspace/news?' + query)[0], 400)
        self.assertEqual(self.request('/api/desk/news?stream=hn')[0], 400)
        path = '/api/workspace/news/refresh'
        self.assertEqual(self.request(path, 'POST', {'stream': 'ai'})[0], 200)
        self.workspace.news.assert_called_with('ai', refresh=True)
        for body in ({}, {'stream': 'other'}, {'stream': 'hn', 'url': 'https://example.com'},
                     b'{"stream":"hn","stream":"ai"}', b'{"stream":NaN}'):
            self.assertEqual(self.request(path, 'POST', body)[0], 400)
        self.assertEqual(self.request(path, 'POST', b' ' * 1025)[0], 413)
        self.assertEqual(self.request(path + '?stream=hn', 'POST', {'stream': 'hn'})[0], 400)

    def test_security_precedes_refresh_without_source_call(self):
        path = '/api/workspace/news/refresh'
        for headers, reason in (({'Host': 'evil'}, 'host_rejected'), ({'Origin': ''}, 'origin_rejected'),
                                ({'X-Session-Client': ''}, 'client_header_required'), ({'X-Session-CSRF': ''}, 'csrf_rejected')):
            self.assertEqual(self.request(path, 'POST', {'stream': 'ai'}, headers)[1]['reason'], reason)
        self.workspace.news.assert_not_called()

    def test_close_releases_lease_once_without_observer_or_listener(self):
        server = PanelServer.__new__(PanelServer)
        server.news_source, server._news_lease = self.news, 'lease'
        with patch('http.server.ThreadingHTTPServer.server_close'):
            server.server_close()
            server.server_close()
        self.news.release.assert_called_once_with('lease')


if __name__ == '__main__':
    unittest.main()
