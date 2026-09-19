"""Constraint proofs.

These go straight at the database with raw SQL rather than through model.py.
The point is to show the *schema* enforces these things, so that they hold
from the CLI, the API, the web UI, an importer, or a hand-typed statement --
including in five years when some new code path forgets the rule.
"""

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import UTC, DatabaseTestCase  # noqa: E402


class TestFacetGuard(DatabaseTestCase):
    """The composite foreign key that makes facet invariants real."""

    def test_facet_cannot_attach_to_the_wrong_kind(self):
        note = self.insert_item(kind="note")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO item_event(item_id, kind, all_day, starts_local) "
                "VALUES (?, 'event', 1, '2026-05-01')", (note,))

    def test_kind_cannot_be_flipped_out_from_under_a_facet(self):
        event = self.insert_item(kind="event")
        self.conn.execute(
            "INSERT INTO item_event(item_id, kind, all_day, starts_local) "
            "VALUES (?, 'event', 1, '2026-05-01')", (event,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE item SET kind='note' WHERE id=?", (event,))

    def test_timed_event_must_carry_an_absolute_instant(self):
        event = self.insert_item(kind="event")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO item_event(item_id, kind, all_day, starts_local) "
                "VALUES (?, 'event', 0, '2026-05-01T09:00:00')", (event,))

    def test_all_day_event_may_float(self):
        """An all-day date must NOT be forced through a timezone.

        2026-09-19 anchored in Pacific/Auckland renders as 2026-09-18 in most
        of the world, so all-day events stay as floating local dates and the
        epoch is legitimately absent.
        """
        event = self.insert_item(kind="event")
        self.conn.execute(
            "INSERT INTO item_event(item_id, kind, all_day, starts_local) "
            "VALUES (?, 'event', 1, '2026-09-19')", (event,))
        row = self.conn.execute(
            "SELECT starts_local, starts_epoch FROM item_event WHERE item_id=?", (event,)
        ).fetchone()
        self.assertEqual(row[0], "2026-09-19")
        self.assertIsNone(row[1])

    def test_due_date_requires_its_timezone(self):
        """A bare due timestamp with no zone is what makes "due today" read as
        overdue for everyone west of UTC."""
        task = self.insert_item(kind="task")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO item_task(item_id, kind, due_local) "
                "VALUES (?, 'task', '2026-05-01T09:00:00')", (task,))

    def test_done_task_must_record_when(self):
        task = self.insert_item(kind="task")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO item_task(item_id, kind, status) VALUES (?, 'task', 'done')",
                (task,))


class TestAuditLedgerSurvivesDeletion(DatabaseTestCase):

    def test_hard_delete_writes_audit_row(self):
        """Regression: change_log.item_id once had a foreign key to item(id)
        with ON DELETE SET NULL, while an AFTER DELETE trigger inserted
        old.id.  SQLite runs the FK action first, so deleting any item died
        with "FOREIGN KEY constraint failed".

        An audit ledger must have no referential dependency on what it audits.
        """
        item = self.insert_item(kind="note", title="doomed")
        uid = self.conn.execute("SELECT uid FROM item WHERE id=?", (item,)).fetchone()[0]
        self.conn.execute(
            "INSERT INTO change_log(txn_id, at, actor, op, item_id, row_uid, patch_json) "
            "VALUES (?,?,?,?,?,?,'{}')", ("0" * 32, UTC, "test", "create", item, uid))

        self.conn.execute("DELETE FROM item WHERE id=?", (item,))

        row = self.conn.execute(
            "SELECT item_id, row_uid FROM change_log WHERE row_uid=?", (uid,)).fetchone()
        self.assertIsNotNone(row, "the audit row must outlive the item")
        self.assertEqual(row[1], uid, "row_uid is what identifies a purged item")


