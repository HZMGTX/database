"""Reading another application's database.

These run against a stand-in built from VYREX's own applySchema.js, because
the real vyrex.db is gitignored and so is never present in a checkout. That
is a better fixture anyway: it is built from the same file the bot builds
from, and it can be seeded to any size.
"""

import json
import shutil
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import DatabaseTestCase  # noqa: E402

from db import search  # noqa: E402
from db.connectors import vyrex  # noqa: E402
from db.db import connect_readonly  # noqa: E402

SCHEMA_JS = Path("/home/user/vyrex/src/database/applySchema.js")


@unittest.skipUnless(SCHEMA_JS.is_file(), "VYREX checkout not present")
class TestVyrexConnector(DatabaseTestCase):

    def setUp(self):
        super().setUp()
        self.source = self._tmp / "vyrex.db"
        self.built = vyrex.synthesize_from_schema(SCHEMA_JS, self.source, rows_per_table=6)

    def test_the_fixture_reflects_the_real_schema(self):
        self.assertGreater(len(self.built), 25,
                           "applySchema.js should yield a substantial schema")

    def test_describe_reports_tables_without_importing(self):
        described = vyrex.describe(self.source)
        self.assertGreater(len(described["tables"]), 25)
        self.assertGreater(described["total_rows"], 100)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM item").fetchone()[0], 0)

    def test_rows_become_searchable_items(self):
        result = vyrex.connect(self.db, self.source)
        self.assertGreater(result.created, 100)
        self.assertEqual(len(result.errors), 0)
        self.assertGreater(
            len(search.search(self.db, "kind:vyrex_users").hits), 0)

    def test_row_values_are_findable_as_free_text(self):
        """Properties are indexed for field queries, but `attr` is not a
        full-text column. Someone searching for a half-remembered username is
        doing a word search, so text values go into the index too."""
        vyrex.connect(self.db, self.source)
        self.assertGreater(len(search.search(self.db, "username-3").hits), 0)

    def test_high_volume_log_tables_are_skipped_by_default(self):
        result = vyrex.connect(self.db, self.source)
        self.assertGreater(result.skipped, 0)
        self.assertEqual(
            len(search.search(self.db, "kind:vyrex_command_analytics").hits), 0)

    def test_a_second_run_reads_nothing_new(self):
        vyrex.connect(self.db, self.source)
        self.assertEqual(vyrex.connect(self.db, self.source).created, 0)

    def test_only_new_rows_are_read_after_the_source_changes(self):
        vyrex.connect(self.db, self.source)
        writer = sqlite3.connect(self.source)
        try:
            columns = [(r[1], (r[2] or "TEXT").upper())
                       for r in writer.execute("PRAGMA table_info(bounties)")]
            values = [4242 if "INT" in t else (1.5 if "REAL" in t else f"{n}-added")
                      for n, t in columns]
            writer.execute(
                f"INSERT INTO bounties ({','.join(n for n, _ in columns)}) "
                f"VALUES ({','.join('?' * len(columns))})", values)
            writer.commit()
        finally:
            writer.close()

        self.assertEqual(vyrex.connect(self.db, self.source).created, 1)

    def test_the_source_is_opened_read_only(self):
        """The guarantee the whole connector rests on. SQLite enforces this,
        so a bug in The database cannot corrupt a live bot's data."""
        reader = connect_readonly(self.source)
        try:
            for statement in ("INSERT INTO bounties DEFAULT VALUES",
                              "UPDATE bounties SET reward = 0",
                              "DROP TABLE bounties",
                              "DELETE FROM users"):
                with self.subTest(sql=statement), self.assertRaises(sqlite3.OperationalError):
                    reader.execute(statement)
        finally:
            reader.close()

    def test_the_source_is_unchanged_after_a_full_ingest(self):
        before = {}
        probe = sqlite3.connect(self.source)
        try:
            for (name,) in probe.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"):
                before[name] = probe.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
        finally:
            probe.close()

        vyrex.connect(self.db, self.source, include_noisy=True)

        after = {}
        probe = sqlite3.connect(self.source)
        try:
            for (name,) in probe.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"):
                after[name] = probe.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
            self.assertEqual(probe.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            probe.close()
        self.assertEqual(before, after)

    def test_a_dry_run_writes_nothing(self):
        result = vyrex.connect(self.db, self.source, dry_run=True)
        self.assertTrue(result.dry_run)
        self.assertGreater(result.created, 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM item").fetchone()[0], 0)

    def test_a_missing_source_explains_where_it_lives(self):
        with self.assertRaises(FileNotFoundError) as ctx:
            vyrex.connect(self.db, self._tmp / "not-there.db")
        self.assertIn("gitignored", str(ctx.exception))

    def test_the_database_stays_sound_after_ingest(self):
        vyrex.connect(self.db, self.source)
        self.assert_all_integrity_clean()


class TestClientModulesExist(unittest.TestCase):
    """The canonical copies of the two client modules.

    Each of these is also installed in its own project, where it is the file
    that actually runs; the copy here exists so the code lives beside the
    server it talks to. The tests that matter are in those projects, against
    their own runners -- what is checked here is that the copies are present,
    parse, and still have the three properties the projects depend on.
    """

    ROOT = Path(__file__).resolve().parent.parent / "clients"

    # Installed path in the project, and the canonical copy here.
    CLIENTS = {
        "vyrex/remoteDbService.js": "src/services/remoteDbService.js",
        "remote-db-client/src/index.ts": "lib/remote-db-client/src/index.ts",
    }

    def test_both_clients_are_present(self):
        for name in self.CLIENTS:
            with self.subTest(client=name):
                self.assertTrue((self.ROOT / name).is_file(), name)
        self.assertTrue((self.ROOT / "README.md").is_file())
        self.assertTrue((self.ROOT / "vyrex" / "remoteDbService.test.js").is_file())
        # VYREX does not merely carry the client, it uses it.
        self.assertTrue((self.ROOT / "vyrex" / "command" / "database.js").is_file())
        self.assertTrue((self.ROOT / "vyrex" / "command" / "databaseCommand.test.js").is_file())
        # The genesis copy is a whole workspace package, not a loose file.
        for name in ("package.json", "tsconfig.json", "README.md"):
            self.assertTrue((self.ROOT / "remote-db-client" / name).is_file(), name)

    def test_the_genesis_package_uses_the_monorepo_scope(self):
        """@genesis/ was wrong: that monorepo scopes its packages @workspace/,
        and an import under the wrong scope does not resolve at all."""
        manifest = json.loads((self.ROOT / "remote-db-client" / "package.json").read_text())
        self.assertEqual(manifest["name"], "@workspace/remote-db-client")
        self.assertEqual(manifest["exports"]["."], "./src/index.ts")

    def test_clients_fail_soft_rather_than_throwing(self):
        """A sidecar must not be able to take down what it sits beside, so the
        default path returns an empty result and `strict` opts back in."""
        for name in self.CLIENTS:
            with self.subTest(client=name):
                source = (self.ROOT / name).read_text()
                self.assertIn("strict", source)
                self.assertRegex(source, r"return (\[\]|null|\{ hits: \[\])")

    def test_clients_send_an_idempotency_key(self):
        """A timeout is the failure most likely to be retried, and a retry
        without this creates a second copy of whatever was captured."""
        for name in self.CLIENTS:
            with self.subTest(client=name):
                self.assertIn("Idempotency-Key", (self.ROOT / name).read_text())

    def test_clients_reach_the_real_database_with_nothing_configured(self):
        """A localhost default makes every call fail and read as a broken
        client rather than an unconfigured one. DB_TOKEN is the only thing
        that has to be set, because it is the only thing that is secret."""
        for name in self.CLIENTS:
            with self.subTest(client=name):
                source = (self.ROOT / name).read_text()
                self.assertNotIn("http://127.0.0.1:8787", source)
                self.assertRegex(source, r'DEFAULT_BASE = "?\'?https://')

    def test_the_command_never_leaks_the_snippet_markers(self):
        """Two invisible control characters in a Discord channel is what
        happens when a snippet is posted unprocessed."""
        source = (self.ROOT / "vyrex" / "command" / "database.js").read_text()
        self.assertIn("renderSnippet", source)
        tests = (self.ROOT / "vyrex" / "command" / "databaseCommand.test.js").read_text()
        self.assertIn("STX leaked", tests)

    def test_clients_render_the_snippet_markers(self):
        """A hit's snippet wraps matches in STX and ETX rather than markup, so
        a caller that prints one unprocessed emits two invisible control
        characters. Both clients have to offer a way out."""
        for name in self.CLIENTS:
            with self.subTest(client=name):
                source = (self.ROOT / name).read_text()
                self.assertIn("renderSnippet", source)
                self.assertIn("\\u0002", source)
                self.assertIn("\\u0003", source)

    def test_the_javascript_client_parses(self):
        """`node --check` is cheap and catches the copy landing truncated."""
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        result = subprocess.run(
            [node, "--check", str(self.ROOT / "vyrex" / "remoteDbService.js")],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
