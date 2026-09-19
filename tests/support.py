"""Shared helpers for Vault's tests.

Tests use unittest from the standard library rather than pytest: the whole
point of this project is that it runs with nothing installed, and a test suite
that needs a package manager undermines that claim.
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from vault.db import Database          # noqa: E402
from vault.migrate import migrate      # noqa: E402
from vault.paths import resolve        # noqa: E402

UTC = "2026-01-01T00:00:00Z"


class VaultTestCase(unittest.TestCase):
    """A fresh, fully migrated database per test, in a temp directory."""

    migrate_db = True

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="vault-test-"))
        self.layout = resolve(self._tmp / "vault.db")
        self.layout.ensure()
        self.db = Database(self.layout)
        if self.migrate_db:
            migrate(self.db)

    def tearDown(self) -> None:
        self.db.close()
        shutil.rmtree(self._tmp, ignore_errors=True)

    # -- helpers -------------------------------------------------------------

    @property
    def conn(self):
        return self.db.conn()

    def insert_item(self, *, uid=None, kind="note", title="", body="", props="{}",
                    search_extra="", item_id=None):
        """Insert directly, bypassing model.py.

        Schema tests want to prove the *database* enforces something, so they
        must not go through the layer that also enforces it.
        """
        from vault import ids
        # `is None`, not `or`: an empty-string uid is a value a test may want
        # to prove the schema rejects, and `or` would quietly replace it with
        # a valid one and make the assertion pass for the wrong reason.
        if uid is None:
            uid = ids.uuid7()
        cur = self.conn.execute(
            "INSERT INTO item(id, uid, kind, title, body, props, search_extra, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (item_id, uid, kind, title, body, props, search_extra, UTC, UTC),
        )
        return cur.lastrowid

    def assert_all_integrity_clean(self):
        """Every integrity check Vault knows how to run, all of them."""
        self.assertEqual(self.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        for fts in ("item_fts", "item_trgm"):
            # rank=1 is the content-aware form; the cheap form does not detect
            # external-content drift at all.
            self.conn.execute(f"INSERT INTO {fts}({fts}, rank) VALUES('integrity-check', 1)")
