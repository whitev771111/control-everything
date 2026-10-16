import copy
import io
import json
import sys
from types import SimpleNamespace
import os
import re
import subprocess
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from session_panel.sessions import remote_probe
from session_panel.sessions.service import RemoteClient, SessionService
from session_panel.sessions.source import MAX_TRANSCRIPT_BYTES, MAX_RESPONSE_BYTES
from tests.backend.test_observation import MAIN
from tests.fixtures.integration_config import integration_config


FIELDS = {"type", "uuid", "session_id", "message", "parent_tool_use_id", "parent_agent_id"}


class SixFieldMessage:
    __slots__ = tuple(FIELDS)

    def __init__(self, index=0, text="synthetic"):
        self.type, self.uuid, self.session_id = "assistant", str(index), MAIN
        self.message = {"role": "assistant", "content": [{"type": "text", "text": text}]}
        self.parent_tool_use_id, self.parent_agent_id = None, None


class MessageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.projects = Path(self.temporary.name).resolve() / "projects"
        self.cwd = "/srv/projects/synthetic"
        self.directory = self.projects / re.sub(r"[^a-zA-Z0-9]", "-", self.cwd)
        self.directory.mkdir(parents=True)
        self.path = self.directory / (MAIN + ".jsonl")
        self.path.write_text(json.dumps({"sessionId": MAIN, "cwd": self.cwd}) + "\n", encoding="utf-8")
        # Only the foreign cwd existence is shimmed on Windows; files/stat remain real.
        actual_is_dir, actual_realpath = Path.is_dir, os.path.realpath
        self.patch_dir = patch.object(Path, "is_dir", lambda path: True if path == Path(self.cwd) else actual_is_dir(path))
        self.patch_realpath = patch.object(remote_probe.os.path, "realpath",
                                           lambda value, *args, **kwargs: self.cwd if str(value) == self.cwd
                                           else actual_realpath(value, *args, **kwargs))
        self.patch_dir.start()
        self.patch_realpath.start()
        self.reader = Mock(return_value=[SixFieldMessage(i) for i in range(45)])

    def tearDown(self):
        self.patch_dir.stop()
        self.patch_realpath.stop()
        self.temporary.cleanup()

    def read(self, **kwargs):
        return remote_probe.read_messages(MAIN, projects=self.projects, reader=self.reader, **kwargs)

    def test_tail_page_six_actual_fields_no_message_time(self):
        result = self.read()
        self.assertTrue(result["available"])
        self.assertEqual(result["offset"], 25)
        self.assertEqual(result["total_visible"], 45)
        self.assertIsNone(result["next_offset"])
        self.assertEqual(set(result["items"][0]), FIELDS)
        self.assertNotIn("timestamp", result["items"][0])
        self.assertIsNone(result["items"][0]["parent_agent_id"])
        self.reader.assert_called_once_with(MAIN, directory=self.cwd, limit=None, offset=0)

    def test_explicit_pages_and_nonempty_history_empty_page(self):
        first = self.read(offset=0, limit=20)
        second = self.read(offset=first["next_offset"], limit=20, revision=first["revision"])
        self.assertEqual(first["next_offset"], 20)
        self.assertFalse(set(item["uuid"] for item in first["items"]) & set(item["uuid"] for item in second["items"]))
        empty_page = self.read(offset=90)
        self.assertTrue(empty_page["available"])
        self.assertEqual(empty_page["items"], [])
        self.assertEqual(empty_page["total_visible"], 45)

    def test_sdk_empty_unverified_even_when_stat_unchanged(self):
        for scenario in ("swallowed read failure", "genuine zero visible history"):
            with self.subTest(scenario=scenario):
                self.reader.return_value = []
                before = remote_probe.revision_of(self.path)
                result = self.read()
                self.assertFalse(result["available"])
                self.assertEqual(result["reason"], "reader_empty_unverified")
                self.assertIsNone(result["total_visible"])
                self.assertIsNone(result["next_offset"])
                self.assertEqual(before, remote_probe.revision_of(self.path))

    def test_explicit_sdk_failure_never_becomes_empty_success(self):
        self.reader.side_effect = OSError("synthetic private path/body")
        result = self.read()
        self.assertEqual(result["reason"], "reader_failed")
        self.assertNotIn("private", json.dumps(result))

    def test_parent_scope_mismatch_stops_before_sdk(self):
        alternate = self.projects / "wrong"
        alternate.mkdir()
        self.path.rename(alternate / self.path.name)
        self.assertEqual(self.read()["reason"], "reader_scope_unverified")
        self.reader.assert_not_called()

    def test_long_and_non_ascii_scope_stops_before_sdk(self):
        for cwd in ("/" + "a" * 201, "/srv/中文"):
            with self.subTest(cwd=cwd):
                self.path.write_text(json.dumps({"sessionId": MAIN, "cwd": cwd}), encoding="utf-8")
                self.assertEqual(self.read()["reason"], "reader_scope_unverified")
        self.reader.assert_not_called()

    def test_duplicate_uuid_missing_id_and_metadata_conflict(self):
        duplicate_dir = self.projects / "other"
        duplicate_dir.mkdir()
        duplicate = duplicate_dir / self.path.name
        duplicate.write_text(self.path.read_text(), encoding="utf-8")
        self.assertEqual(self.read()["reason"], "identity_conflict")
        duplicate.unlink()
        self.path.write_text('{"sessionId":"wrong","cwd":"/srv/projects/synthetic"}', encoding="utf-8")
        self.assertEqual(self.read()["reason"], "identity_conflict")
        self.path.unlink()
        self.assertEqual(self.read()["reason"], "not_found")
        self.reader.assert_not_called()

    def test_big_file_and_response_budget_refuse_without_truncation(self):
        with self.path.open("ab") as stream:
            stream.truncate(MAX_TRANSCRIPT_BYTES + 1)
        self.assertEqual(self.read()["reason"], "transcript_too_large")
        self.reader.assert_not_called()
        self.path.write_text(json.dumps({"sessionId": MAIN, "cwd": self.cwd}), encoding="utf-8")
        self.reader.return_value = [SixFieldMessage(text="x" * MAX_RESPONSE_BYTES)]
        result = self.read(limit=1)
        self.assertEqual(result["reason"], "response_too_large")
        self.assertEqual(result["items"], [])

    def test_change_retries_once_and_repeated_change_fails(self):
        def change_once(*args, **kwargs):
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write("\n{}")
            self.reader.side_effect = None
            return [SixFieldMessage()]
        self.reader.side_effect = change_once
        self.assertTrue(self.read()["available"])
        self.assertEqual(self.reader.call_count, 2)
        self.reader.reset_mock()
        def always_change(*args, **kwargs):
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write("\n{}")
            return [SixFieldMessage()]
        self.reader.side_effect = always_change
        self.assertEqual(self.read()["reason"], "messages_changed")
        self.assertEqual(self.reader.call_count, 2)

    def test_revision_mismatch_no_reader_call(self):
        self.assertEqual(self.read(revision="old-revision")["reason"], "revision_mismatch")
        self.reader.assert_not_called()

    def test_revision_change_after_metadata_stops_before_reader(self):
        requested_revision = remote_probe.revision_of(self.path)
        resolve = remote_probe.resolve_exact_uuid
        def resolve_then_change(*args, **kwargs):
            resolved = resolve(*args, **kwargs)
            self.assertEqual(resolved[1]["revision"], requested_revision)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write("\n{}")
            return resolved
        with patch.object(remote_probe, "resolve_exact_uuid", side_effect=resolve_then_change):
            result = self.read(offset=20, limit=20, revision=requested_revision)
        self.assertNotEqual(remote_probe.revision_of(self.path), requested_revision)
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "revision_mismatch")
        self.reader.assert_not_called()

    def test_missing_or_wrong_sdk_is_not_claimed_as_compatibility(self):
        with patch.object(remote_probe.importlib.metadata, "version", side_effect=remote_probe.importlib.metadata.PackageNotFoundError):
            with self.assertRaises(remote_probe.ProbeError) as raised:
                remote_probe.reader_capability()
            self.assertEqual(raised.exception.reason, "sdk_unavailable")
        with patch.object(remote_probe.importlib.metadata, "version", return_value="wrong"):
            with self.assertRaises(remote_probe.ProbeError) as raised:
                remote_probe.reader_capability()
            self.assertEqual(raised.exception.reason, "sdk_version_mismatch")

    def test_invalid_uuid_offset_limit_do_not_enter_reader(self):
        for kwargs in ({"offset": -1}, {"offset": True}, {"limit": 51}, {"limit": False}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.read(**kwargs)
        with self.assertRaises(ValueError):
            remote_probe.read_messages("not-an-id", projects=self.projects, reader=self.reader)
        self.reader.assert_not_called()

    def test_message_ssh_timeout_and_exact_runtime_filtered(self):
        runner = Mock(side_effect=subprocess.TimeoutExpired("ssh", 20))
        client = RemoteClient(runner, config=integration_config()["integrations"]["remote"])
        result = client.read_messages(MAIN)
        self.assertEqual(result["reason"], "reader_timeout")
        self.assertEqual("/home/example/reader/bin/python -", runner.call_args.args[0][-1])
        self.assertEqual(result["items"], [])
        self.assertIsNone(result["total_visible"])
        self.assertEqual(result["reader_version"], "0.2.163")

    def test_service_serializes_reads_and_does_not_observe_or_store_body(self):
        active, maximum = [0], [0]
        reader = Mock()
        def read(*args, **kwargs):
            active[0] += 1
            maximum[0] = max(maximum[0], active[0])
            time.sleep(0.02)
            active[0] -= 1
            return {"available": True, "items": [{"message": "synthetic"}]}
        reader.read_messages.side_effect = read
        service = SessionService(remote=reader, terminal=Mock())
        threads = [threading.Thread(target=service.get_session_messages, args=(MAIN,)) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(maximum[0], 1)
        reader.observe_source.assert_not_called()
        self.assertNotIn("synthetic", json.dumps(service._snapshot))


class ProbePayloadTests(unittest.TestCase):
    def setUp(self):
        self.config = integration_config()["integrations"]["remote"]
        self.context = patch.multiple(remote_probe, SOURCE=None, HOME=None, SLOT_FILES={}, MCP_VERSION="1.30.0")
        self.context.start()
        self.addCleanup(self.context.stop)

    def payload(self, mode="observe"):
        return RemoteClient(config=self.config)._payload(mode, None if mode == "observe" else {
            "session_id": MAIN, "offset": None, "limit": 20, "revision": None})

    def invoke(self, payload):
        output = io.StringIO()
        with patch.object(remote_probe.sys, "stdout", output):
            remote_probe.main(payload)
        return json.loads(output.getvalue())

    def test_invalid_payload_never_enters_observer_or_reader(self):
        payload = self.payload()
        for candidate in (None, {}, {**payload, "extra": 1}, {**payload, "mode": "exec"},
                          {**self.payload("messages"), "request": {"session_id": MAIN, "limit": False,
                                                                    "offset": None, "revision": None}}):
            with self.subTest(candidate=candidate), patch.object(remote_probe, "observe_source") as observer, \
                    patch.object(remote_probe, "read_messages") as reader:
                result = self.invoke(candidate)
                self.assertEqual(result["reason"], "invalid_request")
                self.assertIsNone(result["source"])
                observer.assert_not_called(); reader.assert_not_called()

    def test_valid_payload_selects_fixed_operations_and_old_reader_has_upgrade_hint(self):
        for version in ("1.26.0", "1.30.0"):
            payload = self.payload(); payload["reader_mcp_version"] = version
            with patch.object(remote_probe, "observe_source", side_effect=lambda: {
                    "ok": True, "source": remote_probe.SOURCE}) as observer:
                result = self.invoke(payload)
            observer.assert_called_once_with()
            self.assertEqual(result["source"], self.config["source"])
            self.assertEqual(bool(result.get("reader_upgrade_required")), version == "1.26.0")
            self.assertEqual(remote_probe.HOME, self.config["home"])
            self.assertEqual(remote_probe.SLOT_FILES, self.config["slot_files"])
        request = self.payload("messages")
        with patch.object(remote_probe, "read_messages", return_value={"available": True}) as reader:
            self.assertTrue(self.invoke(request)["available"])
        reader.assert_called_once_with(**request["request"])

    def test_user_home_config_and_resolved_root_all_remain_exact(self):
        source = self.config["source"]
        fake_pwd = SimpleNamespace(getpwuid=lambda _uid: SimpleNamespace(pw_name=source["user"]))
        remote_probe.SOURCE, remote_probe.HOME = copy.deepcopy(source), self.config["home"]
        with patch.dict(sys.modules, {"pwd": fake_pwd}), patch.object(remote_probe.os, "getuid", return_value=1000, create=True), \
                patch.object(remote_probe.Path, "home", return_value=self.config["home"]), \
                patch.object(remote_probe.Path, "resolve", autospec=True, side_effect=lambda path, **_: path), \
                patch.dict(remote_probe.os.environ, {"CLAUDE_CONFIG_DIR": source["config_dir"]}, clear=True):
            remote_probe.fixed_source_check()
            for failed in ("user", "home", "config", "resolved_root"):
                with self.subTest(failed=failed):
                    context = (patch.object(fake_pwd, "getpwuid", return_value=SimpleNamespace(pw_name="other"))
                               if failed == "user" else patch.object(remote_probe.Path, "home", return_value="/srv/other-home")
                               if failed == "home" else patch.dict(remote_probe.os.environ, {"CLAUDE_CONFIG_DIR": "/srv/other-home/.claude"})
                               if failed == "config" else patch.object(remote_probe.Path, "resolve", return_value=Path("/different")))
                    with context, self.assertRaises(remote_probe.ProbeError) as raised:
                        remote_probe.fixed_source_check()
                    self.assertEqual(raised.exception.reason, "source_mismatch")

    def test_supported_reader_versions_are_checked_exactly(self):
        reader = Mock()
        for version in ("1.26.0", "1.30.0"):
            remote_probe.MCP_VERSION = version
            actual = {"claude-agent-sdk": "0.2.163", "mcp": version}
            with patch.dict(sys.modules, {"claude_agent_sdk": SimpleNamespace(get_session_messages=reader)}), \
                    patch.object(remote_probe.importlib.metadata, "version", side_effect=actual.__getitem__):
                self.assertIs(remote_probe.reader_capability(), reader)
                actual["mcp"] = "wrong"
                with self.assertRaises(remote_probe.ProbeError) as raised:
                    remote_probe.reader_capability()
                self.assertEqual(raised.exception.reason, "sdk_version_mismatch")
                actual["mcp"], actual["claude-agent-sdk"] = version, "wrong"
                with self.assertRaises(remote_probe.ProbeError):
                    remote_probe.reader_capability()


if __name__ == "__main__":
    unittest.main()