class TestBlobRefcounting(DatabaseTestCase):

    def _blob(self, digest="a" * 64):
        self.conn.execute(
            "INSERT INTO blob(sha256, size_bytes, created_at) VALUES (?,?,?)",
            (digest, 10, UTC))
        return self.conn.execute("SELECT id FROM blob WHERE sha256=?", (digest,)).fetchone()[0]

    def _refcount(self, blob_id):
        return self.conn.execute("SELECT refcount FROM blob WHERE id=?", (blob_id,)).fetchone()[0]

    def test_refcount_tracks_an_archive_attached_by_update(self):
        """Regression: refcount triggers covered INSERT and DELETE but not
        UPDATE.  A link item is created first and its archived copy attached
        later by UPDATE, so the refcount stayed 0 and `db gc` would delete
        bytes that were very much in use.
        """
        blob = self._blob()
        link = self.insert_item(kind="link")
        self.conn.execute(
            "INSERT INTO item_link(item_id, kind, url, url_norm) "
            "VALUES (?, 'link', 'https://example.com', 'https://example.com')", (link,))
        self.assertEqual(self._refcount(blob), 0)

        self.conn.execute(
            "UPDATE item_link SET archive_blob_id=?, fetched_at=? WHERE item_id=?",
            (blob, UTC, link))
        self.assertEqual(self._refcount(blob), 1, "gc would otherwise reclaim live bytes")

    def test_repointing_an_archive_moves_the_count(self):
        first, second = self._blob("a" * 64), self._blob("b" * 64)
        link = self.insert_item(kind="link")
        self.conn.execute(
            "INSERT INTO item_link(item_id, kind, url, url_norm, archive_blob_id) "
            "VALUES (?, 'link', 'https://e.com', 'https://e.com', ?)", (link, first))
        self.conn.execute("UPDATE item_link SET archive_blob_id=? WHERE item_id=?", (second, link))
        self.assertEqual((self._refcount(first), self._refcount(second)), (0, 1))

    def test_bytes_a_revision_still_references_cannot_be_dropped(self):
        blob = self._blob()
        f = self.insert_item(kind="file")
        self.conn.execute(
            "INSERT INTO item_file(item_id, kind, blob_id, filename) VALUES (?,'file',?,'a.txt')",
            (f, blob))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM blob WHERE id=?", (blob,))


class TestSoftDeleteAndUniqueness(DatabaseTestCase):

    def test_a_trashed_url_can_be_bookmarked_again(self):
        """Regression: a global UNIQUE(url_norm) meant bookmarking a URL,
        trashing it, then bookmarking it again failed with IntegrityError,
        because a soft delete leaves the item_link row in place.
        """
        first = self.insert_item(kind="link")
        self.conn.execute(
            "INSERT INTO item_link(item_id, kind, url, url_norm) "
            "VALUES (?, 'link', 'https://e.com', 'https://e.com')", (first,))
        self.conn.execute("UPDATE item SET deleted_at=? WHERE id=?", (UTC, first))
        self.assertEqual(
            self.conn.execute("SELECT live FROM item_link WHERE item_id=?", (first,)).fetchone()[0],
            0)

        second = self.insert_item(kind="link")
        self.conn.execute(
            "INSERT INTO item_link(item_id, kind, url, url_norm) "
            "VALUES (?, 'link', 'https://e.com', 'https://e.com')", (second,))

    def test_two_live_bookmarks_cannot_share_a_url(self):
        for i in (1, 2):
            item = self.insert_item(kind="link")
            stmt = ("INSERT INTO item_link(item_id, kind, url, url_norm) "
                    "VALUES (?, 'link', 'https://e.com', 'https://e.com')")
            if i == 1:
                self.conn.execute(stmt, (item,))
            else:
                with self.assertRaises(sqlite3.IntegrityError):
                    self.conn.execute(stmt, (item,))


class TestPropsProjection(DatabaseTestCase):

    def test_arrays_project_one_row_per_element(self):
        """Regression: array-valued props produced zero rows in attr_multi,
        so `authors:ada` never matched anything."""
        item = self.insert_item(props='{"authors":["ada","grace","alan"]}')
        rows = self.conn.execute(
            "SELECT ord, vtext FROM attr_multi WHERE item_id=? ORDER BY ord", (item,)
        ).fetchall()
        self.assertEqual([r[1] for r in rows], ["ada", "grace", "alan"])

    def test_shrinking_an_array_leaves_no_orphans(self):
        """Regression: nothing deleted from attr_multi on update, so cutting
        three authors down to one left the other two matching forever."""
        item = self.insert_item(props='{"authors":["ada","grace","alan"]}')
        self.conn.execute("UPDATE item SET props=? WHERE id=?", ('{"authors":["ada"]}', item))
        rows = self.conn.execute(
            "SELECT vtext FROM attr_multi WHERE item_id=?", (item,)).fetchall()
        self.assertEqual([r[0] for r in rows], ["ada"])

    def test_scalars_are_typed_for_range_queries(self):
        item = self.insert_item(props='{"amount":420.5,"label":"open"}')
        by_key = {r[0]: (r[1], r[2]) for r in self.conn.execute(
            "SELECT key, vtext, vnum FROM attr WHERE item_id=?", (item,))}
        self.assertEqual(by_key["amount"], (None, 420.5))
        self.assertEqual(by_key["label"], ("open", None))

    def test_removing_a_property_clears_its_projection(self):
        item = self.insert_item(props='{"amount":1,"other":2}')
        self.conn.execute("UPDATE item SET props=? WHERE id=?", ('{"amount":1}', item))
        keys = [r[0] for r in self.conn.execute(
            "SELECT key FROM attr WHERE item_id=?", (item,))]
        self.assertEqual(keys, ["amount"])


