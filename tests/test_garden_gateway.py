import json
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from session_panel.gateway import PanelHandler
from tests import test_gateway
from tests.test_garden import Fixture, ROOT, WORKER, REVIEWER, OTHER_WORKER, TASK


class GardenGatewayTests(unittest.TestCase):
    def handler(self, garden, path, body=b"{}", headers=None):
        handler = test_gateway.GatewayDispatchTests.handler(self, headers=headers, body=body, path=path)
        handler.server.garden = garden
        return handler

    def test_bound_root_read_query_reject_and_old_bootstrap_without_context(self):
        with TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            root = fixture.assign()
            handler = self.handler(root, "/api/garden"); handler.do_GET()
            self.assertEqual(handler.reply.call_args.args[1]["root_thread_id"], ROOT)
            before = root.path.read_bytes()
            handler = self.handler(root, "/api/garden?root=other"); handler.do_GET()
            handler.reject.assert_called_once_with(400, "invalid_request")
            handler = self.handler(None, "/api/bootstrap"); handler.do_GET()
            self.assertNotIn("garden_root", handler.reply.call_args.args[1])
            self.assertEqual(before, root.path.read_bytes())

    def test_request_receipt_is_pending_and_header_body_failures_do_not_write(self):
        with TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            root, version, digest = fixture.delivered()
            payload = {"task_id": TASK, "version": version, "artifact_sha256": digest, "reviewer_id": REVIEWER}
            body = json.dumps(payload).encode()
            before = root.path.read_bytes()
            cases = [({"X-Session-CSRF": "bad"}, body), ({"Origin": "https://outside"}, body), ({"Host": "outside"}, body),
                     ({"X-Session-Client": ""}, body), ({"Transfer-Encoding": "chunked"}, body),
                     ({"Content-Type": "text/plain"}, body), ({}, b"[]"), ({}, b"{}"), ({}, b"\xff"), ({}, b" " * 4097),
                     ({}, json.dumps({**payload, "actor": ROOT}).encode()), ({}, json.dumps({**payload, "version": True}).encode())]
            for headers, data in cases:
                handler = self.handler(root, "/api/garden/request-review", data, headers)
                handler.do_POST()
                self.assertEqual(before, root.path.read_bytes())
                self.assertTrue(handler.reject.called or handler.reply.call_args.args[0] >= 400)
                self.assertEqual(handler.server.service.mock_calls, [])
            for header in ("Content-Length", "Content-Type", "Origin", "X-Session-CSRF"):
                handler = self.handler(root, "/api/garden/request-review", body)
                handler.headers[header] = handler.headers[header]
                handler.do_POST()
                self.assertEqual(before, root.path.read_bytes())
            handler = self.handler(root, "/api/garden/request-review", body); handler.do_POST()
            self.assertEqual(handler.reply.call_args.args[0], 200)
            receipt = handler.reply.call_args.args[1]["receipt"]
            self.assertEqual((receipt["result"], receipt["source"], receipt["actor_id"]), ("review_pending", "panel_web", ROOT))

    def test_partial_body_uses_one_deadline_and_restores_timeout(self):
        handler = self.handler(None, "/api/garden/request-review", b"x" * 4)
        handler.rfile = Mock(); handler.rfile.read1.return_value = b"x"
        handler.connection.gettimeout.return_value = 0.75
        with patch("session_panel.gateway.time.monotonic", side_effect=(0, 0, .10, .24, .26)):
            handler.do_POST()
        self.assertEqual(handler.rfile.read1.call_count, 3)
        self.assertEqual(handler.connection.settimeout.call_args.args, (0.75,))
        handler.reject.assert_called_once_with(400, "invalid_request")
        self.assertEqual(handler.server.service.mock_calls, [])

    def test_public_read_requires_bound_root_registered_actor_and_clean_get(self):
        with TemporaryDirectory() as directory:
            root = Fixture(directory).assign()
            route = f"/api/garden/actors/{WORKER}/public-messages"
            with patch("session_panel.gateway.read_public_messages", return_value={"available": True, "root_thread_id": ROOT,
                       "actor_id": WORKER, "messages": []}) as reader:
                handler = self.handler(root, route); handler.do_GET()
                self.assertEqual(handler.reply.call_args.args[0], 200)
                reader.assert_called_once_with(root, WORKER)
            for path in (route + "?root=other", f"/api/garden/actors/{WORKER.upper()}/public-messages"):
                handler = self.handler(root, path); handler.do_GET()
                self.assertTrue(handler.reject.called or handler.reply.call_args.args[0] >= 400)
            handler = self.handler(None, route); handler.do_GET()
            self.assertEqual(handler.reply.call_args.args[0], 503)
            handler = self.handler(root, f"/api/garden/actors/{OTHER_WORKER}/public-messages"); handler.do_GET()
            self.assertEqual(handler.reply.call_args.args[0], 404)
            for header in ({"Host": "outside"}, {"Origin": "https://outside"}, {"X-Session-Client": ""}):
                handler = self.handler(root, route, headers=header); handler.do_GET()
                self.assertTrue(handler.reject.called)


if __name__ == "__main__":
    unittest.main()
