"""Socket-free HTTP handler checks; actual listeners belong to root integration."""

import io
import json
import unittest
from email.message import Message
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from session_panel.gateway import PanelHandler, PanelServer, make_server
from session_panel.workspace_store import WorkspaceError


class ExecutionHTTPTests(unittest.TestCase):
    def setUp(self):
        self.execution = Mock()
        self.execution.snapshot.return_value = {"runs": []}
        self.execution.context.return_value = {"context_hash": "a" * 64}
        self.execution.submit.return_value = {"run": {"id": str(uuid4())}, "replayed": False}
        self.execution.act.return_value = {"run": {}, "replayed": False}
        self.server = SimpleNamespace(execution=self.execution, execution_reason=None, workspace=None,
                                      expected_host="127.0.0.1:1234", origin="http://127.0.0.1:1234", csrf_token="token")

    def request(self, path, method="GET", body=None, headers=None, *, disconnect=False):
        handler = PanelHandler.__new__(PanelHandler)
        handler.server, handler.path = self.server, path
        handler.headers = Message()
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else b""
        supplied = {"Host": self.server.expected_host, "X-Session-Client": "panel"}
        if method == "POST":
            supplied.update({"Origin": self.server.origin, "X-Session-CSRF": "token", "Content-Type": "application/json",
                             "Content-Length": str(len(data))})
        supplied.update(headers or {})
        for key, value in supplied.items():
            if isinstance(value, list):
                for item in value:
                    handler.headers[key] = item
            else:
                handler.headers[key] = value
        handler.rfile = io.BytesIO(data)
        handler.connection = Mock()
        handler.connection.gettimeout.return_value = 2
        handler.reply = Mock()
        if disconnect:
            handler.reply.side_effect = ConnectionAbortedError("disconnected fixture")
        self.last_handler = handler
        getattr(handler, "do_" + method)()
        return handler.reply.call_args.args[:2]

    def test_exact_routes_query_shapes_and_response_codes(self):
        run_id, task_id, approval_id, request_id = (str(uuid4()) for _ in range(4))
        base = "/api/workspace/execution"
        self.assertEqual(self.request(base)[0], 200)
        self.assertEqual(self.request(base + "?request_id=" + request_id)[0], 200)
        self.execution.snapshot.assert_called_with(request_id=request_id)
        self.assertEqual(self.request(base + "/context?task_id=" + task_id)[0], 200)
        self.assertEqual(self.request(base + "/runs/" + run_id + "?after=22")[0], 200)
        self.execution.snapshot.assert_called_with(run_id, 22)
        self.assertEqual(self.request(base + "/runs", "POST", {})[0], 202)
        self.execution.submit.return_value["replayed"] = True
        self.assertEqual(self.request(base + "/runs", "POST", {})[0], 200)
        self.assertEqual(self.request(base + "/runs/" + run_id + "/cancel", "POST", {})[0], 202)
        self.assertEqual(self.request(base + "/runs/" + run_id + "/approvals/" + approval_id,
                                      "POST", {"decision": "decline"})[0], 202)
        self.execution.act.assert_called_with(run_id, {"decision": "decline"}, approval_id=approval_id)
        for path in (base + "?unknown=1", base + "?request_id=x", base + "?request_id=" + request_id + "&request_id=" + request_id,
                     base + "/context", base + "/context?task_id=" + task_id + "&cwd=x", base + "/runs/" + run_id + "?after=-1",
                     base + "/runs/" + run_id + "?after=9223372036854775808"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 400)
        self.assertEqual(self.request(base + "/runs/" + run_id + "/extra")[0], 404)
        self.assertEqual(self.request(base + "/runs?x=1", "POST", {})[0], 400)

    def test_security_boundary_precedes_execution(self):
        for headers, reason in (({"Host": "evil"}, "host_rejected"), ({"Origin": "https://evil"}, "origin_rejected"),
                                ({"X-Session-Client": ""}, "client_header_required"), ({"X-Session-CSRF": ""}, "csrf_rejected")):
            status, body = self.request("/api/workspace/execution/runs", "POST", {}, headers)
            self.assertEqual((status, body["reason"]), (403, reason))
        self.execution.submit.assert_not_called()

    def test_only_submission_has_larger_body_budget(self):
        prompt = {"prompt": "😀" * 4000}
        self.assertEqual(self.request("/api/workspace/execution/runs", "POST", prompt)[0], 202)
        path = "/api/workspace/execution/runs/" + str(uuid4()) + "/cancel"
        self.assertEqual(self.request(path, "POST", prompt)[0], 400)
        self.assertEqual(self.request("/api/workspace/execution/runs", "POST", b" " * 24577)[0], 400)
        for data, headers in ((b"{}", {"Content-Type": "text/plain"}), (b"{}", {"Content-Length": ["2", "2"]}),
                              (b"{}", {"Transfer-Encoding": "chunked"}), (b"[]", {}), (b'{"x":NaN}', {}), (b"{", {})):
            self.assertEqual(self.request("/api/workspace/execution/runs", "POST", data, headers)[0], 400)
        handler = PanelHandler.__new__(PanelHandler)
        handler.headers = Message()
        handler.headers["Content-Length"], handler.headers["Content-Type"] = "4097", "application/json"
        with self.assertRaises(ValueError):
            handler.garden_body()

    def test_fixed_error_matrix_never_exposes_exception_text(self):
        reasons = {400: ["invalid_request"], 404: ["task_not_found", "run_not_found", "approval_not_found", "request_not_found"],
                   409: ["idempotency_conflict", "context_changed", "project_busy", "capacity_busy", "approval_expired",
                         "decision_conflict", "other_instance"], 503: ["execution_unavailable", "storage_unavailable"]}
        for expected, items in reasons.items():
            for reason in items:
                with self.subTest(reason=reason):
                    self.execution.submit.side_effect = ValueError(reason) if expected == 400 else WorkspaceError(reason)
                    status, body = self.request("/api/workspace/execution/runs", "POST", {})
                    self.assertEqual((status, body["reason"]), (expected, reason))
        self.execution.submit.side_effect = RuntimeError("private command secret")
        self.assertEqual(self.request("/api/workspace/execution/runs", "POST", {})[1]["reason"], "execution_unavailable")
        self.execution.snapshot.side_effect = WorkspaceError("request_not_found")
        self.assertEqual(self.request("/api/workspace/execution?request_id=" + str(uuid4()))[0], 404)

    def test_disabled_execution_keeps_old_health_and_no_constructor_side_effects(self):
        self.server.execution = None
        self.assertEqual(self.request("/api/workspace/execution")[1]["reason"], "execution_unavailable")
        self.assertEqual(self.request("/health")[0], 200)
        fake_server = Mock()
        fake_server.server_address = ("127.0.0.1", 1234)
        with patch("session_panel.gateway.PanelServer", return_value=fake_server):
            result = make_server(Mock(), ("127.0.0.1", 0))
        self.assertIsNone(result.execution)
        with patch("session_panel.gateway.PanelServer", side_effect=OSError("bind")):
            with self.assertRaises(OSError):
                make_server(Mock(), ("127.0.0.1", 0), execution=self.execution)
        self.execution.close.assert_called_once()

    def test_server_close_is_idempotent_even_if_execution_close_fails(self):
        server = PanelServer.__new__(PanelServer)
        server.execution = self.execution
        self.execution.close.side_effect = RuntimeError("close")
        with patch("http.server.ThreadingHTTPServer.server_close") as parent_close:
            with self.assertRaises(RuntimeError):
                server.server_close()
            server.server_close()
        self.execution.close.assert_called_once()
        self.assertEqual(parent_close.call_count, 2)

    def test_disconnected_client_never_gets_a_second_reply(self):
        status, _body = self.request("/api/workspace/execution/runs", "POST", {}, disconnect=True)
        self.assertEqual(status, 202)
        self.assertEqual(self.last_handler.reply.call_count, 1)
        self.execution.submit.assert_called_once()
        self.execution.snapshot.side_effect = WorkspaceError("storage_unavailable")
        status, body = self.request("/api/workspace/execution", disconnect=True)
        self.assertEqual((status, body["reason"]), (503, "storage_unavailable"))
        self.assertEqual(self.last_handler.reply.call_count, 1)


if __name__ == "__main__":
    unittest.main()
