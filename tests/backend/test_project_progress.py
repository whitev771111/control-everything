import copy
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from control_everything.workspace import project_progress as sources


class FakeEntry:
    def __init__(self, path, *, directory=False, link=False, sentinel=False):
        self.path, self.name = str(path), path.name
        self.directory, self.link, self.sentinel = directory, link, sentinel
        self.type_calls = 0

    def is_symlink(self):
        self.type_calls += 1
        if self.sentinel:
            raise AssertionError("overflow sentinel type was inspected")
        return self.link

    def is_dir(self, *, follow_symlinks):
        self.type_calls += 1
        if self.sentinel:
            raise AssertionError("overflow sentinel type was inspected")
        return self.directory


class FakeScan:
    def __init__(self, entries):
        self.entries, self.index, self.closed = entries, 0, False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def __iter__(self):
        return self

    def __next__(self):
        if self.closed:
            raise AssertionError("next after close")
        if self.index == len(self.entries):
            raise StopIteration
        value = self.entries[self.index]
        self.index += 1
        return value


class ProjectProgressTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.binding = self.root / "project"
        self.binding.mkdir()
        self.project = {"id": "example", "name": "样例", "root": str(self.binding),
                        "directories": [{"id": "main", "path": str(self.binding)}],
                        "source": {"kind": "none", "directory_id": None, "relative_path": None},
                        "percent": None, "state": None, "tasks": [], "history": []}

    def configure(self, kind="text", relative="progress.txt", binding="main"):
        self.project["source"] = {"kind": kind, "directory_id": binding, "relative_path": relative}

    def write(self, relative, data):
        path = self.binding / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return path

    def wfc(self):
        self.configure("wfc", "docs/current")
        root = self.binding / "docs/current"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def assert_state(self, expected, reason=None, budget=None):
        result = sources.observe(self.project, budget)
        self.assertEqual(result["status"], expected, result)
        if reason is not None:
            self.assertEqual(result["reason"], reason, result)
        self.assertIsInstance(result["records"], list)
        return result

    def test_unconfigured_never_accesses_sources(self):
        with patch.object(Path, "resolve", side_effect=AssertionError("source access")), \
                patch.object(Path, "open", side_effect=AssertionError("source open")), \
                patch.object(os, "scandir", side_effect=AssertionError("source scan")):
            result = self.assert_state("not_configured", "not_configured")
        self.assertEqual(result["records"], [])
        self.assertIsNone(result["reported"])

    def test_missing_binding_file_and_wfc_are_local_states(self):
        self.configure()
        self.assert_state("missing", "source_missing")
        self.configure(binding="unknown")
        self.assert_state("error", "source_binding_missing")
        self.configure("wfc", "docs/current")
        self.assert_state("missing", "source_missing")
        self.project["directories"][0]["path"] = str(self.root / "absent")
        self.assert_state("missing", "source_missing")

    def test_text_empty_bom_stage_and_plain_summary(self):
        self.configure()
        for text in ("", "  \n\t", "\ufeff  "):
            self.write("progress.txt", text)
            self.assert_state("unrecognized", "empty_source")
        for text, expected in (("\ufeffSTage: implementation\nnotes", "implementation"),
                               ("阶段：等待测试", "等待测试"), ("free text", "free text")):
            self.write("progress.txt", text)
            result = self.assert_state("available")
            self.assertEqual(result["records"][0]["text"], expected)
            self.assertIsNone(result["reported"])
        self.write("progress.txt", "x" * 3000)
        self.assertEqual(len(self.assert_state("available")["records"][0]["text"]), 2000)

    def test_bad_utf8_and_source_type_are_errors(self):
        self.configure()
        self.write("progress.txt", b"\xffinvalid")
        self.assert_state("error", "invalid_encoding")
        self.configure(relative="folder")
        (self.binding / "folder").mkdir()
        self.assert_state("error", "invalid_source_type")

    def test_json_zero_hundred_and_source_time_do_not_write_project(self):
        self.configure("json", "progress.json")
        before = copy.deepcopy(self.project)
        for percent in (0, 100):
            data = {"version": 1, "state": "completed", "percent": percent,
                    "updated_at": "2026-10-01T08:00:00+08:00"}
            path = self.write("progress.json", "\ufeff" + json.dumps(data))
            original = path.read_bytes()
            result = self.assert_state("available")
            self.assertEqual(result["reported"], data)
            self.assertNotEqual(result["observed_at"], data["updated_at"])
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(self.project, before)
            self.assertIsNone(self.project["percent"])

    def test_json_strict_shape_version_fields_and_values(self):
        self.configure("json", "progress.json")
        invalid = ["{", "[]", '{"version":1,"note":"a","note":"b"}',
                   '{"version":1,"percent":NaN}', '{"version":1,"percent":Infinity}',
                   '{"version":1,"percent":1e999}',
                   {"version": True, "note": "x"}, {"version": 2, "note": "x"},
                   {"version": 1.0, "note": "x"}, {"note": "x"},
                   {"version": 1, "unknown": "x"}, {"version": 1, "state": "done"},
                   {"version": 1, "state": None}, {"version": 1, "stage": "x" * 121},
                   {"version": 1, "note": "x" * 2001}, {"version": 1, "stage": {}},
                   {"version": 1, "percent": True}, {"version": 1, "percent": 0.0},
                   {"version": 1, "percent": -1}, {"version": 1, "percent": 101},
                   {"version": 1, "percent": 1, "updated_at": "2026-10-01T08:00:00"},
                   {"version": 1, "percent": 1, "updated_at": "2026-02-30T08:00:00Z"},
                   {"version": 1, "percent": 1, "updated_at": 123}]
        for data in invalid:
            with self.subTest(data=data):
                self.write("progress.json", data if isinstance(data, str) else json.dumps(data))
                result = self.assert_state("error")
                self.assertEqual(result["records"], [])
                self.assertIsNone(result["reported"])
        for data in ({"version": 1}, {"version": 1, "percent": None, "note": "  "},
                     {"version": 1, "updated_at": "2026-10-01T08:00:00Z"}):
            self.write("progress.json", json.dumps(data))
            self.assert_state("unrecognized", "empty_report")
        self.write("progress.json", json.dumps({"version": 1, "note": "text", "updated_at": None}))
        self.assert_state("available")

    def test_lexical_escape_is_rejected_before_open(self):
        for path in ("../outside.txt", "safe/../outside.txt", "/absolute", "C:/example/drive.txt", "C:drive.txt",
                     "//server/share/file", "\\\\server\\share\\file", "safe\\..\\file", "file:stream", "a\x00b"):
            with self.subTest(path=path):
                self.configure(relative=path)
                with patch.object(Path, "open", side_effect=AssertionError("opened target")):
                    self.assert_state("error", "invalid_source_path")

    def test_wfc_pass_and_nonempty_fallback_are_visible_without_mutation(self):
        self.wfc()
        self.write("docs/current/feature/README.md", "当前阶段：方案评审 PASS，等待实施")
        self.write("docs/current/feature/progress.md", "plain progress without labels")
        self.write("docs/current/other/README.md", "# 当前阶段\n\nImplementation pending")
        self.write("docs/current/state/README.md", "状态：BLOCKED")
        before = copy.deepcopy(self.project)
        result = self.assert_state("available")
        self.assertEqual(result["total"], 4)
        self.assertTrue(any("PASS" in item["text"] for item in result["records"]))
        self.assertTrue(any(item["text"] == "BLOCKED" for item in result["records"]))
        self.assertEqual(self.project, before)
        self.assertIsNone(result["reported"])

    def test_wfc_empty_and_fixed_nonrecursive_names(self):
        self.wfc()
        self.write("docs/current/ignored.txt", "ignored")
        self.write("docs/current/feature/other.md", "ignored")
        self.write("docs/current/feature/nested/README.md", "ignored")
        self.assert_state("unrecognized", "empty_source")
        self.write("docs/current/feature/README.md", "  \n")
        self.assert_state("unrecognized", "empty_source")

    def test_wfc_sorted_mtime_and_stable_path_and_display_truncation(self):
        self.wfc()
        for index in range(22):
            path = self.write(f"docs/current/f{index:02}/README.md", "x" * 2001)
            os.utime(path, (100, 100))
        latest = self.binding / "docs/current/f21/README.md"
        os.utime(latest, (101, 101))
        result = self.assert_state("available")
        self.assertEqual((len(result["records"]), result["total"], result["truncated"]), (20, 22, True))
        self.assertEqual([item["feature"] for item in result["records"][:3]], ["f21", "f00", "f01"])
        self.assertTrue(all(len(item["text"]) == 2000 for item in result["records"]))

    def test_single_file_exact_byte_limit_and_overflow_prevents_open(self):
        self.configure()
        self.write("progress.txt", b"x" * sources.FILE_BYTES)
        budget = sources.RequestBudget()
        self.assert_state("available", budget=budget)
        self.assertEqual((budget.bytes_read, budget.files_read), (sources.FILE_BYTES, 1))
        self.write("progress.txt", b"x" * (sources.FILE_BYTES + 1))
        with patch.object(Path, "open", side_effect=AssertionError("oversize opened")):
            self.assert_state("error", "budget_exceeded")

    def test_project_two_mib_exact_and_overflow_no_partial_success(self):
        self.wfc()
        for index in range(8):
            self.write(f"docs/current/f{index}/README.md", b"x" * sources.FILE_BYTES)
        budget = sources.RequestBudget()
        result = self.assert_state("available", budget=budget)
        self.assertEqual((result["total"], budget.bytes_read), (8, sources.PROJECT_BYTES))
        self.write("docs/current/f8/README.md", "x")
        result = self.assert_state("error", "budget_exceeded")
        self.assertEqual((result["records"], result["total"]), ([], 0))

    def test_shared_request_byte_and_file_budget_boundaries(self):
        self.configure()
        self.write("progress.txt", "x")
        budget = sources.RequestBudget(bytes_read=sources.REQUEST_BYTES - 1)
        self.assert_state("available", budget=budget)
        with patch.object(Path, "open", side_effect=AssertionError("budget overflow open")):
            self.assert_state("error", "budget_exceeded", budget)
        budget = sources.RequestBudget(files_read=511)
        self.assert_state("available", budget=budget)
        self.assertEqual(budget.files_read, 512)
        with patch.object(Path, "open", side_effect=AssertionError("budget overflow open")):
            self.assert_state("error", "budget_exceeded", budget)
        self.wfc()
        with patch.object(os, "scandir", side_effect=AssertionError("exhausted request scan")):
            self.assert_state("error", "budget_exceeded", budget)

    def test_request_byte_budget_is_shared_across_actual_observations(self):
        self.wfc()
        for index in range(8):
            self.write(f"docs/current/f{index}/README.md", b"x" * sources.FILE_BYTES)
        budget = sources.RequestBudget()
        for _index in range(4):
            self.assert_state("available", budget=budget)
        self.assertEqual(budget.bytes_read, sources.REQUEST_BYTES)
        with patch.object(os, "scandir", side_effect=AssertionError("byte exhausted scan")):
            self.assert_state("error", "budget_exceeded", budget)

    def test_request_512_file_budget_is_shared_across_actual_observations(self):
        self.configure()
        self.write("progress.txt", "x")
        budget = sources.RequestBudget()
        for _index in range(512):
            self.assert_state("available", budget=budget)
        self.assertEqual((budget.files_read, budget.bytes_read), (512, 512))
        with patch.object(Path, "open", side_effect=AssertionError("513th file opened")):
            self.assert_state("error", "budget_exceeded", budget)

    def test_wfc_64_directories_and_128_candidates_then_65_rejected(self):
        self.wfc()
        for index in range(64):
            self.write(f"docs/current/f{index:02}/README.md", "readme")
            self.write(f"docs/current/f{index:02}/progress.md", "progress")
        budget = sources.RequestBudget()
        result = self.assert_state("available", budget=budget)
        self.assertEqual((result["total"], budget.files_read), (128, 128))
        self.write("docs/current/overflow/README.md", "overflow")
        self.assert_state("error", "budget_exceeded")

    def test_candidate_budget_remains_independent(self):
        self.wfc()
        self.write("docs/current/f/README.md", "text")
        with patch.object(sources, "WFC_CANDIDATES", 1):
            self.assert_state("error", "budget_exceeded")

    def fake_scan(self, count, *, sentinel=False):
        root = self.wfc()
        entries = [FakeEntry(root / f"file-{index}", sentinel=sentinel and index == count - 1)
                   for index in range(count)]
        return FakeScan(entries)

    def test_all_noncandidates_exact_256_entries_are_bounded(self):
        scan = self.fake_scan(256)
        budget = sources.RequestBudget()
        with patch.object(os, "scandir", return_value=scan):
            self.assert_state("unrecognized", "empty_source", budget)
        self.assertEqual((scan.index, scan.closed, budget.entries_checked), (256, True, 256))

    def test_257th_entry_is_uninspected_sentinel_and_scan_closes(self):
        scan = self.fake_scan(258)
        scan.entries[256].sentinel = True
        budget = sources.RequestBudget()
        with patch.object(os, "scandir", return_value=scan):
            self.assert_state("error", "budget_exceeded", budget)
        self.assertEqual((scan.index, scan.closed, budget.entries_checked), (257, True, 257))
        self.assertEqual((scan.entries[256].type_calls, scan.entries[257].type_calls), (0, 0))

    def test_request_2048_boundary_and_2049_sentinel_no_further_scan(self):
        budget = sources.RequestBudget()
        for _index in range(8):
            scan = self.fake_scan(256)
            with patch.object(os, "scandir", return_value=scan):
                self.assert_state("unrecognized", "empty_source", budget)
            self.assertTrue(scan.closed)
        self.assertEqual(budget.entries_checked, 2048)
        scan = self.fake_scan(2)
        scan.entries[0].sentinel = True
        with patch.object(os, "scandir", return_value=scan):
            self.assert_state("error", "budget_exceeded", budget)
        self.assertEqual((scan.index, scan.entries[0].type_calls, scan.closed), (1, 0, True))
        with patch.object(os, "scandir", side_effect=AssertionError("scan after exhaustion")):
            self.assert_state("error", "budget_exceeded", budget)

    def test_mixed_files_directories_and_skipped_links_all_consume_entries(self):
        root = self.wfc()
        entries = []
        for index in range(256):
            directory = index % 5 == 0
            path = root / f"entry-{index}"
            if directory:
                path.mkdir()
            entries.append(FakeEntry(path, directory=directory, link=index % 5 == 1))
        scan = FakeScan(entries)
        budget = sources.RequestBudget()
        with patch.object(os, "scandir", return_value=scan):
            self.assert_state("unrecognized", "empty_source", budget)
        self.assertEqual(budget.entries_checked, 256)
        self.assertTrue(all(entry.type_calls for entry in entries))

    def test_read_growth_and_fstat_identity_change_are_errors(self):
        self.configure()
        path = self.write("progress.txt", "x")
        original = path.stat()
        changed = SimpleNamespace(st_dev=original.st_dev, st_ino=original.st_ino + 1,
                                  st_size=original.st_size, st_mtime_ns=original.st_mtime_ns,
                                  st_mode=original.st_mode)
        with patch.object(os, "fstat", return_value=changed):
            self.assert_state("error", "source_changed")
        with patch.object(os, "fstat", side_effect=[original, changed]):
            self.assert_state("error", "source_changed")
        stream = Mock()
        stream.__enter__ = Mock(return_value=stream)
        stream.__exit__ = Mock(return_value=False)
        stream.read.return_value = b"xx"
        with patch.object(sources, "FILE_BYTES", 1), patch.object(Path, "open", return_value=stream), \
                patch.object(os, "fstat", return_value=original):
            self.assert_state("error", "budget_exceeded")
        stream.read.assert_called_once_with(2)

    def test_permission_and_enumeration_errors_remain_local(self):
        self.configure()
        self.write("progress.txt", "private text")
        with patch.object(Path, "open", side_effect=PermissionError("secret traceback")):
            result = self.assert_state("error", "source_unavailable")
        self.assertNotIn("secret traceback", json.dumps(result))
        self.wfc()
        with patch.object(os, "scandir", side_effect=PermissionError("secret traceback")):
            self.assert_state("error", "source_unavailable")

    def test_resolved_escape_never_opens_or_enumerates_target(self):
        self.configure()
        outside = self.root / "sibling"
        outside.mkdir()
        original_resolve = Path.resolve

        def resolve(path, *args, **kwargs):
            if path.name == "progress.txt":
                return outside / "progress.txt"
            if path.name == "current":
                return outside
            return original_resolve(path, *args, **kwargs)

        with patch.object(Path, "resolve", resolve), \
                patch.object(Path, "open", side_effect=AssertionError("outside open")):
            self.assert_state("error", "path_outside_binding")
        self.wfc()
        with patch.object(Path, "resolve", resolve), \
                patch.object(os, "scandir", side_effect=AssertionError("outside scan")):
            self.assert_state("error", "path_outside_binding")

    def test_wfc_each_directory_and_candidate_rechecks_containment(self):
        root = self.wfc()
        folder = root / "feature"
        folder.mkdir()
        outside = self.root / "sibling"
        outside.mkdir()
        original_resolve = Path.resolve
        for escape in (folder, folder / "README.md"):
            def resolve(path, *args, **kwargs):
                if path == escape:
                    return outside / path.name
                return original_resolve(path, *args, **kwargs)
            with self.subTest(escape=escape), patch.object(Path, "resolve", resolve), \
                    patch.object(Path, "open", side_effect=AssertionError("outside open")):
                self.assert_state("error", "path_outside_binding")

    def test_selected_extra_binding_is_used_and_missing_old_root_is_ignored(self):
        extra = self.root / "extra"
        extra.mkdir()
        (extra / "progress.txt").write_text("extra source", encoding="utf-8")
        self.project["root"] = str(self.root / "old-missing")
        self.project["directories"].append({"id": "d-extra", "path": str(extra)})
        self.configure(binding="d-extra")
        self.assertEqual(self.assert_state("available")["records"][0]["text"], "extra source")


if __name__ == "__main__":
    unittest.main()
