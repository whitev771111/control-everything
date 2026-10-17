"""No native daemon is started: injected transport and Win32 completion evidence."""

import copy
import ctypes as C
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from session_panel.sessions.observer_client import ObserverClient
from session_panel.sessions.observer_pipe import IoGate, Overlapped, CleanupPending, Arena, PipeError, WindowsPipe
from tests.backend.test_observer_protocol import sample
from session_panel.sessions import observer_protocol as p
from session_panel.profiles import observation_settings as settings
from session_panel.profiles.profile import Profile, ProfileError
from tests.backend.test_observation_settings import registration, save


def until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("bounded condition did not become true")


class FakeTransport:
    def __init__(self, client, gate):
        self.client, self.gate = client, gate
        self.key, self.build = "a" * 64, "build"
        self.revision, self.digest, self.seq = 0, "", 0
        self.calls, self.closed = [], False
        self.value = sample()
        self.expiry = int(time.time() * 1000) + 10000
        self.config = settings.pair(settings.EMPTY)

    def connect(self):
        pass

    def close(self, retired=None):
        self.closed = True

    def rpc(self, request, timeout=2):
        method = request["method"]
        self.calls.append(copy.deepcopy(request))
        if method in ("hello", "status"):
            return {"status": "available", "protocol": p.VERSION, "profile_key": self.key, "generation": "generation",
                    "build": self.build, "scope_revision": self.revision, "scope_hash": self.digest, **self.config}
        if method == "sync_registrations":
            self.config = {key: request[key] for key in ("config_revision", "config_hash")}
            return {"status": "available", **self.config}
        if method == "sync_scope_begin":
            self.digest = request["scope_hash"]
            return {"status": "available", "unchanged": False, "scope_revision": self.revision}
        if method == "sync_scope_chunk":
            return {"status": "available", "index": request["index"]}
        if method == "sync_scope_commit":
            self.revision += 1
            return {"status": "available", "scope_revision": self.revision, "scope_hash": self.digest}
        if method == "get_snapshot":
            self.seq += 1
            value = copy.deepcopy(self.value)
            value.update(seq=self.seq, scope_revision=self.revision, **self.config)
            value["instances"][0]["return_action"]["expires_at_ms"] = self.expiry
            return value
        if method == "list_window_choices":
            action = copy.deepcopy(self.value["instances"][0]["return_action"])
            action.update(action_id="manual", kind="manual_navigation", scope="application_window", title="<synthetic>")
            return {"status": "available", "reason": None, "generation": "generation", "selection_id": "selection",
                    "expires_at_ms": self.expiry, "coverage": {"status": "complete", "omitted": 0}, "choices": [action], **self.config}
        if method == "return_window":
            return {"status": "available", "reason": None, "kind": "manual_navigation" if request["action_id"] == "manual" else "observed_instance"}
        return {"status": "available"}


class RegistrationClientTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.profile = Profile.open(str(Path(temporary.name) / "profile"))
        self.gate = SimpleNamespace(state="idle", wake=threading.Event(), reap_retired=lambda: None)

    def client(self):
        value = ObserverClient(self.profile.data_dir, gate=self.gate, transport_factory=FakeTransport,
                               config_supplier=self.profile.observation_config, config_guard=self.profile.observation_guard)
        value._step(self.gate)
        self.assertEqual(value.state, "available")
        return value

    def execute(self, client, method="list_window_choices", **changes):
        request = {"method": method, "body": {"registration_id": "a" * 32}, "done": threading.Event(),
                   "abandoned": False, "deadline": time.monotonic() + 1, "config_pair": client.desired_pair}
        request.update(changes)
        client._execute(request)
        self.assertTrue(request["done"].is_set())
        return request["response"]

    def test_second_client_and_external_file_change_immediately_revoke_old_trust(self):
        save(self.profile, [registration()])
        first, second = self.client(), self.client()
        self.assertTrue(first.grants and second.grants)
        # An external writer changes the content without increasing revision.
        config = self.profile.config()
        config["observations"]["registrations"][0]["enabled"] = False
        self.profile._write_config(config)
        for client in (first, second):
            cached = client.cached()
            self.assertEqual(cached["reason"], "config_pending")
            self.assertFalse(client.grants)
            self.assertIsNone(cached["snapshot"]["instances"][0]["return_action"])
            self.assertNotEqual(cached["config_pair"], cached["applied_pair"])

    def test_local_commit_broadcast_clears_queued_picker_without_waiting_for_poll(self):
        client = self.client()
        client.started = True
        request = {"done": threading.Event()}
        client.pending = request
        save(self.profile, [registration()], on_commit=lambda value: ObserverClient.invalidate_profile(self.profile.data_dir, value))
        self.assertTrue(request["done"].is_set())
        self.assertEqual(request["response"]["reason"], "config_pending")
        self.assertIsNone(client.pending)
        self.assertFalse(client.grants)

    def test_save_revokes_old_return_and_picker_as_http409_without_rpc_but_missing_backend_is503(self):
        from session_panel.http.gateway import PanelHandler
        for rows in ([registration(enabled=False)], []):
            with self.subTest(rows=rows):
                save(self.profile, [registration()])
                client = self.client()
                client.started = True
                choice = self.execute(client)["choices"][0]
                self.assertIn(choice["action_id"], client.grants)
                save(self.profile, rows, on_commit=lambda value: ObserverClient.invalidate_profile(self.profile.data_dir, value))
                before = len(client.transport.calls)
                for endpoint, body in (("return", {"action_id": choice["action_id"], "generation": client.generation, "seq": choice["issued_seq"]}),
                                       ("window-choices", {"registration_id": "a" * 32})):
                    handler = object.__new__(PanelHandler)
                    handler.server = SimpleNamespace(observer=client)
                    handler.read_json_body = Mock(return_value=body)
                    handler.reply, handler.reject = Mock(), Mock()
                    handler.observation_post(urlsplit("/api/workspace/observations/" + endpoint))
                    handler.reply.assert_called_once_with(409, {"status": "limited", "reason": "config_pending"})
                self.assertEqual(len(client.transport.calls), before)
                self.assertFalse(client.grants)
                client.transport = None
                client.state, client.reason = "unavailable", "native_missing"
                handler = object.__new__(PanelHandler)
                handler.server = SimpleNamespace(observer=client)
                handler.read_json_body = Mock(return_value={"registration_id": "a" * 32})
                handler.reply, handler.reject = Mock(), Mock()
                handler.observation_post(urlsplit("/api/workspace/observations/window-choices"))
                handler.reply.assert_called_once_with(503, {"status": "unavailable", "reason": "native_missing"})

    def test_config_changed_after_queue_never_sends_picker_or_return(self):
        for method in ("list_window_choices", "return_window"):
            client = self.client()
            pair = client.desired_pair
            save(self.profile, [registration(label=method)])
            calls = len(client.transport.calls)
            value = self.execute(client, method, config_pair=pair)
            self.assertEqual(value["reason"], "config_pending")
            self.assertEqual(len(client.transport.calls), calls)

    def test_ack_does_not_accept_old_sample_and_change_during_ack_discards_it(self):
        client = self.client()
        transport = client.transport
        save(self.profile, [registration()])
        original = transport.rpc
        def changed(request, timeout=2):
            result = original(request, timeout)
            if request["method"] == "sync_registrations":
                save(self.profile, [registration(enabled=False)])
            return result
        transport.rpc = changed
        client.next_poll = 0
        client._step(self.gate)
        self.assertEqual(client.reason, "config_pending")
        self.assertFalse(client.grants)
        self.assertNotEqual(client.desired_pair, client.applied_pair)
        old = copy.deepcopy(client.value)
        old.update(config_revision=client.applied_pair[0], config_hash=client.applied_pair[1])
        client.value = old
        self.assertFalse(client._fresh_config(client.desired_pair))

    def test_action_guard_blocks_competing_writer_and_receipt_never_replays(self):
        save(self.profile, [registration()])
        client = self.client()
        original = client.transport.rpc
        blocked = []
        def guarded(request, timeout=2):
            if request["method"] == "list_window_choices":
                competitor = Profile(self.profile.data_dir)
                try:
                    save(competitor, [])
                except ProfileError as error:
                    blocked.append(error.reason)
            return original(request, timeout)
        client.transport.rpc = guarded
        self.assertEqual(self.execute(client)["status"], "available")
        self.assertEqual(blocked, ["config_busy"])
        self.assertEqual(self.profile.observation_config()["revision"], 1)

    def test_read_failure_and_guard_busy_fail_closed_without_io(self):
        client = self.client()
        calls = len(client.transport.calls)
        with patch.object(self.profile, "config", side_effect=ProfileError("invalid_config")):
            self.assertEqual(client.cached()["reason"], "config_unavailable")
            self.assertEqual(client.request("list_window_choices", {"registration_id": "a" * 32})["reason"], "config_unavailable")
        self.assertFalse(client.grants)
        self.assertEqual(len(client.transport.calls), calls)
        client = self.client()
        with patch.object(client, "config_guard", side_effect=ProfileError("config_busy")):
            self.assertEqual(self.execute(client)["reason"], "config_busy")

    def test_config_conflict_reconciles_once_and_never_overwrites_repeated_conflict(self):
        client = self.client()
        save(self.profile, [registration()])
        calls = []
        original = client.transport.rpc
        def conflict(request, timeout=2):
            if request["method"] == "sync_registrations":
                calls.append(request)
                return {"status": "unavailable", "reason": "config_conflict"}
            return original(request, timeout)
        client.transport.rpc = conflict
        client.next_poll = 0
        client._step(self.gate)
        self.assertEqual(len(calls), 2)
        self.assertEqual(client.reason, "config_conflict")
        self.assertFalse(client.grants)


