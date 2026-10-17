"""Observation action handlers called after gateway authorization."""

from session_panel.sessions import observer_protocol


def handle_post(handler, path):
    try:
        if path.query:
            raise ValueError("invalid_request")
        body = handler.read_json_body(max_bytes=4096, strict=True)
        if path.path.endswith("/return"):
            observer_protocol.exact(body, {"action_id", "generation", "seq"})
            observer_protocol.token(body["action_id"])
            observer_protocol.token(body["generation"])
            observer_protocol.integer(body["seq"])
            method = "return_window"
        elif path.path.endswith("/close"):
            observer_protocol.exact(body, {"selection_id", "generation"})
            observer_protocol.token(body["selection_id"])
            observer_protocol.token(body["generation"])
            method = "dismiss_window_choices"
        else:
            observer_protocol.exact(body, {"registration_id"})
            observer_protocol.registration_id(body["registration_id"])
            method = "list_window_choices"
    except (ValueError, UnicodeError, OSError):
        return handler.reject(400, "invalid_request")
    observer = handler.server.__dict__.get("observer")
    if observer is None:
        return handler.reply(503, {"status": "unavailable", "reason": "observer_unavailable"})
    value = observer.request(method, body)
    status = 200 if value["status"] == "available" else 503 if value["status"] == "unavailable" else 409
    # A limited choice list remains a usable, explicitly bounded response.
    if method == "list_window_choices" and "choices" in value:
        status = 200
    handler.reply(status, value)
