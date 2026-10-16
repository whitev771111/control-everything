import copy
from functools import partial
import json
import subprocess
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from session_panel.sessions.remote_probe import read_registration
from session_panel.sessions.service import RemoteClient, SessionService as RealSessionService, merge_observation as real_merge_observation
from session_panel.sessions.source import validate_remote_payload
from tests.fixtures.integration_config import integration_config
from session_panel.sessions.terminal import parse_slot_command


CONFIG = integration_config()["integrations"]
REMOTE, TERMINAL = CONFIG["remote"], CONFIG["terminal"]
SOURCE = REMOTE["source"]
merge_observation = partial(real_merge_observation, source=SOURCE, terminal_config=TERMINAL, remote_config=REMOTE)


def configured_service(remote, terminal, **kwargs):
    remote.source, remote.config, terminal.config = copy.deepcopy(SOURCE), copy.deepcopy(REMOTE), copy.deepcopy(TERMINAL)
    return RealSessionService(remote, terminal, source=SOURCE, **kwargs)


MAIN = "b7ed3430-9064-4153-894d-d295a388a572"
FILES = "6ee281b5-e4f7-43ce-b1b7-92da287e5809"


def registration(session_id=MAIN, slot="main", pid=123, ticks="100"):
    return {"session_id": session_id, "cwd": "/srv/projects/shared", "pid": pid,
            "process_start_ticks": ticks, "boot_id": "boot-one", "registered_started_at": 123000,
            "process_ticks_stable": True, "slot": slot, "reason_codes": []}


def pane(slot="main", pane_id=2, **extra):
    return {"id": pane_id, "is_plugin": False, "tab_name": TERMINAL["tab"],
            "terminal_command": f"ssh.exe -t {SOURCE['host']} bash {TERMINAL['pane_script']} " + slot,
            **extra}


def remote(*records):
    return {"ok": True, "source": SOURCE, "registration_count": len(records),
            "process_count": 11, "registrations": list(records)}