class FakeArena:
    def __init__(self, mode="pending"):
        self.storage = C.create_string_buffer(1024 * 1024)
        self.address = C.addressof(self.storage)
        self.buffer = self.address + 64
        self.overlapped = Overlapped.from_address(self.address)
        self.event, self.pipe, self.operation_id = 7, None, 0
        self.api = self
        self.mode, self.frees, self.issues, self.cancels = mode, 0, 0, 0
        self.late = False

    def ResetEvent(self, _event):
        return 1

    def ReadFile(self, *args):
        self.issues += 1
        C.set_last_error(997)
        return 0

    WriteFile = ReadFile

    def completion(self):
        if self.late:
            return "terminal", 995
        if self.mode == "abort" and self.cancels:
            return "terminal", 995
        return "pending", 996

    def CancelIoEx(self, *_args):
        self.cancels += 1
        C.set_last_error(1168)
        return 0

    def WaitForSingleObject(self, *_args):
        time.sleep(0.002)
        return 258

    def dispose(self):
        self.frees += 1
        if self.frees > 1:
            raise AssertionError("double free")


class OperationClient:
    def __init__(self, *, issue=True):
        self.closed, self.detached = threading.Event(), threading.Event()
        self.reason, self.issue, self.once = None, issue, False

    def _step(self, gate):
        if not self.once and self.issue:
            self.once = True
            gate.operation(self, 3, size=4, deadline=time.monotonic() + 5)

    def _failed(self, reason):
        self.reason = reason

    def _detach(self, retired=None):
        self.detached.set()


