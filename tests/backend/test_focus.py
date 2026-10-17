import copy
import json
import subprocess
import threading
import unittest
from unittest.mock import Mock

from tests.fixtures.integration_config import integration_config
from control_everything.sessions.service import SessionService
from control_everything.sessions.terminal import Terminal, _focus_candidates, parse_clients, parse_slot_command
from tests.backend.test_gateway import SESSION_ID
from tests.backend.test_observation import registration, remote


CONFIG = integration_config()["integrations"]
TERMINAL = CONFIG["terminal"]
REMOTE = CONFIG["remote"]
ZELLIJ, ZELLIJ_SESSION = TERMINAL["zellij"], TERMINAL["session"]


def pane(pane_id=2, slot="main", **changes):
    return {"id": pane_id, "is_plugin": False, "tab_name": TERMINAL["tab"],
            "terminal_command": f"ssh.exe -t {REMOTE['source']['host']} bash {TERMINAL['pane_script']} {slot}", **changes}


class FakeWorkbench:
    def __init__(self, owner):
        self.owner = owner
        self.action_lock = threading.Lock()
        self.cleanup_latch = False
        self.integrity = Mock(return_value=True)
        self.observations = 0

    def observe(self, deadline=None):
        self.observations += 1
        clients = parse_clients(self.owner.clients)
        valid = clients is not None and len(clients) == 1
        return {"can_focus": valid, "reason": None if valid else "workbench_association_unknown",
                "_server": "server", "_guis": "gui", "_attaches": "attach", "_mux": "mux",
                "_client_ids": tuple(row["client_id"] for row in clients or []), "_clients": clients}