class TestTagCache(DatabaseTestCase):

    def _tag(self, slug):
        self.conn.execute(
            "INSERT INTO tag(slug, label, created_at) VALUES (?,?,?)", (slug, slug, UTC))
        return self.conn.execute("SELECT id FROM tag WHERE slug=?", (slug,)).fetchone()[0]

    def _cache(self, item):
        return self.conn.execute("SELECT tags_cache FROM item WHERE id=?", (item,)).fetchone()[0]

    def test_cache_is_deterministically_ordered(self):
        """group_concat's order follows the query plan, which changes with
        ANALYZE or a SQLite upgrade -- so an unordered cache makes `doctor`
        report corruption that is not there."""
        item = self.insert_item()
        for slug in ("work", "admin", "zebra"):
            self.conn.execute("INSERT INTO item_tag VALUES (?,?)", (item, self._tag(slug)))
        self.assertEqual(self._cache(item), "admin work zebra")

    def test_renaming_a_tag_refreshes_every_item_carrying_it(self):
        item = self.insert_item()
        tag = self._tag("work")
        self.conn.execute("INSERT INTO item_tag VALUES (?,?)", (item, tag))
        self.conn.execute("UPDATE tag SET slug='job' WHERE id=?", (tag,))
        self.assertEqual(self._cache(item), "job")

    def test_untagging_updates_the_cache(self):
        item = self.insert_item()
        tag = self._tag("work")
        self.conn.execute("INSERT INTO item_tag VALUES (?,?)", (item, tag))
        self.conn.execute("DELETE FROM item_tag WHERE item_id=? AND tag_id=?", (item, tag))
        self.assertEqual(self._cache(item), "")

    def test_malformed_slugs_are_rejected(self):
        for bad in ("/leading", "trailing/", "a//b", "Has Caps", "has space", "punc!"):
            with self.subTest(slug=bad), self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(
                    "INSERT INTO tag(slug, label, created_at) VALUES (?,?,?)", (bad, bad, UTC))


class TestValueConstraints(DatabaseTestCase):

    def test_uid_must_be_lowercase_hex_of_exactly_32(self):
        """Regression: the original check was `[0-9a-f]*`, and in GLOB `*`
        matches any sequence rather than repeating the class -- so it
        validated one character and permitted anything after it, including
        'ab; DROP TABLE item;--'.
        """
        for bad in ("ab; DROP TABLE item;--", "A" * 32, "0" * 31, "g" * 32, ""):
            with self.subTest(uid=bad), self.assertRaises(sqlite3.IntegrityError):
                self.insert_item(uid=bad)

    def test_timestamps_must_be_utc_iso8601(self):
        for bad in ("2026-09-19 12:00:00", "2026-09-19T12:00:00", "not a date",
                    "2026-09-19T12:00:00Z trailing"):
            with self.subTest(ts=bad), self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(
                    "INSERT INTO item(uid, kind, created_at, updated_at) VALUES (?,?,?,?)",
                    ("0" * 32, "note", bad, bad))

    def test_props_must_be_a_json_object(self):
        for bad in ("[1,2]", '"text"', "not json", "42"):
            with self.subTest(props=bad), self.assertRaises(sqlite3.IntegrityError):
                self.insert_item(props=bad)

    def test_strict_tables_reject_wrong_types(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO blob(sha256, size_bytes, created_at) VALUES (?,?,?)",
                ("c" * 64, "not a number", UTC))

    def test_an_item_cannot_link_to_itself(self):
        item = self.insert_item()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO edge(src_id, rel, dst_id, created_at) VALUES (?,?,?,?)",
                (item, "mentions", item, UTC))

    def test_unknown_relationship_verbs_are_rejected(self):
        a, b = self.insert_item(), self.insert_item()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO edge(src_id, rel, dst_id, created_at) VALUES (?,?,?,?)",
                (a, "invented_verb", b, UTC))


if __name__ == "__main__":
    unittest.main()
