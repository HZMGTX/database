"""The single write path.

These go through model.py rather than raw SQL, because what is being tested
is the behaviour every interface inherits by funnelling through it.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import DatabaseTestCase  # noqa: E402

from db import ids, model  # noqa: E402


class TestCreate(DatabaseTestCase):

    def test_create_returns_the_composed_document(self):
        doc = model.create(self.db, kind="note", title="Hello", body="World")
        self.assertEqual(doc["kind"], "note")
        self.assertEqual(doc["rev"], 1)
        self.assertTrue(ids.is_uid(doc["uid"]))

    def test_unknown_kind_is_rejected_with_the_known_ones(self):
        with self.assertRaises(model.ValidationError) as ctx:
            model.create(self.db, kind="nonsense", title="x")
        self.assertIn("note", str(ctx.exception))

    def test_tag_ancestors_are_materialised(self):
        """Typing one deep tag must populate the whole path, or the tree view
        shows a leaf hanging off nothing."""
        model.create(self.db, title="x", tags=["work/clients/acme"])
        slugs = [r[0] for r in self.conn.execute("SELECT slug FROM tag ORDER BY slug")]
        self.assertEqual(slugs, ["work", "work/clients", "work/clients/acme"])

    def test_duplicate_json_keys_are_collapsed_before_storage(self):
        """Regression: json_valid() accepts a repeated key, json_each() then
        emits both, and the attr projection dies on its primary key.  SQLite
        prohibits subqueries in CHECK, so this can only be caught here."""
        doc = model.create(self.db, title="x", props='{"a":1,"a":2}')
        self.assertEqual(doc["props"], {"a": 2})
        rows = self.conn.execute(
            "SELECT key, vnum FROM attr WHERE item_id=(SELECT id FROM item WHERE uid=?)",
            (doc["uid"],)).fetchall()
        self.assertEqual([tuple(r) for r in rows], [("a", 2.0)])

    def test_search_extra_captures_urls_and_emails(self):
        doc = model.create(
            self.db, title="x",
            body="see https://example.com/docs/plan and mail ada@example.com")
        extra = self.conn.execute(
            "SELECT search_extra FROM item WHERE uid=?", (doc["uid"],)).fetchone()[0]
        for term in ("example.com", "ada@example.com", "docs", "plan"):
            self.assertIn(term, extra)

    def test_declared_enum_fields_are_enforced(self):
        with self.assertRaises(model.ValidationError):
            model.create(self.db, kind="task", title="x",
                         props={"status": "not-a-real-status"},
                         facet={"status": "todo"})

    def test_undeclared_properties_are_allowed(self):
        """Capture must never fail because a field has not been declared."""
        doc = model.create(self.db, title="x", props={"invented_field": 7})
        self.assertEqual(doc["props"]["invented_field"], 7)


class TestShortHandles(DatabaseTestCase):

    def test_handles_are_distinct_for_a_burst_of_items(self):
        """Regression: short handles were the uid PREFIX, which on a UUIDv7 is
        the millisecond timestamp.  2,000 ids generated in a burst shared a
        single 8-character prefix, so every handle collided."""
        docs = [model.create(self.db, title=f"Note {i}") for i in range(200)]
        handles = {ids.short(d["uid"]) for d in docs}
        self.assertEqual(len(handles), 200)

    def test_every_handle_resolves_to_its_own_item(self):
        docs = [model.create(self.db, title=f"Note {i}") for i in range(200)]
        for doc in docs:
            self.assertEqual(
                model.resolve(self.db, ids.short(doc["uid"])),
                model.resolve(self.db, doc["uid"]))

    def test_handle_lookup_uses_an_index(self):
        plan = self.conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM item "
            "WHERE substr(uid,-8)=? AND deleted_at IS NULL", ("00000000",)).fetchall()
        self.assertIn("item_uid_suffix", " ".join(r[3] for r in plan))

    def test_ambiguous_title_is_an_error_not_a_guess(self):
        model.create(self.db, title="Report January")
        model.create(self.db, title="Report February")
        with self.assertRaises(model.ItemNotFound) as ctx:
            model.resolve(self.db, "Report")
        self.assertIn("matches 2", str(ctx.exception))

    def test_exact_title_wins_over_prefix_siblings(self):
        wanted = model.create(self.db, title="Report")
        model.create(self.db, title="Report February")
        self.assertEqual(model.resolve(self.db, "Report"),
                         model.resolve(self.db, wanted["uid"]))


class TestRevisionDiscipline(DatabaseTestCase):

    def test_rev_bumps_when_only_tags_change(self):
        """The ETag is uid.rev.  If a tag edit left rev alone, a client could
        overwrite a concurrent change while its ETag still matched."""
        doc = model.create(self.db, title="x")
        item = model.resolve(self.db, doc["uid"])
        after = model.set_tags(self.db, item, ["work"])
        self.assertGreater(after, doc["rev"])

    def test_rev_bumps_on_both_endpoints_when_linked(self):
        a = model.create(self.db, title="A")
        b = model.create(self.db, title="B")
        ia, ib = model.resolve(self.db, a["uid"]), model.resolve(self.db, b["uid"])
        model.add_edge(self.db, ia, "mentions", ib)
        for doc in (model.compose(self.db, ia), model.compose(self.db, ib)):
            self.assertGreater(doc["rev"], 1)

    def test_stale_write_is_refused(self):
        doc = model.create(self.db, title="x")
        item = model.resolve(self.db, doc["uid"])
        model.update(self.db, item, title="second")
        with self.assertRaises(model.StaleWrite):
            model.update(self.db, item, title="third", expected_rev=1)

    def test_a_no_op_update_does_not_bump_rev(self):
        doc = model.create(self.db, title="same")
        item = model.resolve(self.db, doc["uid"])
        self.assertEqual(model.update(self.db, item, title="same")["rev"], doc["rev"])

    def test_every_change_writes_a_revision(self):
        doc = model.create(self.db, title="one")
        item = model.resolve(self.db, doc["uid"])
        model.update(self.db, item, title="two")
        model.update(self.db, item, title="three")
        revs = [r[0] for r in self.conn.execute(
            "SELECT rev FROM revision WHERE item_id=? ORDER BY rev", (item,))]
        self.assertEqual(revs, [1, 2, 3])


class TestFacets(DatabaseTestCase):

    def test_task_due_carries_its_timezone(self):
        doc = model.create(self.db, kind="task", title="Ship",
                           facet={"status": "todo", "due": "2026-05-01"},
                           tzid="America/Los_Angeles")
        row = self.conn.execute(
            "SELECT due_local, due_tzid, due_epoch FROM item_task "
            "WHERE item_id=(SELECT id FROM item WHERE uid=?)", (doc["uid"],)).fetchone()
        self.assertEqual(row[0], "2026-05-01T23:59:59")
        self.assertEqual(row[1], "America/Los_Angeles")
        self.assertIsNotNone(row[2])

    def test_all_day_event_keeps_a_floating_date(self):
        doc = model.create(self.db, kind="event", title="Birthday",
                           facet={"starts": "2026-09-19", "all_day": True},
                           tzid="Pacific/Auckland")
        row = self.conn.execute(
            "SELECT all_day, starts_local, starts_epoch FROM item_event "
            "WHERE item_id=(SELECT id FROM item WHERE uid=?)", (doc["uid"],)).fetchone()
        self.assertEqual((row[0], row[1], row[2]), (1, "2026-09-19", None))

    def test_url_is_normalised(self):
        doc = model.create(
            self.db, kind="link", title="Docs",
            facet={"url": "HTTPS://Example.COM:443/docs/?utm_source=news&q=1#frag"})
        norm = self.conn.execute(
            "SELECT url_norm FROM item_link WHERE item_id=(SELECT id FROM item WHERE uid=?)",
            (doc["uid"],)).fetchone()[0]
        self.assertEqual(norm, "https://example.com/docs?q=1")

    def test_event_without_a_start_is_rejected(self):
        with self.assertRaises(model.ValidationError):
            model.create(self.db, kind="event", title="x", facet={})


class TestLifecycle(DatabaseTestCase):

    def test_trashed_items_are_hidden_but_recoverable(self):
        doc = model.create(self.db, title="x")
        item = model.resolve(self.db, doc["uid"])
        model.trash(self.db, item)
        with self.assertRaises(model.ItemNotFound):
            model.resolve(self.db, doc["uid"])
        self.assertEqual(model.resolve(self.db, doc["uid"], include_trashed=True), item)
        model.restore(self.db, item)
        self.assertEqual(model.resolve(self.db, doc["uid"]), item)

    def test_restoring_into_a_url_clash_explains_itself(self):
        """The schema raises IntegrityError here; a person needs a sentence."""
        first = model.create(self.db, kind="link", title="A",
                             facet={"url": "https://example.com/x"})
        item = model.resolve(self.db, first["uid"])
        model.trash(self.db, item)
        model.create(self.db, kind="link", title="B", facet={"url": "https://example.com/x"})
        with self.assertRaises(model.ValidationError) as ctx:
            model.restore(self.db, item)
        self.assertIn("already bookmarks", str(ctx.exception))

    def test_purge_leaves_a_tombstone_and_an_audit_row(self):
        doc = model.create(self.db, kind="note", title="doomed")
        item = model.resolve(self.db, doc["uid"])
        uid = model.purge(self.db, item)
        tomb = self.conn.execute(
            "SELECT reason, title FROM tombstone WHERE uid=?", (uid,)).fetchone()
        self.assertEqual(tuple(tomb), ("purge", "doomed"))
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM change_log WHERE row_uid=? AND op='purge'", (uid,)).fetchone())

    def test_purge_records_inbound_links_before_the_cascade_removes_them(self):
        """ON DELETE CASCADE silently removes edges belonging to surviving
        items, so their stored revisions would list a link that is gone."""
        keep = model.create(self.db, title="survivor")
        doomed = model.create(self.db, title="doomed")
        ik, idm = model.resolve(self.db, keep["uid"]), model.resolve(self.db, doomed["uid"])
        model.add_edge(self.db, ik, "mentions", idm)
        model.purge(self.db, idm)
        note = self.conn.execute(
            "SELECT note FROM change_log WHERE op='purge' ORDER BY seq DESC LIMIT 1"
        ).fetchone()[0]
        self.assertIn("1 inbound", note)

    def test_everything_stays_consistent_after_a_full_lifecycle(self):
        doc = model.create(self.db, kind="note", title="x", tags=["a/b"])
        item = model.resolve(self.db, doc["uid"])
        model.update(self.db, item, title="y", props={"n": 1})
        model.set_tags(self.db, item, ["c"])
        model.trash(self.db, item)
        model.restore(self.db, item)
        model.purge(self.db, item)
        self.assert_all_integrity_clean()


class TestEdges(DatabaseTestCase):

    def test_unknown_verb_is_rejected(self):
        a, b = model.create(self.db, title="A"), model.create(self.db, title="B")
        with self.assertRaises(model.ValidationError):
            model.add_edge(self.db, model.resolve(self.db, a["uid"]),
                           "invented", model.resolve(self.db, b["uid"]))

    def test_backlink_appears_without_a_second_row(self):
        a, b = model.create(self.db, title="A"), model.create(self.db, title="B")
        ia, ib = model.resolve(self.db, a["uid"]), model.resolve(self.db, b["uid"])
        model.add_edge(self.db, ia, "mentions", ib)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM edge").fetchone()[0], 1)
        self.assertEqual(model.compose(self.db, ib)["links"]["in"][0]["rel"], "mentions")

    def test_adding_the_same_edge_twice_is_a_no_op(self):
        a, b = model.create(self.db, title="A"), model.create(self.db, title="B")
        ia, ib = model.resolve(self.db, a["uid"]), model.resolve(self.db, b["uid"])
        model.add_edge(self.db, ia, "mentions", ib)
        model.add_edge(self.db, ia, "mentions", ib)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM edge").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