class FocusTests(unittest.TestCase):
    def setUp(self):
        self.panes = [pane()]; self.after_panes = None
        self.clients = "CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n1 terminal_2 ssh.exe\n"
        self.focus_exit = 0; self.calls = []; self.inventory_count = 0
        self.now = 1.0; self.inventory_delay = 0
        self.record = {"session_id": SESSION_ID, "terminal_binding": "confirmed", "identity_state": "verified",
                       "instance_key": "fixed-source:boot:123:456", "slot": "main", "pane_id": "terminal_2", "age_ms": 10}
        self.service = Mock(); self.service.get_session_status.side_effect = lambda _id: copy.deepcopy(self.record)
        self.fresh = copy.deepcopy(self.record)
        def refresh(force=False, _include_inventory=False):
            value = {"sessions": [copy.deepcopy(self.fresh)]}
            if _include_inventory:
                started = self.now
                inventory = self.terminal.list_panes()
                value["sessions"][0]["age_ms"] += (self.now-started)*1000
                return value, inventory
            return value
        self.service.refresh_observation.side_effect = refresh
        self.workbench = FakeWorkbench(self)
        self.terminal = Terminal(runner=self.run_command, clock=lambda: self.now, workbench=self.workbench, config=TERMINAL, remote_config=REMOTE)

    def run_command(self, argv, **kwargs):
        self.calls.append(argv)
        self.assertFalse(kwargs.get("shell", False))
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, "zellij 0.45.1\n", "")
        action = argv[4]
        if action == "list-panes":
            self.now += self.inventory_delay
            self.inventory_count += 1
            values = self.after_panes if self.inventory_count > 1 and self.after_panes is not None else self.panes
            return subprocess.CompletedProcess(argv, 0, json.dumps(values), "")
        if action == "focus-pane-id":
            if getattr(self, "clients_after_action", None) is not None:
                self.clients = self.clients_after_action
            return subprocess.CompletedProcess(argv, self.focus_exit, "", "")
        if action == "list-clients":
            return subprocess.CompletedProcess(argv, 0, self.clients, "")
        raise AssertionError(argv)

    def focus_calls(self):
        return [args for args in self.calls if "focus-pane-id" in args]

    def test_precise_argv_and_text_clients_confirm_existing_pane(self):
        result = self.terminal.focus_session(self.service, SESSION_ID)
        self.assertTrue(result["focused"]); self.assertEqual(result["pane_id"], "terminal_2")
        self.assertEqual(self.focus_calls(), [[ZELLIJ, "--session", ZELLIJ_SESSION, "action", "focus-pane-id", "terminal_2"]])
        self.assertEqual(self.workbench.observations, 3); self.assertEqual(self.inventory_count, 2)
        self.service.refresh_observation.assert_called_once_with(force=True, _include_inventory=True)

    def test_stale_instance_pane_and_unknown_identity_never_focus(self):
        changes = [{"age_ms": 5001}, {"instance_key": "reused-pid"}, {"pane_id": "terminal_9"},
                   {"slot": "files"}, {"identity_state": "unknown"}, {"terminal_binding": "unknown"}, {"instance_key": None}]
        for change in changes:
            with self.subTest(change=change):
                self.calls.clear(); self.fresh = {**self.record, **change}
                result = self.terminal.focus_session(self.service, SESSION_ID)
                self.assertFalse(result["focused"]); self.assertEqual(result["reason"], "binding_unknown")
                self.assertEqual(self.focus_calls(), [])

    def test_duplicate_null_plugin_or_rebuilt_pane_never_focus(self):
        candidates = [[pane(), pane(3)], [pane(terminal_command=None)], [pane(is_plugin=True)], [pane(9)], [pane(terminal_command=pane()["terminal_command"] + "; extra-command")]]
        for values in candidates:
            with self.subTest(values=values):
                self.calls.clear(); self.panes = values
                result = self.terminal.focus_session(self.service, SESSION_ID)
                self.assertEqual(result["reason"], "binding_unknown"); self.assertEqual(self.focus_calls(), [])

    def test_invalid_zero_and_multiple_clients_reject_before_action(self):
        for text in ("invalid header", "CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n",
                     "CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n1 terminal_2 cmd\n2 terminal_2 cmd\n"):
            with self.subTest(text=text):
                self.clients = text
                result = self.terminal.focus_session(self.service, SESSION_ID)
                self.assertFalse(result["focused"]); self.assertEqual(result["reason"], "workbench_association_unknown")
                self.assertEqual(self.focus_calls(), [])

    def test_client_disappears_changes_id_or_wrong_pane_after_action_is_unverified(self):
        for text in ("CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n", "CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n2 terminal_2 cmd\n",
                     "CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n1 plugin_2 cmd\n", "CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n1 terminal_3 cmd\n"):
            self.clients = "CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n1 terminal_2 cmd\n"
            self.clients_after_action = text
            self.assertEqual(self.terminal.focus_session(self.service, SESSION_ID)["reason"], "focus_sent_unverified")

    def test_rebuild_after_action_is_unverified_and_failure_exit_preserved(self):
        self.after_panes = [pane(9)]
        result = self.terminal.focus_session(self.service, SESSION_ID)
        self.assertFalse(result["focused"]); self.assertEqual(result["reason"], "focus_sent_unverified")
        self.after_panes = None; self.inventory_count = 0
        self.focus_exit = 2
        result = self.terminal.focus_session(self.service, SESSION_ID)
        self.assertEqual(result["reason"], "focus_failed"); self.assertEqual(result["exit_code"], 2)

    def test_inventory_budget_can_expire_before_submission(self):
        self.inventory_delay = 6
        result = self.terminal.focus_session(self.service, SESSION_ID)
        self.assertEqual(result["reason"], "binding_unknown"); self.assertEqual(self.focus_calls(), [])

    def test_action_lock_cleanup_latch_and_integrity_failure_never_focus(self):
        self.workbench.action_lock.acquire()
        try:
            self.assertEqual(self.terminal.focus_session(self.service, SESSION_ID)["reason"], "terminal_action_busy")
        finally:
            self.workbench.action_lock.release()
        self.workbench.cleanup_latch = True
        self.assertEqual(self.terminal.focus_session(self.service, SESSION_ID)["reason"], "cleanup_unverified")
        self.workbench.cleanup_latch = False; self.workbench.integrity.return_value = False
        self.assertEqual(self.terminal.focus_session(self.service, SESSION_ID)["reason"], "workbench_integrity_failed")
        self.assertEqual(self.focus_calls(), [])

    def test_refresh_list_panes_does_not_reacquire_action_lock(self):
        def refresh(force=False, _include_inventory=False):
            self.assertTrue(self.workbench.action_lock.locked())
            inventory = self.terminal.list_panes()
            self.assertTrue(inventory["ok"])
            value = {"sessions": [copy.deepcopy(self.fresh)]}
            return (value, inventory) if _include_inventory else value
        self.service.refresh_observation.side_effect = refresh
        self.assertTrue(self.terminal.focus_session(self.service, SESSION_ID)["focused"])

    def test_nonzero_final_inventory_retains_original_unverified_semantics(self):
        original = self.terminal.runner
        def run(argv, **kwargs):
            if argv[-1] == "--version" and self.inventory_count == 1:
                return subprocess.CompletedProcess(argv, 9, "", "")
            return original(argv, **kwargs)
        self.terminal.runner = run
        result = self.terminal.focus_session(self.service, SESSION_ID)
        self.assertEqual(result["reason"], "focus_sent_unverified")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(len(self.focus_calls()), 1)

    def test_final_sampling_overlaps_and_joins_before_lock_release(self):
        inventory_started = threading.Event()
        overlap, workers, alive_at_release = [], [], []
        original_observe, original_inventory = self.workbench.observe, self.terminal.list_panes
        def observe(deadline=None):
            if self.workbench.observations == 2:
                overlap.append(inventory_started.wait(1))
            return original_observe(deadline)
        def inventory(deadline=None):
            if self.inventory_count == 1:
                workers.append(threading.current_thread())
                inventory_started.set()
            return original_inventory(deadline)
        original_lock = self.workbench.action_lock
        self.workbench.action_lock = Mock(wraps=original_lock)
        def release():
            alive_at_release.append(any(worker.is_alive() for worker in workers))
            original_lock.release()
        self.workbench.action_lock.release.side_effect = release
        self.workbench.observe, self.terminal.list_panes = observe, inventory
        result = self.terminal.focus_session(self.service, SESSION_ID)
        self.assertEqual(overlap, [True], "complete final samples must overlap")
        self.assertEqual(alive_at_release, [False], "worker must exit before lock release")
        self.assertTrue(result["focused"])
        self.assertFalse(original_lock.locked())

    def test_final_errors_and_late_samples_join_before_unlock(self):
        modes = ("post_oserror", "post_timeout", "post_unicode", "pane_oserror", "pane_timeout",
                 "pane_unicode", "age", "deadline", "unexpected")
        for mode in modes:
            with self.subTest(mode=mode):
                self.setUp()
                if mode == "age": self.fresh["age_ms"] = 4990
                started, release = threading.Event(), threading.Event()
                workers, alive_at_release = [], []
                original_observe, original_inventory = self.workbench.observe, self.terminal.list_panes
                def failure():
                    if mode.endswith("timeout"): raise subprocess.TimeoutExpired("sample", 1)
                    if mode.endswith("unicode"): raise UnicodeError("sample")
                    raise OSError("sample")
                def inventory(deadline=None):
                    if self.inventory_count == 1:
                        workers.append(threading.current_thread()); started.set()
                        if not release.wait(2): raise RuntimeError("test release missing")
                        if mode.startswith("pane_"): failure()
                        if mode == "age": self.now += .02
                        if mode == "deadline": self.now += 6
                    return original_inventory(deadline)
                def observe(deadline=None):
                    if self.workbench.observations != 2: return original_observe(deadline)
                    try:
                        self.assertTrue(started.wait(1))
                        if mode.startswith("post_"): failure()
                        if mode == "unexpected": raise RuntimeError("unexpected sample error")
                        return original_observe(deadline)
                    finally: release.set()
                original_lock = self.workbench.action_lock
                self.workbench.action_lock = Mock(wraps=original_lock)
                def unlock():
                    alive_at_release.append(any(worker.is_alive() for worker in workers))
                    original_lock.release()
                self.workbench.action_lock.release.side_effect = unlock
                self.workbench.observe, self.terminal.list_panes = observe, inventory
                if mode == "unexpected":
                    with self.assertRaisesRegex(RuntimeError, "unexpected sample error"):
                        self.terminal.focus_session(self.service, SESSION_ID)
                else:
                    result = self.terminal.focus_session(self.service, SESSION_ID)
                    self.assertFalse(result["focused"])
                    self.assertEqual(result["reason"], "focus_sent_unverified")
                self.assertEqual(alive_at_release, [False]); self.assertFalse(original_lock.locked())

    def test_final_pending_worker_holds_action_lock_until_exit(self):
        started, release = threading.Event(), threading.Event()
        workers, results, errors = [], [], []
        original_inventory = self.terminal.list_panes
        def inventory(deadline=None):
            if self.inventory_count == 1:
                workers.append(threading.current_thread()); started.set()
                if not release.wait(2): raise RuntimeError("test release missing")
            return original_inventory(deadline)
        self.terminal.list_panes = inventory
        def focus():
            try: results.append(self.terminal.focus_session(self.service, SESSION_ID))
            except Exception as error: errors.append(error)
        caller = threading.Thread(target=focus)
        caller.start()
        try:
            self.assertTrue(started.wait(1))
            self.assertFalse(self.workbench.action_lock.acquire(blocking=False))
            self.assertEqual(results, [])
        finally:
            release.set(); caller.join(2)
        self.assertFalse(caller.is_alive()); self.assertEqual(errors, [])
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertTrue(results[0]["focused"])
        self.assertFalse(self.workbench.action_lock.locked())


