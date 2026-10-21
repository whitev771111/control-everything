"""Bounded, token-authenticated client for the local Agent HTTP contract."""

import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID

MAX_REVISION = 9_007_199_254_740_991
MAX_RESPONSE = 2 * 1024 * 1024
REASONS = frozenset({"agent_unauthorized", "task_not_found", "run_not_found", "not_assigned", "project_forbidden",
                     "run_conflict", "run_mismatch", "item_changed", "review_not_ready", "review_already_submitted",
                     "invalid_agent_request", "invalid_request", "route_not_found", "store_busy",
                     "host_rejected", "origin_rejected"})


class AgentClientError(Exception):
    def __init__(self, reason, status=None):
        self.reason, self.status = reason, status
        super().__init__(reason)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def _identity(value):
    try:
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise AgentClientError("invalid_task_id") from None
    return value


def _revision(value):
    if type(value) is not int or not 1 <= value <= MAX_REVISION:
        raise AgentClientError("invalid_revision")
    return value


def _run_identity(value):
    try:
        return _identity(value)
    except AgentClientError:
        raise AgentClientError("invalid_run_id") from None


def _text(value, maximum, required=True):
    if type(value) is not str or len(value) > maximum or "\0" in value or required and not value.strip():
        raise AgentClientError("invalid_agent_request")
    return value


class AgentClient:
    def __init__(self, base_url, token, timeout=10):
        try:
            parts = urlsplit(base_url)
            valid = (parts.scheme == "http" and parts.hostname in {"127.0.0.1", "::1"}
                     and parts.port is not None and 1 <= parts.port <= 65535
                     and parts.path in {"", "/"} and not parts.query and not parts.fragment
                     and parts.username is None and parts.password is None
                     and type(base_url) is str and not any(ord(c) <= 32 for c in base_url))
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise AgentClientError("invalid_workbench_url")
        if type(token) is not str or not token or len(token) > 4096 or any(not 33 <= ord(c) <= 126 for c in token):
            raise AgentClientError("missing_or_invalid_token")
        if type(timeout) not in (int, float) or not 0 < timeout <= 30:
            raise AgentClientError("invalid_timeout")
        self._base_url, self._token, self._timeout = base_url.rstrip("/"), token, timeout
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    @classmethod
    def from_env(cls, environ=None):
        environ = os.environ if environ is None else environ
        return cls(environ.get("WORKBENCH_URL"), environ.get("WORKBENCH_TOKEN"))

    def _request(self, path, body=None):
        try:
            encoded = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, UnicodeError, TypeError):
            raise AgentClientError("invalid_agent_request") from None
        if encoded is not None and len(encoded) > 16384:
            raise AgentClientError("request_too_large")
        request = Request(self._base_url + path, data=encoded, headers={
            "Authorization": "Bearer " + self._token, "Content-Type": "application/json"})
        try:
            try:
                response = self._opener.open(request, timeout=self._timeout)
            except HTTPError as error:
                response = error
            with response:
                status = response.code
                raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise AgentClientError("response_too_large", status)
                try:
                    value = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise AgentClientError("invalid_response", status) from None
                if not isinstance(value, dict):
                    raise AgentClientError("invalid_response", status)
                if not 200 <= status < 300:
                    reason = value.get("reason")
                    raise AgentClientError(reason if type(reason) is str and reason in REASONS else "http_error", status)
                return value
        except (URLError, TimeoutError, OSError):
            # Never include exception strings: they can contain URLs or credentials.
            raise AgentClientError("workbench_unavailable") from None

    def list_my_tasks(self, state=None):
        if state is not None and (type(state) is not str or state not in {"active", "todo", "doing", "blocked", "review", "done"}):
            raise AgentClientError("invalid_agent_request")
        return self._request("/api/agent/tasks" + ("?" + urlencode({"state": state}) if state else ""))

    def get_task(self, task_id):
        return self._request("/api/agent/tasks/" + _identity(task_id))

    def get_review(self, task_id):
        return self._request("/api/agent/tasks/" + _identity(task_id) + "/review")

    def claim_task(self, task_id, expected_revision):
        return self._request("/api/agent/tasks/" + _identity(task_id) + "/claim", {"expected_revision": _revision(expected_revision)})

    def report_progress(self, task_id, expected_revision, stage, percent=None, message=None,
                        wait_kind=None, request_id=None, agent_hint=None, *, run_id=None):
        run_id = _run_identity(run_id)
        if type(stage) is not str or stage not in {"analyzing", "implementing", "testing", "blocked", "waiting_user"}:
            raise AgentClientError("invalid_agent_request")
        if percent is not None and (type(percent) is not int or not 0 <= percent <= 100):
            raise AgentClientError("invalid_agent_request")
        body = {"expected_revision": _revision(expected_revision), "run_id": run_id, "stage": stage, "percent": percent}
        if message is not None:
            body["message"] = _text(message, 1000, required=stage == "waiting_user")
        if stage == "waiting_user":
            if message is None:
                raise AgentClientError("invalid_agent_request")
            wait_kind = "unknown" if wait_kind is None else wait_kind
            if type(wait_kind) is not str or wait_kind not in {"tool_authorization", "user_input", "external_approval", "unknown"}:
                raise AgentClientError("invalid_agent_request")
            body["wait_kind"] = wait_kind
            if request_id is not None:
                body["request_id"] = _text(request_id, 200, required=False)
            if agent_hint is not None:
                body["agent_hint"] = _text(agent_hint, 500, required=False)
        elif wait_kind is not None or request_id is not None or agent_hint is not None:
            raise AgentClientError("invalid_agent_request")
        return self._request("/api/agent/tasks/" + _identity(task_id) + "/progress", body)

    def submit_for_review(self, task_id, expected_revision, summary, evidence=None, *, run_id=None):
        run_id = _run_identity(run_id)
        evidence = [] if evidence is None else evidence
        if type(evidence) is not list or len(evidence) > 20:
            raise AgentClientError("invalid_agent_request")
        body = {"expected_revision": _revision(expected_revision), "run_id": run_id, "summary": _text(summary, 4000),
                "evidence": [_text(entry, 2048) for entry in evidence]}
        return self._request("/api/agent/tasks/" + _identity(task_id) + "/submit-review", body)