class ObservationTests(unittest.TestCase):
    def merge(self, records, panes=None, previous=None):
        return merge_observation(remote(*records), {"ok": True, "panes": panes or [pane()]},
                                 previous or {}, "2026-10-03T00:00:00Z")

    def test_same_cwd_separate_real_ids_and_counts(self):
        result = self.merge([registration(), registration(FILES, "files", 124)], [pane(), pane("files", 4)])
        self.assertEqual([row["session_id"] for row in result["sessions"]], sorted([MAIN, FILES]))
        self.assertEqual(result["coverage"]["registration_count"], 2)
        self.assertEqual(result["coverage"]["process_count"], 11)
        self.assertEqual(result["coverage"]["confirmed_binding_count"], 2)
        for row in result["sessions"]:
            self.assertEqual(row["activity_state"], "unknown")
            self.assertEqual(row["task_completion"], "unknown")

    def test_duplicate_id_and_slot_are_not_guessable(self):
        duplicate_id = self.merge([registration(), registration(pid=999)])
        self.assertEqual(duplicate_id["sessions"][0]["reason"], "identity_conflict")
        duplicate_slot = self.merge([registration(), registration(FILES, "main", 124)])
        self.assertTrue(all(row["terminal_binding"] == "unknown" for row in duplicate_slot["sessions"]))
        duplicate_pane = self.merge([registration()], [pane(), pane(pane_id=19)])
        self.assertEqual(duplicate_pane["sessions"][0]["terminal_binding"], "unknown")

    def test_bad_registry_retained_without_removing_good_id(self):
        result = self.merge([registration(), {"registration_path": "broken.json", "reason_codes": ["registry_invalid"]}])
        self.assertEqual(len(result["sessions"]), 1)
        self.assertEqual(len(result["coverage"]["invalid_registrations"]), 1)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "123.json"
            path.write_text('{"pid":456}', encoding="utf-8")
            row = read_registration(path, Path(directory), "boot", 0, 100, 1000)
            self.assertEqual(row["reason_codes"], ["registry_invalid"])
            path.write_text('{broken', encoding="utf-8")
            self.assertEqual(read_registration(path, Path(directory), "boot", 0, 100, 1000)["reason_codes"],
                             ["registration_unreadable"])

    def test_missing_process_argv_conflict_and_time_warning_unknown(self):
        for reason in ("process_unverified", "identity_conflict", "instance_changed", "registration_time_unexplained"):
            row = registration()
            row["reason_codes"] = [reason]
            result = self.merge([row])["sessions"][0]
            self.assertEqual(result["identity_state"], "unknown")
            self.assertIsNone(result["pane_id"])

    def test_reused_pid_clears_old_binding_and_next_observation_revalidates(self):
        first = self.merge([registration()])
        second = self.merge([registration(ticks="200")], previous=first)
        self.assertEqual(second["sessions"][0]["reason"], "instance_changed")
        self.assertEqual(second["sessions"][0]["last_evidence"]["pane_id"], "terminal_2")
        third = self.merge([registration(ticks="200")], previous=second)
        self.assertEqual(third["sessions"][0]["terminal_binding"], "confirmed")

    def test_exact_command_only_null_plugin_and_extra_arguments_rejected(self):
        self.assertEqual(parse_slot_command(pane(), TERMINAL, REMOTE), "main")
        for candidate in (pane(terminal_command=None), pane(is_plugin=True), pane(tab_name="Elsewhere"),
                          pane(terminal_command=pane()["terminal_command"] + " --resume x"),
                          pane(terminal_command=pane()["terminal_command"].replace(SOURCE["host"], "evil.example"))):
            self.assertIsNone(parse_slot_command(candidate, TERMINAL, REMOTE))

    def test_source_or_terminal_failure_revokes_current_binding(self):
        first = self.merge([registration()])
        for failure_remote, failure_terminal in (({"ok": False, "reason": "version_mismatch"}, {"ok": True}),
                                                (remote(registration()), {"ok": False, "reason": "terminal_timeout"})):
            result = merge_observation(failure_remote, failure_terminal, first, "later")
            row = result["sessions"][0]
            self.assertEqual(row["identity_state"], "unknown")
            self.assertIsNone(row["pane_id"])
            self.assertEqual(row["last_evidence"]["pane_id"], "terminal_2")

    def test_recovered_source_missing_registration_has_current_reason_only(self):
        first = self.merge([registration()])
        failed = merge_observation({"ok": False, "reason": "source_unavailable"},
                                   {"ok": True, "panes": [pane()]}, first, "failed-sample")
        recovered = merge_observation(remote(), {"ok": True, "panes": [pane()]},
                                      failed, "recovered-sample")
        row = recovered["sessions"][0]
        self.assertEqual(row["reason_codes"], ["registration_missing"])
        self.assertEqual(row["registration_state"], "missing")
        self.assertEqual(row["last_evidence"]["reason_codes"], ["source_unavailable"])
        self.assertEqual(row["last_evidence"]["observed_at"], "failed-sample")
        self.assertNotIn("last_evidence", row["last_evidence"])
        self.assertIsNone(row["pane_id"])
        self.assertEqual(row["activity_state"], "unknown")
        self.assertEqual(row["task_completion"], "unknown")

    def test_legacy_reasons_missing_again_and_reappearance_are_bounded(self):
        first = self.merge([registration()])
        first["sessions"][0]["reason_codes"] = ["source_unavailable", "identity_conflict", "binding_unknown"]
        original = copy.deepcopy(first)
        missing = self.merge([], previous=first)
        row = missing["sessions"][0]
        self.assertEqual(row["reason_codes"], ["registration_missing"])
        self.assertEqual(row["last_evidence"]["reason_codes"], original["sessions"][0]["reason_codes"])
        self.assertEqual(row["last_evidence"]["slot"], "main")
        self.assertEqual(first, original)
        missing_again = self.merge([], previous=missing)
        again = missing_again["sessions"][0]
        self.assertEqual(again["reason_codes"], ["registration_missing"])
        self.assertEqual(again["last_evidence"]["reason_codes"], ["registration_missing"])
        self.assertNotIn("last_evidence", again["last_evidence"])
        returned = self.merge([registration()], previous=missing_again)["sessions"][0]
        self.assertEqual(returned["registration_state"], "present")
        self.assertEqual(returned["reason_codes"], [])
        self.assertEqual(returned["terminal_binding"], "confirmed")

    def test_current_source_and_terminal_failures_keep_only_current_reasons(self):
        candidate = registration()
        candidate["reason_codes"] = ["identity_conflict", "identity_conflict", "process_unverified"]
        original = copy.deepcopy(candidate)
        conflict = self.merge([candidate])["sessions"][0]
        self.assertEqual(conflict["reason_codes"], ["identity_conflict", "process_unverified"])
        self.assertEqual(conflict["registration_state"], "present")
        failed = merge_observation({**remote(candidate), "ok": False, "reason": "version_mismatch"},
                                   {"ok": True}, {}, "source-failure")["sessions"][0]
        self.assertEqual(failed["reason_codes"], ["version_mismatch"])
        self.assertEqual(failed["registration_state"], "unknown")
        self.assertEqual(candidate, original)
        terminal_failed = merge_observation(remote(registration()), {"ok": False, "reason": "terminal_timeout"},
                                           {}, "terminal-failure")["sessions"][0]
        self.assertEqual(terminal_failed["reason_codes"], ["terminal_timeout"])
        self.assertEqual(terminal_failed["registration_state"], "present")
        self.assertIsNone(terminal_failed["pane_id"])

    def test_stale_view_revokes_registration_without_mutating_snapshot(self):
        clock = Mock(return_value=0.0)
        source, terminal = Mock(), Mock()
        source.observe_source.return_value = remote(registration())
        terminal.list_panes.return_value = {"ok": True, "panes": [pane()]}
        service = configured_service(source, terminal, clock=clock, utc_now=lambda: "sample")
        service.list_sessions()
        original = copy.deepcopy(service._snapshot)
        clock.return_value = 31
        stale = service._view()["sessions"][0]
        self.assertEqual(stale["registration_state"], "unknown")
        self.assertIsNone(stale["pane_id"])
        self.assertEqual(service._snapshot, original)

    def test_cache_earliest_clock_stale_failure_and_reconnect(self):
        clock = Mock(return_value=0.0)
        source = Mock()
        terminal = Mock()
        source.observe_source.return_value = remote(registration())
        terminal.list_panes.return_value = {"ok": True, "panes": [pane()]}
        service = configured_service(source, terminal, clock=clock, utc_now=lambda: "now")
        service.list_sessions()
        clock.return_value = 4
        self.assertEqual(service.list_sessions()["age_ms"], 4000)
        source.observe_source.assert_called_once()
        clock.return_value = 31
        self.assertEqual(service._view()["sessions"][0]["reason"], "observation_stale")
        source.observe_source.return_value = {"ok": False, "reason": "source_unavailable"}
        failed = service.list_sessions()
        self.assertEqual(failed["sessions"][0]["terminal_binding"], "unknown")
        self.assertEqual(failed["last_success_at"], "now")
        source.observe_source.return_value = remote(registration())
        self.assertEqual(service.refresh_observation(force=True)["sessions"][0]["terminal_binding"], "confirmed")

    def test_collection_duration_is_not_reset_when_merge_finishes(self):
        now = [10.0]
        source, terminal = Mock(), Mock()
        def observe():
            now[0] = 18.0
            return remote(registration())
        source.observe_source.side_effect = observe
        terminal.list_panes.return_value = {"ok": True, "panes": [pane()]}
        service = configured_service(source, terminal, clock=lambda: now[0])
        self.assertEqual(service.refresh_observation()["age_ms"], 8000)

    def test_private_force_returns_exact_merge_inventory_and_keeps_public_cache(self):
        clock = Mock(return_value=0.0); source, terminal = Mock(), Mock()
        source.observe_source.return_value = remote(registration())
        first = {"ok": True, "panes": [pane()], "version": "0.45.1", "session": TERMINAL["session"]}
        second = {**first, "panes": [pane(pane_id=19)]}
        terminal.list_panes.side_effect = [first, second]
        service = configured_service(source, terminal, clock=clock, utc_now=lambda: "sample")
        public = service.refresh_observation(force=True)
        clock.return_value = 1
        view, inventory = service.refresh_observation(force=True, _include_inventory=True)
        self.assertIs(inventory, second)
        self.assertEqual(view["sessions"][0]["pane_id"], "terminal_19")
        self.assertEqual(set(public), set(view)); self.assertNotIn("inventory", service._snapshot)
        self.assertFalse(any("inventory" in key for key in vars(service)))
        view["sessions"][0]["pane_id"] = "changed"
        inventory["panes"][0]["id"] = 99
        clock.return_value = 4
        cached = service.refresh_observation()
        self.assertIsInstance(cached, dict)
        self.assertEqual(cached["age_ms"], 3000)
        self.assertEqual(cached["sessions"][0]["pane_id"], "terminal_19")
        self.assertEqual(source.observe_source.call_count, 2)
        self.assertEqual(terminal.list_panes.call_count, 2)

    def test_private_force_overlaps_reads_and_joins_before_merge(self):
        inventory_started = threading.Event()
        overlap, workers, alive_at_merge = [], [], []
        source, terminal = Mock(), Mock()
        def observe():
            workers.append(threading.current_thread())
            overlap.append(inventory_started.wait(1))
            return remote(registration())
        def inventory():
            inventory_started.set()
            return {"ok": True, "panes": [pane()]}
        def merge(*args, **kwargs):
            alive_at_merge.append(any(worker.is_alive() for worker in workers))
            return merge_observation(*args, **kwargs)
        source.observe_source.side_effect = observe
        terminal.list_panes.side_effect = inventory
        service = configured_service(source, terminal, clock=lambda: 0, utc_now=lambda: "sample")
        with patch("session_panel.sessions.service.merge_observation", side_effect=merge):
            view, panes = service.refresh_observation(force=True, _include_inventory=True)
        self.assertEqual(overlap, [True], "private reads must overlap")
        self.assertEqual(alive_at_merge, [False], "worker must exit before merge")
        self.assertEqual(view["sessions"][0]["terminal_binding"], "confirmed")
        self.assertTrue(panes["ok"])
        self.assertFalse(service._observation_lock.locked())

    def test_private_pending_worker_holds_lock_and_delays_publication(self):
        started, release = threading.Event(), threading.Event()
        results, errors, workers = [], [], []
        source, terminal = Mock(), Mock()
        def observe():
            workers.append(threading.current_thread())
            started.set()
            if not release.wait(2): raise RuntimeError("test release missing")
            return remote(registration())
        source.observe_source.side_effect = observe
        terminal.list_panes.return_value = {"ok": True, "panes": [pane()]}
        service = configured_service(source, terminal, clock=lambda: 0)
        def refresh():
            try: results.append(service.refresh_observation(force=True, _include_inventory=True))
            except Exception as error: errors.append(error)
        caller = threading.Thread(target=refresh)
        caller.start()
        try:
            self.assertTrue(started.wait(1))
            self.assertFalse(service._observation_lock.acquire(blocking=False))
            self.assertEqual(service._snapshot["sessions"], [])
            self.assertIsNone(service._started)
            self.assertEqual(results, [])
        finally:
            release.set(); caller.join(2)
        self.assertFalse(caller.is_alive()); self.assertEqual(errors, [])
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(results[0][0]["sessions"][0]["terminal_binding"], "confirmed")
        self.assertFalse(service._observation_lock.locked())

    def test_private_parallel_errors_and_age_keep_original_merge_semantics(self):
        for mode in ("remote", "inventory", "late"):
            with self.subTest(mode=mode):
                started, release = threading.Event(), threading.Event()
                now, workers, alive_at_merge = [10.0], [], []
                source, terminal = Mock(), Mock()
                source.observe_source.return_value = remote(registration())
                terminal.list_panes.return_value = {"ok": True, "panes": [pane()]}
                service = configured_service(source, terminal, clock=lambda: now[0])
                service.refresh_observation(force=True)
                def observe():
                    workers.append(threading.current_thread()); started.set()
                    if not release.wait(2): raise RuntimeError("test release missing")
                    if mode == "remote": raise OSError("remote failed")
                    return remote(registration())
                def inventory():
                    try:
                        self.assertTrue(started.wait(1))
                        self.assertTrue(service._observation_lock.locked())
                        if mode == "late": now[0] = 15.001
                        if mode == "inventory": raise OSError("inventory failed")
                        return {"ok": True, "panes": [pane()]}
                    finally: release.set()
                def merge(*args, **kwargs):
                    alive_at_merge.append(any(worker.is_alive() for worker in workers))
                    return merge_observation(*args, **kwargs)
                source.observe_source.side_effect = observe; terminal.list_panes.side_effect = inventory
                with patch("session_panel.sessions.service.merge_observation", side_effect=merge):
                    view, panes = service.refresh_observation(force=True, _include_inventory=True)
                self.assertEqual(alive_at_merge, [False]); self.assertFalse(service._observation_lock.locked())
                if mode == "late":
                    self.assertGreaterEqual(view["age_ms"], 5000)
                    self.assertEqual(service._started, 10.0)
                else:
                    self.assertEqual(view["status"], "unknown")
                    self.assertIsNone(view["sessions"][0]["pane_id"])
                    self.assertEqual(view["sessions"][0]["last_evidence"]["pane_id"], "terminal_2")
                    self.assertEqual(view["reason"], "source_unavailable" if mode == "remote" else "terminal_unavailable")

    def test_private_inventory_requires_force_and_never_returns_cached_identity(self):
        source, terminal = Mock(), Mock()
        source.observe_source.return_value = remote(registration())
        terminal.list_panes.return_value = {"ok":True, "panes":[pane()]}
        service = configured_service(source, terminal, clock=lambda: 1, utc_now=lambda: "sample")
        service.refresh_observation()
        with self.assertRaisesRegex(ValueError, "fresh inventory requires force"):
            service.refresh_observation(_include_inventory=True)
        self.assertEqual(source.observe_source.call_count, 1)
        self.assertEqual(terminal.list_panes.call_count, 1)

    def test_private_failed_inventory_retains_exit_and_revokes_old_binding(self):
        source, terminal = Mock(), Mock()
        source.observe_source.return_value = remote(registration())
        good = {"ok":True,"panes":[pane()]}
        failure = {"ok":False,"reason":"terminal_version_mismatch","exit_code":9}
        terminal.list_panes.side_effect = [good, failure]
        service = configured_service(source, terminal, clock=lambda: 1, utc_now=lambda: "sample")
        service.refresh_observation(force=True)
        view, inventory = service.refresh_observation(force=True, _include_inventory=True)
        self.assertIs(inventory, failure); self.assertEqual(inventory["exit_code"], 9)
        self.assertEqual(view["status"], "unknown")
        self.assertEqual(view["sessions"][0]["terminal_binding"], "unknown")
        self.assertIsNone(view["sessions"][0]["pane_id"])
        self.assertEqual(view["sessions"][0]["last_evidence"]["pane_id"], "terminal_2")

    def test_private_force_exception_does_not_reuse_success(self):
        for failed in ("remote", "terminal"):
            with self.subTest(failed=failed):
                source, terminal = Mock(), Mock()
                source.observe_source.return_value = remote(registration())
                terminal.list_panes.return_value = {"ok":True,"panes":[pane()]}
                service = configured_service(source, terminal, clock=lambda: 1, utc_now=lambda: "sample")
                service.refresh_observation(force=True)
                target = source.observe_source if failed=="remote" else terminal.list_panes
                target.side_effect = RuntimeError("unavailable")
                view, inventory = service.refresh_observation(force=True, _include_inventory=True)
                self.assertEqual(view["sessions"][0]["terminal_binding"], "unknown")
                self.assertIsNone(view["sessions"][0]["pane_id"])
                if failed=="terminal": self.assertEqual(inventory, {"ok":False,"reason":"terminal_unavailable"})
                else: self.assertTrue(inventory["ok"])
                self.assertEqual(source.observe_source.call_count, 2)
                self.assertEqual(terminal.list_panes.call_count, 2)

    def test_private_age_contains_remote_and_inventory_duration(self):
        now = [10.0]; source, terminal = Mock(), Mock()
        def observe(): now[0] += .781; return remote(registration())
        def inventory(): now[0] += .423; return {"ok":True,"panes":[pane()]}
        source.observe_source.side_effect = observe; terminal.list_panes.side_effect = inventory
        service = configured_service(source, terminal, clock=lambda:now[0], utc_now=lambda:"sample")
        view, panes = service.refresh_observation(force=True, _include_inventory=True)
        self.assertTrue(panes["ok"])
        self.assertGreaterEqual(view["age_ms"], 1203); self.assertLessEqual(view["age_ms"], 1204)
        self.assertEqual(service._started, 10.0)
        now[0] += 1
        self.assertGreaterEqual(service._view()["age_ms"], 2203)

    def test_ssh_argv_bootstrap_and_filtered_failures(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(remote(registration())), ""))
        client = RemoteClient(runner, config=REMOTE)
        self.assertTrue(client.observe_source()["ok"])
        argv = runner.call_args.args[0]
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertIn("BatchMode=yes", argv)
        self.assertEqual(argv[-2:], [SOURCE["host"], "python3 -"])
        compile(runner.call_args.kwargs["input"], "stdin-probe", "exec")
        runner.return_value = subprocess.CompletedProcess([], 255, "", "private error")
        self.assertNotIn("private", json.dumps(client.observe_source()))
        runner.side_effect = subprocess.TimeoutExpired(argv, 15)
        self.assertEqual(client.observe_source()["reason"], "observe_timeout")

    def test_snapshot_does_not_mutate_inputs(self):
        original = registration()
        result = self.merge([original])
        result["sessions"][0]["provenance"]["registrations"][0]["cwd"] = "changed"
        self.assertEqual(original["cwd"], "/srv/projects/shared")




