"""Pure fixture contracts for native Claude and OpenCode registration."""

import copy
import json
from pathlib import Path
import unittest

from control_everything.input_validation import ProfileError
from control_everything.sessions import observation_settings as settings
from control_everything.sessions import observer_protocol as protocol
from tests.backend.test_observer_protocol import sample


def row(provider="claude", kind="native_executable", path="C:/example/tool.exe"):
    return {"id": "a" * 32, "category": "builtin", "provider_id": provider,
            "environment_id": "windows:local", "label": "Example tool", "locator_kind": kind,
            "path": path, "enabled": True, "binding_state": "confirmed"}


class NativeRegistrationContractTests(unittest.TestCase):
    def test_exact_matrix_and_canonical_vectors(self):
        matrix = {"codex": ("npm_command", "codex_desktop"),
                  "claude": ("npm_command", "native_executable"),
                  "opencode": ("npm_command", "native_executable"),
                  "pi": ("npm_command",), "hermes": ("hermes_launcher",)}
        for provider, kinds in matrix.items():
            for kind in kinds:
                with self.subTest(provider=provider, kind=kind):
                    settings.validate({"revision": 1, "registrations": [row(provider, kind)]})
        for provider in ("codex", "pi", "hermes", "unknown", None):
            with self.subTest(provider=provider), self.assertRaises(ProfileError):
                settings.validate({"revision": 1, "registrations": [row(provider)]})
        schema = json.loads((Path(__file__).resolve().parents[2] / "native/observer/protocol.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["protocol"], 2)
        self.assertEqual(schema["registration_limits"]["records"], 32)
        self.assertEqual(schema["registration_limits"]["canonical_bytes"], 48 * 1024)
        for vector in schema["registration_vectors"]:
            self.assertEqual(settings.canonical(settings.validate(vector["observations"])).decode(), vector["canonical_utf8"])
            self.assertEqual(settings.pair(vector["observations"])["config_hash"], vector["sha256"])

    def test_native_paths_and_existing_registration_limits(self):
        for path in ("C:/example/tool.cmd", "tool.exe", "C:tool.exe", r"\\example.invalid\share\tool.exe",
                     r"\\?\C:\example\tool.exe", "C:/example/tool\n.exe", "C:/" + "x" * 2048 + ".exe"):
            with self.subTest(path=path), self.assertRaises(ProfileError):
                settings.validate({"revision": 1, "registrations": [row(path=path)]})
        for rows in ([dict(row(), id=f"{index:032x}", path=f"C:/example/{index}.exe") for index in range(33)],
                     [dict(row(), id=f"{index:032x}", path=f"C:/example/{index}" + "x" * 2000 + ".exe") for index in range(25)]):
            with self.assertRaises(ProfileError):
                settings.validate({"revision": 1, "registrations": rows})
        restored = settings.restore_intent({"revision": 1, "registrations": [row("opencode")]})
        self.assertEqual(restored["registrations"][0]["binding_state"], "rebind_required")

    def test_candidates_and_provider_snapshot_without_private_fields(self):
        value = {"status": "available", "reason": None, **settings.pair(settings.EMPTY),
                 "coverage": {"status": "complete", "omitted": 0}, "candidates": []}
        for provider in ("claude", "opencode"):
            value["candidates"] = [{"provider_id": provider, "locator_kind": "native_executable",
                                    "path": f"C:/example/{provider}.exe", "version": None}]
            protocol.candidates(value)
        for changes in ({"provider_id": "codex"}, {"path": "C:/example/opencode.cmd"},
                        {"path": "relative.exe"}, {"argv": "private"}, {"locator_kind": "executable"}):
            invalid = copy.deepcopy(value)
            invalid["candidates"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(protocol.ProtocolError):
                protocol.candidates(invalid)
        value["candidates"] *= 65
        with self.assertRaises(protocol.ProtocolError):
            protocol.candidates(value)
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode(b" " * (65536 + 1), maximum=65536)
        value = sample()
        value["installations"][0]["provider_id"] = "opencode"
        value["instances"][0].update(provider_id="opencode", return_action={"status": "limited", "reason": "binding_unknown", "action_id": None})
        protocol.snapshot(value, "a" * 64, "generation", 1)
        value["instances"][0]["provider_id"] = "unknown"
        with self.assertRaises(protocol.ProtocolError):
            protocol.snapshot(value, "a" * 64, "generation", 1)


if __name__ == "__main__":
    unittest.main()
