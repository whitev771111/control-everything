"""Asynchronous native observation cache; Python remains business authority."""

import copy
import hashlib
import json
from pathlib import Path
import secrets
import threading
import time
import weakref
from contextlib import nullcontext

from control_everything.sessions import observer_protocol as p
from control_everything.sessions import observation_settings as settings
from control_everything.sessions.observer_pipe import GATE, WindowsPipe, PipeError


class ObserverClient:
    clients = weakref.WeakSet()
    clients_lock = threading.Lock()

    def __init__(self, profile_directory, *, scope_supplier=lambda: [], config_supplier=lambda: settings.EMPTY,
                 config_guard=None, gate=None, transport_factory=None):
        # Construction must not resolve paths, touch profiles, allocate I/O, or start threads.
        self.directory = Path(profile_directory)
        self.scope_supplier = scope_supplier
        self.config_supplier = config_supplier
        self.config_guard = config_guard or (lambda: nullcontext())
        self.gate = gate or GATE
        self.transport_factory = transport_factory or WindowsPipe
        self.lock = threading.RLock()
        self.closed, self.detached = threading.Event(), threading.Event()
        self.detached.set()
        self.transport = None
        self.state, self.reason = "unavailable", "not_started"
        self.generation = self.key = self.build = None
        self.revision, self.scope_hash, self.seq = 0, "", -1
        self.value = None
        self.grants, self.receipts = {}, {}
        self.selection = None
        self.pending = None
        self.next_poll = self.next_connect = self.last_list = 0
        self.backoff, self.blocked = 0.25, False
        self.started = False
        self.desired_pair = tuple(settings.pair(settings.EMPTY).values())
        self.applied_pair = None
        with self.clients_lock:
            self.clients.add(self)

    @classmethod
    def invalidate_profile(cls, directory, observations):
        with cls.clients_lock:
            clients = list(cls.clients)
        for client in clients:
            if client.directory == Path(directory) and client.started:
                client.invalidate_config(observations)

    def invalidate_config(self, observations=None):
        pair = tuple(settings.pair(observations).values()) if observations is not None else None
        with self.lock:
            self._invalidate()
            if self.reason == "config_conflict":
                self.blocked = False
            self.desired_pair = pair
            self.state, self.reason = "stale" if self.value else "unavailable", "config_pending" if pair else "config_unavailable"
            if self.pending:
                pending, self.pending = self.pending, None
                pending["response"] = {"status": "limited", "reason": self.reason}
                pending["done"].set()
            self.next_poll = 0
        self.gate.wake.set()

    def _read_config(self):
        try:
            value = settings.validate(self.config_supplier())
            pair = tuple(settings.pair(value).values())
        except Exception:
            self.invalidate_config()
            raise PipeError("config_unavailable") from None
        with self.lock:
            changed = pair != self.desired_pair
        if changed:
            self.invalidate_config(value)
        return value, pair

    def _fresh_config(self, pair):
        return (self.applied_pair == pair and self.value is not None
                and p.config_pair(self.value) == pair and self.value["status"] == "available"
                and int(time.time() * 1000) - self.value["sampled_at_ms"] <= 12000)

    def start(self):
        with self.lock:
            if self.started and not self.detached.is_set():
                return
            self.closed.clear()
            self.detached.clear()
            self.blocked = False
            self.started = True
            self.state, self.reason = "unavailable", "connecting"
        reason = self.gate.start(self)
        if reason:
            self._failed(reason)
            self.detached.set()

    def close_client(self):
        self.closed.set()
        with self.lock:
            self._invalidate()
            self.state, self.reason = "unavailable", "client_closed"
            pending, self.pending = self.pending, None
            if pending:
                pending["response"] = {"status": "unknown", "reason": "return_unknown"}
                pending["done"].set()
        if self.started and not self.detached.is_set():
            self.gate.close(self)
        self.gate.reap_retired()

    def _invalidate(self):
        self.grants.clear()
        self.selection = None
        if self.value:
            for item in self.value["instances"]:
                item["return_action"] = None

    def _failed(self, reason):
        with self.lock:
            self._invalidate()
            self.state = "stale" if self.value else "unavailable"
            self.reason = reason
            if reason in ("identity_mismatch", "protocol_mismatch", "invalid_message", "io_cleanup_pending", "scope_conflict", "config_conflict"):
                self.blocked = True
            self.next_connect = time.monotonic() + min(2, self.backoff + secrets.randbelow(51) / 1000)
            self.backoff = min(self.backoff * 2, 2)

    def _detach(self, retired=None):
        if self.transport:
            self.transport.close(retired=retired)
            self.transport = None
        self.detached.set()

    def _rpc(self, request, timeout=2):
        value = self.transport.rpc(request, timeout=timeout)
        # Injected transports must meet exactly the same byte/depth/Unicode boundary.
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        value = p.decode(encoded, 65536 if request["method"] in ("list_window_choices", "list_installation_candidates") else p.RESPONSE_MAX)
        if p.error_response(value):
            raise PipeError(value["reason"])
        return value

    def _sync_config(self):
        for attempt in range(2):
            observations, pair = self._read_config()
            if self.applied_pair == pair:
                return
            response = None
            try:
                response = self._rpc({"method": "sync_registrations", "base_revision": self.applied_pair[0],
                                      "base_hash": self.applied_pair[1], "config_revision": pair[0],
                                      "config_hash": pair[1], "observations": observations})
            except PipeError as error:
                if error.reason != "config_conflict" or attempt:
                    raise
                hello = p.hello(self._rpc({"method": "status"}), self.key, self.build)
                with self.lock:
                    self._invalidate()
                    self.applied_pair = p.config_pair(hello)
                continue
            p.exact(response, {"status", "config_revision", "config_hash"})
            p.require(response["status"] == "available" and p.config_pair(response) == pair)
            _, current = self._read_config()
            with self.lock:
                self.applied_pair = pair
            if current != pair:
                raise PipeError("config_pending")
            return

    def _sync(self):
        self._sync_config()
        roots = self.scope_supplier()
        p.require(type(roots) is list and len(roots) <= 8000)
        data = json.dumps(roots, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(data) > 8 * 1024 * 1024:
            raise PipeError("scope_budget_exceeded")
        digest = hashlib.sha256(data).hexdigest()
        if digest == self.scope_hash:
            return
        transaction = secrets.token_hex(16)
        begin = self._rpc({"method": "sync_scope_begin", "transaction_id": transaction,
                           "base_revision": self.revision, "scope_hash": digest})
        p.require(type(begin.get("unchanged")) is bool)
        if begin["unchanged"]:
            p.exact(begin, {"status", "unchanged", "scope_revision", "scope_hash"})
            p.require(begin["scope_hash"] == digest)
        else:
            p.exact(begin, {"status", "unchanged", "scope_revision"})
            p.require(begin["scope_revision"] == self.revision)
            deadline = time.monotonic() + 5
            chunk, index = [], 0
            def send():
                nonlocal index, chunk
                if self.closed.is_set() or time.monotonic() >= deadline:
                    raise PipeError("scope_expired")
                ack = self._rpc({"method": "sync_scope_chunk", "transaction_id": transaction, "index": index, "roots": chunk})
                p.exact(ack, {"status", "index"})
                p.require(ack == {"status": "available", "index": index})
                index, chunk = index + 1, []
            for root in roots:
                p.exact(root, {"directory_id", "path"})
                p.token(root["directory_id"])
                p.require(type(root["path"]) is str and 0 < len(root["path"]) <= 2048 and "\0" not in root["path"])
                candidate = {"method": "sync_scope_chunk", "transaction_id": transaction, "index": index, "roots": chunk + [root]}
                try:
                    p.encode(candidate)
                except p.ProtocolError:
                    if not chunk:
                        raise
                    send()
                chunk.append(root)
            if chunk:
                send()
            if time.monotonic() >= deadline:
                raise PipeError("scope_expired")
            begin = self._rpc({"method": "sync_scope_commit", "transaction_id": transaction})
            p.exact(begin, {"status", "scope_revision", "scope_hash"})
            p.require(begin["scope_hash"] == digest and begin["scope_revision"] == self.revision + 1)
        p.integer(begin["scope_revision"])
        with self.lock:
            self._invalidate()
            self.revision, self.scope_hash = begin["scope_revision"], digest

    def _step(self, gate):
        if self.blocked or self.closed.is_set():
            return
        now = time.monotonic()
        try:
            if self.transport is None:
                if now < self.next_connect:
                    return
                self.transport = self.transport_factory(self, gate)
                self.transport.connect()
                self.key, self.build = self.transport.key, self.transport.build
                hello = p.hello(self._rpc({"method": "hello", "protocol": p.VERSION, "profile_key": self.key,
                                          "role": "python", "build": self.build}), self.key, self.build)
                with self.lock:
                    self._invalidate()
                    self.generation, self.seq = hello["generation"], -1
                    self.revision, self.scope_hash = hello["scope_revision"], hello["scope_hash"]
                    self.applied_pair = p.config_pair(hello)
                self.next_poll = 0
            with self.lock:
                request, self.pending = self.pending, None
            if request:
                self._execute(request)
                return
            if now < self.next_poll:
                return
            self._sync()
            value = p.snapshot(self._rpc({"method": "get_snapshot"}), self.key, self.generation, self.revision, self.applied_pair)
            _, current_pair = self._read_config()
            if current_pair != self.applied_pair:
                raise PipeError("config_pending")
            with self.lock:
                if value["seq"] >= self.seq:
                    self.seq, self.value = value["seq"], value
                    self.state, self.reason = "available", None
                    self.backoff = 0.25
                    self._prune()
                    live = {item["instance_id"] for item in value["instances"] if item["status"] == "available" and value["status"] == "available"}
                    self.grants = {key: grant for key, grant in self.grants.items()
                                   if grant["kind"] == "manual_navigation" or grant["instance_id"] in live}
                    for item in value["instances"]:
                        action = item["return_action"]
                        if action and action.get("action_id") and item["instance_id"] in live:
                            self._remember(action, instance_id=item["instance_id"])
                elif value["seq"] < self.seq:
                    raise p.ProtocolError("invalid_message")
            self.next_poll = time.monotonic() + 1
        except Exception as error:
            reason = getattr(error, "reason", None) or (str(error) if isinstance(error, p.ProtocolError) else "observer_unavailable")
            self._failed(reason)
            if self.transport and gate.state != "retired":
                self.transport.close()
                self.transport = None

    def _prune(self):
        now, wall = time.monotonic(), int(time.time() * 1000)
        self.grants = {key: item for key, item in self.grants.items() if item["deadline"] > now and item["expires_at_ms"] > wall}
        self.receipts = {key: item for key, item in self.receipts.items() if item[0] > now}
        if self.selection and self.selection[1] <= now:
            self._dismiss_local(self.selection[0])

    def _remember(self, action, *, instance_id=None, selection_id=None):
        key = action["action_id"]
        if key in self.grants or key in self.receipts or len(self.grants) >= 64:
            return
        remaining = max(0, min(10, (action["expires_at_ms"] - time.time() * 1000) / 1000))
        self.grants[key] = {name: action[name] for name in p.GRANT_FIELDS}
        self.grants[key].update(deadline=time.monotonic() + remaining, instance_id=instance_id,
                                selection_id=selection_id, generation=self.generation, revision=self.revision,
                                config_pair=self.applied_pair)

    def cached(self):
        self.gate.reap_retired()
        try:
            self._read_config()
        except PipeError:
            pass
        with self.lock:
            self._prune()
            value = copy.deepcopy(self.value)
            state, reason = self.state, self.reason
            if value and int(time.time() * 1000) - value["sampled_at_ms"] > 12000:
                state, reason = "stale", "sample_timeout"
            if value:
                for item in value["instances"]:
                    action = item["return_action"]
                    if state != "available" or not action or action.get("action_id") not in self.grants:
                        item["return_action"] = None
            return {"status": state, "reason": reason, "snapshot": value,
                    "scope_revision": self.revision, "scope_hash": self.scope_hash,
                    "config_pair": self.desired_pair, "applied_pair": self.applied_pair}

    def _dismiss_local(self, selection):
        self.grants = {key: item for key, item in self.grants.items() if item["selection_id"] != selection}
        if self.selection and self.selection[0] == selection:
            self.selection = None

    def request(self, method, body):
        try:
            _, pair = self._read_config()
        except PipeError:
            return {"status": "unavailable", "reason": "config_unavailable"}
        timeout = 1 if method in ("list_window_choices", "list_installation_candidates") else 2
        p.encode({"method": method, **body})
        with self.lock:
            self._prune()
            if method == "return_window" and body["action_id"] in self.receipts:
                return copy.deepcopy(self.receipts[body["action_id"]][1])
            if (method in ("list_window_choices", "return_window") and self.reason == "config_pending"
                    and self.transport is not None and not self.closed.is_set()):
                return {"status": "limited", "reason": "config_pending"}
            if self.state != "available" or self.closed.is_set() or self.transport is None:
                return {"status": "unavailable", "reason": self.reason or "observer_unavailable"}
            if method in ("list_window_choices", "return_window") and not self._fresh_config(pair):
                return {"status": "limited", "reason": "config_pending"}
            if method == "return_window":
                key = body["action_id"]
                if key in self.receipts:
                    return copy.deepcopy(self.receipts[key][1])
                grant = self.grants.get(key)
                if (not grant or body["generation"] != self.generation or grant["generation"] != self.generation
                        or body["seq"] != grant["issued_seq"] or grant["revision"] != self.revision or grant["config_pair"] != pair):
                    return {"status": "limited", "reason": "action_expired"}
                if grant["kind"] == "observed_instance" and (not self.value or self.value["status"] != "available"
                        or int(time.time() * 1000) - self.value["sampled_at_ms"] > 12000):
                    return {"status": "limited", "reason": "action_expired"}
                if len(self.receipts) >= 64:
                    return {"status": "limited", "reason": "action_busy"}
            if method == "list_window_choices":
                if time.monotonic() - self.last_list < 1:
                    return {"status": "limited", "reason": "action_busy"}
                self.last_list = time.monotonic()
            if method == "dismiss_window_choices":
                if body["generation"] != self.generation:
                    return {"status": "limited", "reason": "action_expired"}
                self._dismiss_local(body["selection_id"])
            if self.pending is not None:
                return {"status": "limited", "reason": "action_busy"}
            request = {"method": method, "body": body, "done": threading.Event(), "abandoned": False,
                       "response": None, "deadline": time.monotonic() + timeout, "config_pair": pair}
            if method == "return_window":
                request["kind"] = grant["kind"]
                request["grant_expires_at_ms"] = grant["expires_at_ms"]
                request["scope_revision"] = grant["revision"]
                result = {"status": "unknown", "reason": "return_unknown", "kind": grant["kind"]}
                self.receipts[key] = (time.monotonic() + 30, result)
                del self.grants[key]
            self.pending = request
            self.gate.wake.set()
        if not request["done"].wait(timeout):
            with self.lock:
                request["abandoned"] = True
                return {"status": "unknown", "reason": "return_unknown" if method == "return_window" else "pipe_timeout"}
        response, request["response"] = request["response"], None
        return response

    def _execute(self, request):
        method, body = request["method"], request["body"]
        try:
            if request["abandoned"] or time.monotonic() >= request["deadline"]:
                request["response"] = {"status": "unknown", "reason": "return_unknown" if method == "return_window" else "pipe_timeout"}
                return
            with self.config_guard():
                _, pair = self._read_config()
                with self.lock:
                    if (self.closed.is_set() or request["config_pair"] != pair
                            or method in ("list_window_choices", "return_window") and not self._fresh_config(pair)):
                        request["response"] = {"status": "limited", "reason": "config_pending"}
                        return
                    if method == "return_window" and (body["generation"] != self.generation
                            or request["scope_revision"] != self.revision or int(time.time() * 1000) >= request["grant_expires_at_ms"]):
                        request["response"] = {"status": "limited", "reason": "action_expired"}
                        self.receipts[body["action_id"]] = (time.monotonic() + 30, request["response"])
                        return
                wire_body = {key: value for key, value in body.items() if key != "generation" or method == "return_window"}
                if method in ("list_window_choices", "return_window"):
                    wire_body.update(config_revision=pair[0], config_hash=pair[1])
                value = self._rpc({"method": method, **wire_body}, max(0.01, request["deadline"] - time.monotonic()))
                with self.lock:
                    if method == "list_window_choices":
                        value = p.choices(value, self.generation, pair)
                        if not request["abandoned"]:
                            selection = value["selection_id"]
                            self.selection = selection, time.monotonic() + max(0, min(10, (value["expires_at_ms"] - time.time() * 1000) / 1000))
                            for item in value["choices"]:
                                self._remember(item, selection_id=selection)
                    elif method == "list_installation_candidates":
                        value = p.candidates(value)
                    elif method == "return_window":
                        p.exact(value, {"status", "reason", "kind"})
                        p.require(value["status"] in ("available", "limited") and value["kind"] == request["kind"])
                        p.code(value["reason"])
                        if not request["abandoned"]:
                            self.receipts[body["action_id"]] = (time.monotonic() + 30, value)
                        if value["kind"] == "manual_navigation" and self.selection:
                            self._dismiss_local(self.selection[0])
                    else:
                        p.require(value == {"status": "available"})
                    if not request["abandoned"]:
                        request["response"] = value
        except Exception as error:
            reason = getattr(error, "reason", None) or "invalid_message"
            if reason in ("invalid_config", "storage_error", "config_busy"):
                self.invalidate_config()
                if reason != "config_busy":
                    reason = "config_unavailable"
            if reason in ("action_expired", "action_busy", "action_in_progress", "unsupported_tool", "unsupported_installation",
                          "config_pending", "config_conflict", "config_unavailable", "config_busy", "registration_disabled", "rebind_required", "entry_unsupported"):
                request["response"] = {"status": "limited", "reason": reason}
                if method == "return_window" and not request["abandoned"]:
                    with self.lock:
                        self.receipts[body["action_id"]] = (time.monotonic() + 30, request["response"])
                return
            self._failed(reason)
            if self.transport and self.gate.state != "retired":
                self.transport.close()
                self.transport = None
            request["response"] = {"status": "unknown" if method == "return_window" else "unavailable",
                                   "reason": "return_unknown" if method == "return_window" else reason}
        finally:
            request["done"].set()