def fallback_pane(pane_id=3, slot="dev", **changes):
    return pane(pane_id, slot, tab_id=1, is_selectable=True, is_floating=False,
                is_suppressed=False, is_fullscreen=False, exited=False, is_held=False,
                pane_x=0, pane_y=1, pane_rows=40, pane_columns=48, **changes)


class FocusFallbackTests(unittest.TestCase):
    setUp_base = FocusTests.setUp

    def setUp(self):
        self.setUp_base()
        self.record.update(slot="dev", pane_id="terminal_3")
        self.fresh = copy.deepcopy(self.record)
        self.panes = [fallback_pane(2, "main"), fallback_pane(), fallback_pane(4, "files"),
                      {**fallback_pane(5), "terminal_command": None},
                      {**fallback_pane(2), "is_plugin": True, "is_selectable": False, "terminal_command": None},
                      {**fallback_pane(8), "tab_id": 4, "tab_name": "Music", "terminal_command": "player"}]
        self.panes[0]["is_held"] = self.panes[2]["is_held"] = True
        for row, geometry in zip(self.panes[1:5], ((48, 1, 18, 102), (48, 19, 22, 69),
                                                 (117, 19, 22, 33), (0, 0, 1, 150))):
            row.update(zip(("pane_x", "pane_y", "pane_rows", "pane_columns"), geometry))
        self.clients = self.client_text(8)
        self.moves = {"go-to-tab-by-id": [2], "focus-next-pane": [3]}
        self.action_exits = {}; self.action_delays = {}; self.action_errors = {}
        self.inventory_at_counts = {}; self.client_read_exit = 0
        self.version_count = 0; self.version_exits = {}; self.version_texts = {}

    @staticmethod
    def client_text(pane_id, client_id=1):
        return f"CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n{client_id} terminal_{pane_id} cmd\n"

    def run_command(self, argv, **kwargs):
        if argv[-1] == "--version":
            self.version_count += 1
            if self.version_count in self.version_exits or self.version_count in self.version_texts:
                self.calls.append(argv)
                return subprocess.CompletedProcess(argv, self.version_exits.get(self.version_count, 0),
                                                   self.version_texts.get(self.version_count, ""), "")
        action = argv[4] if len(argv) > 4 else None
        if action == "list-panes" and self.inventory_count + 1 in self.inventory_at_counts:
            self.after_panes = self.inventory_at_counts[self.inventory_count + 1]
        if action == "list-clients" and self.client_read_exit:
            self.calls.append(argv)
            return subprocess.CompletedProcess(argv, self.client_read_exit, "", "")
        if action in ("go-to-tab-by-id", "focus-next-pane", "move-focus"):
            self.calls.append(argv)
            self.assertGreater(kwargs["timeout"], 0)
            if action in self.action_errors:
                raise self.action_errors[action]
            self.now += self.action_delays.get(action, 0)
            sequence = self.moves.get(action, [])
            if sequence:
                value = sequence.pop(0)
                self.clients = value if isinstance(value, str) else self.client_text(value)
            return subprocess.CompletedProcess(argv, self.action_exits.get(action, 0), "", "")
        return FocusTests.run_command(self, argv, **kwargs)

    def actions(self):
        return [args[4:] for args in self.calls if len(args) > 4 and args[4] in
                ("focus-pane-id", "go-to-tab-by-id", "focus-next-pane", "move-focus")]

    def test_actual_service_wrong_panes_and_saved_wrong_tab_keep_original_budget(self):
        for current in (4, 5, 8):
            with self.subTest(current=current):
                self.setUp(); self.clients = self.client_text(current)
                self.moves = {"go-to-tab-by-id": [current if current != 8 else 5],
                              "focus-next-pane": [5, 2, 3] if current == 4 else [2, 3],
                              "move-focus": [3]}
                original_run, original_observe = self.terminal.runner, self.workbench.observe
                def run(argv, **kwargs):
                    action = argv[4] if len(argv) > 4 else "--version"
                    self.now += {"--version": .031, "list-clients": .188,
                                 "go-to-tab-by-id": .046, "focus-next-pane": .046,
                                 "move-focus": .046}.get(action, 0)
                    if action == "list-panes": self.now += .391 if self.inventory_count == 0 else .516
                    return original_run(argv, **kwargs)
                def observe(deadline=None):
                    self.now += .922 if self.workbench.observations == 0 else (.734 if self.workbench.observations == 1 else .657)
                    return original_observe(deadline)
                source = Mock()
                source.source = copy.deepcopy(REMOTE["source"])
                source.config = copy.deepcopy(REMOTE)
                def source_observe():
                    self.now += .781
                    return remote(registration(SESSION_ID, "dev"))
                source.observe_source.side_effect = source_observe
                service = SessionService(source, self.terminal, clock=lambda: self.now, utc_now=lambda: "sample")
                self.terminal.runner = run
                service.refresh_observation(force=True)
                self.calls.clear(); self.inventory_count = 0
                force_finished, force_ages, deadlines = [], [], []
                original_refresh = service.refresh_observation
                def refresh(*args, **kwargs):
                    value = original_refresh(*args, **kwargs)
                    if kwargs.get("force"):
                        force_finished.append(self.now)
                        force_ages.append((value[0] if isinstance(value, tuple) else value)["age_ms"])
                    return value
                def integrity(deadline):
                    deadlines.append(deadline); self.now += .203; return True
                service.refresh_observation = refresh
                self.workbench.observe = observe; self.workbench.integrity.side_effect = integrity
                result = self.terminal.focus_session(service, SESSION_ID)
                self.assertTrue(result["focused"], result)
                self.assertEqual(result["pane_id"], "terminal_3")
                self.assertEqual(self.actions(), ([["go-to-tab-by-id", "1"]] if current == 8 else []) + [["move-focus", "up"]])
                self.assertEqual(self.workbench.observations, 3)
                self.assertEqual(self.inventory_count, 4 if current == 8 else 3)
                self.assertEqual(source.observe_source.call_count, 2)
                self.assertGreaterEqual(force_ages[0], 1202)
                self.assertLessEqual(force_ages[0] + (self.now-force_finished[0])*1000, 5000)
                self.assertEqual(deadlines[0], force_finished[0]+5)
                self.assertLess(self.now, deadlines[0])

    def test_strict_route_completes_current_evidence_within_original_budget(self):
        self.fresh["age_ms"] = 1593
        self.moves = {"go-to-tab-by-id": [3], "focus-next-pane": []}
        refreshed_at, deadlines = [], []
        def refresh(force=False, _include_inventory=False):
            self.now += 1.172
            inventory = self.terminal.list_panes()
            refreshed_at.append(self.now)
            value = {"sessions": [copy.deepcopy(self.fresh)]}
            return (value, inventory) if _include_inventory else value
        self.service.refresh_observation.side_effect = refresh
        original_observe, original_run = self.workbench.observe, self.terminal.runner
        def observe(deadline=None):
            self.now += .650
            return original_observe(deadline)
        def run(argv, **kwargs):
            action = argv[4] if len(argv) > 4 else "--version"
            self.now += {"--version": .031, "list-panes": .391, "list-clients": .188,
                         "focus-pane-id": .046, "go-to-tab-by-id": .046}.get(action, 0)
            return original_run(argv, **kwargs)
        def integrity(deadline):
            deadlines.append(deadline)
            self.now += .170
            return True
        self.workbench.observe = observe
        self.workbench.integrity.side_effect = integrity
        self.terminal.runner = run
        result = self.terminal.focus_session(self.service, SESSION_ID)
        self.assertTrue(result["focused"], result)
        self.assertEqual(self.actions(), [["go-to-tab-by-id", "1"]])
        self.assertEqual(self.workbench.observations, 3)
        self.assertLessEqual(self.fresh["age_ms"] + (self.now - refreshed_at[0]) * 1000, 5000)
        self.assertLess(self.now, deadlines[0])

    def test_new_client_uses_tab_then_reaches_exact_pane(self):
        result = self.terminal.focus_session(self.service, SESSION_ID)
        self.assertTrue(result["focused"])
        self.assertEqual(result["pane_id"], "terminal_3")
        self.assertEqual(self.actions(), [["go-to-tab-by-id", "1"], ["focus-next-pane"]])
        self.assertEqual(parse_clients(self.clients), [{"client_id": "1", "pane_id": "terminal_3"}])

    def test_same_tab_uses_real_pre_pane_and_already_target_keeps_full_final(self):
        for current, action in ((2, ["focus-next-pane"]), (4, ["move-focus", "up"]),
                                (5, ["move-focus", "up"]), (3, None)):
            with self.subTest(current=current):
                self.setUp(); self.clients = self.client_text(current)
                self.moves = {"focus-next-pane": [3], "move-focus": [3]}
                result = self.terminal.focus_session(self.service, SESSION_ID)
                self.assertTrue(result["focused"], result)
                self.assertEqual(self.actions(), [action] if action else [])
                self.assertEqual(self.workbench.observations, 3)
                self.assertEqual(self.inventory_count, 3 if action else 2)

    def test_unique_up_is_geometric_not_id_or_inventory_order(self):
        self.clients = self.client_text(9)
        for row in self.panes:
            if row["is_plugin"] is False: row["id"] = {2:42, 3:31, 4:9, 5:71, 8:8}[row["id"]]
        self.panes.reverse(); self.record["pane_id"] = self.fresh["pane_id"] = "terminal_31"
        self.moves = {"move-focus": [31]}
        result = self.terminal.focus_session(self.service, SESSION_ID)
        self.assertTrue(result["focused"])
        self.assertEqual(result["pane_id"], "terminal_31")
        self.assertEqual(self.actions(), [["move-focus", "up"]])

    def test_up_ambiguous_or_not_adjacent_keeps_original_bounded_next(self):
        for ambiguous in (False, True):
            with self.subTest(ambiguous=ambiguous):
                self.setUp(); self.clients = self.client_text(4)
                if ambiguous:
                    self.panes[1]["pane_columns"] = 51
                    neighbor = {**fallback_pane(6), "terminal_command": None,
                                "pane_x":99, "pane_y":1, "pane_rows":18, "pane_columns":51}
                    self.panes.append(neighbor)
                else: self.panes[1]["pane_rows"] = 17
                self.moves = {"focus-next-pane": [5, 2, 3]}
                self.assertTrue(self.terminal.focus_session(self.service, SESSION_ID)["focused"])
                self.assertEqual(self.actions(), [["focus-next-pane"]]*3)

    def test_invalid_geometry_never_guesses_guided_actions(self):
        fields = ("pane_x", "pane_y", "pane_rows", "pane_columns")
        for field in fields:
            for value in (None, True, "1", -1, "missing") + ((0,) if field in fields[2:] else ()):
                with self.subTest(field=field, value=value):
                    self.setUp()
                    if value == "missing": self.panes[1].pop(field)
                    else: self.panes[1][field] = value
                    self.assertIsNone(_focus_candidates(self.panes, self.fresh, TERMINAL, REMOTE))
                    self.terminal.focus_session(self.service, SESSION_ID)
                    self.assertEqual(self.actions(), [["focus-pane-id", "terminal_3"]])
        self.setUp(); self.panes[2].update(pane_x=48, pane_y=1)
        self.assertIsNone(_focus_candidates(self.panes, self.fresh, TERMINAL, REMOTE))
        self.terminal.focus_session(self.service, SESSION_ID)
        self.assertEqual(self.actions(), [["focus-pane-id", "terminal_3"]])

    def test_up_wrong_noop_or_invalid_client_stops_without_next(self):
        bad = [4, 5, 2, 9, "bad header", "CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n",
               self.client_text(3, 2), self.client_text(3)+"2 terminal_3 cmd\n",
               "CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n1 plugin_2 cmd\n"]
        for value in bad:
            with self.subTest(value=value):
                self.setUp(); self.clients = self.client_text(4); self.moves = {"move-focus": [value]}
                result = self.terminal.focus_session(self.service, SESSION_ID)
                self.assertEqual(result["reason"], "focus_sent_unverified")
                self.assertEqual(self.actions(), [["move-focus", "up"]])

    def test_up_nonzero_timeout_and_late_action_keep_failure_semantics(self):
        for mode in ("exit", "client_exit", "version_exit", "inventory_exit", "timeout", "deadline", "age"):
            with self.subTest(mode=mode):
                self.setUp(); self.clients = self.client_text(4); self.moves = {"move-focus": [3]}
                if mode == "exit": self.action_exits["move-focus"] = 7
                elif mode == "client_exit": self.client_read_exit = 8
                elif mode == "version_exit": self.version_exits[2] = 9
                elif mode == "inventory_exit":
                    original = self.terminal.runner
                    def failed(argv, **kwargs):
                        if len(argv)>4 and argv[4]=="list-panes" and self.inventory_count==1:
                            return subprocess.CompletedProcess(argv, 11, "", "")
                        return original(argv, **kwargs)
                    self.terminal.runner = failed
                elif mode == "timeout": self.action_errors["move-focus"] = subprocess.TimeoutExpired("up", 1)
                elif mode == "deadline": self.action_delays["move-focus"] = 6
                else: self.fresh["age_ms"] = 4999; self.action_delays["move-focus"] = .01
                result = self.terminal.focus_session(self.service, SESSION_ID)
                self.assertEqual(result["reason"], "focus_failed" if mode.endswith("exit") else "focus_sent_unverified")
                if mode.endswith("exit"): self.assertEqual(result["exit_code"], {"exit":7,"client_exit":8,"version_exit":9,"inventory_exit":11}[mode])
                self.assertEqual(self.actions(), [["move-focus", "up"]])

    def test_geometry_changes_after_up_or_final_never_confirm(self):
        for count in (2, 3):
            for field in ("pane_x", "pane_y", "pane_rows", "pane_columns"):
                with self.subTest(count=count, field=field):
                    self.setUp(); self.clients = self.client_text(4); self.moves = {"move-focus": [3]}
                    changed = copy.deepcopy(self.panes); changed[1][field] += 1
                    self.inventory_at_counts[count] = changed
                    self.assertEqual(self.terminal.focus_session(self.service, SESSION_ID)["reason"], "focus_sent_unverified")
                    self.assertEqual(self.actions(), [["move-focus", "up"]])

    def test_zero_action_still_rejects_all_final_guards(self):
        for mode in ("_server", "_guis", "_attaches", "_mux", "_client_ids", "pane", "geometry", "age", "deadline"):
            with self.subTest(mode=mode):
                self.setUp(); self.clients = self.client_text(3)
                if mode == "age": self.fresh["age_ms"] = 4999
                if mode == "geometry":
                    changed = copy.deepcopy(self.panes); changed[1]["pane_rows"] += 1
                    self.inventory_at_counts[2] = changed
                original = self.workbench.observe
                def observe(deadline=None):
                    if self.workbench.observations == 2:
                        if mode == "pane": self.clients = self.client_text(5)
                        elif mode == "age": self.now += .01
                        elif mode == "deadline": self.now += 6
                    state = original(deadline)
                    if self.workbench.observations == 3 and mode.startswith("_"): state[mode] = "replacement"
                    return state
                self.workbench.observe = observe
                result = self.terminal.focus_session(self.service, SESSION_ID)
                self.assertEqual(result["reason"], "focus_sent_unverified")
                self.assertEqual(self.actions(), [])

    def test_tab_already_reaches_target_or_next_traverses_held_neighbors(self):
        for tab, moves in ((3, []), (4, [5, 2, 3])):
            with self.subTest(tab=tab):
                self.setUp(); self.moves = {"go-to-tab-by-id": [tab], "focus-next-pane": moves.copy()}
                self.panes[1]["pane_rows"] = 17  # No adjacent up target: retain original next traversal.
                self.assertTrue(self.terminal.focus_session(self.service, SESSION_ID)["focused"])
                self.assertEqual(sum(action == ["focus-next-pane"] for action in self.actions()), len(moves))
                self.assertFalse(any("write" in arg or "write-chars" in arg for args in self.calls for arg in args))

    def test_invalid_candidates_never_send_fallback_actions(self):
        changes = [{"tab_id": None}, {"tab_id": True}, {"tab_id": -1}, {"is_selectable": False}, {"is_selectable": 1},
                   {"is_held": True}, {"is_floating": True}, {"is_suppressed": True},
                   {"is_fullscreen": True}, {"exited": True}, {"is_plugin": True}, {"is_held": None}]
        for change in changes:
            with self.subTest(change=change):
                self.setUp(); self.panes[1].update(change)
                self.assertFalse(self.terminal.focus_session(self.service, SESSION_ID)["focused"])
                self.assertFalse(any(action[0] != "focus-pane-id" for action in self.actions()))
        for alter in (lambda: self.panes[4].update(is_selectable=True),
                      lambda: self.panes[0].update(tab_id=9),
                      lambda: self.panes[5].update(tab_id=1),
                      lambda: self.panes.append(copy.deepcopy(self.panes[0]))):
            with self.subTest(alter=alter):
                self.setUp(); alter()
                self.assertFalse(self.terminal.focus_session(self.service, SESSION_ID)["focused"])
                self.assertFalse(any(action[0] != "focus-pane-id" for action in self.actions()))

    def test_each_step_requires_same_single_real_client_and_known_focus(self):
        bad = ["bad header", "CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n", self.client_text(3, 2),
               self.client_text(3) + "2 terminal_3 cmd\n", self.client_text(9),
               "CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n1 plugin_2 cmd\n"]
        for action in ("go-to-tab-by-id", "focus-next-pane"):
            for clients in bad:
                with self.subTest(action=action, clients=clients):
                    self.setUp(); self.moves[action] = [clients]
                    self.assertEqual(self.terminal.focus_session(self.service, SESSION_ID)["reason"], "focus_sent_unverified")
                    self.assertEqual(len(self.actions()), 1 if action == "go-to-tab-by-id" else 2)

    def test_noop_cycle_and_nonzero_exit_stop_without_more_actions(self):
        for sequence in ([2], [4, 5, 2]):
            with self.subTest(sequence=sequence):
                self.setUp(); self.moves["focus-next-pane"] = sequence.copy()
                self.panes[1]["pane_rows"] = 17
                result = self.terminal.focus_session(self.service, SESSION_ID)
                self.assertEqual(result["reason"], "focus_sent_unverified")
                self.assertEqual(len(self.actions()), 1 + len(sequence))
        for action in ("go-to-tab-by-id", "focus-next-pane"):
            with self.subTest(action=action):
                self.setUp(); self.action_exits[action] = 7
                result = self.terminal.focus_session(self.service, SESSION_ID)
                self.assertEqual(result["reason"], "focus_failed"); self.assertEqual(result["exit_code"], 7)
        self.setUp(); self.client_read_exit = 8
        result = self.terminal.focus_session(self.service, SESSION_ID)
        self.assertEqual(result["reason"], "focus_failed"); self.assertEqual(result["exit_code"], 8)

    def test_candidate_changes_after_action_and_final_association_never_confirm(self):
        for change in ({"terminal_command": "rebuilt"}, {"is_held": False}, {"tab_id": 2}):
            with self.subTest(change=change):
                self.setUp(); self.after_panes = copy.deepcopy(self.panes)
                self.after_panes[0].update(change)
                result = self.terminal.focus_session(self.service, SESSION_ID)
                self.assertEqual(result["reason"], "focus_sent_unverified")
                self.assertEqual(len(self.actions()), 1)
        self.setUp()
        original = self.workbench.observe
        def changed(deadline=None):
            state = original(deadline)
            if self.workbench.observations >= 3:
                state["_guis"] = "replacement"
            return state
        self.workbench.observe = changed
        self.assertEqual(self.terminal.focus_session(self.service, SESSION_ID)["reason"], "focus_sent_unverified")

    def test_inventory_changes_mid_traversal_or_final_reject_exact_focus(self):
        for count in (2, 3, 4):
            for change in ("add", "remove", "command", "held"):
                with self.subTest(count=count, change=change):
                    self.setUp(); altered = copy.deepcopy(self.panes)
                    if change == "add": altered.append(fallback_pane(6))
                    elif change == "remove": del altered[3]
                    elif change == "command": altered[0]["terminal_command"] = "changed command"
                    else: altered[0]["is_held"] = False
                    self.inventory_at_counts[count] = altered
                    self.assertEqual(self.terminal.focus_session(self.service, SESSION_ID)["reason"], "focus_sent_unverified")
                    self.assertEqual(len(self.actions()), 1 if count == 2 else 2)

    def test_complete_final_guards_reject_after_strict_route(self):
        for key in ("_server", "_guis", "_attaches", "_mux", "_client_ids"):
            with self.subTest(key=key):
                self.setUp(); original = self.workbench.observe
                def changed(deadline=None):
                    state = original(deadline)
                    if self.workbench.observations == 3: state[key] = "replacement"
                    return state
                self.workbench.observe = changed
                self.assertEqual(self.terminal.focus_session(self.service, SESSION_ID)["reason"], "focus_sent_unverified")
                self.assertEqual(len(self.actions()), 2)

    def test_nonzero_fallback_version_exit_is_preserved_without_more_focus(self):
        for count in (2, 4):
            with self.subTest(version_call=count):
                self.setUp(); self.version_exits[count] = 9
                result = self.terminal.focus_session(self.service, SESSION_ID)
                self.assertEqual(result["reason"], "focus_failed")
                self.assertEqual(result["exit_code"], 9)
                self.assertEqual(len(self.actions()), 1 if count == 2 else 2)

    def test_fallback_version_text_mismatch_stays_unverified_without_more_focus(self):
        for count in (2, 4):
            with self.subTest(version_call=count):
                self.setUp(); self.version_texts[count] = "zellij unexpected"
                result = self.terminal.focus_session(self.service, SESSION_ID)
                self.assertEqual(result["reason"], "focus_sent_unverified")
                self.assertEqual(len(self.actions()), 1 if count == 2 else 2)

    def test_fallback_budget_remote_age_and_timeout_never_extend(self):
        for mode in ("deadline", "age", "timeout"):
            with self.subTest(mode=mode):
                self.setUp()
                if mode == "deadline": self.action_delays["go-to-tab-by-id"] = 6
                elif mode == "age": self.fresh["age_ms"] = 4999; self.action_delays["go-to-tab-by-id"] = .01
                else: self.action_errors["go-to-tab-by-id"] = subprocess.TimeoutExpired("tab", 1)
                self.assertEqual(self.terminal.focus_session(self.service, SESSION_ID)["reason"], "focus_sent_unverified")
                self.assertEqual(len(self.actions()), 1)

    def test_complete_final_late_age_deadline_or_changed_real_pane_never_confirm(self):
        for mode in ("age", "deadline", "pane"):
            with self.subTest(mode=mode):
                self.setUp()
                if mode == "age": self.fresh["age_ms"] = 4999
                original = self.workbench.observe
                def changed(deadline=None):
                    if self.workbench.observations == 2:
                        if mode == "age": self.now += .01
                        elif mode == "deadline": self.now += 6
                        else: self.clients = self.client_text(5)
                    return original(deadline)
                self.workbench.observe = changed
                self.assertEqual(self.terminal.focus_session(self.service, SESSION_ID)["reason"], "focus_sent_unverified")
                self.assertEqual(self.actions(), [["go-to-tab-by-id", "1"], ["focus-next-pane"]])


