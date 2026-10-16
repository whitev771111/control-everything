"""Public rollout projection stays bound to registered children and public text."""

import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from session_panel.garden.native import GardenError
from session_panel.garden.public import MAX_FILE_BYTES, MAX_LINE_BYTES, read_public_messages
from tests.backend.test_garden import Fixture, OTHER_ROOT, OTHER_WORKER, REVIEWER, ROOT, WORKER


def line(role, text, phase="commentary", kind="response_item"):
    return {"timestamp": "2026-10-06T01:02:03Z", "type": kind,
            "payload": {"type": "message", "role": role, "phase": phase,
                        "content": [{"type": "output_text", "text": text}]}}


class PublicMessagesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = Fixture(self.temporary.name)
        self.root = self.fixture.assign()
        self.sessions = self.fixture.workspace / "sessions" / "2026" / "10" / "06"
        self.sessions.mkdir(parents=True)
        with closing(sqlite3.connect(self.fixture.native)) as db:
            db.execute("ALTER TABLE threads ADD COLUMN rollout_path TEXT")
            db.commit()

    def attach(self, actor, records, path=None):
        path = path or self.sessions / f"rollout-2026-10-06T00-00-00-{actor}.jsonl"
        path.write_bytes(b"".join(json.dumps(value, ensure_ascii=False).encode() + b"\n" for value in records))
        with closing(sqlite3.connect(self.fixture.native)) as db:
            db.execute("UPDATE threads SET rollout_path=? WHERE id=?", (str(path), actor))
            db.commit()
        return path

    def test_only_public_assistant_text_is_projected_and_bounded(self):
        records = [line("user", "PRIVATE USER"), line("developer", "PRIVATE SYSTEM"),
                   line("assistant", "PRIVATE ANALYSIS", "analysis"),
                   line("assistant", "PRIVATE TOOL", "commentary", "event_msg"),
                   line("assistant", "公开进度", "commentary"),
                   line("assistant", "公开结果", "final_answer")]
        self.attach(WORKER, records)
        before = self.root.path.read_bytes()
        result = read_public_messages(self.root, WORKER)
        self.assertEqual([item["text"] for item in result["messages"]], ["公开进度", "公开结果"])
        self.assertEqual([item["phase"] for item in result["messages"]], ["commentary", "final_answer"])
        self.assertEqual(result["root_thread_id"], ROOT)
        self.assertEqual(before, self.root.path.read_bytes())
        self.attach(WORKER, [line("assistant", str(i)) for i in range(10)])
        self.assertEqual(len(read_public_messages(self.root, WORKER)["messages"]), 6)

    def test_registration_root_and_cross_root_are_closed(self):
        self.attach(WORKER, [line("assistant", "public")])
        for actor in (ROOT, OTHER_WORKER, REVIEWER):
            with self.subTest(actor=actor), self.assertRaises(GardenError):
                read_public_messages(self.root, actor)
        with closing(sqlite3.connect(self.fixture.native)) as db:
            source = json.loads(db.execute("SELECT source FROM threads WHERE id=?", (WORKER,)).fetchone()[0])
            source["subagent"]["thread_spawn"]["parent_thread_id"] = OTHER_ROOT
            db.execute("UPDATE threads SET source=? WHERE id=?", (json.dumps(source), WORKER))
            db.commit()
        with self.assertRaises(GardenError):
            read_public_messages(self.root, WORKER)

    def test_path_and_content_fail_closed(self):
        good = self.attach(WORKER, [line("assistant", "public")])
        with closing(sqlite3.connect(self.fixture.native)) as db:
            db.execute("UPDATE threads SET rollout_path=? WHERE id=?", ("\\\\?\\" + str(good), WORKER))
            db.commit()
        self.assertEqual(read_public_messages(self.root, WORKER)["messages"][0]["text"], "public")
        for bad in (self.fixture.workspace / "outside.jsonl", self.sessions / "other.jsonl"):
            bad.write_text("{}\n", encoding="utf-8")
            with closing(sqlite3.connect(self.fixture.native)) as db:
                db.execute("UPDATE threads SET rollout_path=? WHERE id=?", (str(bad), WORKER))
                db.commit()
            with self.assertRaises(GardenError):
                read_public_messages(self.root, WORKER)
        self.attach(WORKER, [line("assistant", "public")], good)
        good.write_bytes(b"not-json\n")
        with self.assertRaises(GardenError):
            read_public_messages(self.root, WORKER)
        good.write_bytes(b"x" * (MAX_FILE_BYTES + 1))
        with self.assertRaises(GardenError):
            read_public_messages(self.root, WORKER)
        good.write_bytes(b"x" * (MAX_LINE_BYTES + 1) + b"\n")
        with self.assertRaises(GardenError):
            read_public_messages(self.root, WORKER)


if __name__ == "__main__":
    unittest.main()
