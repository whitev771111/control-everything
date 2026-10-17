"""Synthetic cross-language protocol and privacy boundary tests."""

import copy
import json
from pathlib import Path
import time
import unittest

from control_everything.sessions import observer_protocol as p
from control_everything.profiles import observation_settings as settings


def sample(seq=1, revision=1):
    now = int(time.time() * 1000)
    return {"protocol": p.VERSION, "profile_key": "a" * 64, "generation": "generation", "seq": seq,
            **settings.pair(settings.EMPTY), "registrations": [],
            "scope_revision": revision, "environment_id": "windows:local", "sampled_at_ms": now,
            "metrics_interval_ms": 1000, "status": "available", "reason": None,
            "coverage": {"status": "complete", "scanned": 1, "recognized": 1, "omitted": 0, "denied": 0},
            "installations": [{"provider_id": "codex", "status": "available", "reason": None,
                               "source": "npm_manifest", "commands": ["codex"], "version": "1.0.0"}],
            "instances": [{"instance_id": "instance", "provider_id": "codex", "kind": "cli", "pid": 123,
                           "classification": "provider", "registration_ids": [],
                           "resource_id": "resource", "directory_ids": [], "status": "available", "reason": None,
                           "source": "verified_os_process", "sampled_at_ms": now,
                           "session": {"status": "unknown", "reason": "no_session_source"},
                           "pending": {"status": "unknown", "reason": "no_pending_source"},
                           "return_action": {"action_id": "action", "issued_seq": 1, "expires_at_ms": now + 10000,
                                             "kind": "observed_instance", "status": "limited", "reason": "verify_on_request", "scope": "console_window"}}],
            "resources": [{"resource_id": "resource", "scope": "process", "cpu_percent_one_core": None,
                           "rss_bytes": 1024, "threads": None, "handles": None, "metrics_interval_ms": 1000,
                           "source": "windows_process", "sampled_at_ms": now}]}


class ObserverProtocolTests(unittest.TestCase):
    def test_single_schema_fields_and_identity_vector(self):
        schema = json.loads((Path(__file__).resolve().parents[2] / "native/observer/protocol.json").read_text(encoding="utf-8"))
        for key, fields in (("snapshot_fields", p.SNAPSHOT_FIELDS), ("installation_fields", p.INSTALLATION_FIELDS),
                            ("instance_fields", p.INSTANCE_FIELDS), ("resource_fields", p.RESOURCE_FIELDS), ("grant_fields", p.GRANT_FIELDS)):
            self.assertEqual(set(schema[key]), fields)
        vector = schema["profile_key_vector"]
        self.assertEqual(schema["protocol"], p.VERSION)
        for item in schema["registration_vectors"]:
            self.assertEqual(settings.canonical(settings.validate(item["observations"])).decode(), item["canonical_utf8"])
            self.assertEqual(settings.pair(item["observations"])["config_hash"], item["sha256"])
        self.assertEqual(p.profile_key(bytes.fromhex(vector["sid_hex"]), int.from_bytes(bytes.fromhex(vector["volume_le_hex"]), "little"),
                                       bytes.fromhex(vector["file_id_hex"])), vector["sha256"])

    def test_strict_json_limits_duplicates_depth_numbers_unicode(self):
        invalid = [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b'{"a":18446744073709551616}',
                   b'{"a":"\\ud800"}', b'{"a":"\xff"}', b'[]', b'{"a":' + b'[' * 14 + b'0' + b']' * 14 + b'}', b' ' * (p.RESPONSE_MAX + 1)]
        for raw in invalid:
            with self.subTest(raw=raw[:32]), self.assertRaises(p.ProtocolError):
                p.decode(raw)
        self.assertEqual(p.decode(b'{"ok":true}'), {"ok": True})

    def test_snapshot_rejects_private_unknown_identity_revision_and_invalid_metrics(self):
        p.snapshot(sample(), "a" * 64, "generation", 1)
        for change in (lambda value: value.update(title="private"), lambda value: value.update(profile_key="other"),
                       lambda value: value.update(scope_revision=0), lambda value: value["instances"][0].update(argv="private"),
                       lambda value: value["resources"][0].update(cpu_percent_one_core=True),
                       lambda value: value["instances"][0].update(session={"status": "available", "reason": None})):
            value = sample(); change(value)
            with self.assertRaises(p.ProtocolError):
                p.snapshot(value, "a" * 64, "generation", 1)

    def test_optional_short_grants_and_choice_title_budget(self):
        for grant in ({"status": "limited", "reason": "grant_capacity", "action_id": None},
                      {"status": "stale", "reason": "sample_timeout", "action_id": None}):
            p.grant(grant)
        choice = copy.deepcopy(sample()["instances"][0]["return_action"])
        choice.update(kind="manual_navigation", scope="application_window", status="available", reason=None, title="<literal>" + "界" * 247)
        p.grant(choice, choice=True)
        choice["title"] += "界"
        with self.assertRaises(p.ProtocolError):
            p.grant(choice, choice=True)

    def test_v2_custom_classification_pair_and_explicit_candidate_boundaries(self):
        value = sample()
        identity = "a" * 32
        value["registrations"] = [{"registration_id": identity, "status": "available", "reason": None,
                                    "source": "profile_registration", "sampled_at_ms": value["sampled_at_ms"], "duplicate_of_id": None}]
        value["instances"][0].update(classification="custom_exe", provider_id=None, kind="custom_exe", registration_ids=[identity],
                                     return_action={"status": "limited", "reason": "binding_unknown", "action_id": None})
        p.snapshot(value, "a" * 64, "generation", 1, p.config_pair(value))
        for changes in ({"provider_id": "codex"}, {"kind": "cli"}, {"registration_ids": ["b" * 32]}, {"registration_ids": [identity, identity]}):
            invalid = copy.deepcopy(value)
            invalid["instances"][0].update(changes)
            with self.assertRaises(p.ProtocolError):
                p.snapshot(invalid, "a" * 64, "generation", 1)
        with self.assertRaises(p.ProtocolError):
            p.snapshot(value, "a" * 64, "generation", 1, (1, "b" * 64))
        with self.assertRaisesRegex(p.ProtocolError, "protocol_mismatch"):
            p.hello({"protocol": 1}, "a" * 64, "build")
        candidates = {"status": "available", "reason": None, **settings.pair(settings.EMPTY),
                      "coverage": {"status": "complete", "omitted": 0}, "candidates": [
                          {"provider_id": "pi", "locator_kind": "npm_command", "path": "C:/example/pi.cmd", "version": "1.0"}]}
        p.candidates(candidates)
        for path in (r"\\example.invalid\share\pi.cmd", r"\\?\C:\example\pi.cmd", "relative"):
            candidates["candidates"][0]["path"] = path
            with self.assertRaises(p.ProtocolError):
                p.candidates(candidates)
