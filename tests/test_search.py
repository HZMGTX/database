"""Search: the tokenizer, the query language, and index integrity.

The first two tests here are the ones that matter most. Both describe
defects that shipped silently in an earlier design: a search box that
returned nothing for ordinary words, and an integrity check that reported
success on a corrupted index.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import DatabaseTestCase  # noqa: E402

from db import model, search  # noqa: E402
from db.search.parse import QueryError, compile_query, fts_quote  # noqa: E402


class TestTokenizer(DatabaseTestCase):

    def setUp(self):
        super().setUp()
        model.create(self.db, title="Q3 planning",
                     body="Please review the budget. Next week is well-known.")

    def test_ordinary_word_at_end_of_sentence_matches(self):
        """Regression, and the worst defect found in review.

        The tokenizer once had tokenchars '_@.-', which makes '.' and '-'
        part of a token. Verified against this SQLite build: the body above
        returned ZERO hits for the query `budget`, because the trailing full
        stop was part of the word. This would have shipped a search box that
        silently fails on ordinary prose.
        """
        self.assertEqual(len(search.search(self.db, "budget").hits), 1)

    def test_both_halves_of_a_hyphenated_word_match(self):
        for term in ("well", "known"):
            with self.subTest(term=term):
                self.assertEqual(len(search.search(self.db, term).hits), 1)

    def test_snippets_come_back_highlighted(self):
        """snippet() returns real text only on an external-content table --
        it returns NULL on a contentless one, which is why the index stores
        content rather than just positions."""
        hit = search.search(self.db, "budget").hits[0]
        self.assertTrue(hit.snippet)
        self.assertIn("budget", hit.snippet.lower())


class TestIndexIntegrity(DatabaseTestCase):

    def test_integrity_check_detects_drift(self):
        """Regression: the whole correctness net rested on the wrong call.

        `INSERT INTO item_fts(item_fts) VALUES('integrity-check')` does NOT
        detect an external-content index that has drifted from its content
        table -- verified, it passes happily. Only the rank=1 form is
        content-aware. This asserts the form The database uses actually catches it.
        """
        import sqlite3
        model.create(self.db, title="alpha", body="alpha")
        # Drift the content table behind the index's back.
        self.conn.execute("UPDATE item SET title='omega' WHERE title='alpha'")
        self.conn.execute(
            "INSERT INTO item_fts(item_fts, rowid, title, body, tags_cache, search_extra) "
            "VALUES('delete', (SELECT id FROM item WHERE title='omega'), 'omega', "
            "'alpha', '', '')")
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("INSERT INTO item_fts(item_fts, rank) VALUES('integrity-check', 1)")

    def test_soft_delete_keeps_the_index_consistent(self):
        """Trashed rows stay indexed and are filtered at query time.
        Excluding them makes the content-aware check report corruption."""
        doc = model.create(self.db, title="findme", body="findme")
        model.trash(self.db, model.resolve(self.db, doc["uid"]))
        self.assert_all_integrity_clean()
        self.assertEqual(len(search.search(self.db, "findme").hits), 0)
        self.assertEqual(len(search.search(self.db, "findme is:trashed").hits), 1)

    def test_edits_keep_the_index_in_step(self):
        doc = model.create(self.db, title="before", body="before")
        model.update(self.db, model.resolve(self.db, doc["uid"]),
                     title="after", body="after")
        self.assert_all_integrity_clean()
        self.assertEqual(len(search.search(self.db, "before").hits), 0)
        self.assertEqual(len(search.search(self.db, "after").hits), 1)

    def test_tag_changes_reach_the_index(self):
        doc = model.create(self.db, title="x")
        model.set_tags(self.db, model.resolve(self.db, doc["uid"]), ["findable"])
        self.assert_all_integrity_clean()
        self.assertEqual(len(search.search(self.db, "findable").hits), 1)


class TestQuoting(DatabaseTestCase):

    def test_fts_syntax_in_user_input_is_neutralised(self):
        """Raw, each of these is an FTS5 syntax error. Quoted, none is."""
        model.create(self.db, title="x", body="ordinary text")
        for hostile in ("AND", "NOT", "OR", "(", ")", '"', "a OR", '"unclosed',
                        "-", "*", "NEAR(", "{title}:"):
            with self.subTest(q=hostile):
                search.search(self.db, hostile)   # must not raise

    def test_quote_doubles_embedded_quotes(self):
        self.assertEqual(fts_quote('say "hi"'), '"say ""hi"""')

    def test_a_query_of_only_exclusions_still_works(self):
        """FTS5 has no unary NOT: 'NOT x' alone is a syntax error, so a
        query that is purely exclusions has to become a SQL predicate."""
        model.create(self.db, title="keep", body="alpha")
        model.create(self.db, title="drop", body="beta")
        titles = [h.title for h in search.search(self.db, "-beta").hits]
        self.assertIn("keep", titles)
        self.assertNotIn("drop", titles)


