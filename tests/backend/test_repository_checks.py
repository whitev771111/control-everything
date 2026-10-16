"""Repository policy checks use only synthetic repositories and runtime fixtures."""
import importlib.util
import ctypes
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("repository_checks", Path(__file__).parents[2] / "scripts/check_repository.py")
CHECKS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKS)


class RepositoryChecksTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Repository Test")
        self.git("config", "user.email", "test@example.invalid")
        self.tool = patch.object(CHECKS, "scan_gitleaks", return_value={"version": CHECKS.VERSION, "exit_code": 0, "findings": []})
        self.scanner = self.tool.start()
        self.addCleanup(self.tool.stop)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True, check=True).stdout.decode().strip()

    def save(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def commit(self):
        self.git("add", "--all")
        self.git("commit", "-qm", "test: synthetic fixture")
        return self.git("rev-parse", "HEAD")

    def check(self, mode, *args):
        code = CHECKS.main([mode, "--root", str(self.root), "--report", "work/report.json", *args])
        return code, json.loads((self.root / "work/report.json").read_text(encoding="utf-8"))

    def release(self, files, excluded=None):
        self.save("work/manifest.json", json.dumps({"files": files, "excluded": excluded or []}))
        return self.check("release", "--manifest", str(self.root / "work/manifest.json"))

    @staticmethod
    def personal_path():
        return "C:" + "/Users/" + "synthetic-person/private"

    def test_old_value_and_same_file_safe_addition_pass(self):
        self.save("sample.txt", self.personal_path() + "\nold\n")
        base = self.commit()
        self.save("sample.txt", self.personal_path() + "\nold\nsafe addition\n")
        head = self.commit()
        code, report = self.check("changes", "--base", base, "--head", head)
        self.assertEqual(code, 0)
        self.assertEqual(report["findings"], [])

    def test_new_personal_value_blocks_without_echo(self):
        self.save("sample.txt", "safe\n")
        base = self.commit()
        self.save("sample.txt", "safe\n" + self.personal_path() + "\n")
        code, report = self.check("changes", "--base", base, "--head", self.commit())
        self.assertEqual(code, 1)
        self.assertEqual(report["findings"][0]["line"], 2)
        self.assertNotIn(self.personal_path(), json.dumps(report))

    def test_rename_does_not_reintroduce_old_values(self):
        self.save("old.txt", self.personal_path() + "\n")
        base = self.commit()
        self.git("mv", "old.txt", "new.txt")
        code, report = self.check("changes", "--base", base, "--head", self.commit())
        self.assertEqual(code, 0)
        self.assertEqual(report["findings"], [])

    def test_escaped_ip_home_and_project_path(self):
        address = ".".join(["8", "8", "4", "4"])
        text = "\n".join([address.replace(".", "\\."), "/" + "home/private-person/data", "E:" + "/project-private/data"])
        self.save("sample.txt", text)
        code, report = self.release(["sample.txt"])
        self.assertEqual(code, 1)
        self.assertEqual(len(report["findings"]), 3)
        self.assertNotIn("private-person", json.dumps(report))

    def test_document_addresses_and_placeholders_pass(self):
        self.save("sample.txt", "127.0.0.1 192.0.2.10 198.51.100.20 203.0.113.30\n/home/example/data\nC:/temporary/example\n")
        self.assertEqual(self.release(["sample.txt"])[0], 0)

    def test_official_https_url_is_not_a_windows_drive(self):
        self.save("sample.txt", "https://github.com/gitleaks/gitleaks\n")
        self.assertEqual(self.release(["sample.txt"])[0], 0)

    def test_release_is_strict_and_manifest_limited(self):
        self.save("old.txt", self.personal_path())
        self.save("safe.txt", "safe\n")
        code, report = self.release(["safe.txt"], ["old.txt"])
        self.assertEqual(code, 0)
        self.assertEqual(report["excluded"], ["old.txt"])
        self.assertEqual(self.release(["old.txt"])[0], 1)

    def test_forbidden_paths_are_never_read(self):
        for name in [".env", "videos/draft.txt", "data.sqlite3", "image.png", "work/private.txt"]:
            with self.subTest(name=name), patch.object(CHECKS, "safe_file", side_effect=AssertionError("must not read")):
                code, report = self.release([name])
                self.assertEqual(code, 1)
                self.assertTrue(report["blocked"])

    def test_empty_outside_missing_and_binary_fail_closed(self):
        self.assertEqual(self.release([])[0], 1)
        self.assertEqual(self.release(["../outside.txt"])[0], 1)
        self.assertEqual(self.release(["missing.txt"])[0], 1)
        (self.root / "binary.txt").write_bytes(b"safe\x00binary")
        self.assertEqual(self.release(["binary.txt"])[0], 1)

    def test_binary_markers_in_added_text_remain_scannable(self):
        self.save(".gitignore", "work/\n")
        self.save("sample.txt", "safe\n")
        base = self.commit()
        self.save("sample.txt", "GIT binary patch\nBinary files a/sample.txt and b/sample.txt differ\n")
        head = self.commit()
        code, report = self.check("changes", "--base", base, "--head", head)
        self.assertEqual(code, 0)
        self.assertEqual(report["blocked"], [])
        self.assertIn({"path": "sample.txt", "commit": head}, report["included"])
        self.scanner.assert_called_once()
        self.save("sample.txt", "GIT binary patch\n" + self.personal_path() + "\n")
        code, report = self.check("changes", "--base", head, "--head", self.commit())
        self.assertEqual(code, 1)
        self.assertEqual(report["blocked"], [])
        self.assertEqual(report["findings"][0]["rule"], "absolute-windows-path")

    def test_binary_commit_is_blocked_before_scanner(self):
        self.save("sample.txt", "safe\n")
        base = self.commit()
        (self.root / "sample.txt").write_bytes(b"safe\x00binary")
        code, report = self.check("changes", "--base", base, "--head", self.commit())
        self.assertEqual(code, 1)
        self.assertEqual(report["blocked"], [{"path": "sample.txt", "reason": "binary-candidate"}])
        self.scanner.assert_not_called()

    def test_tool_exit_and_missing_tool_fail_closed(self):
        self.save("safe.txt", "safe\n")
        self.scanner.return_value = {"version": CHECKS.VERSION, "exit_code": 2, "findings": []}
        self.assertEqual(self.release(["safe.txt"])[0], 1)
        self.scanner.side_effect = CHECKS.CheckError("tool-unavailable")
        self.assertEqual(self.release(["safe.txt"])[0], 1)

    def test_zero_missing_equal_and_unrelated_commits_fail(self):
        self.save("safe.txt", "safe")
        head = self.commit()
        for base in ["0" * 40, "f" * 40, head, "--bad-option"]:
            with self.subTest(base=base):
                self.assertEqual(self.check("changes", "--base=" + base, "--head", head)[0], 1)

    def test_pr_base_can_advance_after_branching(self):
        self.save("safe.txt", "safe\n")
        original = self.commit()
        self.git("checkout", "-qb", "feature")
        self.save("feature.txt", "safe change\n")
        head = self.commit()
        self.git("checkout", "--detach", original)
        self.save("main.txt", self.personal_path())
        base = self.commit()
        code, report = self.check("changes", "--base", base, "--head", head)
        self.assertEqual(code, 0)
        self.assertEqual(report["merge_base"], original)

    def test_invalid_and_non_object_manifests_fail_without_values(self):
        for content in ["[]", "null", '{"files":["."]}', '{"files":["../private"]}']:
            self.save("work/manifest.json", content)
            code, report = self.check("release", "--manifest", str(self.root / "work/manifest.json"))
            self.assertEqual(code, 1)
            self.assertNotIn("../private", json.dumps(report))

    def test_transient_forbidden_commit_blocks_before_scanner(self):
        self.save("safe.txt", "safe")
        base = self.commit()
        self.save("private.sqlite3", "must not read")
        self.commit()
        self.git("rm", "private.sqlite3")
        head = self.commit()
        self.scanner.reset_mock()
        code, report = self.check("changes", "--base", base, "--head", head)
        self.assertEqual(code, 1)
        self.scanner.assert_not_called()
        self.assertTrue(report["blocked"])

    def test_merge_only_forbidden_file_blocks_before_scanner(self):
        self.save("safe.txt", "base\n")
        original = self.commit()
        self.git("checkout", "-qb", "side")
        self.save("side.txt", "side\n")
        self.commit()
        self.git("checkout", "--detach", original)
        self.save("safe.txt", "main\n")
        base = self.commit()
        self.git("merge", "--no-ff", "--no-commit", "side")
        self.save("private.sqlite3", "must not read")
        head = self.commit()
        self.scanner.reset_mock()
        self.assertEqual(self.check("changes", "--base", base, "--head", head)[0], 1)
        self.scanner.assert_not_called()

    def test_full_interval_is_forwarded_even_when_final_patch_is_empty(self):
        self.save("safe.txt", "safe\n")
        base = self.commit()
        self.save("safe.txt", "intermediate secret\n")
        self.commit()
        self.save("safe.txt", "safe\n")
        head = self.commit()
        self.scanner.return_value = {"version": CHECKS.VERSION, "exit_code": 1, "findings": [{"rule": "gitleaks:test", "path": "safe.txt", "line": 1}]}
        self.assertEqual(self.check("changes", "--base", base, "--head", head)[0], 1)
        self.assertEqual(self.scanner.call_args.args[-1], base + ".." + head)

    def test_personal_path_introduced_then_deleted_is_reported_at_commit(self):
        self.save("safe.txt", "safe\n")
        base = self.commit()
        self.save("safe.txt", self.personal_path() + "\n")
        introduced = self.commit()
        self.save("safe.txt", "safe\n")
        code, report = self.check("changes", "--base", base, "--head", self.commit())
        self.assertEqual(code, 1)
        self.assertEqual(report["findings"][0]["commit"], introduced)
        self.assertNotIn(self.personal_path(), json.dumps(report))

    def test_side_branch_transient_privacy_is_not_skipped(self):
        self.save("safe.txt", "safe\n")
        original = self.commit()
        self.git("checkout", "-qb", "side")
        self.save("private.txt", self.personal_path())
        introduced = self.commit()
        self.git("rm", "private.txt")
        self.commit()
        self.git("checkout", "--detach", original)
        self.save("main.txt", "safe\n")
        base = self.commit()
        self.git("merge", "--no-ff", "-m", "test: merge side", "side")
        code, report = self.check("changes", "--base", base, "--head", self.git("rev-parse", "HEAD"))
        self.assertEqual(code, 1)
        self.assertTrue(any(finding["commit"] == introduced for finding in report["findings"]))

    def test_merge_resolution_privacy_then_deleted_is_reported(self):
        self.save("safe.txt", "safe\n")
        original = self.commit()
        self.git("checkout", "-qb", "side")
        self.save("side.txt", "safe\n")
        self.commit()
        self.git("checkout", "--detach", original)
        self.save("main.txt", "safe\n")
        base = self.commit()
        self.git("merge", "--no-ff", "--no-commit", "side")
        self.save("private.txt", self.personal_path())
        introduced = self.commit()
        self.git("rm", "private.txt")
        code, report = self.check("changes", "--base", base, "--head", self.commit())
        self.assertEqual(code, 1)
        self.assertEqual(report["findings"][0]["commit"], introduced)

    def test_merging_existing_base_privacy_is_not_a_new_value(self):
        self.save("safe.txt", "safe\n")
        original = self.commit()
        self.git("checkout", "-qb", "feature")
        self.save("feature.txt", "safe\n")
        self.commit()
        self.git("checkout", "--detach", original)
        self.save("old.txt", self.personal_path())
        base = self.commit()
        self.git("checkout", "feature")
        self.git("merge", "--no-ff", "-m", "test: synchronize base", base)
        code, report = self.check("changes", "--base", base, "--head", self.git("rev-parse", "HEAD"))
        self.assertEqual(code, 0)
        self.assertEqual(report["findings"], [])

    def test_windows_trailing_aliases_rejected_before_file_access(self):
        for name in [".env ", "videos./draft.txt", "nested /safe.txt"]:
            with self.subTest(name=name), patch.object(CHECKS, "safe_file", side_effect=AssertionError("must not access alias")):
                self.assertEqual(self.release([name])[0], 1)

    @unittest.skipUnless(hasattr(ctypes, "WinDLL"), "Windows short names require Win32")
    def test_windows_short_name_cannot_read_private_candidate(self):
        self.save("progress-registry.json", "synthetic private data")
        function = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
        function.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        function.restype = ctypes.c_uint32
        buffer = ctypes.create_unicode_buffer(32768)
        count = function(str(self.root / "progress-registry.json"), buffer, len(buffer))
        self.assertGreater(count, 0)
        alias = Path(buffer.value).name
        if alias.lower() == "progress-registry.json":
            self.skipTest("8.3 alias unavailable on this volume")
        with patch.object(Path, "read_bytes", side_effect=AssertionError("must not read private alias")):
            self.assertEqual(self.release([alias])[0], 1)


if __name__ == "__main__":
    unittest.main()
