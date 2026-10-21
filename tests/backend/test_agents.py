"""Agent registration contracts over disposable profiles; no external process is used."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from control_everything.profiles.profile import Profile, ProfileError
from tests.backend.test_profile_http import ProfileHTTPTests


def agent(**fields):
    return {"id": None, "name": "Agent", "kind": "codex", "command": "codex", "enabled": True, **fields}


class AgentRegistryTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile = Profile.open(str(self.root / "profile"))

    def save(self, items, revision=0):
        return self.profile.save_agents({"expected_revision": revision, "items": items})

    def test_registration_upgrades_config_and_preserves_identity(self):
        result = self.save([agent(name="Codex"),
                            agent(name="DeepSeek Harness", kind="dsh", command=r"D:/example/dsh\DeepSeek Harness.exe")])
        self.assertEqual(result["revision"], 1)
        self.assertEqual([row["name"] for row in result["items"]], ["Codex", "DeepSeek Harness"])
        config = self.profile.config()
        self.assertEqual(config["version"], 5)
        self.assertEqual(set(config), {"version", "codex", "integrations", "observations", "workbench", "agents"})
        codex = result["items"][0]
        updated = self.save([{**codex, "name": "Codex CLI"}], revision=1)
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(updated["items"][0]["id"], codex["id"])
        self.assertEqual(updated["items"][0]["created_at"], codex["created_at"])
        self.assertNotEqual(updated["items"][0]["updated_at"], codex["updated_at"])
        removed = self.save([], revision=2)
        self.assertEqual((removed["revision"], removed["items"]), (3, []))
        reopened = Profile.open(str(self.root / "profile"))
        self.assertEqual(reopened.agent_settings()["items"], [])

    def test_project_scope_roundtrip_and_validation(self):
        saved = self.save([agent(name="Scoped", project_ids=["p-one", "p-two"])])
        self.assertEqual(saved["items"][0]["project_ids"], ["p-one", "p-two"])
        self.assertEqual(self.profile.agent_settings()["items"][0]["project_ids"], ["p-one", "p-two"])
        for invalid in (["p-one", "p-one"], [""], [1], "p-one"):
            with self.subTest(invalid=invalid), self.assertRaises(ProfileError):
                self.save([agent(name="Bad", project_ids=invalid)], revision=saved["revision"])

    def test_adapter_connection_roundtrip_preserves_when_edited(self):
        saved = self.save([agent(name="Claude", adapter="claude-code", connection={
            "command": "claude", "args": ["--mcp"], "cwd": "C:/example/work"})])
        item = saved["items"][0]
        self.assertEqual((item["adapter"], item["connection"]["args"]), ("claude-code", ["--mcp"]))
        reopened = Profile.open(str(self.root / "profile"))
        self.assertEqual(reopened.agent_settings()["items"][0]["connection"]["cwd"], "C:/example/work")
        edited = self.save([{key: item[key] for key in ("id", "name", "kind", "command", "enabled")}],
                           revision=saved["revision"])
        self.assertEqual(edited["items"][0]["adapter"], "claude-code")
        self.assertEqual(edited["items"][0]["connection"]["command"], "claude")
        for invalid in ({"adapter": "Bad Adapter"},
                        {"connection": {"token": "secret"}},
                        {"connection": {"nested": {"x": "y"}}},
                        {"connection": "not-a-dict"}):
            with self.subTest(invalid=invalid), self.assertRaises(ProfileError):
                self.save([agent(name="Bad", **invalid)], revision=edited["revision"])

    def test_agent_validation_cas_and_roundtrip(self):
        with self.assertRaises(ProfileError) as error:
            self.profile.save_agents({"expected_revision": 0, "items": [agent(kind="other")]})
        self.assertEqual(error.exception.reason, "invalid_config")
        with self.assertRaises(ProfileError):
            self.profile.save_agents({"expected_revision": True, "items": []})
        with self.assertRaises(ProfileError):
            self.profile.save_agents({"expected_revision": 0, "items": [agent(name="bad\nname")]})
        with self.assertRaises(ProfileError):
            self.profile.save_agents({"expected_revision": 0, "items": [agent(command="x" * 2049)]})
        duplicate = agent(id=str(uuid4()))
        with self.assertRaises(ProfileError):
            self.profile.save_agents({"expected_revision": 0, "items": [duplicate, duplicate]})
        with self.assertRaises(ProfileError):
            self.profile.save_agents({"expected_revision": 0, "items": [agent()] * 21})
        saved = self.save([agent()])
        with self.assertRaises(ProfileError) as error:
            self.save([], revision=0)
        self.assertEqual(error.exception.reason, "config_conflict")
        stored = self.profile.agent_settings()
        self.assertEqual(stored, saved)
        for row in stored["items"]:
            self.assertRegex(row["id"], r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
            self.assertIn("T", row["created_at"])

    def test_v5_settings_write_preserves_agent_registry(self):
        self.save([agent(name="Codex")])
        codex = self.profile.config()["codex"]
        self.profile.settings({"codex": codex})
        config = self.profile.config()
        self.assertEqual((config["version"], len(config["agents"]["items"])), (5, 1))
        self.assertEqual(config["agents"]["items"][0]["name"], "Codex")

    def test_token_survives_edit_and_heartbeat(self):
        saved = self.save([agent(name="Codex")])
        item = saved["items"][0]
        rotated = self.profile.rotate_agent_token({"id": item["id"]})
        self.assertTrue(rotated["agents"]["tokens"][item["id"]]["has_token"])
        edited = self.save([{**item, "name": "Codex CLI"}], revision=rotated["agents"]["revision"])
        self.assertTrue(edited["tokens"][item["id"]]["has_token"])
        self.assertEqual(self.profile.agent_heartbeat(rotated["token"])["id"], item["id"])
        self.assertIsNone(self.profile.agent_heartbeat("wrong"))


class AgentAPITests(unittest.TestCase):
    request = ProfileHTTPTests.request

    def setUp(self):
        ProfileHTTPTests.setUp(self)

    def test_get_and_post_agents_with_csrf(self):
        status, body = self.request("/api/workspace/agents")
        self.assertEqual(status, 200)
        self.assertEqual((body["revision"], body["items"]), (0, []))
        request = {"expected_revision": 0, "items": [agent(name="Codex"),
                   agent(name="DeepSeek Harness", kind="dsh", command=r"D:/example/dsh\DeepSeek Harness.exe")]}
        status, body = self.request("/api/workspace/agents", "POST", request)
        self.assertEqual(status, 200, body)
        self.assertEqual((body["revision"], len(body["items"])), (1, 2))
        self.assertEqual(self.request("/api/workspace/agents")[1]["items"][1]["kind"], "dsh")
        self.assertEqual(self.request("/api/workspace/agents", "POST", request)[0], 409)
        bad = {"expected_revision": 1, "items": [agent(kind="unknown")]}
        self.assertEqual(self.request("/api/workspace/agents", "POST", bad)[0], 422)
        self.assertEqual(self.request("/api/workspace/agents?x=1")[0], 400)

    def test_agent_posts_require_security_headers_and_strict_json(self):
        for headers in ({"Host": "bad"}, {"Origin": ""}, {"X-Session-Client": ""}, {"X-Session-CSRF": ""}):
            self.assertEqual(self.request("/api/workspace/agents", "POST", {}, headers)[0], 403)
        for raw in (b'{"expected_revision":0,"items":[],"items":[]}', b'{"n":NaN}', b"[]"):
            self.assertEqual(self.request("/api/workspace/agents", "POST", raw)[0], 400)

    def test_agent_adapter_catalog_is_declarative(self):
        status, body = self.request("/api/workspace/agent-adapters")
        self.assertEqual(status, 200, body)
        adapters = {item["id"]: item for item in body["adapters"]}
        self.assertLessEqual({"codex", "claude-code", "pi", "grok", "mcp.stdio", "mcp.http", "cli", "manual"},
                             set(adapters))
        for item in adapters.values():
            self.assertNotIn("token", item)
            self.assertNotIn("secret", item)
            keys = [field["key"] for field in item["fields"]]
            self.assertEqual(len(keys), len(set(keys)))

    def test_token_and_heartbeat_show_online(self):
        status, body = self.request("/api/workspace/agents", "POST",
            {"expected_revision": 0, "items": [agent(name="Codex")]})
        self.assertEqual(status, 200, body)
        item = body["items"][0]
        status, rotated = self.request("/api/workspace/agents/token", "POST", {"id": item["id"]})
        self.assertEqual(status, 200, rotated)
        self.assertTrue(rotated["token"].startswith("ag_"))
        self.assertTrue(rotated["agents"]["tokens"][item["id"]]["has_token"])
        status, heartbeat = self.request("/api/workspace/agents/heartbeat", "POST",
            {"id": item["id"]}, {"Authorization": "Bearer " + rotated["token"]})
        self.assertEqual(status, 200, heartbeat)
        listed = self.request("/api/workspace/agents")[1]["items"][0]
        self.assertTrue(listed["online"])
        self.assertIsNotNone(listed["last_seen_at"])
        bad = self.request("/api/workspace/agents/heartbeat", "POST",
            {"id": item["id"]}, {"Authorization": "Bearer ag_wrong"})
        self.assertEqual(bad[0], 401)


if __name__ == "__main__":
    unittest.main()