class TestQueryLanguage(DatabaseTestCase):

    def setUp(self):
        super().setUp()
        model.create(self.db, title="Q3 planning", body="review the budget",
                     tags=["work/finance"])
        model.create(self.db, kind="task", title="Ship release",
                     facet={"status": "todo", "due": "2026-05-01"},
                     tags=["work"], tzid="UTC")
        model.create(self.db, kind="task", title="Write docs",
                     facet={"status": "done"}, tags=["work"])
        model.create(self.db, title="Invoice 118", props={"amount": 8200})
        model.create(self.db, title="Invoice 119", props={"amount": 300})
        model.create(self.db, title="Loose note")

    def _titles(self, q):
        return sorted(h.title for h in search.search(self.db, q, tzid="UTC").hits)

    def test_kind_filter(self):
        self.assertEqual(self._titles("kind:task"), ["Ship release", "Write docs"])

    def test_status_filter(self):
        self.assertEqual(self._titles("status:todo"), ["Ship release"])

    def test_tag_exact_versus_subtree(self):
        self.assertEqual(self._titles("tag:work"), ["Ship release", "Write docs"])
        self.assertEqual(
            self._titles("tag:work/*"),
            ["Q3 planning", "Ship release", "Write docs"])

    def test_numeric_property_comparison(self):
        """A property nobody declared is still range-queryable, because the
        trigger-maintained projection indexes it by type."""
        self.assertEqual(self._titles("amount>5000"), ["Invoice 118"])
        self.assertEqual(self._titles("amount<1000"), ["Invoice 119"])

    def test_untagged_flag(self):
        self.assertEqual(self._titles("is:untagged"),
                         ["Invoice 118", "Invoice 119", "Loose note"])

    def test_date_comparison(self):
        self.assertEqual(self._titles("due:<2026-06-01"), ["Ship release"])
        self.assertEqual(self._titles("due:>2026-06-01"), [])

    def test_phrase_is_stricter_than_loose_terms(self):
        self.assertEqual(self._titles('"review the budget"'), ["Q3 planning"])
        self.assertEqual(self._titles('"budget the review"'), [])

    def test_explain_reports_what_was_understood(self):
        page = search.search(self.db, "kind:task status:todo budget", tzid="UTC")
        joined = "; ".join(page.explain)
        self.assertIn("task", joined)
        self.assertIn("todo", joined)

    def test_unknown_flag_is_an_error_not_silence(self):
        with self.assertRaises(QueryError):
            compile_query("is:nonsense")

    def test_totals_are_capped_rather_than_counted_exactly(self):
        page = search.search(self.db, "sort:recent")
        self.assertFalse(page.total_capped)
        self.assertEqual(page.total, 6)


class TestShortAndUnsegmentedTerms(DatabaseTestCase):

    def test_two_character_cjk_is_found(self):
        """FTS5's trigram tokenizer cannot match terms under 3 characters,
        so these route to a substring scan rather than returning nothing."""
        model.create(self.db, title="予算会議", body="来週の予算会議についてのメモです")
        self.assertEqual(len(search.search(self.db, "予算").hits), 1)

    def test_the_fallback_says_it_was_a_scan(self):
        model.create(self.db, title="予算会議", body="x")
        self.assertIn("too short", search.search(self.db, "予算").note)

    def test_a_narrowed_fallback_does_not_warn(self):
        model.create(self.db, title="予算会議", body="x", tags=["work"])
        self.assertEqual(search.search(self.db, "tag:work 予算").note, "")


if __name__ == "__main__":
    unittest.main()


class AdvertisedVocabularyTest(DatabaseTestCase):
    """Whatever the error message offers has to work.

    `is:attached` and `has:props` were both listed in the "Try:" line and
    both rejected by the parser, while `is:any` worked and was never
    mentioned. Being told to try a flag and then told that flag is unknown
    is worse than a plain error, because it reads as the tool being broken
    rather than the query.

    This walks the advertised sets rather than naming the values, so adding
    a branch without adding its name -- or the reverse -- fails here.
    """

    def test_every_advertised_flag_parses(self) -> None:
        from db.search.parse import FLAG_VALUES, QueryError, compile_query

        for flag in sorted(FLAG_VALUES):
            with self.subTest(flag=flag):
                try:
                    compile_query(f"is:{flag}")
                except QueryError as exc:
                    self.fail(f"is:{flag} is advertised but rejected: {exc}")

    def test_every_advertised_structure_parses(self) -> None:
        from db.search.parse import STRUCTURE_VALUES, QueryError, compile_query

        for what in sorted(STRUCTURE_VALUES):
            with self.subTest(has=what):
                try:
                    compile_query(f"has:{what}")
                except QueryError as exc:
                    self.fail(f"has:{what} is advertised but rejected: {exc}")

    def test_an_unknown_flag_only_suggests_flags_that_work(self) -> None:
        """The suggestion list is the thing that was wrong, so check it."""
        from db.search.parse import QueryError, compile_query

        with self.assertRaises(QueryError) as caught:
            compile_query("is:nonsense")
        offered = str(caught.exception).split("Try: ")[1].split(", ")

        for flag in offered:
            with self.subTest(flag=flag):
                compile_query(f"is:{flag.strip()}")

    def test_the_flags_run_against_a_real_database(self) -> None:
        """Parsing is not enough: the SQL each flag builds must execute."""
        from db import model, search
        from db.search.parse import FLAG_VALUES, STRUCTURE_VALUES

        model.create(self.db, kind="note", title="something", body="text",
                     tags=["work"])
        model.create(self.db, kind="task", title="a task",
                     facet={"status": "todo"})

        for query in ([f"is:{f}" for f in sorted(FLAG_VALUES)] +
                      [f"has:{h}" for h in sorted(STRUCTURE_VALUES)]):
            with self.subTest(query=query):
                search.search(self.db, query)
