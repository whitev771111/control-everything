import copy
import http.client
import json
import io
import socket
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from email.message import Message

from control_everything.http.gateway import WEB, PanelHandler, build_profile_server, make_server
from tests.fixtures.integration_config import integration_config

SOURCE = integration_config()["integrations"]["remote"]["source"]

SESSION_ID = "b7ed3430-9064-4153-894d-d295a388a572"
OTHER_ID = "6ee281b5-e4f7-43ce-b1b7-92da287e5809"
MESSAGE = {"type": "assistant", "uuid": "synthetic-message", "session_id": SESSION_ID,
           "message": {"role": "assistant", "content": [{"type": "text", "text": "<script>synthetic</script>"}]},
           "parent_tool_use_id": None, "parent_agent_id": None}
PAGE = {"available": True, "items": [MESSAGE], "revision": "fixture-v1", "offset": 0, "limit": 20,
        "total_visible": 1, "next_offset": None, "reader_version": "0.2.163", "read_at": "2026-10-03T00:00:00Z", "source": SOURCE}


def fake_service():
    service = Mock()
    service.source = copy.deepcopy(SOURCE)
    service.list_sessions.return_value = {"status": "observed", "source": SOURCE, "observation_revision": "fixture-observation",
        "sessions": [{"session_id": SESSION_ID, "cwd": "/same"}, {"session_id": OTHER_ID, "cwd": "/same"}]}
    service.get_session_status.return_value = {"status": "observed", "session_id": SESSION_ID, "source": SOURCE}
    service.get_session_messages.return_value = copy.deepcopy(PAGE)
    service.terminal.focus_session.return_value = {"focused": True, "status": "confirmed", "session_id": SESSION_ID, "pane_id": "terminal_2", "source": SOURCE}
    service.terminal.workbench.public_status.return_value = {"session": integration_config()["integrations"]["terminal"]["session"], "status": "unknown", "can_connect": False, "can_focus": False}
    service.terminal.connect_workbench.return_value = {"connected": True, "launched": False, "status": "unique_observed", "cleanup": "not_needed"}
    return service


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.service = fake_service()
        self.server = make_server(self.service, ("127.0.0.1", 0))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def request(self, path, method="GET", headers=None, body=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            connection.request(method, path, body=body, headers={"X-Session-Client": "panel", **(headers or {})})
            response = connection.getresponse()
            data = response.read()
            return response.status, dict(response.headers), json.loads(data) if "application/json" in response.getheader("Content-Type", "") else data
        finally:
            connection.close()

    def focus_headers(self):
        return {"Origin": self.server.origin, "X-Session-CSRF": self.server.csrf_token, "Content-Type": "application/json"}

    def test_list_never_reads_messages_and_keeps_same_cwd_ids(self):
        status, headers, result = self.request("/api/sessions")
        self.assertEqual(status, 200)
        self.assertEqual([r["session_id"] for r in result["sessions"]], [SESSION_ID, OTHER_ID])
        self.service.get_session_messages.assert_not_called()
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_source_and_six_message_fields_are_unchanged(self):
        status, _, result = self.request(f"/api/sessions/{SESSION_ID}/messages?limit=20&offset=0&revision=fixture-v1")
        self.assertEqual(status, 200); self.assertEqual(result, PAGE)
        self.assertEqual(set(result["items"][0]), {"type", "uuid", "session_id", "message", "parent_tool_use_id", "parent_agent_id"})
        self.service.get_session_messages.assert_called_once_with(SESSION_ID, offset=0, limit=20, revision="fixture-v1")

    def test_empty_and_scope_failures_remain_unavailable(self):
        for reason in ("reader_empty_unverified", "reader_scope_unverified"):
            with self.subTest(reason=reason):
                value = {"available": False, "reason": reason, "items": [], "total_visible": None, "next_offset": None, "source": SOURCE, "read_at": PAGE["read_at"]}
                self.service.get_session_messages.return_value = value
                status, _, result = self.request(f"/api/sessions/{SESSION_ID}/messages")
                self.assertEqual(status, 200); self.assertEqual(result, value)

    def test_rejects_cross_origin_host_missing_client_and_preflight(self):
        checks = [({"Origin": "https://outside.example"}, "origin_rejected"),
                  ({"Host": "outside.example"}, "host_rejected"),
                  ({"X-Session-Client": ""}, "client_header_required")]
        for headers, reason in checks:
            status, _, result = self.request("/api/sessions", headers=headers)
            self.assertEqual((status, result["reason"]), (403, reason))
        status, headers, _ = self.request("/api/sessions", method="OPTIONS")
        self.assertEqual(status, 403); self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.service.list_sessions.assert_not_called()

    def test_invalid_uuid_query_and_encoded_path_do_not_call_reader(self):
        paths = [f"/api/sessions/{SESSION_ID.upper()}/messages", "/api/sessions/not-a-uuid/messages",
                 f"/api/sessions/{SESSION_ID}/messages?limit=0", f"/api/sessions/{SESSION_ID}/messages?limit=51",
                 f"/api/sessions/{SESSION_ID}/messages?offset=-1", f"/api/sessions/{SESSION_ID}/messages?limit=20&limit=10",
                 f"/api/sessions/{SESSION_ID}/messages?host=elsewhere", "/api/sessions/%2e%2e/messages"]
        for path in paths:
            self.assertEqual(self.request(path)[0], 400)
        self.service.get_session_messages.assert_not_called()

    def test_not_found_and_revision_conflict_status(self):
        self.service.get_session_status.return_value = {"status": "unknown", "reason": "not_found", "source": SOURCE}
        self.assertEqual(self.request(f"/api/sessions/{SESSION_ID}")[0], 404)
        self.service.get_session_messages.return_value = {"available": False, "reason": "revision_mismatch", "source": SOURCE}
        self.assertEqual(self.request(f"/api/sessions/{SESSION_ID}/messages")[0], 409)

    def test_focus_requires_exact_origin_and_per_process_csrf(self):
        for headers in ({}, {"Origin": self.server.origin}, {**self.focus_headers(), "X-Session-CSRF": "wrong"}):
            self.assertEqual(self.request(f"/api/sessions/{SESSION_ID}/focus", "POST", headers, "{}")[0], 403)
        self.service.terminal.focus_session.assert_not_called()
        status, _, result = self.request(f"/api/sessions/{SESSION_ID}/focus", "POST", self.focus_headers(), "{}")
        self.assertEqual(status, 200); self.assertTrue(result["focused"])
        self.service.terminal.focus_session.assert_called_once_with(self.service, SESSION_ID)

    def test_delayed_post_body_receives_original_csrf_rejection(self):
        self.h1_attempts = []
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertNotEqual(self.server.server_address[1], 8766)
        cases = [("missing_origin_and_csrf", {}),
                 ("origin_without_csrf", {"Origin": self.server.origin}),
                 ("wrong_csrf", {"Origin": self.server.origin, "X-Session-CSRF": "wrong",
                                 "Content-Type": "application/json"})]
        for name, headers in cases:
            with self.subTest(header_case=name):
                attempt = {"header_case": name, "delay_seconds": 0.020, "stages": []}
                self.h1_attempts.append(attempt)
                connection = http.client.HTTPConnection(*self.server.server_address, timeout=1.0)
                started = time.monotonic()
                stage = "headers_send"
                try:
                    connection.putrequest("POST", f"/api/sessions/{SESSION_ID}/focus")
                    for key, value in {"X-Session-Client": "panel", **headers}.items():
                        connection.putheader(key, value)
                    connection.putheader("Content-Length", "2")
                    connection.endheaders()
                    attempt["stages"].append({"stage": stage, "ok": True})
                    time.sleep(0.020)
                    stage = "body_send"
                    connection.send(b"{}")
                    attempt["stages"].append({"stage": stage, "ok": True})
                    stage = "getresponse"
                    response = connection.getresponse()
                    attempt["stages"].append({"stage": stage, "ok": True, "status": response.status})
                    stage = "response_read"
                    body = response.read()
                    result = json.loads(body)
                    attempt["stages"].append({"stage": stage, "ok": True, "bytes": len(body)})
                    attempt["response"] = result
                    self.assertEqual((response.status, result["reason"]), (403, "csrf_rejected"))
                    self.assertEqual(self.service.mock_calls, [])
                    attempt["ok"] = True
                except Exception as error:
                    attempt["ok"] = False
                    attempt["stages"].append({"stage": stage, "ok": False})
                    attempt["error"] = {"type": type(error).__name__, "message": str(error),
                                        "winerror": getattr(error, "winerror", None)}
                    raise
                finally:
                    connection.close()
                    attempt["elapsed_ms"] = (time.monotonic() - started) * 1000
                    attempt["service_calls_after"] = [str(call) for call in self.service.mock_calls]
        self.service.terminal.focus_session.assert_not_called()
        self.assertEqual(self.service.mock_calls, [])

    def test_focus_rejects_pane_input_big_or_bad_body(self):
        path = f"/api/sessions/{SESSION_ID}/focus"
        for body in ('{"pane_id":"terminal_4"}', "not json", "[]"):
            self.assertEqual(self.request(path, "POST", self.focus_headers(), body)[0], 400)
        self.assertEqual(self.request(path, "POST", self.focus_headers(), " " * 4097)[0], 413)
        self.service.terminal.focus_session.assert_not_called()

    def test_quota_module_is_served_with_fixed_bytes_and_headers(self):
        status, headers, body = self.request('/quota.js')
        self.assertEqual(status, 200)
        self.assertEqual(body, (WEB / 'workspace' / 'quota.js').read_bytes())
        self.assertEqual(headers['Content-Type'], 'text/javascript; charset=utf-8')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertIn("default-src 'self'", headers['Content-Security-Policy'])
        self.service.list_sessions.assert_not_called()

    def test_rejected_missing_body_times_out_and_next_connection_remains_valid(self):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=1.0)
        try:
            connection.putrequest("POST", f"/api/sessions/{SESSION_ID}/focus")
            connection.putheader("X-Session-Client", "panel")
            connection.putheader("Content-Length", "2")
            started = time.monotonic()
            connection.endheaders()
            response = connection.getresponse()
            result = json.loads(response.read())
            elapsed = time.monotonic() - started
            self.assertEqual((response.status, result["reason"]), (403, "csrf_rejected"))
            self.assertGreaterEqual(elapsed, 0.20)
            self.assertLess(elapsed, 0.75)
            self.assertEqual(self.service.mock_calls, [])
        finally:
            connection.close()
        self.assertEqual(self.request(f"/api/sessions/{SESSION_ID}/focus", "POST", self.focus_headers(), "{}")[0], 200)
        self.service.terminal.focus_session.assert_called_once_with(self.service, SESSION_ID)

    def test_workbench_get_is_readonly_and_rejects_query(self):
        self.assertEqual(self.request("/api/workbench")[0], 200)
        self.service.terminal.workbench.public_status.assert_called_once_with()
        self.service.terminal.connect_workbench.assert_not_called()
        self.service.get_session_messages.assert_not_called()
        self.assertEqual(self.request("/api/workbench?session=other")[0], 400)

    def test_workbench_connect_uses_same_csrf_and_strict_empty_body(self):
        path = "/api/workbench/connect"
        self.assertEqual(self.request(path, "POST", {}, "{}")[0], 403)
        for body in ('{"session":"other"}', '[]', 'null', 'not-json'):
            self.assertEqual(self.request(path, "POST", self.focus_headers(), body)[0], 400)
        self.assertEqual(self.request(path + "?x=1", "POST", self.focus_headers(), "{}")[0], 404)
        self.service.terminal.connect_workbench.assert_not_called()
        self.assertEqual(self.request(path, "POST", self.focus_headers(), "{}")[0], 200)
        self.service.terminal.connect_workbench.assert_called_once_with()
        self.service.terminal.focus_session.assert_not_called()

    def test_workbench_business_refusal_and_launch_error_status(self):
        for reason, code in [("terminal_action_busy", 409), ("cleanup_unverified", 409),
                             ("workbench_not_running", 409), ("workbench_launch_failed", 503), ("workbench_timeout", 503)]:
            self.service.terminal.connect_workbench.return_value = {"connected": False, "reason": reason}
            self.assertEqual(self.request("/api/workbench/connect", "POST", self.focus_headers(), "{}")[0], code)

    def test_health_is_only_local_gateway_and_static_paths_are_fixed(self):
        status, _, result = self.request("/health")
        self.assertEqual(status, 200); self.assertEqual(result, {"app": "remote-claude-session-panel", "protocol_version": 1})
        status, headers, page = self.request("/")
        self.assertEqual(status, 200); self.assertIn(b'lang="zh-CN"', page)
        self.assertIn("img-src 'none'", headers["Content-Security-Policy"])
        self.assertEqual(self.request("/../requirements.txt")[0], 404)
        self.service.list_sessions.assert_not_called()

    def test_business_static_routes_preserve_bytes_mime_and_security_headers(self):
        routes = ["app.js", "style.css"]
        for filename in routes:
            with self.subTest(filename=filename):
                status, headers, body = self.request('/' + filename)
                self.assertEqual(status, 200)
                self.assertEqual(body, (WEB / "sessions" / filename).read_bytes())
                self.assertEqual(headers["Content-Type"], "text/css; charset=utf-8" if filename.endswith(".css")
                                 else "text/javascript; charset=utf-8")
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(headers["Content-Security-Policy"], "default-src 'self'; img-src 'none'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        self.service.list_sessions.assert_not_called()
        self.service.get_session_messages.assert_not_called()
        self.service.terminal.focus_session.assert_not_called()

    def test_retired_garden_routes_are_404_for_legacy_and_profile_servers(self):
        # Retirement coverage uses valid security headers, so 403 cannot hide a live route.
        legacy_server = self.server
        with TemporaryDirectory() as directory:
            profile_server = build_profile_server(directory, 0, quota_source=Mock(), news_source=Mock())
            profile_thread = threading.Thread(target=profile_server.serve_forever, daemon=True)
            profile_thread.start()
            try:
                for mode, server in (("legacy", legacy_server), ("profile", profile_server)):
                    self.server = server
                    with self.subTest(mode=mode):
                        self.assertNotIn("garden", server.__dict__)
                        routes = ["/api/garden", f"/api/garden/actors/{SESSION_ID}/public-messages", "/garden.js"]
                        for route in routes:
                            with self.subTest(route=route):
                                status, headers, result = self.request(route)
                                self.assertEqual((status, result["reason"]), (404, "route_not_found"))
                                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                        for route in ("/api/garden/request-review", "/api/garden/accept"):
                            with self.subTest(route=route):
                                status, _, result = self.request(route, "POST", self.focus_headers(), "{}")
                                self.assertEqual((status, result["reason"]), (404, "route_not_found"))
                        status, _, bootstrap = self.request("/api/bootstrap")
                        self.assertEqual(status, 200)
                        self.assertNotIn("garden_root", bootstrap)
                        self.assertNotIn("garden", bootstrap.get("capabilities", {}))
            finally:
                self.server = legacy_server
                profile_server.shutdown()
                profile_server.server_close()
                profile_thread.join()
        self.assertEqual(self.service.mock_calls, [])

    def test_retired_rendering_routes_are_404_without_business_calls(self):
        routes = ["/cockpit.js", "/robot3d.js", "/vendor/three-0.186.0/three.module.js",
                  "/vendor/three-0.186.0/three.core.js", "/vendor/three-0.186.0/LICENSE"]
        for route in routes:
            with self.subTest(route=route):
                status, headers, body = self.request(route)
                self.assertEqual((status, body["reason"]), (404, "route_not_found"))
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.service.list_sessions.assert_not_called()
        self.service.get_session_messages.assert_not_called()
        self.service.terminal.focus_session.assert_not_called()

    def test_business_static_missing_file_is_explicit_404(self):
        with TemporaryDirectory() as directory, patch("control_everything.http.gateway.WEB", Path(directory)):
            for path in ("/app.js", "/style.css"):
                status, headers, body = self.request(path)
                self.assertEqual((status, body["reason"]), (404, "route_not_found"))
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertIn("default-src 'self'", headers["Content-Security-Policy"])

    def test_static_whitelist_rejects_retired_prefix_query_and_encoded_paths(self):
        paths = ["/cockpit.js?v=1", "/vendor/three-0.186.0/", "/vendor/three-0.186.0/package.json",
                 "/vendor/three-0.186.0/../three.core.js", "/vendor/three-0.186.0/%2e%2e/three.core.js",
                 "/vendor/three-0.186.0/%74hree.core.js", "/vendor/three-0.186.0/three.core.js/extra"]
        for path in paths:
            with self.subTest(path=path):
                status, _, body = self.request(path)
                self.assertEqual((status, body["reason"]), (404, "route_not_found"))
        self.service.list_sessions.assert_not_called()
        self.service.get_session_messages.assert_not_called()

    def test_exception_does_not_leak_message_or_credentials(self):
        self.service.get_session_messages.side_effect = RuntimeError("synthetic-private-content")
        status, _, result = self.request(f"/api/sessions/{SESSION_ID}/messages")
        self.assertEqual(status, 503); self.assertEqual(result["reason"], "gateway_query_failed")
        self.assertNotIn("synthetic-private-content", json.dumps(result)); self.assertEqual(result["source"], SOURCE)

    def test_existing_listener_refuses_second_gateway_or_reuse_socket(self):
        # Own temporary loopback fixture only; never inspect or bind production 8766.
        with self.assertRaises(OSError):
            with make_server(fake_service(), self.server.server_address):
                pass
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as competing:
            competing.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with self.assertRaises(OSError):
                competing.bind(self.server.server_address)


class GatewayDispatchTests(unittest.TestCase):
    """Direct handler tests never bind a listener or execute platform actions."""

    def handler(self, headers=None, body=b"{}", path="/api/workbench/connect"):
        handler = PanelHandler.__new__(PanelHandler)
        handler.server = Mock(expected_host="127.0.0.1:1", origin="http://127.0.0.1:1", csrf_token="fixture")
        handler.server.service = fake_service()
        handler.connection = Mock()
        handler.connection.gettimeout.return_value = None
        handler.path = path; handler.rfile = io.BytesIO(body)
        handler.headers = Message()
        for name, value in {"Host": handler.server.expected_host, "Origin": handler.server.origin,
                            "X-Session-Client": "panel", "X-Session-CSRF": "fixture", "Content-Type": "application/json",
                            "Content-Length": str(len(body)), **(headers or {})}.items():
            handler.headers[name] = value
        handler.reply = Mock(); handler.reject = Mock()
        return handler

    def test_valid_empty_connect_dispatches_no_arguments(self):
        handler = self.handler(); handler.do_POST()
        handler.server.service.terminal.connect_workbench.assert_called_once_with()
        handler.server.service.terminal.focus_session.assert_not_called()
        self.assertEqual(handler.reply.call_args.args[0], 200)

    def test_invalid_headers_body_query_and_duplicate_headers_never_dispatch(self):
        handlers = [self.handler({"Origin": "https://outside"}), self.handler({"X-Session-CSRF": "bad"}),
                    self.handler({"Transfer-Encoding": "chunked"}), self.handler(body=b'{"pane_id":"terminal_2"}'),
                    self.handler(path="/api/workbench/connect?x=1"), self.handler(path="/api/workbench/connect/extra")]
        duplicate = self.handler(); duplicate.headers["Content-Length"] = "2"; handlers.append(duplicate)
        duplicate = self.handler(); duplicate.headers["Origin"] = duplicate.server.origin; handlers.append(duplicate)
        for handler in handlers:
            with self.subTest(path=handler.path, headers=str(handler.headers)):
                handler.do_POST(); handler.reject.assert_called_once()
                handler.server.service.terminal.connect_workbench.assert_not_called()

    def test_codex_get_dispatch_is_independent_and_passes_only_offset(self):
        for suffix, offset in [('', 0), ('?offset=12', 12), ('?offset=999999999', 999999999)]:
            with self.subTest(suffix=suffix), patch('control_everything.http.gateway.list_codex_threads', return_value={'available': True}) as reader:
                handler = self.handler(path='/api/codex/threads' + suffix); handler.do_GET()
                reader.assert_called_once_with(offset); handler.reply.assert_called_once_with(200, {'available': True})
                self.assertEqual(handler.server.service.mock_calls, [])

    def test_codex_query_path_and_post_reject_before_reader(self):
        paths = ['/api/codex/threads?' + query for query in ('offset=', 'offset=-1', 'offset=1.1', 'offset=0&offset=1',
                 'limit=12', 'path=synthetic', 'source=cli', 'sql=select', 'offset=1000000000', 'offset', 'offset=0&x=1')]
        for path in paths + ['/api/codex/threads/extra']:
            with self.subTest(path=path), patch('control_everything.http.gateway.list_codex_threads') as reader:
                handler = self.handler(path=path); handler.do_GET(); reader.assert_not_called()
                self.assertEqual(handler.reject.call_args.args[0], 404 if path.endswith('/extra') else 400)
        with patch('control_everything.http.gateway.list_codex_threads') as reader:
            handler = self.handler(path='/api/codex/threads'); handler.do_POST(); reader.assert_not_called()
            handler.reject.assert_called_once_with(404, 'route_not_found')

    def test_codex_same_host_origin_client_guards_precede_reader(self):
        for headers in ({'Host': 'outside.example'}, {'Origin': 'https://outside.example'}, {'X-Session-Client': ''}):
            with self.subTest(headers=headers), patch('control_everything.http.gateway.list_codex_threads') as reader:
                handler = self.handler(headers=headers, path='/api/codex/threads'); handler.do_GET()
                reader.assert_not_called(); self.assertEqual(handler.reject.call_args.args[0], 403)
        with patch('control_everything.http.gateway.list_codex_threads') as reader:
            handler = self.handler(path='/api/codex/threads'); handler.headers['Host'] = handler.server.expected_host
            handler.do_GET(); reader.assert_not_called(); self.assertEqual(handler.reject.call_args.args[0], 403)

    def test_codex_unavailable_is_503_and_private_exception_is_sanitized(self):
        with patch('control_everything.http.gateway.list_codex_threads', return_value={'available': False, 'reason': 'codex_index_unavailable'}) as reader:
            handler = self.handler(path='/api/codex/threads'); handler.do_GET()
            reader.assert_called_once_with(0); handler.reply.assert_called_once_with(503, {'available': False, 'reason': 'codex_index_unavailable'})
        with patch('control_everything.http.gateway.list_codex_threads', side_effect=RuntimeError('synthetic-private')):
            handler = self.handler(path='/api/codex/threads'); handler.do_GET()
            handler.reject.assert_called_once_with(503, 'gateway_query_failed')


class GatewayRejectionTests(unittest.TestCase):
    def handler(self, headers=None, body=b"{}", path="/api/workbench/connect"):
        handler = GatewayDispatchTests.handler(self, headers, body, path)
        handler.connection.gettimeout.return_value = 0.75
        handler.rfile = Mock(wraps=io.BytesIO(body))
        handler.reject = PanelHandler.reject.__get__(handler)
        return handler

    def test_small_body_keeps_rejection_precedence_and_restores_timeout(self):
        cases = [({"Host": "outside", "Origin": "outside"}, 403, "host_rejected"),
                 ({"Origin": "outside", "X-Session-Client": ""}, 403, "origin_rejected"),
                 ({"X-Session-Client": "", "X-Session-CSRF": "bad"}, 403, "client_header_required"),
                 ({"X-Session-CSRF": "bad"}, 403, "csrf_rejected"),
                 ({"Content-Type": "text/plain"}, 400, "invalid_request")]
        for headers, status, reason in cases:
            with self.subTest(headers=headers):
                handler = self.handler(headers); handler.do_POST()
                self.assertEqual((handler.reply.call_args.args[0], handler.reply.call_args.args[1]["reason"]), (status, reason))
                handler.rfile.read1.assert_called_once_with(2)
                handler.rfile.read.assert_not_called()
                self.assertEqual(handler.connection.settimeout.call_args.args, (0.75,))
                self.assertEqual(handler.server.service.mock_calls, [])
                handler.reject(status, reason)
                handler.rfile.read1.assert_called_once_with(2)

    def test_length_boundary_and_unreliable_framing_never_read(self):
        handler = self.handler({"X-Session-CSRF": "bad", "Content-Length": "4096"}, b"x" * 4096 + b"unread")
        handler.do_POST(); handler.rfile.read1.assert_called_once_with(4096)
        self.assertEqual(handler.rfile.tell(), 4096)
        handlers = [self.handler({"X-Session-CSRF": "bad", "Content-Length": length})
                    for length in ("0", "4097", "-1", "2, 2", "10000000000", "", "abc")]
        missing = self.handler({"X-Session-CSRF": "bad"}); del missing.headers["Content-Length"]; handlers.append(missing)
        duplicate = self.handler({"X-Session-CSRF": "bad"}); duplicate.headers["Content-Length"] = "2"; handlers.append(duplicate)
        handlers.extend(self.handler({"X-Session-CSRF": "bad", "Transfer-Encoding": value}) for value in ("chunked", ""))
        for handler in handlers:
            with self.subTest(headers=str(handler.headers)):
                handler.do_POST()
                self.assertEqual((handler.reply.call_args.args[0], handler.reply.call_args.args[1]["reason"]), (403, "csrf_rejected"))
                handler.rfile.read1.assert_not_called(); handler.rfile.read.assert_not_called()
                self.assertEqual(handler.connection.mock_calls, [])
                self.assertEqual(handler.server.service.mock_calls, [])

    def test_eof_and_read_errors_restore_original_timeout_and_rejection(self):
        for previous_timeout, error in ((None, None), (0.75, OSError("fixture")), (0, TimeoutError("fixture"))):
            with self.subTest(previous_timeout=previous_timeout, error=error):
                handler = self.handler({"X-Session-CSRF": "bad"})
                handler.connection.gettimeout.return_value = previous_timeout
                handler.rfile.read1.side_effect = error
                handler.rfile.read1.return_value = b""
                handler.do_POST()
                handler.rfile.read1.assert_called_once_with(2)
                self.assertEqual(handler.connection.settimeout.call_args.args, (previous_timeout,))
                self.assertEqual((handler.reply.call_args.args[0], handler.reply.call_args.args[1]["reason"]), (403, "csrf_rejected"))
                self.assertEqual(handler.server.service.mock_calls, [])

    def test_partial_bytes_do_not_restart_cumulative_wait_budget(self):
        handler = self.handler({"X-Session-CSRF": "bad", "Content-Length": "4096"})
        handler.rfile.read1.side_effect = lambda _size: b"x"
        with patch("control_everything.http.gateway.time.monotonic", side_effect=(0, 0, 0.10, 0.24, 0.26)):
            handler.do_POST()
        self.assertEqual([call.args for call in handler.rfile.read1.call_args_list], [(4096,), (4095,), (4094,)])
        for call, expected in zip(handler.connection.settimeout.call_args_list, (0.25, 0.15, 0.01, 0.75), strict=True):
            self.assertAlmostEqual(call.args[0], expected)
        self.assertEqual(handler.reply.call_args.args[0], 403)
        self.assertEqual(handler.server.service.mock_calls, [])

    def test_normal_body_read_is_not_repeated_after_json_or_service_error(self):
        for body in (b"not-json", b"[]", b"null", b"\xff", b"{}"):
            with self.subTest(body=body):
                handler = self.handler(body=body)
                handler.server.service.terminal.connect_workbench.side_effect = RuntimeError("fixture-private")
                handler.do_POST()
                handler.rfile.read.assert_called_once_with(len(body)); handler.rfile.read1.assert_not_called()
                self.assertEqual(handler.connection.mock_calls, [])
                self.assertEqual(handler.reply.call_args.args[0], 503 if body == b"{}" else 400)
                self.assertNotIn("fixture-private", json.dumps(handler.reply.call_args.args[1]))
                if body != b"{}":
                    self.assertEqual(handler.server.service.mock_calls, [])

    def test_get_and_options_rejections_do_not_consume_body(self):
        for method in ("do_GET", "do_OPTIONS"):
            with self.subTest(method=method):
                handler = self.handler({"Host": "outside"})
                getattr(handler, method)()
                self.assertEqual(handler.reply.call_args.args[0], 403)
                handler.rfile.read.assert_not_called(); handler.rfile.read1.assert_not_called()
                self.assertEqual(handler.connection.mock_calls, [])
                self.assertEqual(handler.server.service.mock_calls, [])




class GatewayEntryTests(unittest.TestCase):
    def test_legacy_cli_contract(self):
        import runpy
        from unittest.mock import patch

        with patch("control_everything.http.gateway.main", return_value=7) as main:
            with self.assertRaises(SystemExit) as caught:
                runpy.run_module("control_everything.gateway", run_name="__main__")
        self.assertEqual(caught.exception.code, 7)
        main.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