class TerminalConfigurationTests(unittest.TestCase):
    def test_unconfigured_and_disabled_never_probe_or_construct_workbench(self):
        for config in (None, {**TERMINAL, "enabled": False}):
            runner = Mock()
            terminal = Terminal(runner=runner, config=config, remote_config=REMOTE)
            self.assertIsNone(terminal.workbench)
            self.assertEqual(terminal.list_panes()["reason"], "not_configured")
            self.assertEqual(terminal.focus_session(Mock(), SESSION_ID)["reason"], "not_configured")
            self.assertEqual(terminal.connect_workbench()["reason"], "not_configured")
            runner.assert_not_called()

    def test_observation_without_workbench_and_two_snapshots(self):
        config, remote_config = copy.deepcopy(TERMINAL), copy.deepcopy(REMOTE)
        runner = Mock(side_effect=[subprocess.CompletedProcess([], 0, "zellij 0.45.1", ""),
                                  subprocess.CompletedProcess([], 0, "[]", "")])
        first = Terminal(runner=runner, config=config, remote_config=remote_config)
        config["session"] = "second-session"
        remote_config["source"]["host"] = "second.invalid"
        second = Terminal(config=config, remote_config=remote_config)
        self.assertTrue(first.list_panes()["ok"])
        self.assertEqual(runner.call_args.args[0][2], TERMINAL["session"])
        self.assertNotEqual(first.source, second.source)
        self.assertEqual(first.focus_session(Mock(), SESSION_ID)["reason"], "capability_unavailable")
        self.assertEqual(first.connect_workbench()["reason"], "capability_unavailable")
        self.assertEqual(parse_slot_command(pane(), first.config, first.remote_config), "main")
        self.assertIsNone(parse_slot_command(pane(), second.config, second.remote_config))

    def test_slot_matching_uses_exact_registered_literals(self):
        for change in ({"tab_name": "wrong"}, {"terminal_command": pane()["terminal_command"] + " extra"},
                       {"terminal_command": pane()["terminal_command"].replace("main", "unknown")},
                       {"terminal_command": pane()["terminal_command"].replace("example.invalid", "exampleXinvalid")}):
            self.assertIsNone(parse_slot_command(pane(**change), TERMINAL, REMOTE))
        config = copy.deepcopy(TERMINAL)
        config["ssh_executables"] = ["C:/example/工具 目录/ssh.exe"]
        suffix = f" -t {REMOTE['source']['host']} bash {config['pane_script']} main"
        command = '"' + config["ssh_executables"][0] + '"' + suffix
        self.assertEqual(parse_slot_command(pane(terminal_command=command), config, REMOTE), "main")
        self.assertIsNone(parse_slot_command(pane(terminal_command=command.replace('"', '')), config, REMOTE))


if __name__ == "__main__":
    unittest.main()
