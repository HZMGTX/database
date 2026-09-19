"""Backups and the read-only SQL sandbox."""

import sqlite3
import sys
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import DatabaseTestCase  # noqa: E402

from db import backup, files, model, sqlsh  # noqa: E402


class TestBackups(DatabaseTestCase):

    def setUp(self):
        super().setUp()
        for n in range(10):
            model.create(self.db, title=f"Item {n}", props={"n": n})

    def test_a_backup_is_verified_before_it_is_recorded(self):
        """A backup nobody checked is a backup nobody has."""
        result = backup.take(self.db)
        self.assertTrue(result.verified)
        self.assertTrue(result.path.exists())
        self.assertIn("10 items", result.note)

    def test_a_corrupt_file_fails_verification(self):
        bad = self.layout.backups / "database-20200101T000000Z.db"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_bytes(b"SQLite format 3\x00" + b"\x00" * 900)
        verified, _ = backup._verify(bad)
        self.assertFalse(verified)

    def test_a_backup_is_a_complete_standalone_database(self):
        result = backup.take(self.db)
        conn = sqlite3.connect(result.path)
        try:
            self.assertEqual(conn.execute("SELECT count(*) FROM item").fetchone()[0], 10)
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            conn.close()

    def test_backup_is_safe_while_the_database_is_in_use(self):
        """VACUUM INTO is chosen precisely because it does not need the
        database to be idle."""
        model.create(self.db, title="during")
        result = backup.take(self.db)
        self.assertTrue(result.verified)

    def test_rotation_keeps_one_per_day_then_week_then_month(self):
        self.layout.backups.mkdir(parents=True, exist_ok=True)
        stamps = [
            "20260919T100000Z", "20260919T160000Z",   # two on one day
            "20260918T100000Z", "20260910T100000Z",
            "20260820T100000Z", "20260720T100000Z",
        ]
        for stamp in stamps:
            (self.layout.backups / f"database-{stamp}.db").write_bytes(b"x")
        result = backup.prune(self.db, keep_daily=2, keep_weekly=1, keep_monthly=2,
                              dry_run=True)
        self.assertTrue(result["removed"])
        self.assertTrue(result["kept"])
        # Nothing actually deleted in a dry run.
        self.assertEqual(len(list(self.layout.backups.glob("database-*.db"))), len(stamps))

    def test_a_bundle_carries_the_attachments_too(self):
        """A database without its attachments restores to file items
        pointing at nothing."""
        source = self._tmp / "a.txt"
        source.write_text("attachment content")
        files.attach(self.db, source)

        target = backup.bundle(self.db, self._tmp / "bundle.zip")
        with zipfile.ZipFile(target) as archive:
            names = archive.namelist()
        self.assertIn("db.db", names)
        self.assertIn("RESTORE.txt", names)
        self.assertTrue(any(n.startswith("files/") for n in names))

    def test_restore_refuses_a_backup_that_does_not_verify(self):
        bad = self._tmp / "bad.db"
        bad.write_bytes(b"not a database at all")
        with self.assertRaises(ValueError):
            backup.restore(self.db, bad, confirm=True)

    def test_restore_previews_before_confirming(self):
        result = backup.take(self.db)
        preview = backup.restore(self.db, result.path, confirm=False)
        self.assertFalse(preview["confirmed"])


class TestSqlSandbox(DatabaseTestCase):

    def setUp(self):
        super().setUp()
        model.create(self.db, title="Present")

    def test_a_read_returns_rows(self):
        result = sqlsh.run(self.db, "SELECT title FROM item")
        self.assertEqual(result["columns"], ["title"])
        self.assertEqual(result["rows"], [["Present"]])

    def test_writes_are_refused(self):
        for statement in ("DELETE FROM item",
                          "UPDATE item SET title='x'",
                          "INSERT INTO item(uid) VALUES('x')",
                          "DROP TABLE item",
                          "CREATE TABLE evil(x)"):
            with self.subTest(sql=statement), self.assertRaises(sqlsh.SqlError):
                sqlsh.run(self.db, statement)

    def test_attach_and_pragma_are_refused(self):
        """mode=ro does not block these on its own -- the authorizer does."""
        for statement in ("ATTACH DATABASE '/etc/passwd' AS p",
                          "PRAGMA journal_mode=DELETE"):
            with self.subTest(sql=statement), self.assertRaises(sqlsh.SqlError):
                sqlsh.run(self.db, statement)

    def test_only_one_statement_at_a_time(self):
        with self.assertRaises(sqlsh.SqlError):
            sqlsh.run(self.db, "SELECT 1; DELETE FROM item")

    def test_a_runaway_query_is_stopped(self):
        with self.assertRaises(sqlsh.SqlError) as ctx:
            sqlsh.run(self.db,
                      "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) "
                      "SELECT count(*) FROM c", timeout_ms=300)
        self.assertIn("longer than", str(ctx.exception))

    def test_the_database_is_unchanged_afterwards(self):
        for statement in ("DELETE FROM item", "DROP TABLE item"):
            try:
                sqlsh.run(self.db, statement)
            except sqlsh.SqlError:
                pass
        self.assertEqual(self.conn.execute("SELECT count(*) FROM item").fetchone()[0], 1)
        self.assert_all_integrity_clean()


if __name__ == "__main__":
    unittest.main()
