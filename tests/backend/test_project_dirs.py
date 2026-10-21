"""Local project directory contracts over disposable folders."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from control_everything.input_validation import ProfileError
from control_everything.profiles.profile import Profile
from control_everything.workspace import project_dirs
from tests.backend.test_profile_http import ProfileHTTPTests


class ProjectDirectoryTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.profile = Profile.open(str(self.root / "profile"))
        self.existing = self.root / "existing"
        self.existing.mkdir()
        (self.existing / "keep.txt").write_text("keep", encoding="utf-8")

    def test_inspect_preview_register_and_create(self):
        info = project_dirs.inspect({"path": str(self.existing)})
        self.assertEqual((info["status"], info["name"], info["empty"]), ("ok", "existing", False))
        preview = project_dirs.preview({"name": "new-project", "parent_path": str(self.root)})
        self.assertEqual((preview["status"], preview["exists"], preview["can_create"]), ("preview", False, True))
        registered = project_dirs.register(self.profile.store, {"name": "Existing", "path": str(self.existing)})
        self.assertEqual(registered["status"], "registered")
        self.assertEqual(registered["project"]["directories"][0]["path"], str(self.existing))
        self.assertEqual((self.existing / "keep.txt").read_text(encoding="utf-8"), "keep")
        created = project_dirs.create(self.profile.store, {
            "name": "created-project", "parent_path": str(self.root), "confirmed": True})
        self.assertEqual(created["status"], "created")
        self.assertTrue((self.root / "created-project").is_dir())
        self.assertEqual(created["project"]["name"], "created-project")

    def test_create_rejects_confirmation_name_and_existing_target(self):
        with self.assertRaises(project_dirs.DirectoryError) as error:
            project_dirs.create(self.profile.store, {"name": "x", "parent_path": str(self.root), "confirmed": False})
        self.assertEqual(error.exception.reason, "confirmation_required")
        for name in ("../escape", "bad/name", "", "."):
            with self.subTest(name=name), self.assertRaises(project_dirs.DirectoryError):
                project_dirs.create(self.profile.store, {"name": name, "parent_path": str(self.root), "confirmed": True})
        (self.root / "already").mkdir()
        with self.assertRaises(project_dirs.DirectoryError) as error:
            project_dirs.create(self.profile.store, {"name": "already", "parent_path": str(self.root), "confirmed": True})
        self.assertEqual((error.exception.reason, error.exception.status), ("directory_exists", 409))

    def test_partial_create_reports_created_directory_when_registration_fails(self):
        with patch.object(self.profile.store, "save_project", side_effect=ProfileError("invalid_project", "name")):
            result = project_dirs.create(self.profile.store, {
                "name": "partial", "parent_path": str(self.root), "confirmed": True})
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["directory_created"])
        self.assertTrue((self.root / "partial").is_dir())

    def test_picker_selection_cancel_and_busy(self):
        selected = project_dirs.pick({}, ask=lambda _title, _initial: str(self.existing))
        self.assertEqual(selected, {"status": "selected", "path": str(self.existing)})
        cancelled = project_dirs.pick({}, ask=lambda _title, _initial: "")
        self.assertEqual(cancelled, {"status": "cancelled"})
        self.assertTrue(project_dirs.PICK_LOCK.acquire(blocking=False))
        try:
            with self.assertRaises(project_dirs.DirectoryError) as error:
                project_dirs.pick({}, ask=lambda _title, _initial: str(self.existing))
            self.assertEqual((error.exception.reason, error.exception.status), ("picker_busy", 409))
        finally:
            project_dirs.PICK_LOCK.release()

    def test_relative_paths_and_missing_parent_are_rejected(self):
        for body in ({"path": "relative"}, {"path": str(self.root / "missing")}):
            with self.subTest(body=body), self.assertRaises(project_dirs.DirectoryError):
                project_dirs.inspect(body)
        with self.assertRaises(project_dirs.DirectoryError) as error:
            project_dirs.preview({"name": "x", "parent_path": str(self.root / "missing")})
        self.assertEqual(error.exception.reason, "directory_not_found")


class ProjectDirectoryHTTPTests(unittest.TestCase):
    request = ProfileHTTPTests.request

    def setUp(self):
        ProfileHTTPTests.setUp(self)
        self.parent = self.root / "parent"
        self.parent.mkdir()

    def test_inspect_register_preview_create_and_picker_routes(self):
        status, inspected = self.request("/api/workspace/projects/inspect", "POST", {"path": str(self.parent)})
        self.assertEqual((status, inspected["status"]), (200, "ok"))
        status, preview = self.request("/api/workspace/projects/create-preview", "POST",
                                      {"name": "api-created", "parent_path": str(self.parent)})
        self.assertEqual((status, preview["can_create"]), (200, True))
        status, created = self.request("/api/workspace/projects/create", "POST",
                                      {"name": "api-created", "parent_path": str(self.parent), "confirmed": True})
        self.assertEqual((status, created["status"]), (200, "created"))
        self.assertEqual(created["project"]["directories"][0]["path"], str(self.parent / "api-created"))
        status, registered = self.request("/api/workspace/projects/register", "POST",
                                         {"name": "Existing", "path": str(self.parent)})
        self.assertEqual((status, registered["status"]), (200, "registered"))
        with patch.object(project_dirs, "pick", return_value={"status": "cancelled"}):
            status, picked = self.request("/api/workspace/directories/pick", "POST", {})
        self.assertEqual((status, picked["status"]), (200, "cancelled"))

    def test_directory_routes_require_csrf_and_validate_body(self):
        self.assertEqual(self.request("/api/workspace/projects/inspect", "POST",
                                      {"path": str(self.parent)}, {"X-Session-CSRF": ""})[0], 403)
        self.assertEqual(self.request("/api/workspace/projects/create-preview", "POST",
                                      {"name": "x", "parent_path": "relative"})[0], 422)
        self.assertEqual(self.request("/api/workspace/projects/inspect", "POST", {"path": "relative"})[0], 422)
