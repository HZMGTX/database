"""Export everything, import it into an empty database, compare row by row.

This is the test that turns "your data is never trapped" from a claim into
something checked on every run. If any field is lost on the way out or the
way back, ``diff-db`` finds it and this fails.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import DatabaseTestCase  # noqa: E402

from db import diffdb, files, model  # noqa: E402
from db.db import Database  # noqa: E402
from db.exporters import jsonl as export_jsonl  # noqa: E402
from db.importers import jsonl as import_jsonl  # noqa: E402
from db.migrate import migrate  # noqa: E402
from db.paths import resolve  # noqa: E402


class TestJsonlRoundTrip(DatabaseTestCase):

    def _populate(self):
        """One of everything, including the awkward cases."""
        note = model.create(
            self.db, kind="note", title="Q3 planning",
            body="Review the budget.\n\nSecond paragraph with \"quotes\" and émojis 🎉.",
            props={"amount": 8200.5, "client": "acme", "authors": ["ada", "grace"],
                   "open": True, "nested_ignored": 1},
            tags=["work/finance", "urgent"], pinned=True)
        task = model.create(
            self.db, kind="task", title="Ship the release",
            facet={"status": "todo", "due": "2026-05-01", "priority": 3},
            tags=["work"], tzid="America/Los_Angeles")
        event = model.create(
            self.db, kind="event", title="Birthday",
            facet={"starts": "2026-09-19", "all_day": True}, tags=["personal"],
            tzid="Pacific/Auckland")
        timed = model.create(
            self.db, kind="event", title="Standup",
            facet={"starts": "2026-05-01T09:30:00", "ends": "2026-05-01T09:45:00",
                   "location": "Room 2"}, tzid="Europe/London")
        link = model.create(
            self.db, kind="link", title="FTS5 docs",
            facet={"url": "https://sqlite.org/fts5.html"}, tags=["ref"])
        person = model.create(
            self.db, kind="person", title="Ada Lovelace",
            facet={"given_name": "Ada", "family_name": "Lovelace", "org": "AE",
                   "emails": ["ada@example.com"], "phones": ["+44 20 7946 0958"]})
        model.add_edge(self.db, model.resolve(self.db, note["uid"]),
                       "mentions", model.resolve(self.db, task["uid"]))
        model.add_edge(self.db, model.resolve(self.db, timed["uid"]),
                       "attended_by", model.resolve(self.db, person["uid"]))
        return [note, task, event, timed, link, person]

    def _export_then_import(self) -> Path:
        path = self._tmp / "export.jsonl"
        with open(path, "w", encoding="utf-8") as handle:
            export_jsonl.export(self.db, handle)

        target = self._tmp / "copy.db"
        layout = resolve(target)
        layout.ensure()
        other = Database(layout)
        migrate(other)
        import_jsonl.load(other, path)
        other.close()
        return target

    def test_a_full_round_trip_is_identical(self):
        self._populate()
        copy = self._export_then_import()
        result = diffdb.compare(self.layout.db, copy)
        self.assertTrue(
            result["identical"],
            f"round trip lost something:\n"
            f"  only in original: {result['only_in_left']}\n"
            f"  only in copy: {result['only_in_right']}\n"
            f"  differing: {result['differences']}")

    def test_uids_are_preserved_so_links_survive(self):
        made = self._populate()
        copy = self._export_then_import()
        result = diffdb.compare(self.layout.db, copy)
        self.assertEqual(result["left_items"], len(made))
        self.assertEqual(result["right_items"], len(made))

    def test_unicode_and_quotes_survive(self):
        model.create(self.db, title='He said "hello" — 日本語 🎉',
                     body="line one\nline two\ttabbed")
        copy = self._export_then_import()
        self.assertTrue(diffdb.compare(self.layout.db, copy)["identical"])

    def test_reimporting_the_same_file_is_idempotent(self):
        self._populate()
        path = self._tmp / "export.jsonl"
        with open(path, "w", encoding="utf-8") as handle:
            export_jsonl.export(self.db, handle)

        before = self.conn.execute("SELECT count(*) FROM item").fetchone()[0]
        result = import_jsonl.load(self.db, path)
        after = self.conn.execute("SELECT count(*) FROM item").fetchone()[0]
        self.assertEqual(before, after, "re-import duplicated items")
        self.assertEqual(result.created, 0)
        self.assertGreater(result.updated, 0)

    def test_a_dry_run_writes_nothing(self):
        self._populate()
        path = self._tmp / "export.jsonl"
        with open(path, "w", encoding="utf-8") as handle:
            export_jsonl.export(self.db, handle)

        target = self._tmp / "dry.db"
        layout = resolve(target)
        layout.ensure()
        other = Database(layout)
        migrate(other)
        try:
            result = import_jsonl.load(other, path, dry_run=True)
            self.assertTrue(result.dry_run)
            self.assertGreater(result.created, 0)
            self.assertEqual(
                other.conn().execute("SELECT count(*) FROM item").fetchone()[0], 0)
        finally:
            other.close()

    def test_the_copy_is_sound(self):
        self._populate()
        copy = self._export_then_import()
        other = Database(resolve(copy))
        try:
            conn = other.conn()
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            for table in ("item_fts", "item_trgm"):
                conn.execute(f"INSERT INTO {table}({table}, rank) VALUES('integrity-check', 1)")
        finally:
            other.close()


class TestDiffDbItself(DatabaseTestCase):
    """The comparison has to be able to fail, or the round-trip test is theatre."""

    def test_a_missing_item_is_detected(self):
        model.create(self.db, title="A")
        model.create(self.db, title="B")
        path = self._tmp / "export.jsonl"
        with open(path, "w", encoding="utf-8") as handle:
            export_jsonl.export(self.db, handle)

        target = self._tmp / "partial.db"
        layout = resolve(target)
        layout.ensure()
        other = Database(layout)
        migrate(other)
        try:
            # Import only the header and the first item.
            partial = self._tmp / "partial.jsonl"
            lines = path.read_text().splitlines()[:2]
            partial.write_text("\n".join(lines) + "\n")
            import_jsonl.load(other, partial)
        finally:
            other.close()

        result = diffdb.compare(self.layout.db, target)
        self.assertFalse(result["identical"])
        self.assertEqual(len(result["only_in_left"]), 1)

    def test_a_changed_field_is_detected(self):
        doc = model.create(self.db, title="Original", body="one")
        copy_path = self._tmp / "copy.db"
        layout = resolve(copy_path)
        layout.ensure()
        other = Database(layout)
        migrate(other)
        try:
            model.create(other, title="Different", body="two", uid=doc["uid"])
        finally:
            other.close()

        result = diffdb.compare(self.layout.db, copy_path)
        self.assertFalse(result["identical"])
        self.assertIn("title", result["differences"][0]["fields"])


if __name__ == "__main__":
    unittest.main()
