"""Revisions, diffs, revert and undo."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import VaultTestCase  # noqa: E402

from vault import history, model  # noqa: E402


class TestRevisions(VaultTestCase):

    def _three_revisions(self):
        doc = model.create(self.db, title="Draft", body="first", tags=["work"])
        item = model.resolve(self.db, doc["uid"])
        model.update(self.db, item, title="Draft v2", body="second")
        model.update(self.db, item, title="Final", body="third")
        return item

    def test_every_change_is_recorded(self):
        item = self._three_revisions()
        self.assertEqual([r.rev for r in history.history(self.db, item)], [3, 2, 1])

    def test_a_revision_can_be_read_back(self):
        item = self._three_revisions()
        self.assertEqual(history.read_revision(self.db, item, 1)["body"], "first")

    def test_diff_shows_what_changed(self):
        item = self._three_revisions()
        text = history.diff_text(self.db, item, 1, 3)
        self.assertIn("-title: Draft", text)
        self.assertIn("+title: Final", text)
        self.assertIn("-first", text)
        self.assertIn("+third", text)

    def test_revert_writes_a_new_revision_rather_than_rewinding(self):
        """History is append-only. Reverting is an edit, so undoing a revert
        is just another revert."""
        item = self._three_revisions()
        doc = history.revert(self.db, item, 1)
        self.assertEqual(doc["body"], "first")
        self.assertEqual(doc["rev"], 4)
        self.assertEqual([r.rev for r in history.history(self.db, item)], [4, 3, 2, 1])

    def test_revert_is_recorded_as_a_revert(self):
        item = self._three_revisions()
        history.revert(self.db, item, 1)
        op = self.conn.execute(
            "SELECT op FROM change_log ORDER BY seq DESC LIMIT 1").fetchone()[0]
        self.assertEqual(op, "revert")


class TestUndo(VaultTestCase):

    def test_undo_reverses_a_bulk_change_as_one_operation(self):
        """The reason change_log is keyed by transaction rather than by row.

        Two hundred items retagged in one transaction come back in one undo,
        not two hundred.
        """
        items = [model.resolve(self.db, model.create(self.db, title=f"Bulk {n}",
                                                     tags=["old"])["uid"])
                 for n in range(200)]
        with self.db.write():
            for item in items:
                model.set_tags(self.db, item, ["new", "bulk"])
            txn = self.db.txn.txn_id

        self.assertEqual(model.compose(self.db, items[0])["tags"], ["bulk", "new"])
        result = history.undo(self.db, txn)
        self.assertEqual(len(result["reversed"]), 200)
        self.assertEqual(result["skipped"], [])
        for item in items:
            self.assertEqual(model.compose(self.db, item)["tags"], ["old"])

    def test_undo_restores_an_edited_field(self):
        doc = model.create(self.db, title="Original")
        item = model.resolve(self.db, doc["uid"])
        model.update(self.db, item, title="Changed")
        history.undo(self.db)
        self.assertEqual(model.compose(self.db, item)["title"], "Original")

    def test_undo_brings_back_a_trashed_item(self):
        doc = model.create(self.db, title="Oops")
        item = model.resolve(self.db, doc["uid"])
        model.trash(self.db, item)
        history.undo(self.db)
        self.assertIsNone(model.compose(self.db, item)["deleted_at"])

    def test_undo_of_a_create_trashes_rather_than_destroys(self):
        """Reversing a create must be recoverable: if undo hard-deleted, an
        accidental undo would be unrecoverable, which is the opposite of the
        point."""
        doc = model.create(self.db, title="Fresh")
        history.undo(self.db)
        item = model.resolve(self.db, doc["uid"], include_trashed=True)
        self.assertIsNotNone(model.compose(self.db, item)["deleted_at"])

    def test_undo_of_a_purge_recovers_content_from_the_tombstone(self):
        doc = model.create(self.db, title="Gone", body="valuable")
        uid = model.purge(self.db, model.resolve(self.db, doc["uid"]))
        result = history.undo(self.db)
        self.assertTrue(result["reversed"])
        recovered = model.compose(self.db, model.resolve(self.db, uid))
        self.assertEqual(recovered["body"], "valuable")

    def test_undo_removes_a_link(self):
        a = model.create(self.db, title="A")
        b = model.create(self.db, title="B")
        ia, ib = model.resolve(self.db, a["uid"]), model.resolve(self.db, b["uid"])
        model.add_edge(self.db, ia, "mentions", ib)
        history.undo(self.db)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM edge").fetchone()[0], 0)

    def test_undo_with_nothing_to_undo_says_so(self):
        with self.assertRaises(history.UndoError):
            history.undo(self.db)

    def test_undoable_lists_transactions_newest_first(self):
        model.create(self.db, title="One")
        model.create(self.db, title="Two")
        entries = history.undoable(self.db)
        self.assertGreaterEqual(len(entries), 2)
        self.assertEqual(entries[0]["ops"], ["create"])

    def test_database_stays_sound_after_undo(self):
        doc = model.create(self.db, title="x", tags=["a"])
        item = model.resolve(self.db, doc["uid"])
        model.update(self.db, item, title="y", props={"n": 1})
        history.undo(self.db)
        self.assert_all_integrity_clean()


class TestCompaction(VaultTestCase):

    def test_compaction_is_dry_run_unless_asked(self):
        """Losing history silently is worse than a large database."""
        doc = model.create(self.db, title="x")
        item = model.resolve(self.db, doc["uid"])
        for n in range(5):
            model.update(self.db, item, title=f"v{n}")
        before = self.conn.execute("SELECT count(*) FROM revision").fetchone()[0]
        history.compact(self.db, keep_per_item=2)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM revision").fetchone()[0], before)

    def test_compaction_keeps_the_first_and_the_most_recent(self):
        doc = model.create(self.db, title="v0")
        item = model.resolve(self.db, doc["uid"])
        for n in range(1, 8):
            model.update(self.db, item, title=f"v{n}")
        history.compact(self.db, keep_per_item=2, dry_run=False)
        kept = sorted(r.rev for r in history.history(self.db, item))
        self.assertIn(1, kept, "the original must survive")
        self.assertIn(8, kept, "the current revision must survive")


if __name__ == "__main__":
    unittest.main()