class ObserverClientTests(unittest.TestCase):
    def client(self):
        gate = IoGate()
        client = ObserverClient("C:/temporary/profile", gate=gate, transport_factory=FakeTransport)
        self.addCleanup(client.close_client)
        self.addCleanup(lambda: gate.worker_done.wait(2))
        return client, gate

    def test_constructor_has_no_io_or_thread_and_start_is_explicit(self):
        with patch("threading.Thread", side_effect=AssertionError("thread")), patch("pathlib.Path.resolve", side_effect=AssertionError("filesystem")):
            client = ObserverClient("C:/temporary/profile")
            self.assertEqual(client.cached()["reason"], "not_started")

    def test_scope_ack_grant_stability_old_issued_seq_and_duplicate_receipt(self):
        client, gate = self.client(); client.start()
        until(lambda: client.state == "available")
        transport = client.transport
        deadline = client.grants["action"]["deadline"]
        client.next_poll = 0; gate.wake.set()
        until(lambda: client.seq >= 2)
        self.assertEqual(client.grants["action"]["deadline"], deadline)
        body = {"action_id": "action", "generation": "generation", "seq": 1}
        self.assertEqual(client.request("return_window", body)["status"], "available")
        self.assertEqual(client.request("return_window", body)["status"], "available")
        self.assertEqual(sum(item["method"] == "return_window" for item in transport.calls), 1)
        client.close_client(); self.assertTrue(gate.worker_done.wait(2))
        self.assertTrue(transport.closed)
        self.assertFalse(any(item["method"] == "stop" for item in transport.calls))

    def test_manual_permission_has_no_title_dismisses_without_instance(self):
        client, _ = self.client(); client.start(); until(lambda: client.state == "available")
        choices = client.request("list_window_choices", {"registration_id": "a" * 32})
        self.assertEqual(choices["choices"][0]["title"], "<synthetic>")
        self.assertNotIn("title", repr(client.grants))
        self.assertEqual(client.grants["manual"]["kind"], "manual_navigation")
        self.assertIsNone(client.grants["manual"]["instance_id"])
        body = {"selection_id": "selection", "generation": "generation"}
        self.assertEqual(client.request("dismiss_window_choices", body), {"status": "available"})
        self.assertNotIn("manual", client.grants)
        self.assertEqual(client.request("return_window", {"action_id": "manual", "generation": "generation", "seq": 1})["reason"], "action_expired")

    def test_invalid_snapshot_blocks_without_spawning_or_replaying(self):
        client, gate = self.client(); client.start(); until(lambda: client.state == "available")
        transport = client.transport
        transport.value["instances"][0]["title"] = "synthetic private marker"
        client.next_poll = 0; gate.wake.set()
        until(lambda: client.blocked)
        self.assertFalse(client.grants)
        self.assertNotIn("synthetic private marker", repr(client.cached()))
        self.assertEqual(client.reason, "invalid_message")

    def test_two_clients_pending_retirement_worker_exit_and_late_reap_once(self):
        arena = FakeArena()
        gate = IoGate(arena_factory=lambda: arena)
        first, second = OperationClient(), OperationClient()
        gate.start(first); until(lambda: arena.issues == 1)
        gate.start(second)
        unrelated_start = time.monotonic(); gate.close(second)
        self.assertLess(time.monotonic() - unrelated_start, 0.1)
        start = time.monotonic(); gate.close(first); gate.close(second)
        self.assertLess(time.monotonic() - start, 1.6)
        self.assertTrue(gate.worker_done.wait(1))
        self.assertIsNone(gate.worker)
        self.assertEqual(arena.issues, 1)
        self.assertIs(gate.retired, arena)
        self.assertEqual(arena.frees, 0)
        self.assertEqual(gate.start(OperationClient()), "io_cleanup_pending")
        self.assertFalse(gate.reap_retired())
        arena.late = True
        results = []
        threads = [threading.Thread(target=lambda: results.append(gate.reap_retired())) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(arena.frees, 1)

    def test_terminal_cancel_reuses_slot_and_last_close_exits_worker(self):
        arena = FakeArena("abort")
        gate = IoGate(arena_factory=lambda: arena)
        first, second = OperationClient(), OperationClient(issue=False)
        gate.start(first); until(lambda: arena.issues == 1); gate.start(second)
        gate.close(first)
        self.assertIsNone(gate.retired)
        self.assertEqual(arena.frees, 0)
        second.issue = True; arena.cancels = 0; gate.wake.set()
        until(lambda: arena.issues == 2)
        gate.close(second)
        self.assertTrue(gate.worker_done.wait(1))
        self.assertEqual(arena.frees, 1)

    def test_retired_record_cannot_be_reaped_before_worker_detaches_transports(self):
        arena = FakeArena()
        attempted = []
        class HandoffGate(IoGate):
            def operation(self, *args, **kwargs):
                try:
                    return super().operation(*args, **kwargs)
                except CleanupPending:
                    arena.late = True
                    attempted.append(self.reap_retired())
                    self.assert_not_disposed = arena.frees == 0
                    raise
        gate = HandoffGate(arena_factory=lambda: arena)
        first, second = OperationClient(), OperationClient(issue=False)
        gate.start(first); until(lambda: arena.issues == 1); gate.start(second)
        gate.close(first)
        self.assertTrue(gate.worker_done.wait(1))
        self.assertEqual(attempted, [False])
        self.assertTrue(gate.assert_not_disposed)
        self.assertTrue(first.detached.is_set())
        self.assertTrue(second.detached.is_set())
        # close() may already reap after the worker's ownership handoff.
        gate.reap_retired()
        self.assertEqual(arena.frees, 1)

    def test_invalid_handle_and_parameter_never_count_as_terminal_completion(self):
        arena = object.__new__(Arena)
        arena.pipe, arena.overlapped = 3, Overlapped()
        for error in (6, 87, 996, 1168):
            class Api:
                def GetOverlappedResult(self, *_args):
                    C.set_last_error(error)
                    return 0
            arena.api = Api()
            self.assertEqual(arena.completion()[0], "pending")

    def test_immediate_reopen_registers_while_previous_worker_has_no_clients(self):
        gate = IoGate()
        reached, resume = threading.Event(), threading.Event()
        wake = gate.wake
        class PausingWake:
            def set(self): wake.set()
            def clear(self): wake.clear()
            def wait(self, timeout):
                with gate.lock:
                    empty = not gate.clients
                if empty and not reached.is_set():
                    reached.set(); resume.wait(1)
                return wake.wait(timeout)
        gate.wake = PausingWake()
        first = OperationClient(issue=False)
        self.assertIsNone(gate.start(first))
        gate.close(first)
        self.assertTrue(reached.wait(1))
        second = OperationClient(issue=False)
        try:
            self.assertIsNone(gate.start(second))
        finally:
            resume.set()
            gate.close(second)
            self.assertTrue(gate.worker_done.wait(2))

    def test_scope_is_chunked_with_exact_ack_and_conflict_revokes_actions(self):
        client, gate = self.client()
        roots = [{"directory_id": f"directory-{index}", "path": "C:/example/" + str(index) + "/" + "界" * 500} for index in range(100)]
        client.scope_supplier = lambda: roots
        client.start(); until(lambda: client.state == "available")
        transport = client.transport
        chunks = [item for item in transport.calls if item["method"] == "sync_scope_chunk"]
        self.assertGreater(len(chunks), 1)
        self.assertEqual([item for chunk in chunks for item in chunk["roots"]], roots)
        for chunk in chunks:
            self.assertLessEqual(len(json_bytes(chunk)), 65536)
        rpc = transport.rpc
        def conflict(request, timeout=2):
            if request["method"] == "sync_scope_begin":
                return {"status": "unavailable", "reason": "scope_conflict"}
            return rpc(request, timeout)
        transport.rpc = conflict
        roots.append({"directory_id": "new-directory", "path": "C:/example/new"})
        client.next_poll = 0; gate.wake.set(); until(lambda: client.blocked)
        self.assertEqual(client.reason, "scope_conflict")
        self.assertFalse(client.grants)

    def test_client_and_grant_capacity_are_bounded_without_evicting_live_permissions(self):
        gate = IoGate()
        clients = [OperationClient(issue=False) for _ in range(17)]
        try:
            for client in clients[:16]:
                self.assertIsNone(gate.start(client))
            self.assertEqual(gate.start(clients[16]), "client_limit")
            self.assertIsNone(gate.arena)
        finally:
            for client in clients[:16]: gate.close(client)
            self.assertTrue(gate.worker_done.wait(2))
        client, _ = self.client()
        original = sample()["instances"][0]["return_action"]
        for index in range(65):
            action = {**original, "action_id": f"action-{index}"}
            client._remember(action, instance_id="instance")
        self.assertEqual(len(client.grants), 64)
        self.assertIn("action-0", client.grants)
        self.assertNotIn("action-64", client.grants)

    def test_lost_return_reply_never_replays_after_reconnect(self):
        client, gate = self.client(); client.start(); until(lambda: client.state == "available")
        transport = client.transport
        rpc = transport.rpc
        sent = []
        def dropped(request, timeout=2):
            if request["method"] == "return_window":
                sent.append(request)
                raise PipeError("pipe_timeout")
            return rpc(request, timeout)
        transport.rpc = dropped
        body = {"action_id": "action", "generation": "generation", "seq": 1}
        self.assertEqual(client.request("return_window", body)["reason"], "return_unknown")
        until(lambda: client.transport is not None and client.transport is not transport)
        until(lambda: client.state == "available")
        self.assertEqual(client.request("return_window", body)["reason"], "return_unknown")
        self.assertEqual(len(sent), 1)

    def test_verified_peer_exit_is_reconnectable_but_identity_change_is_blocked(self):
        transport = object.__new__(WindowsPipe)
        transport.peer, transport.created_at = 3, 100
        class Api:
            wait, creation = 0, 100
            def WaitForSingleObject(self, *_args): return self.wait
            def created(self, _handle): return self.creation
        transport.api = Api()
        for state, creation, reason in ((0, 100, "pipe_closed"), (258, 101, "identity_mismatch"),
                                        (0xFFFFFFFF, 100, "identity_mismatch")):
            transport.api.wait, transport.api.creation = state, creation
            with self.assertRaises(PipeError) as raised:
                transport.rpc({"method": "get_snapshot"})
            self.assertEqual(raised.exception.reason, reason)
        client, _ = self.client()
        client._failed("pipe_closed"); self.assertFalse(client.blocked)
        client._failed("identity_mismatch"); self.assertTrue(client.blocked)


def json_bytes(value):
    import json
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
