import copy
import hashlib
import io
import subprocess
import unittest
from unittest.mock import Mock, patch

from tests.fixtures.integration_config import integration_config
from session_panel.sessions.terminal import parse_clients
from session_panel.sessions.workbench import (Workbench, same_association,
                                             command_line_argv, created_ticks, parse_sessions)

CONFIG = integration_config()["integrations"]
TERMINAL, WORKBENCH = CONFIG["terminal"], CONFIG["workbench"]
ZELLIJ, ZELLIJ_SESSION = TERMINAL["zellij"], TERMINAL["session"]
CONFIGURED = Workbench(config=WORKBENCH, terminal_config=TERMINAL)
MARKER, NEW_GUI, LEGACY_GUI = CONFIGURED.marker, CONFIGURED.new_gui, CONFIGURED.legacy_gui
WEZTERM_GUI, WEZTERM_LUA = WORKBENCH["wezterm_gui"], WORKBENCH["wezterm_lua"]
WORKDIR, UNMANAGED_MUX, PINS = WORKBENCH["workdir"], WORKBENCH["unmanaged_mux"], WORKBENCH["pins"]

HEADER = "CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n"
TIME = "2026-10-04T00:00:00.0000000Z"
GUI_TIME = "2026-10-04T00:00:01.0000000Z"
CHILD_TIME = "2026-10-04T00:00:02.0000000Z"