class RemoteSourceLayoutTests(unittest.TestCase):
    def test_remote_source_import_closure_without_ssh_or_observation(self):
        import sys

        runner = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({"source": SOURCE}), ""))
        RemoteClient(runner, config=REMOTE).observe_source()
        script = runner.call_args.kwargs["input"]
        prefix, entry = script.rsplit("\nimport base64\n", 1)
        # A fresh interpreter has no importable local package; the bootstrap supplies it.
        fixture = "\nobserve_source = lambda: {'ok': True, 'source': SOURCE}\n"
        result = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", prefix + fixture + "\nimport base64\n" + entry],
                                capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"ok": True, "source": SOURCE})
        self.assertNotIn("session_panel.profiles", script)
        runner.assert_called_once()

    def test_unconfigured_and_disabled_client_never_runs(self):
        runner = Mock()
        for config in (None, {**REMOTE, "enabled": False}):
            client = RemoteClient(runner, config=config)
            self.assertEqual(client.observe_source()["reason"], "not_configured")
            self.assertEqual(client.read_messages(MAIN)["reason"], "not_configured")
        runner.assert_not_called()

    def test_two_client_sources_are_isolated_and_copied(self):
        one, two = copy.deepcopy(REMOTE), copy.deepcopy(REMOTE)
        two["source"].update(id="second-source", host="other.invalid")
        runners = [Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({"source": cfg["source"]}), ""))
                   for cfg in (one, two)]
        first, second = [RemoteClient(run, config=cfg) for run, cfg in zip(runners, (one, two))]
        one["source"]["host"] = "mutated.invalid"
        self.assertEqual(first.observe_source()["source"], SOURCE)
        self.assertEqual(second.observe_source()["source"], two["source"])
        self.assertEqual(runners[0].call_args.args[0][-2], SOURCE["host"])
        self.assertEqual(runners[1].call_args.args[0][-2], "other.invalid")
        first_service, second_service = RealSessionService(first), RealSessionService(second)
        self.assertNotEqual(first_service.source, second_service.source)
        self.assertIsNot(first_service.source, first.source)

    def test_invalid_host_path_version_and_transport_shape_are_rejected(self):
        payload = RemoteClient(config=REMOTE)._payload("observe", None)
        cases = []
        for host in ("-option", "host;echo", "host name", "host\nname", "中文"):
            candidate = copy.deepcopy(payload); candidate["source"]["host"] = host; cases.append(candidate)
        for path in ("relative", "/home/example/../other", "/home/example/./other", "//home/example"):
            candidate = copy.deepcopy(payload); candidate["home"] = path; cases.append(candidate)
        for version in ("bad", None, True):
            candidate = copy.deepcopy(payload); candidate["reader_mcp_version"] = version; cases.append(candidate)
        cases += [{}, {**payload, "extra": 1}, {**payload, "mode": "exec"}, {**payload, "request": {}}]
        for candidate in cases:
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                validate_remote_payload(candidate)

    def test_opaque_unicode_and_quotes_remain_encoded_data(self):
        import sys
        config = copy.deepcopy(REMOTE)
        config["source"]["id"] = "名字@source:/path/quote'\";__import__('os')"
        config["home"] = "/home/名字'quoted"
        config["reader_python"] = "/home/example/reader folder/python'quoted"
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({"source": config["source"]}), ""))
        client = RemoteClient(runner, config=config)
        client.observe_source()
        script = runner.call_args.kwargs["input"]
        prefix, entry = script.rsplit("\nimport base64\n", 1)
        fixture = "\nobserve_source = lambda: {'ok': True, 'source': SOURCE, 'home': HOME}\n"
        result = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", prefix + fixture + "\nimport base64\n" + entry],
                                capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["home"], config["home"])
        self.assertNotIn(config["source"]["id"], script)
        client.read_messages(MAIN)
        self.assertEqual(runner.call_args.args[0][-1], __import__("shlex").quote(config["reader_python"]) + " -")

    def test_mismatched_source_is_not_accepted(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({"source": {**SOURCE, "user": "other"}}), ""))
        self.assertEqual(RemoteClient(runner, config=REMOTE).observe_source()["reason"], "source_unavailable")

    def test_merge_does_not_reuse_another_source_snapshot(self):
        previous = merge_observation(remote(registration()), {"ok": True, "panes": [pane()]}, {}, "first")
        other = {**SOURCE, "id": "other-source"}
        result = real_merge_observation({"ok": False, "reason": "source_unavailable"}, {"ok": False},
                                        previous, "later", source=other)
        self.assertEqual(result["sessions"], [])


if __name__ == "__main__":
    unittest.main()
