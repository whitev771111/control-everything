"""Observation action contracts without a listener or native processes."""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock
from urllib.parse import urlsplit

from session_panel.http.observation_routes import handle_post


class ObservationRoutesTests(TestCase):
    def handler(self, body, result=None, *, observer=True):
        backend = Mock()
        backend.request.return_value = result or {"status": "available"}
        return Mock(server=SimpleNamespace(observer=backend if observer else None),
                    read_json_body=Mock(return_value=body))

    def test_each_action_validates_before_dispatch_and_preserves_body(self):
        cases = [
            ("return", {"action_id": "action", "generation": "g", "seq": 0}, "return_window"),
            ("window-choices/close", {"selection_id": "choice", "generation": "g"}, "dismiss_window_choices"),
            ("window-choices", {"registration_id": "a" * 32}, "list_window_choices"),
        ]
        for route, body, method in cases:
            with self.subTest(route=route):
                handler = self.handler(body)
                handle_post(handler, urlsplit("/api/workspace/observations/" + route))
                handler.read_json_body.assert_called_once_with(max_bytes=4096, strict=True)
                handler.server.observer.request.assert_called_once_with(method, body)
                handler.reply.assert_called_once_with(200, {"status": "available"})
                handler.reject.assert_not_called()

    def test_invalid_input_never_reaches_observer(self):
        invalid = [{"action_id": "a", "generation": "g", "seq": True},
                   {"action_id": "a", "generation": "g", "seq": -1},
                   {"action_id": "a", "generation": "g", "seq": 0, "extra": 1},
                   {"action_id": "bad token", "generation": "g", "seq": 0}, {}]
        for body in invalid:
            with self.subTest(body=body):
                handler = self.handler(body)
                handle_post(handler, urlsplit("/api/workspace/observations/return"))
                handler.reject.assert_called_once_with(400, "invalid_request")
                handler.server.observer.request.assert_not_called()
        handler = self.handler({})
        handle_post(handler, urlsplit("/api/workspace/observations/return?extra=1"))
        handler.read_json_body.assert_not_called()
        handler.reject.assert_called_once_with(400, "invalid_request")

    def test_missing_backend_and_strict_body_errors_remain_explicit(self):
        handler = self.handler({"registration_id": "a" * 32}, observer=False)
        handle_post(handler, urlsplit("/api/workspace/observations/window-choices"))
        handler.reply.assert_called_once_with(503, {"status": "unavailable", "reason": "observer_unavailable"})
        for error in (ValueError, UnicodeError, OSError):
            handler = self.handler({})
            handler.read_json_body.side_effect = error()
            handle_post(handler, urlsplit("/api/workspace/observations/return"))
            handler.reject.assert_called_once_with(400, "invalid_request")
            handler.server.observer.request.assert_not_called()

    def test_limited_choices_remain_usable_before_generic_status_mapping(self):
        for status, choices, expected in [("available", False, 200), ("unavailable", False, 503),
                                          ("conflict", False, 409), ("unavailable", True, 200)]:
            with self.subTest(status=status, choices=choices):
                result = {"status": status, **({"choices": []} if choices else {})}
                handler = self.handler({"registration_id": "a" * 32}, result)
                handle_post(handler, urlsplit("/api/workspace/observations/window-choices"))
                handler.reply.assert_called_once_with(expected, result)
