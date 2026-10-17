"""Workspace observations use synthetic storage and handler dispatch, with no listener."""

import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from session_panel.http import gateway
from session_panel.profiles.profile import Profile
from session_panel.workspace.observations import project_observations, scope_roots
from session_panel.workspace.service import WorkspaceService
from tests.backend.test_observer_protocol import sample
from tests.backend.test_observation_settings import registration, save
from session_panel.profiles import observation_settings


class WorkspaceObservationTests(unittest.TestCase):
    def test_scope_projection_deduplicates_resources_and_hides_raw_directory_ids(self):
        projects = [{"id": "project-a", "archived": False, "directories": [{"path": "C:/example/project"}]},
                    {"id": "project-b", "archived": False, "directories": [{"path": "C:/example/project/sub"}]}]
        roots = scope_roots(projects)
        value = sample()
        value["instances"][0]["directory_ids"] = [item["directory_id"] for item in roots]
        other = copy.deepcopy(value["instances"][0]); other["instance_id"] = "other"
        value["instances"].append(other)
        digest = hashlib.sha256(json.dumps(roots, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        cached = {"status": "available", "reason": None, "snapshot": value, "scope_revision": 1, "scope_hash": digest, "config_pair": (value["config_revision"], value["config_hash"]), "applied_pair": (value["config_revision"], value["config_hash"])}
        public = project_observations(cached, projects)
        self.assertEqual(public["project_refs"], [{"project_id": "project-b", "instance_ids": ["instance", "other"]}])
        self.assertEqual(len(public["resources"]), 1)
        self.assertNotIn("directory_ids", public["instances"][0])
        self.assertNotIn("profile_key", public)
        projects[1]["archived"] = True
        public = project_observations(cached, projects)
        self.assertEqual(public["reason"], "scope_pending")
        self.assertIsNone(public["instances"][0]["return_action"])
        self.assertEqual(public["unassigned"], ["instance", "other"])

    def test_profile_snapshot_never_scans_path_when_native_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Profile.open(directory)
            workspace = WorkspaceService(profile=profile, quota_source=Mock(), news_source=Mock())
            with patch("shutil.which", side_effect=AssertionError("duplicate PATH scan")):
                value = workspace.snapshot()
            self.assertEqual(len(value["providers"]), 5)
            self.assertTrue(all(item["discovery"]["status"] == "unknown" for item in value["providers"]))
            self.assertEqual(value["observations"]["reason"], "not_started")
            self.assertFalse(profile.public()["config"]["codex"]["enabled"])

    def handler(self, body=None, allowed=True):
        handler = object.__new__(gateway.PanelHandler)
        handler.server = SimpleNamespace(observer=Mock(), workspace=Mock(), profile=None)
        handler.server.observer.request.return_value = {"status": "available", "reason": None, "kind": "observed_instance"}
        handler.reply, handler.reject = Mock(), Mock()
        handler.allowed = Mock(return_value=allowed)
        handler.read_json_body = Mock(return_value=body)
        return handler

    def test_protected_post_requires_exact_body_and_no_client_target_metadata(self):
        for body in ({"action_id": "a", "generation": "g", "seq": True},
                     {"action_id": "a", "generation": "g", "seq": 1, "pid": 123},
                     {"tool_id": "other"}, {"tool_id": "wezterm", "title": "private"}):
            handler = self.handler(body)
            path = "/api/workspace/observations/return" if "action_id" in body else "/api/workspace/observations/window-choices"
            handler.observation_post(urlsplit(path))
            handler.reject.assert_called_once_with(400, "invalid_request")
            handler.server.observer.request.assert_not_called()
        handler = self.handler({"action_id": "a", "generation": "g", "seq": 1})
        handler.path = "/api/workspace/observations/return"
        handler.do_POST()
        handler.allowed.assert_called_once_with(api=True, focus=True)
        handler.server.observer.request.assert_called_once_with("return_window", {"action_id": "a", "generation": "g", "seq": 1})
        handler.reply.assert_called_once_with(200, {"status": "available", "reason": None, "kind": "observed_instance"})

    def test_rejected_origin_never_reads_or_dispatches(self):
        handler = self.handler({}, allowed=False)
        handler.path = "/api/workspace/observations/window-choices"
        handler.do_POST()
        handler.read_json_body.assert_not_called()
        handler.server.observer.request.assert_not_called()

    def test_statuses_and_side_effect_free_bind_assembly(self):
        handler = self.handler({"action_id": "a", "generation": "g", "seq": 1})
        for value, status in (({"status": "limited", "reason": "action_expired"}, 409),
                              ({"status": "unavailable", "reason": "native_missing"}, 503),
                              ({"status": "unknown", "reason": "return_unknown"}, 409)):
            handler.server.observer.request.return_value = value
            handler.observation_post(urlsplit("/api/workspace/observations/return"))
            handler.reply.assert_called_with(status, value)
        with tempfile.TemporaryDirectory() as directory:
            fake = Mock(server_address=("127.0.0.1", 14401))
            with patch.object(gateway, "PanelServer", return_value=fake), patch("threading.Thread", side_effect=AssertionError("thread")):
                server = gateway.build_profile_server(directory, 14401, quota_source=Mock(), news_source=Mock())
            self.assertFalse(server.observer.started)
            self.assertEqual({item.name for item in Path(directory).iterdir()}, {"profile.json", ".profile.lock", "workspace-state.sqlite3"})
            with patch.object(server.observer, "start") as start:
                gateway.PanelServer.start_observer(server)
                start.assert_called_once()

    def test_observation_get_is_http200_without_start(self):
        handler = self.handler()
        handler.path = "/api/workspace/observations"
        handler.server.workspace.observations.return_value = {"status": "unavailable", "reason": "native_missing"}
        handler.do_GET()
        handler.reply.assert_called_once_with(200, {"status": "unavailable", "reason": "native_missing"})

    def test_registered_custom_projection_labels_without_path_and_external_change_revokes_refs(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Profile.open(directory)
            save(profile, [registration()])
            value = sample()
            pair = observation_settings.pair(profile.observation_config())
            value.update(**pair)
            value["instances"][0].update(classification="custom_exe", provider_id=None, kind="custom_exe", registration_ids=["a" * 32])
            value["registrations"] = [{"registration_id": "a" * 32, "status": "available", "reason": None,
                "source": "profile_registration", "sampled_at_ms": value["sampled_at_ms"], "duplicate_of_id": None}]
            scope_hash = hashlib.sha256(b"[]").hexdigest()
            cached = {"status": "available", "reason": None, "snapshot": value, "scope_revision": 1, "scope_hash": scope_hash,
                      "config_pair": tuple(pair.values()), "applied_pair": tuple(pair.values())}
            observer = Mock()
            observer.cached.side_effect = lambda: copy.deepcopy(cached)
            workspace = WorkspaceService(profile=profile, observer=observer, quota_source=Mock(), news_source=Mock())
            public = workspace.observations()
            self.assertEqual(public["instances"][0]["registration_labels"], ["Example tool"])
            self.assertTrue(public["registrations"][0]["can_pick"])
            self.assertNotIn("C:/example/tool.exe", repr(public))
            self.assertEqual(len(public["resources"]), 1)
            save(profile, [registration(enabled=False)])
            stale = workspace.observations()
            self.assertEqual(stale["reason"], "config_pending")
            self.assertEqual(stale["registrations"], [])
            self.assertEqual(stale["instances"][0]["registration_ids"], [])
            self.assertIsNone(stale["instances"][0]["return_action"])
