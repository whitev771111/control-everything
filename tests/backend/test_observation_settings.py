"""Synthetic registration configuration, CAS, compatibility, and restore contracts."""

import copy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from session_panel.profiles import observation_settings as settings
from session_panel.profiles.profile import Profile, ProfileError, normalized_config, validate_config
from session_panel.profiles import profile_import as importer


def registration(identity="a" * 32, **changes):
    value = {"id": identity, "category": "custom_exe", "provider_id": None,
             "environment_id": "windows:local", "label": "Example tool", "locator_kind": "executable",
             "path": "C:/example/tool.exe", "enabled": True, "binding_state": "confirmed"}
    value.update(changes)
    return value


def save(profile, rows, **kwargs):
    current = profile.observation_settings()
    return profile.save_observations({"expected_revision": current["revision"], "expected_hash": current["hash"],
                                      "registrations": rows}, **kwargs)


class ObservationSettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile = Profile.open(str(self.root / "profile"))

    def test_canonical_sort_hash_and_old_logical_empty_do_not_write(self):
        original = self.profile.config_path.read_bytes()
        value = self.profile.observation_config()
        self.assertEqual(value, settings.EMPTY)
        self.assertEqual(settings.pair(value)["config_hash"], hashlib.sha256(b'{"registrations":[],"revision":0}').hexdigest())
        self.assertEqual(self.profile.config_path.read_bytes(), original)
        data = settings.validate({"revision": 1, "registrations": [registration("b" * 32, path="C:/example/other.exe"), registration()]})
        self.assertEqual(data["registrations"][0]["id"], "a" * 32)

    def test_upgrade_backup_cas_idempotence_and_old_writers_preserve_domain(self):
        result = save(self.profile, [registration(None)])
        self.assertEqual((result["revision"], self.profile.config()["version"]), (1, 3))
        self.assertRegex(result["registrations"][0]["id"], r"^[0-9a-f]{32}$")
        backup = next((self.profile.data_dir / "backups").iterdir())
        self.assertEqual(json.loads((backup / "profile.json").read_text())["version"], 2)
        self.assertEqual(save(self.profile, result["registrations"]), result)
        self.profile.settings({"codex": {"enabled": False, "db_path": None}})
        self.profile.import_settings({"version": 2, "integrations": self.profile.integrations()})
        self.assertEqual(self.profile.observation_settings(), result)
        self.assertEqual(normalized_config(self.profile.config())["version"], 3)
        stale = {"expected_revision": 0, "expected_hash": settings.pair(settings.EMPTY)["config_hash"], "registrations": []}
        with self.assertRaises(ProfileError) as caught:
            self.profile.save_observations(stale)
        self.assertEqual((caught.exception.reason, caught.exception.status), ("config_conflict", 409))

    def test_v1_upgrade_and_backup_failure_are_atomic(self):
        config = self.profile.config()
        config = {"version": 1, "codex": config["codex"]}
        self.profile._write_config(config)
        original = self.profile.config_path.read_bytes()
        with patch.object(importer, "create_backup", side_effect=ProfileError("backup_failed")):
            with self.assertRaises(ProfileError):
                save(self.profile, [registration()])
        self.assertEqual(original, self.profile.config_path.read_bytes())
        save(self.profile, [registration()])
        self.assertEqual(self.profile.config()["version"], 3)

    def test_commit_hook_runs_after_replace_inside_guard_and_failure_preserves_bytes(self):
        calls = []
        def committed(value):
            calls.append(value)
            self.assertEqual(self.profile.config()["observations"], value)
        result = save(self.profile, [registration()], on_commit=committed)
        self.assertEqual(len(calls), 1)
        original = self.profile.config_path.read_bytes()
        with patch("session_panel.profiles.profile.os.replace", side_effect=OSError("fixture")):
            with self.assertRaises(ProfileError):
                save(self.profile, [registration(enabled=False)], on_commit=committed)
        self.assertEqual(original, self.profile.config_path.read_bytes())
        self.assertEqual(len(calls), 1)
        self.assertEqual(save(self.profile, [])['revision'], result['revision'] + 1)

    def test_exact_fields_types_limits_and_combinations(self):
        variants = [registration(label=""), registration(label="a\n"), registration(label="\ud800"),
                    registration(path="relative.exe"), registration(path="C:/example/x\x00.exe"),
                    registration(path=r"\\example.invalid\share\tool.exe"), registration(path=r"\\?\C:\example\tool.exe"),
                    registration(path=r"\\.\C:\example\tool.exe"), registration(path=r"\example\tool.exe"), registration(path=r"C:example\tool.exe"),
                    registration(environment_id="wsl:test"), registration(enabled=1), registration(provider_id="codex"),
                    registration(category=[]), registration(locator_kind=[]), registration(binding_state=[]),
                    registration(id="A" * 32), registration(path="C:/example/tool.cmd"),
                    registration(category="builtin", provider_id="pi", locator_kind="codex_desktop"),
                    {**registration(), "secret": "fixture"}]
        for row in variants:
            with self.subTest(row=repr(row)), self.assertRaises(ProfileError):
                settings.validate({"revision": 1, "registrations": [row]})
        for revision in (-1, True, settings.MAX_REVISION + 1):
            with self.assertRaises(ProfileError):
                settings.validate({"revision": revision, "registrations": []})
        with self.assertRaises(ProfileError):
            settings.validate({"revision": 0, "registrations": [registration()]})
        for provider, locator in (("codex", "npm_command"), ("claude", "npm_command"), ("pi", "npm_command"), ("hermes", "hermes_launcher"), ("codex", "codex_desktop")):
            settings.validate({"revision": 1, "registrations": [registration(category="builtin", provider_id=provider, locator_kind=locator)]})

    def test_duplicates_count_and_combined_byte_budget_reject_whole_save(self):
        original = self.profile.config_path.read_bytes()
        variants = [[registration(), registration()], [registration(), registration("b" * 32, path=r"c:\EXAMPLE\tool.exe")],
                    [registration(f"{index:032x}", path=f"C:/example/{index}.exe") for index in range(33)],
                    [registration(f"{index:032x}", path=f"C:/example/{index}" + "x" * 2000 + ".exe", label="界" * 80) for index in range(24)]]
        for rows in variants:
            with self.assertRaises(ProfileError):
                save(self.profile, rows)
            self.assertEqual(self.profile.config_path.read_bytes(), original)

    def test_known_duplicate_can_be_deleted_but_cannot_be_retained(self):
        save(self.profile, [registration()])
        with self.assertRaises(ProfileError) as caught:
            save(self.profile, [registration()], duplicate_ids={"a" * 32})
        self.assertEqual(caught.exception.reason, "duplicate_installation")
        save(self.profile, [], duplicate_ids={"a" * 32})

    def test_restore_rebinds_only_new_domain_and_preserves_original_backup(self):
        save(self.profile, [registration(), registration("b" * 32, path="C:/example/other.exe", enabled=False)])
        backup = importer.create_backup(self.profile)
        before = {path.name: path.read_bytes() for path in backup.iterdir()}
        restored = Profile.open(str(importer.restore_backup(str(backup), str(self.root / "restored"))))
        config = restored.config()
        self.assertEqual(config["observations"]["revision"], 2)
        self.assertEqual([row["binding_state"] for row in config["observations"]["registrations"]], ["rebind_required"] * 2)
        self.assertEqual([row["enabled"] for row in config["observations"]["registrations"]], [True, False])
        self.assertEqual(config["codex"], self.profile.config()["codex"])
        self.assertEqual(config["integrations"], self.profile.config()["integrations"])
        self.assertEqual(before, {path.name: path.read_bytes() for path in backup.iterdir()})

    def test_restore_transform_or_overflow_failure_does_not_publish(self):
        save(self.profile, [registration()])
        backup = importer.create_backup(self.profile)
        with patch("session_panel.profiles.profile.Profile._write_config", side_effect=ProfileError("storage_error")):
            with self.assertRaises(ProfileError):
                importer.restore_backup(str(backup), str(self.root / "failed"))
        self.assertFalse((self.root / "failed").exists())
        config = self.profile.config()
        config["observations"]["revision"] = settings.MAX_REVISION
        self.profile._write_config(config)
        backup = importer.create_backup(self.profile)
        with self.assertRaises(ProfileError):
            importer.restore_backup(str(backup), str(self.root / "overflow"))
        self.assertFalse((self.root / "overflow").exists())

    def test_v3_unknown_keys_and_zero_revision_are_rejected(self):
        config = self.profile.config()
        config.update(version=3, observations=copy.deepcopy(settings.EMPTY))
        with self.assertRaises(ProfileError):
            validate_config(config)
        config["observations"]["revision"] = 1
        validate_config(config)
        config["extra"] = None
        with self.assertRaises(ProfileError):
            validate_config(config)
