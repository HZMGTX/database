"""The command line.

These drive main() with argv, the way a shell does, so they cover argument
parsing and exit codes rather than just the functions underneath.
"""

import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import VaultTestCase  # noqa: E402

from vault import cli  # noqa: E402


class CliTestCase(VaultTestCase):
    """Runs commands against this test's own database."""

    def run_cli(self, *argv, expect=cli.EXIT_OK):
        out, err = io.StringIO(), io.StringIO()
        full = list(argv) + ["--db", str(self.layout.db)]
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(full)
        if expect is not None:
            self.assertEqual(
                code, expect,
                f"`vault {' '.join(argv)}` exited {code}, expected {expect}\n"
                f"stdout: {out.getvalue()}\nstderr: {err.getvalue()}")
        return code, out.getvalue(), err.getvalue()

    def run_json(self, *argv, expect=cli.EXIT_OK):
        _, out, _ = self.run_cli(*argv, "--json", expect=expect)
        return json.loads(out)


class TestCapture(CliTestCase):

    def test_add_and_find_a_note(self):
        self.run_cli("add", "note", "Q3 planning", "--body", "review the budget")
        payload = self.run_json("find", "budget")
        self.assertEqual(payload["hits"][0]["title"], "Q3 planning")

    def test_title_does_not_need_quoting(self):
        """nargs='*' means `vault add note Q3 planning` works unquoted, which
        is what anyone types the first time."""
        self.run_cli("add", "note", "Q3", "planning", "notes")
        payload = self.run_json("find", "planning")
        self.assertEqual(payload["hits"][0]["title"], "Q3 planning notes")

    def test_body_can_come_from_stdin(self):
        original, sys.stdin = sys.stdin, io.StringIO("piped body text")
        try:
            self.run_cli("add", "note", "Piped", "--body", "-")
        finally:
            sys.stdin = original
        self.assertEqual(self.run_json("find", "piped")["hits"][0]["title"], "Piped")

    def test_properties_keep_their_type(self):
        self.run_cli("add", "note", "Invoice", "--prop", "amount=8200")
        self.assertEqual(len(self.run_json("find", "amount>5000")["hits"]), 1)
        self.assertEqual(len(self.run_json("find", "amount>9000", expect=cli.EXIT_NOT_FOUND)["hits"]), 0)

    def test_task_due_date_is_parsed(self):
        self.run_cli("add", "task", "Ship", "--due", "2026-05-01", "--tz", "UTC")
        doc = self.run_json("show", "Ship")
        self.assertEqual(doc["facet"]["due_local"], "2026-05-01T23:59:59")

    def test_link_url_may_be_positional(self):
        self.run_cli("add", "link", "https://sqlite.org/fts5.html", "--title", "FTS5")
        doc = self.run_json("show", "FTS5")
        self.assertEqual(doc["facet"]["url"], "https://sqlite.org/fts5.html")


class TestTagging(CliTestCase):

    def test_removing_a_tag_is_not_read_as_an_option(self):
        """Regression: `vault tag ref -personal` made argparse reject the
        command as having an unrecognised option."""
        self.run_cli("add", "note", "Notes", "--tag", "work", "--tag", "personal")
        self.run_cli("tag", "Notes", "+urgent", "-personal")
        doc = self.run_json("show", "Notes")
        self.assertEqual(sorted(doc["tags"]), ["urgent", "work"])

    def test_tag_subtree_search(self):
        self.run_cli("add", "note", "Deep", "--tag", "work/clients/acme")
        self.assertEqual(len(self.run_json("find", "tag:work/*")["hits"]), 1)
        self.assertEqual(
            len(self.run_json("find", "tag:work", expect=cli.EXIT_NOT_FOUND)["hits"]), 0)


class TestLifecycle(CliTestCase):

    def test_trash_hides_then_restore_returns(self):
        self.run_cli("add", "note", "Temporary")
        self.run_cli("rm", "Temporary")
        self.run_cli("find", "Temporary", expect=cli.EXIT_NOT_FOUND)
        self.run_cli("restore", "Temporary")
        self.run_cli("find", "Temporary")

    def test_purge_refuses_without_yes_when_not_a_terminal(self):
        """A script piping into Vault must not be able to destroy data."""
        self.run_cli("add", "note", "Doomed")
        original, sys.stdin = sys.stdin, io.StringIO("")
        try:
            self.run_cli("purge", "Doomed", expect=cli.EXIT_REFUSED)
        finally:
            sys.stdin = original
        self.run_cli("find", "Doomed")   # still there

    def test_purge_with_yes_removes_it(self):
        self.run_cli("add", "note", "Doomed")
        self.run_cli("purge", "Doomed", "--yes")
        self.run_cli("find", "Doomed", expect=cli.EXIT_NOT_FOUND)


class TestExitCodes(CliTestCase):

    def test_missing_item_is_one(self):
        self.run_cli("show", "nothing-like-this", expect=cli.EXIT_NOT_FOUND)

    def test_bad_query_is_two(self):
        self.run_cli("find", "is:nonsense", expect=cli.EXIT_USAGE)

    def test_empty_result_is_one_so_shell_conditionals_work(self):
        self.run_cli("find", "zzzznotpresent", expect=cli.EXIT_NOT_FOUND)

    def test_success_is_zero(self):
        self.run_cli("add", "note", "Present")
        self.run_cli("find", "Present", expect=cli.EXIT_OK)


class TestOutput(CliTestCase):

    def test_json_is_valid_and_strips_snippet_markers(self):
        self.run_cli("add", "note", "Marked", "--body", "the budget line")
        payload = self.run_json("find", "budget")
        blob = json.dumps(payload)
        self.assertNotIn(cli.MARK_START, blob)
        self.assertNotIn(cli.MARK_END, blob)

    def test_doctor_reports_a_clean_database(self):
        self.run_cli("demo")
        payload = self.run_json("doctor")
        self.assertTrue(payload["ok"], payload["problems"])

    def test_stats_counts_by_kind(self):
        self.run_cli("demo")
        payload = self.run_json("stats")
        self.assertGreater(payload["items"], 5)
        self.assertIn("note", payload["by_kind"])

    def test_demo_data_is_immediately_searchable(self):
        self.run_cli("demo")
        self.assertGreater(len(self.run_json("find", "budget")["hits"]), 0)

    def test_find_reports_what_it_understood(self):
        self.run_cli("demo")
        payload = self.run_json("find", "kind:task status:todo")
        self.assertTrue(any("task" in e for e in payload["understood"]))


class TestLinks(CliTestCase):

    def test_link_shows_from_both_ends(self):
        self.run_cli("add", "note", "Source")
        self.run_cli("add", "note", "Target")
        self.run_cli("link", "Source", "mentions", "Target")
        self.assertEqual(self.run_json("links", "Target")["in"][0]["rel"], "mentions")
        self.assertEqual(self.run_json("links", "Source")["out"][0]["rel"], "mentions")

    def test_unknown_verb_is_a_usage_error(self):
        self.run_cli("add", "note", "A")
        self.run_cli("add", "note", "B")
        self.run_cli("link", "A", "invented_verb", "B", expect=cli.EXIT_USAGE)


if __name__ == "__main__":
    unittest.main()
