"""Request-local workspace contracts over synthetic profiles, without listeners."""

import copy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from session_panel.profiles import observation_settings
from session_panel.profiles.profile import Profile
from session_panel.sessions.codex_index import unavailable
from session_panel.workspace.observations import scope_roots
from session_panel.workspace.service import WorkspaceService
from session_panel.workspace.store import WorkspaceError
from tests.backend.test_observation_settings import registration, save
from tests.backend.test_observer_protocol import sample
from tests.backend.test_project_management import project_body


class WorkspaceRequestReuseTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile = Profile.open(str(self.root / "profile"))
        self.store = self.profile.store
        self.workspace = WorkspaceService(profile=self.profile, quota_source=Mock(), news_source=Mock())
        self.session_value = unavailable(0)
        self.reader = Mock(side_effect=lambda: copy.deepcopy(self.session_value))
        self.workspace.session_reader = self.reader

    def project(self, name="project", **changes):
        return self.store.save_project(project_body(self.root / name, name=name, **changes))

    def task(self, project, title="Task", key=None):
        return self.store.save_task({"title": title, "project_id": project["id"],
            "provider_id": "codex" if key else None, "state": "todo", "session_key": key})

    def sessions_at(self, cwd, key=None):
        key = key or "codex:" + str(uuid4())
        return {**unavailable(0), "available": True, "reason": None, "total_visible": 1,
                "items": [{"session_key": key, "cwd": str(cwd)}]}

    def test_snapshot_reads_each_source_once_for_populated_and_empty_profiles(self):
        for populated in (False, True):
            with self.subTest(populated=populated):
                if populated:
                    project = self.project()
                    self.session_value = self.sessions_at(project["root"])
                    self.task(project, key=self.session_value["items"][0]["session_key"])
                self.reader.reset_mock()
                with patch.object(self.store, "list_projects", wraps=self.store.list_projects) as registry:
                    value = self.workspace.snapshot()
                self.assertEqual((self.reader.call_count, registry.call_count), (1, 1))
                self.assertEqual(value["sessions"], self.session_value)
                self.assertEqual(value["tasks"]["total"], int(populated))
                self.assertEqual(len(value["projects"]["items"]), int(populated))
                self.assertEqual(value["tasks"]["limit"], 50)
                if populated:
                    self.assertEqual(value["tasks"]["items"][0]["session_link_state"], "verified")
                    self.assertEqual(value["projects"]["items"][0]["sessions"][0]["session_key"],
                                     self.session_value["items"][0]["session_key"])

    def test_snapshot_uses_first_session_sample_for_tasks_and_project_hits(self):
        project = self.project()
        first = self.sessions_at(project["root"])
        self.task(project, key=first["items"][0]["session_key"])
        with patch.object(self.workspace, "session_reader", side_effect=[first, unavailable(0)]) as reader:
            value = self.workspace.snapshot()
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(value["tasks"]["items"][0]["session_link_state"], "verified")
        self.assertEqual(len(value["projects"]["items"][0]["sessions"]), 1)

    def test_task_pages_read_current_sources_and_preserve_filters_and_totals(self):
        project = self.project()
        for number in range(53):
            self.task(project, title="Task " + str(number))
        with patch.object(self.store, "list_tasks", wraps=self.store.list_tasks) as query:
            for offset, length in ((50, 3), (0, 5)):
                self.reader.reset_mock()
                with patch.object(self.store, "list_projects", wraps=self.store.list_projects) as registry:
                    page = self.workspace.page_tasks(offset=offset, limit=5,
                                                    project_id=project["id"], scope="active")
                query.assert_called_with(offset=offset, limit=5, project_id=project["id"], scope="active")
                self.assertEqual((self.reader.call_count, registry.call_count), (1, 1))
                self.assertEqual((page["total"], page["offset"], page["limit"], len(page["items"])),
                                 (53, offset, 5, length))
                self.assertTrue(all(row["session_link_state"] == "none" for row in page["items"]))
        self.store.save_project(project_body(project["root"], id=project["id"], archived=True))
        self.assertEqual(self.workspace.page_tasks(scope="active")["total"], 0)
        self.assertEqual(self.workspace.page_tasks(scope="archived")["total"], 53)

    def test_snapshot_link_states_and_next_request_observe_writes(self):
        parent = self.project()
        child = self.project("project/nested")
        self.session_value = self.sessions_at(child["root"])
        key = self.session_value["items"][0]["session_key"]
        expected = {}
        for project, title, session, state in (
                (child, "linked", key, "verified"), (parent, "mismatch", key, "project_mismatch"),
                (parent, "missing", "codex:" + str(uuid4()), "not_in_current_sample"),
                (parent, "unlinked", None, "none")):
            expected[self.task(project, title, session)["id"]] = state
        first = self.workspace.snapshot()
        self.assertEqual({row["id"]: row["session_link_state"] for row in first["tasks"]["items"]}, expected)
        previous = copy.deepcopy(first)
        self.task(child, "new task")
        self.store.save_project(project_body(self.root / "moved", id=child["id"], name="Moved"))
        current = self.workspace.snapshot()
        self.assertEqual(current["tasks"]["total"], 5)
        self.assertIn("Moved", [row["name"] for row in current["projects"]["items"]])
        linked = next(row for row in current["tasks"]["items"] if row["title"] == "linked")
        self.assertEqual(linked["session_link_state"], "project_mismatch")
        self.assertEqual(self.workspace.page_tasks()["total"], 5)
        self.assertEqual(first, previous)

    def test_unknown_and_failed_session_sources_stay_unknown_without_retry(self):
        project = self.project()
        self.task(project, key="codex:" + str(uuid4()))
        for response in (unavailable(0), RuntimeError("synthetic source unavailable")):
            with self.subTest(response=type(response).__name__):
                self.reader.reset_mock()
                self.reader.side_effect = response if isinstance(response, Exception) else lambda: copy.deepcopy(response)
                with patch.object(self.store, "list_projects", wraps=self.store.list_projects) as registry:
                    value = self.workspace.snapshot()
                self.assertEqual((self.reader.call_count, registry.call_count), (1, 1))
                self.assertFalse(value["sessions"]["available"])
                self.assertIsNone(value["sessions"]["total_visible"])
                self.assertEqual(value["tasks"]["items"][0]["session_link_state"], "not_in_current_sample")

    def test_independent_observations_recheck_config_and_external_disable(self):
        project = self.project()
        save(self.profile, [registration()])
        pair = observation_settings.pair(self.profile.observation_config())
        roots = scope_roots(self.store.list_projects())
        value = sample()
        value.update(**pair)
        value["instances"][0].update(classification="custom_exe", provider_id=None, kind="custom_exe",
                                     registration_ids=["a" * 32], directory_ids=[roots[0]["directory_id"]])
        value["registrations"] = [{"registration_id": "a" * 32, "status": "available", "reason": None,
            "source": "registration_verification", "sampled_at_ms": value["sampled_at_ms"], "duplicate_of_id": None}]
        digest = hashlib.sha256(json.dumps(roots, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        cached = {"status": "available", "reason": None, "snapshot": value, "scope_revision": 1,
                  "scope_hash": digest, "config_pair": tuple(pair.values()), "applied_pair": tuple(pair.values())}
        observer = Mock()
        observer.cached.side_effect = lambda: copy.deepcopy(cached)
        self.workspace.observer = observer
        events = []
        original = self.profile.observation_config

        def current_config():
            events.append("config")
            return original()

        def current_cache():
            events.append("cached")
            return copy.deepcopy(cached)

        observer.cached.side_effect = current_cache
        original_projects = self.store.list_projects

        def projects():
            events.append("projects")
            return original_projects()

        with patch.object(self.profile, "observation_config", side_effect=current_config) as config:
            with patch.object(self.store, "list_projects", side_effect=projects) as registry:
                first = self.workspace.observations()
                self.assertEqual((observer.cached.call_count, config.call_count, registry.call_count), (1, 1, 1))
                self.assertEqual(events, ["cached", "config", "projects"])
                self.assertEqual(first["project_refs"][0]["project_id"], project["id"])
                self.assertTrue(first["registrations"][0]["can_pick"])
                external = Profile.open(str(self.profile.data_dir))
                save(external, [registration(enabled=False)])
                events.clear()
                second = self.workspace.observations()
                self.assertEqual((observer.cached.call_count, config.call_count, registry.call_count), (2, 2, 2))
                self.assertEqual(events, ["cached", "config", "projects"])
        self.assertEqual(second["reason"], "config_pending")
        self.assertEqual(second["registrations"], [])
        self.assertEqual(second["project_refs"], [])
        self.assertEqual(second["instances"][0]["registration_ids"], [])
        self.assertIsNone(second["instances"][0]["return_action"])
        self.assertTrue(first["registrations"][0]["can_pick"])
        self.assertEqual(cached["snapshot"], value)

    def test_empty_observations_still_read_each_independent_request(self):
        with patch.object(self.store, "list_projects", wraps=self.store.list_projects) as registry:
            with patch.object(self.profile, "observation_config", wraps=self.profile.observation_config) as config:
                first = self.workspace.observations()
                self.project("new")
                registry.reset_mock()
                second = self.workspace.observations()
                self.assertEqual(registry.call_count, 1)
                self.assertEqual(config.call_count, 2)
        self.assertEqual((first["status"], second["status"]), ("unavailable", "unavailable"))
        self.assertEqual(first["reason"], "not_started")

    def test_store_errors_are_not_replaced_with_empty_success(self):
        for name in ("list_projects", "list_tasks", "list_records"):
            with self.subTest(name=name):
                failure = WorkspaceError("store_busy")
                with patch.object(self.store, name, side_effect=failure):
                    with self.assertRaises(WorkspaceError) as raised:
                        self.workspace.snapshot()
                self.assertIs(raised.exception, failure)
        with patch.object(self.store, "list_tasks", side_effect=WorkspaceError("store_busy")):
            self.reader.reset_mock()
            with self.assertRaises(WorkspaceError):
                self.workspace.page_tasks()
            self.reader.assert_not_called()

    def test_profiles_and_consecutive_requests_keep_local_results(self):
        self.project("first")
        other_profile = Profile.open(str(self.root / "other-profile"))
        other_profile.store.save_project(project_body(self.root / "other", name="Other"))
        other = WorkspaceService(profile=other_profile, quota_source=Mock(), news_source=Mock())
        other.session_reader = Mock(return_value=unavailable(0))
        first = self.workspace.snapshot()
        alternate = other.snapshot()
        second = self.workspace.snapshot()
        self.assertEqual([row["name"] for row in alternate["projects"]["items"]], ["Other"])
        self.assertEqual([row["name"] for row in second["projects"]["items"]], ["first"])
        second["projects"]["items"][0]["name"] = "Changed returned projection"
        self.assertEqual(first["projects"]["items"][0]["name"], "first")
        self.assertEqual(self.store.list_projects()[0]["name"], "first")
        self.assertEqual(self.reader.call_count, 2)

    def test_legacy_snapshot_retains_its_independent_source_contract(self):
        reader = Mock(return_value=unavailable(0))
        legacy = WorkspaceService(self.root / "legacy.sqlite3", session_reader=reader,
                                  quota_source=Mock(), news_source=Mock())
        with patch.object(legacy, "registered_projects", return_value=[]) as registry:
            with patch.object(legacy, "projects", return_value=[]):
                with patch("session_panel.workspace.service.progress.load_todos", return_value=[]):
                    with patch("session_panel.workspace.service.discover_providers", return_value=[]) as discover:
                        value = legacy.snapshot()
        self.assertEqual((reader.call_count, registry.call_count), (1, 1))
        discover.assert_called_once_with()
        self.assertEqual(value["projects"]["items"], [])
        self.assertEqual(value["tasks"]["total"], 0)
        self.assertIn("sessions", [row["id"] for row in value["entrypoints"]])
        with patch.object(legacy, "registered_projects", side_effect=WorkspaceError("workspace_unavailable")):
            with patch("session_panel.workspace.service.progress.load_todos", return_value=[]):
                with patch("session_panel.workspace.service.discover_providers", return_value=[]):
                    failed_source = legacy.snapshot()
        self.assertEqual(failed_source["projects"], {
            "status": "unavailable", "reason": "projects_unavailable", "items": []})
        self.assertEqual(failed_source["tasks"]["total"], 0)
        self.assertEqual(failed_source["todos"]["status"], "available")


if __name__ == "__main__":
    unittest.main()