def process(pid, parent, argv, created=TIME):
    return {"ProcessId": pid, "ParentProcessId": parent, "Name": argv[0].split("/")[-1],
            "ExecutablePath": argv[0], "CommandLine": subprocess.list2cmdline(argv), "CreatedUtc": created}


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.rows = [process(10, 1, [ZELLIJ, "--server", MARKER]), process(40, 2, [UNMANAGED_MUX])]
        self.sessions = "fixture-session [Created 1day 2h 3m 4s ago]\nold [Created 2days ago] (EXITED - attach to resurrect)\n"
        self.clients = HEADER
        self.calls = []
        self.gui = Mock(pid=20, _handle=999)
        self.gui.poll.return_value = None
        self.popen = Mock(side_effect=self.launch)
        self.workbench = Workbench(self.run_command, lambda: self.now, lambda deadline: copy.deepcopy(self.rows), self.popen, config=WORKBENCH, terminal_config=TERMINAL)
        self.marker = patch.object(self.workbench, "_marker_pid", return_value=10); self.marker.start(); self.addCleanup(self.marker.stop)
        self.integrity = patch.object(self.workbench, "integrity", return_value=True); self.integrity.start(); self.addCleanup(self.integrity.stop)
        self.times = patch("session_panel.sessions.workbench.process_times", return_value=(created_ticks(GUI_TIME), 0))
        self.times.start(); self.addCleanup(self.times.stop)

    def run_command(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        self.assertGreater(kwargs["timeout"], 0); self.assertLessEqual(kwargs["timeout"], 8)
        self.assertFalse(kwargs.get("shell", False))
        text = ("zellij 0.45.1\n" if argv[-1] == "--version" else self.sessions
                if "list-sessions" in argv else self.clients)
        return subprocess.CompletedProcess(argv, 0, text, "")

    def attach(self, legacy=False):
        self.rows.extend([process(20, 3, LEGACY_GUI if legacy else NEW_GUI, GUI_TIME),
                          process(30, 20, [ZELLIJ, "attach", ZELLIJ_SESSION, "--create", "--force-run-commands"]
                                  if legacy else [ZELLIJ, "attach"], CHILD_TIME)])
        self.clients = HEADER + "1 terminal_2 ssh.exe\n"

    def launch(self, argv, **kwargs):
        self.assertEqual(argv, NEW_GUI); self.assertFalse(kwargs["shell"])
        self.assertNotIn("creationflags", kwargs)
        self.assertNotIn("ZELLIJ_SOCKET_DIR", kwargs["env"])
        self.attach()
        return self.gui

    def attach_named(self, gui_pid=20, child_pid=30):
        argv = [WEZTERM_GUI, "--config-file", WEZTERM_LUA, "start", "--always-new-process",
                "--cwd", WORKDIR, "--", ZELLIJ, "attach", ZELLIJ_SESSION]
        self.rows.extend([process(gui_pid, 3, argv, GUI_TIME),
                          process(child_pid, gui_pid, [ZELLIJ, "attach", ZELLIJ_SESSION], CHILD_TIME)])
        self.clients = HEADER + "1 terminal_2 ssh.exe\n"

    def test_existing_named_pair_is_unique_without_launcher_or_ownership(self):
        self.attach_named()
        owned_before = object(); self.workbench._owned_gui = owned_before
        cleanup = Mock(); self.workbench._cleanup_owned = cleanup
        state = self.workbench.observe()
        self.assertEqual((state["association_state"], state["gui_count"], state["interactive_client_count"]),
                         ("unique_observed", 1, 1))
        self.assertTrue(state["can_focus"]); self.assertFalse(state["can_connect"])
        for _ in range(2):
            result = self.workbench.connect()
            self.assertEqual((result["connected"], result["launched"], result["cleanup"], result["status"]),
                             (True, False, "not_needed", "unique_observed"))
        self.popen.assert_not_called(); cleanup.assert_not_called()
        self.assertIs(self.workbench._owned_gui, owned_before)

    def test_named_and_original_profiles_cannot_cross_pair(self):
        self.attach_named(); original = copy.deepcopy(self.rows)
        named_gui = original[-2]
        for gui_argv, child_argv in [(NEW_GUI, [ZELLIJ, "attach", ZELLIJ_SESSION]),
                                     (LEGACY_GUI, [ZELLIJ, "attach", ZELLIJ_SESSION]),
                                     (None, [ZELLIJ, "attach"]),
                                     (None, [ZELLIJ, "attach", ZELLIJ_SESSION, "--create", "--force-run-commands"])]:
            with self.subTest(gui=gui_argv, child=child_argv):
                self.rows = copy.deepcopy(original)
                self.rows[-2]["CommandLine"] = subprocess.list2cmdline(gui_argv) if gui_argv else named_gui["CommandLine"]
                self.rows[-1]["CommandLine"] = subprocess.list2cmdline(child_argv)
                state = self.workbench.observe()
                self.assertFalse(state["can_connect"]); self.assertFalse(state["can_focus"])
                self.assertFalse(self.workbench.connect()["connected"])
                self.popen.assert_not_called()

    def test_named_pair_rejects_foreign_identity_and_incomplete_or_extra_argv(self):
        self.attach_named(); original = copy.deepcopy(self.rows)
        gui_argv = [WEZTERM_GUI, "--config-file", WEZTERM_LUA, "start", "--always-new-process",
                    "--cwd", WORKDIR, "--", ZELLIJ, "attach", ZELLIJ_SESSION]
        config = gui_argv.copy(); config[2] = "C:/example/elsewhere/wezterm.lua"
        cwd = gui_argv.copy(); cwd[6] = "C:/example/elsewhere"
        session = gui_argv.copy(); session[-1] = "other-session"
        changes = [(-2, "ExecutablePath", "C:/example/elsewhere/wezterm-gui.exe"), (-2, "Name", "wezterm.exe"),
                   (-1, "ExecutablePath", "C:/example/elsewhere/zellij.exe"), (-2, "ParentProcessId", None),
                   (-2, "CommandLine", subprocess.list2cmdline([WEZTERM_GUI, "start"]))]
        changes += [(-2, "CommandLine", subprocess.list2cmdline(argv))
                    for argv in (config, cwd, session, gui_argv + ["--extra"], gui_argv[:-1])]
        changes += [(-1, "CommandLine", subprocess.list2cmdline(argv))
                    for argv in ([ZELLIJ, "attach", "other-session"], [ZELLIJ, "attach", ZELLIJ_SESSION, "--extra"])]
        for index, field, value in changes:
            with self.subTest(index=index, field=field, value=value):
                self.rows = copy.deepcopy(original); self.rows[index][field] = value
                state = self.workbench.observe()
                self.assertIsNone(state["gui_count"]); self.assertIsNone(state["interactive_client_count"])
                self.assertFalse(state["can_focus"]); self.assertFalse(state["can_connect"])
                self.assertFalse(self.workbench.connect()["connected"])
                self.popen.assert_not_called()

    def test_named_pair_requires_direct_parent_and_later_birth(self):
        self.attach_named(); original = copy.deepcopy(self.rows)
        for field, value in [("ParentProcessId", 99), ("CreatedUtc", TIME)]:
            with self.subTest(field=field):
                self.rows = copy.deepcopy(original); self.rows[-1][field] = value
                self.assertFalse(self.workbench.observe()["can_focus"])
                self.assertFalse(self.workbench.connect()["connected"])
                self.popen.assert_not_called()

    def test_named_pair_does_not_bypass_full_fingerprints_or_client_identity(self):
        self.attach_named(); original = copy.deepcopy(self.rows)
        for index in range(4):
            for field, value in [("ProcessId", original[index]["ProcessId"] + 100),
                                 ("ParentProcessId", original[index]["ParentProcessId"] + 100),
                                 ("CreatedUtc", "2026-10-04T00:00:03.0000000Z")]:
                with self.subTest(index=index, field=field):
                    changed = copy.deepcopy(original); changed[index][field] = value
                    self.workbench.provider = Mock(side_effect=[copy.deepcopy(original), changed])
                    state = self.workbench.observe()
                    self.assertFalse(state["can_focus"]); self.assertFalse(state["can_connect"])
        self.workbench.provider = lambda deadline: copy.deepcopy(original)
        before = self.workbench.observe()
        self.clients = HEADER + "2 terminal_2 ssh.exe\n"
        after = self.workbench.observe()
        self.assertFalse(same_association(before, after))

    def test_named_pair_does_not_bypass_clients_budget_lock_or_latch(self):
        self.attach_named()
        for text in (HEADER, "", "bad header", HEADER + "1 terminal_2 cmd\n2 terminal_3 cmd\n",
                     HEADER + "1 terminal_2 cmd\n01 terminal_3 cmd\n"):
            with self.subTest(clients=text):
                self.clients = text
                self.assertFalse(self.workbench.connect()["connected"])
                self.popen.assert_not_called()
        self.clients = HEADER + "1 terminal_2 cmd\n"
        self.workbench.action_lock.acquire()
        try:
            self.assertEqual(self.workbench.connect()["reason"], "terminal_action_busy")
        finally:
            self.workbench.action_lock.release()
        self.workbench.cleanup_latch = True
        self.assertEqual(self.workbench.connect()["reason"], "cleanup_unverified")
        self.assertFalse(self.workbench.public_status()["can_focus"])
        self.workbench.cleanup_latch = False
        def slow(deadline):
            self.now += 3
            return copy.deepcopy(self.rows)
        self.workbench.provider = slow
        state = self.workbench.observe()
        self.assertEqual((state["age_ms"], state["reason"]), (6000, "workbench_timeout"))
        self.assertFalse(state["can_focus"]); self.assertFalse(state["can_connect"])
        self.popen.assert_not_called()

    def test_all_configured_pins_are_checked_and_any_mismatch_refuses(self):
        self.attach_named()
        real_integrity = Workbench.integrity.__get__(self.workbench)
        for mismatch in [None, *PINS]:
            digests = [Mock(hexdigest=Mock(return_value="0" * 64 if path == mismatch else expected))
                       for path, expected in PINS.items()]
            with self.subTest(mismatch=mismatch), patch("builtins.open", side_effect=lambda path, mode: io.BytesIO(b"fixed-file")), \
                    patch("session_panel.sessions.workbench.hashlib.sha256", side_effect=digests), \
                    patch.object(self.workbench, "integrity", wraps=real_integrity):
                result = self.workbench.connect()
                self.assertEqual(result["connected"], mismatch is None)
                if mismatch is not None:
                    self.assertEqual(result["reason"], "workbench_integrity_failed")
                self.popen.assert_not_called()

    def test_owned_cleanup_preserves_unowned_named_pair(self):
        self.attach_named(gui_pid=60, child_pid=70); named_rows = copy.deepcopy(self.rows[-2:])
        self.attach(); terminated = []
        def terminate(row, deadline):
            terminated.append(row["ProcessId"])
            self.rows = [raw for raw in self.rows if raw["ProcessId"] != row["ProcessId"]]
        self.workbench._terminate_client = terminate
        self.gui.wait.side_effect = lambda timeout: setattr(self, "rows", [row for row in self.rows if row["ProcessId"] != 20])
        with patch("session_panel.sessions.workbench.process_times", return_value=(created_ticks(GUI_TIME), created_ticks(CHILD_TIME) + 10)):
            self.assertEqual(self.workbench._cleanup_owned({"process": self.gui, "created": created_ticks(GUI_TIME)}, 3), "verified")
        self.assertEqual(terminated, [30]); self.gui.terminate.assert_called_once()
        self.assertEqual(self.rows[-2:], named_rows)

    def test_named_child_cannot_be_reclaimed_as_owned_unnamed_attach(self):
        self.attach(); self.rows[-1]["CommandLine"] = subprocess.list2cmdline([ZELLIJ, "attach", ZELLIJ_SESSION])
        self.workbench._terminate_client = Mock()
        owned = {"process": self.gui, "created": created_ticks(GUI_TIME)}
        with patch("session_panel.sessions.workbench.process_times", return_value=(created_ticks(GUI_TIME), created_ticks(CHILD_TIME) + 10)):
            self.assertEqual(self.workbench._cleanup_owned(owned, 3), "unverified")
        self.workbench._terminate_client.assert_not_called()

    def test_disconnected_is_observed_and_fixed_external_mux_is_separate(self):
        state = self.workbench.public_status()
        self.assertEqual((state["server_state"], state["gui_count"], state["interactive_client_count"], state["unmanaged_mux_count"]), ("running", 0, 0, 1))
        self.assertTrue(state["can_connect"]); self.assertFalse(state["can_focus"])
        self.assertEqual((state["watcher_count"], state["gui_visibility"], state["client_scope"]), (None, "unverified", "interactive_only"))
        self.assertNotIn("CommandLine", str(state)); self.assertNotIn("_server", state)

    def test_fixed_launch_is_confirmed_and_repeated_connect_does_not_launch(self):
        first = self.workbench.connect()
        self.assertEqual((first["connected"], first["launched"], first["status"]), (True, True, "unique_observed"))
        second = self.workbench.connect()
        self.assertTrue(second["connected"]); self.assertFalse(second["launched"])
        self.popen.assert_called_once()

    def test_existing_legacy_profile_is_observed_never_launched(self):
        self.attach(legacy=True)
        self.assertTrue(self.workbench.connect()["connected"])
        self.popen.assert_not_called()

    def test_missing_exited_zero_bad_and_multiple_sessions_never_launch(self):
        for text, state in [("old [Created 1s ago]", "missing"),
                            ("fixture-session [Created 1s ago] (EXITED - attach to resurrect)", "exited"),
                            ("", "unknown"), ("fixture-session invalid", "unknown"),
                            ("fixture-session [Created 1s ago]\nextra [Created 1s ago]", "running")]:
            with self.subTest(text=text):
                self.sessions = text
                self.assertEqual(self.workbench.connect()["workbench"]["server_state"], state)
                self.popen.assert_not_called()

    def test_bad_or_unknown_census_never_becomes_zero(self):
        original = copy.deepcopy(self.rows)
        for field in ("ExecutablePath", "CommandLine", "CreatedUtc", "ParentProcessId", "Name"):
            with self.subTest(field=field):
                self.rows = copy.deepcopy(original); self.rows[1][field] = None
                state = self.workbench.observe()
                self.assertIsNone(state["unmanaged_mux_count"]); self.assertIsNone(state["gui_count"])
                self.assertFalse(state["can_connect"])
        self.rows = original + [copy.deepcopy(original[0])]
        self.assertFalse(self.workbench.observe()["can_connect"])
        self.workbench.provider = Mock(side_effect=OSError("private"))
        self.assertNotIn("private", str(self.workbench.public_status()))

    def test_extra_mux_flags_foreign_gui_watch_and_unknown_probe_refuse(self):
        original = copy.deepcopy(self.rows)
        candidates = [process(41, 2, [UNMANAGED_MUX, "--extra"]), process(41, 2, ["C:/example/elsewhere/wezterm-mux-server.exe"]),
                      process(41, 2, [NEW_GUI[0], "start"]), process(41, 2, [ZELLIJ, "watch", ZELLIJ_SESSION]),
                      process(41, 2, [ZELLIJ, "action", "unknown"])]
        for row in candidates:
            with self.subTest(argv=row["CommandLine"]):
                self.rows = original + [row]
                self.assertFalse(self.workbench.connect()["connected"]); self.popen.assert_not_called()

    def test_unstable_pid_parent_or_created_mux_fingerprint_refuses(self):
        for field, value in [("ProcessId", 41), ("ParentProcessId", 4), ("CreatedUtc", GUI_TIME)]:
            changed = copy.deepcopy(self.rows); changed[1][field] = value
            self.workbench.provider = Mock(side_effect=[copy.deepcopy(self.rows), changed])
            self.assertFalse(self.workbench.observe()["can_connect"])

    def test_valid_probes_are_readonly_and_unknown_argv_is_not_ignored(self):
        self.rows.append(process(50, 1, [ZELLIJ, "--session", ZELLIJ_SESSION, "action", "list-panes", "--all", "--json"]))
        self.assertTrue(self.workbench.observe()["can_connect"])
        self.rows[-1]["CommandLine"] += " --extra"
        self.assertFalse(self.workbench.observe()["can_connect"])

    def test_known_owned_client_birth_waits_but_public_state_is_not_ready(self):
        baseline = self.workbench.observe()
        self.attach()
        full = copy.deepcopy(self.rows); before_birth = full[:-1]
        self.workbench.provider = Mock(side_effect=[before_birth, full, full, full])
        self.workbench._owned_gui = {"process": self.gui, "created": created_ticks(GUI_TIME)}
        with patch("session_panel.sessions.workbench.time.sleep", side_effect=lambda amount: setattr(self, "now", self.now + amount)):
            state = self.workbench._wait_connected(8, baseline["_server"], 20, baseline["_mux"])
        self.assertTrue(state["can_focus"]); self.assertEqual(self.workbench.provider.call_count, 4)
        self.workbench.provider = Mock(side_effect=[before_birth, full])
        transition = self.workbench.observe()
        self.assertEqual(transition["reason"], "workbench_transitioning")
        self.assertFalse(transition["can_focus"]); self.assertFalse(transition["can_connect"])

    def test_disconnected_launch_transient_waits_without_accepting_unknown(self):
        baseline = self.workbench.observe(); base = copy.deepcopy(self.rows)
        self.attach(); full = copy.deepcopy(self.rows); self.clients = HEADER
        samples = iter([base, base, full, full]); count = 0
        def provider(deadline):
            nonlocal count
            count += 1
            if count == 3:
                self.clients = HEADER + "1 terminal_2 cmd\n"
            return next(samples)
        self.workbench.provider = provider
        self.workbench._owned_gui = {"process": self.gui, "created": created_ticks(GUI_TIME)}
        with patch("session_panel.sessions.workbench.time.sleep", side_effect=lambda amount: setattr(self, "now", self.now + amount)):
            self.assertTrue(self.workbench._wait_connected(8, baseline["_server"], 20, baseline["_mux"])["can_focus"])
        full[-1]["CommandLine"] += " --unexpected"
        self.workbench.provider = Mock(return_value=full)
        refused = self.workbench._wait_connected(8, baseline["_server"], 20, baseline["_mux"])
        self.assertFalse(refused["can_focus"]); self.assertEqual(refused["reason"], "workbench_observation_unknown")
        self.assertEqual(self.workbench.provider.call_count, 1)

    def test_original_server_replacement_and_gui_pid_reuse_are_not_confirmed(self):
        baseline = self.workbench.observe(); self.attach()
        self.workbench._owned_gui = {"process": self.gui, "created": created_ticks(GUI_TIME)}
        self.rows[0]["CreatedUtc"] = GUI_TIME
        self.assertFalse(self.workbench._wait_connected(8, baseline["_server"], 20, baseline["_mux"])["can_focus"])
        self.rows[0]["CreatedUtc"] = TIME; self.rows[-2]["CreatedUtc"] = CHILD_TIME
        refused = self.workbench._wait_connected(8, baseline["_server"], 20, baseline["_mux"])
        self.assertFalse(refused["can_focus"])

    def test_finite_wait_does_not_open_a_new_observation_budget(self):
        baseline = self.workbench.observe()
        self.workbench._owned_gui = {"process": self.gui, "created": created_ticks(GUI_TIME)}
        with patch("session_panel.sessions.workbench.time.sleep", side_effect=lambda amount: setattr(self, "now", self.now + amount)):
            result = self.workbench._wait_connected(1, baseline["_server"], 20, baseline["_mux"])
        self.assertEqual(result["reason"], "workbench_connect_timeout"); self.assertEqual(self.now, 1)
        self.assertTrue(all(call[1]["timeout"] <= 1 for call in self.calls[-12:]))

    def test_bad_clients_extra_clients_and_wrong_parent_refuse_before_launch(self):
        for text in ("", "bad header", HEADER + "1 terminal_2 cmd\n2 terminal_2 cmd\n"):
            self.clients = text
            self.assertFalse(self.workbench.connect()["connected"])
            self.popen.assert_not_called()
        self.clients = HEADER; self.attach(); self.rows[-1]["ParentProcessId"] = 99
        self.assertFalse(self.workbench.observe()["can_focus"])

    def test_earliest_census_age_shared_budget_and_action_lock(self):
        def slow(deadline):
            self.now += 3
            return copy.deepcopy(self.rows)
        self.workbench.provider = slow
        state = self.workbench.observe()
        self.assertEqual((state["age_ms"], state["reason"]), (6000, "workbench_timeout"))
        self.assertFalse(state["can_connect"])
        self.workbench.action_lock.acquire()
        try:
            self.calls.clear()
            self.assertEqual(self.workbench.connect()["reason"], "terminal_action_busy")
            self.assertEqual(self.workbench.public_status()["reason"], "terminal_action_busy")
            self.assertEqual(self.calls, [])
        finally:
            self.workbench.action_lock.release()

    def test_integrity_failure_refuses_and_latch_has_no_reset(self):
        self.workbench.integrity.return_value = False
        self.assertEqual(self.workbench.connect()["reason"], "workbench_integrity_failed")
        self.popen.assert_not_called()
        self.workbench.cleanup_latch = True
        self.assertEqual(self.workbench.connect()["reason"], "cleanup_unverified")
        self.assertTrue(self.workbench.public_status()["actions_blocked"])

    def test_owned_failure_cleanup_targets_only_client_and_gui(self):
        self.workbench._wait_connected = Mock(return_value=self.workbench._state(0, TIME))
        terminated = []
        def terminate(row, deadline):
            terminated.append(row["ProcessId"])
            self.rows = [raw for raw in self.rows if raw["ProcessId"] != row["ProcessId"]]
        self.workbench._terminate_client = terminate
        def wait(timeout):
            self.rows = [raw for raw in self.rows if raw["ProcessId"] != 20]
            return 1
        self.gui.wait.side_effect = wait
        with patch("session_panel.sessions.workbench.process_times", side_effect=[(created_ticks(GUI_TIME), 0), (created_ticks(GUI_TIME), created_ticks(CHILD_TIME) + 10000000)]):
            result = self.workbench.connect()
        self.assertEqual(result["cleanup"], "verified"); self.assertFalse(result["connected"])
        self.assertEqual(terminated, [30]); self.gui.terminate.assert_called_once()
        self.assertEqual([row["ProcessId"] for row in self.rows], [10, 40])

    def test_cleanup_unknown_child_and_pid_reuse_do_not_kill_and_latch(self):
        self.attach(); self.rows[-1]["CreatedUtc"] = TIME
        owned = {"process": self.gui, "created": created_ticks(GUI_TIME)}
        self.workbench._terminate_client = Mock()
        self.assertEqual(self.workbench._cleanup_owned(owned, 3), "unverified")
        self.workbench._terminate_client.assert_not_called()
        dll = Mock(); dll.OpenProcess.return_value = 333
        row = self.workbench._rows(3)[-1]
        with patch("session_panel.sessions.workbench.kernel32", return_value=dll), patch("session_panel.sessions.workbench.process_times", return_value=(row["_ticks"] + 10, 0)):
            with self.assertRaises(ValueError):
                self.workbench._terminate_client = Workbench._terminate_client.__get__(self.workbench)
                self.workbench._terminate_client(row, 3)
        dll.TerminateProcess.assert_not_called(); dll.CloseHandle.assert_called_once_with(333)

    def test_indirect_new_attach_is_not_killed_or_claimed_cleaned(self):
        self.attach(); self.rows[-1]["ParentProcessId"] = 99
        owned = {"process": self.gui, "created": created_ticks(GUI_TIME)}
        self.workbench._terminate_client = Mock()
        with patch("session_panel.sessions.workbench.process_times", return_value=(created_ticks(GUI_TIME), created_ticks(CHILD_TIME) + 10)):
            self.assertEqual(self.workbench._cleanup_owned(owned, 3), "unverified")
        self.workbench._terminate_client.assert_not_called()

    def test_native_handle_creation_outside_gui_interval_is_not_terminated(self):
        self.attach(); row = self.workbench._rows(3)[-1]
        row.update(_gui_created=created_ticks(GUI_TIME), _gui_exited=row["_ticks"])
        dll = Mock(); dll.OpenProcess.return_value = 333
        with patch("session_panel.sessions.workbench.kernel32", return_value=dll), patch("session_panel.sessions.workbench.process_times", return_value=(row["_ticks"] + 1, 0)):
            with self.assertRaises(ValueError):
                self.workbench._terminate_client(row, 3)
        dll.TerminateProcess.assert_not_called(); dll.CloseHandle.assert_called_once_with(333)

    def test_cleanup_failure_latches_future_actions(self):
        self.workbench._wait_connected = Mock(return_value=self.workbench._state(0, TIME))
        self.workbench._cleanup_owned = Mock(return_value="unverified")
        result = self.workbench.connect()
        self.assertEqual((result["reason"], result["cleanup"]), ("cleanup_unverified", "unverified"))
        self.assertTrue(self.workbench.cleanup_latch)
        self.assertEqual(self.workbench.connect()["reason"], "cleanup_unverified")
        self.assertFalse(self.workbench.public_status()["can_focus"])

    def test_child_born_before_gui_exit_is_reclaimed_after_second_sweep(self):
        self.attach(); self.rows = [raw for raw in self.rows if raw["ProcessId"] != 30]
        terminated = []
        def terminate(row, deadline):
            terminated.append(row["ProcessId"])
            self.rows = [raw for raw in self.rows if raw["ProcessId"] != row["ProcessId"]]
        self.workbench._terminate_client = terminate
        def wait(timeout):
            self.rows = [raw for raw in self.rows if raw["ProcessId"] != 20]
            self.rows.append(process(30, 20, [ZELLIJ, "attach"], CHILD_TIME))
        self.gui.wait.side_effect = wait
        owned = {"process": self.gui, "created": created_ticks(GUI_TIME)}
        with patch("session_panel.sessions.workbench.process_times", return_value=(created_ticks(GUI_TIME), created_ticks(CHILD_TIME) + 10)):
            self.assertEqual(self.workbench._cleanup_owned(owned, 3), "verified")
        self.assertEqual(terminated, [30])


class WorkbenchConfigurationTests(unittest.TestCase):
    def test_pin_case_does_not_change_digest_and_roles_map_to_actual_paths(self):
        config = copy.deepcopy(WORKBENCH)
        digest = hashlib.sha256(b"synthetic-file").hexdigest()
        for role in config["pins"]:
            config["pins"][role] = digest.upper() if role == "wezterm_lua" else digest
        workbench = Workbench(config=config, terminal_config=TERMINAL)
        with patch("builtins.open", side_effect=lambda path, mode: io.BytesIO(b"synthetic-file")) as read:
            self.assertTrue(workbench.integrity(workbench.clock() + 5))
        self.assertEqual({call.args[0] for call in read.call_args_list},
                         {TERMINAL["zellij"], WORKBENCH["wezterm_gui"], WORKBENCH["wezterm_lua"], WORKBENCH["zellij_config"]})

    def test_missing_or_disabled_configuration_has_zero_tool_or_file_probes(self):
        for config, terminal in ((None, None), ({**WORKBENCH, "enabled": False}, TERMINAL), (WORKBENCH, None)):
            runner, provider, popen = Mock(), Mock(), Mock()
            workbench = Workbench(runner=runner, provider=provider, popen=popen, config=config, terminal_config=terminal)
            with patch("builtins.open") as read:
                self.assertEqual(workbench.observe()["reason"], "not_configured")
                self.assertEqual(workbench.connect()["reason"], "not_configured")
                self.assertFalse(workbench.integrity(10))
                read.assert_not_called()
            runner.assert_not_called(); provider.assert_not_called(); popen.assert_not_called()

    def test_all_four_pin_roles_required_before_file_access(self):
        for missing in (None, *PINS):
            config = copy.deepcopy(WORKBENCH)
            if missing is None:
                config["pins"] = {}
            else:
                del config["pins"][missing]
            workbench = Workbench(config=config, terminal_config=TERMINAL)
            with patch("builtins.open") as read:
                self.assertFalse(workbench.integrity(10))
                read.assert_not_called()

    def test_independent_snapshots_reject_other_session_processes(self):
        config, terminal = copy.deepcopy(WORKBENCH), copy.deepcopy(TERMINAL)
        first = Workbench(config=config, terminal_config=terminal)
        terminal["session"] = "second-session"
        config["wezterm_gui"] = "C:/example/second/wezterm-gui.exe"
        second = Workbench(config=config, terminal_config=terminal)
        self.assertNotEqual(first.marker, second.marker)
        self.assertNotEqual(first.new_gui, second.new_gui)
        raw = process(10, 1, [ZELLIJ, "--server", first.marker])
        first.provider = lambda deadline: [raw]
        rows = first._rows(first.clock() + 5)
        self.assertEqual(len(first._classify(rows)[0]), 1)
        with self.assertRaises(ValueError):
            second._classify(rows)


class WorkbenchParserTests(unittest.TestCase):
    def test_windows_argv_quotes_unicode_and_exact_profiles(self):
        argv = ["C:/example/软件/wezterm/wezterm-mux-server.exe", "a b", 'a"b']
        self.assertEqual(command_line_argv(subprocess.list2cmdline(argv)), argv)

    def test_sessions_reject_duplicate_bad_age_ansi_and_empty(self):
        for text in ("", "fixture-session [Created yesterday ago]", "x [Created 1s ago]\nx [Created 2s ago]", "\x1b[32mx [Created 1s ago]"):
            self.assertIsNone(parse_sessions(text))

    def test_clients_zero_is_distinct_from_invalid_duplicate_and_out_of_range(self):
        self.assertEqual(parse_clients(HEADER), [])
        for text in ("", "bad", HEADER + "1 terminal_2 cmd\n01 terminal_3 cmd", HEADER + "65536 terminal_2 cmd", HEADER + "1 unknown cmd"):
            self.assertIsNone(parse_clients(text))


if __name__ == "__main__":
    unittest.main()
