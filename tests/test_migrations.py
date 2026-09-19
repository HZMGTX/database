"""Migration runner behaviour, and the guarantees that depend on it."""

import shutil
import sqlite3
import sys
import unittest
from pathlib import Path

from support import REPO, DatabaseTestCase  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db import SCHEMA_VERSION                                   # noqa: E402
from db.db import Database                                      # noqa: E402
from db.migrate import (MigrationError, applied_versions,       # noqa: E402
                           current_version, discover, migrate)


class TestMigrationRunner(DatabaseTestCase):

    def test_0001_applies_on_empty_db(self):
        """Regression: the audit triggers once read `FROM temp._ctx`.

        SQLite refuses to create such a trigger at all -- "trigger cannot
        reference objects in database temp" -- so migration 0001 aborted and
        `db init` never completed.  Nothing in the schema may reference the
        temp database.
        """
        self.assertEqual(current_version(self.conn), SCHEMA_VERSION)
        self.assertGreater(
            self.conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0], 20)
        schema = "\n".join(
            r[0] for r in self.conn.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL")
        )
        self.assertNotIn("temp.", schema)
        self.assertNotIn("temp._ctx", schema)

    def test_schema_version_matches_highest_migration(self):
        """SCHEMA_VERSION and the migrations on disk must not drift.

        If they do, The database refuses to open its own freshly created database.
        """
        self.assertEqual(max(m.version for m in discover()), SCHEMA_VERSION)

    def test_migration_is_idempotent(self):
        self.assertEqual(migrate(self.db), [])

    def test_fresh_database_passes_every_integrity_check(self):
        self.assert_all_integrity_clean()

    def test_seed_relationship_vocabulary_is_closed(self):
        """Every verb's inverse must itself be a verb, or backlinks render wrong."""
        missing = self.conn.execute(
            "SELECT name FROM rel WHERE inverse NOT IN (SELECT name FROM rel)"
        ).fetchall()
        self.assertEqual(missing, [])

    def test_ledger_records_every_applied_migration(self):
        applied = applied_versions(self.conn)
        self.assertEqual(sorted(applied), [m.version for m in discover()])
        for m in discover():
            self.assertEqual(applied[m.version][1], m.checksum)


class TestMigrationRefusals(DatabaseTestCase):
    """The three things the runner must refuse to do."""

    def _copy_migrations(self) -> Path:
        d = self._tmp / "mig"
        d.mkdir(exist_ok=True)
        for m in discover():
            shutil.copy(m.path, d / m.path.name)
        return d

    def test_refuses_a_changed_migration(self):
        d = self._copy_migrations()
        target = d / "0002_seed.sql"
        target.write_text(target.read_text() + "\n-- edited after the fact\n")
        with self.assertRaises(MigrationError) as ctx:
            migrate(self.db, directory=d)
        self.assertIn("has changed since it was applied", str(ctx.exception))

    def test_refuses_a_database_from_the_future(self):
        self.conn.execute("PRAGMA user_version = 999")
        with self.assertRaises(MigrationError) as ctx:
            migrate(self.db)
        self.assertIn("only understands", str(ctx.exception))

    def test_failed_migration_is_atomic(self):
        """A migration that fails halfway must leave nothing behind.

        Python's executescript commits any pending transaction before it runs,
        so the transaction has to live inside the script text.  This asserts
        that it really does: the table created before the bad statement must
        not survive, and user_version must not move.
        """
        import db.migrate as mig

        d = self._copy_migrations()
        (d / "0003_bad.sql").write_text(
            "CREATE TABLE half_applied(x INT);\nTHIS IS NOT VALID SQL;\n"
        )
        before = current_version(self.conn)
        original = mig.SCHEMA_VERSION
        mig.SCHEMA_VERSION = 3
        try:
            with self.assertRaises(MigrationError):
                migrate(self.db, directory=d)
        finally:
            mig.SCHEMA_VERSION = original

        self.assertEqual(current_version(self.conn), before)
        self.assertIsNone(
            self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='half_applied'"
            ).fetchone()
        )

    def test_duplicate_version_numbers_are_rejected(self):
        d = self._copy_migrations()
        (d / "0002_other.sql").write_text("SELECT 1;\n")
        with self.assertRaises(MigrationError) as ctx:
            discover(d)
        self.assertIn("share version", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
