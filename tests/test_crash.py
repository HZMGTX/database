"""Crash safety.

The claim is that a power cut or a kill mid-write cannot corrupt the
database. That is worth testing rather than asserting, because it depends on
WAL, synchronous=FULL and transaction boundaries all being right at once.
"""

import os
import signal
import subprocess
import sys
import textwrap
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import REPO, VaultTestCase  # noqa: E402

from vault import model  # noqa: E402


class TestCrashSafety(VaultTestCase):

    def _kill_mid_write(self, db_path: Path) -> None:
        """Start a process writing in a loop and SIGKILL it mid-transaction.

        SIGKILL cannot be caught, so nothing gets a chance to tidy up -- which
        is exactly the scenario the durability settings exist for.
        """
        script = textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(REPO / 'src')!r})
            from vault.db import Database
            from vault.paths import resolve
            from vault import model
            db = Database(resolve({str(db_path)!r}))
            n = 0
            while True:
                model.create(db, kind="note", title="crash-%d" % n,
                             body="x" * 2000, tags=["crash"])
                n += 1
        """)
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.2)                      # let it get well into writing
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=10)

    def test_database_survives_a_kill_mid_transaction(self):
        model.create(self.db, title="written before the crash")
        self.db.close()

        self._kill_mid_write(self.layout.db)

        # Reopen from scratch, exactly as a user would after a power cut.
        from vault.db import Database
        reopened = Database(self.layout)
        try:
            conn = reopened.conn()
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            for table in ("item_fts", "item_trgm"):
                conn.execute(f"INSERT INTO {table}({table}, rank) VALUES('integrity-check', 1)")

            # The item written before the crash must still be there.
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM item WHERE title='written before the crash'"
            ).fetchone()[0], 1)

            # And the killed process must actually have committed work, or
            # this test would pass without ever exercising a crash.
            committed = conn.execute(
                "SELECT count(*) FROM item WHERE tags_cache='crash'").fetchone()[0]
            self.assertGreater(committed, 10,
                               "the subprocess did not write enough before being killed "
                               "for this to be a meaningful test")

            # And every item that did survive must be whole -- no item whose
            # tags or revision were written but whose row was not.
            orphan_revisions = conn.execute(
                "SELECT count(*) FROM revision r "
                "WHERE NOT EXISTS (SELECT 1 FROM item i WHERE i.id = r.item_id)"
            ).fetchone()[0]
            self.assertEqual(orphan_revisions, 0)

            orphan_tags = conn.execute(
                "SELECT count(*) FROM item_tag it "
                "WHERE NOT EXISTS (SELECT 1 FROM item i WHERE i.id = it.item_id)"
            ).fetchone()[0]
            self.assertEqual(orphan_tags, 0)

            # Every surviving item must be indexed, or search would lie.
            unindexed = conn.execute(
                "SELECT count(*) FROM item i WHERE NOT EXISTS "
                "(SELECT 1 FROM item_fts f WHERE f.rowid = i.id)").fetchone()[0]
            self.assertEqual(unindexed, 0)
        finally:
            reopened.close()
            self.db = reopened

    def test_a_rolled_back_transaction_leaves_nothing_behind(self):
        before = self.conn.execute("SELECT count(*) FROM item").fetchone()[0]
        try:
            with self.db.write():
                model.create(self.db, title="doomed", tags=["a", "b"])
                raise RuntimeError("simulated failure")
        except RuntimeError:
            pass
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM item").fetchone()[0], before)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM change_log").fetchone()[0], 0)
        self.assert_all_integrity_clean()


if __name__ == "__main__":
    unittest.main()
