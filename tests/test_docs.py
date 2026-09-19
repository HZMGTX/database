"""The documentation makes factual claims. These check them.

Instructions that have never been run are guesses, and recovery
instructions that turn out to be guesses are worse than none at all.
"""

import hashlib
import json
import re
import sqlite3
import sys
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import REPO, VaultTestCase  # noqa: E402

from vault import files, model  # noqa: E402
from vault.cli import build_parser  # noqa: E402


class TestRecoveryInstructions(VaultTestCase):
    """RECOVERY.md promises the data is reachable without Vault."""

    def setUp(self):
        super().setUp()
        note = model.create(self.db, kind="note", title="Recoverable",
                            body="the body", props={"amount": 42},
                            tags=["work/finance"])
        other = model.create(self.db, kind="note", title="Linked")
        model.add_edge(self.db, model.resolve(self.db, note["uid"]),
                       "mentions", model.resolve(self.db, other["uid"]))
        source = self._tmp / "attached.txt"
        source.write_text("attachment bytes")
        files.attach(self.db, source)
        self.db.close()

    def test_the_documented_query_reads_every_item(self):
        """The exact SELECT quoted in RECOVERY.md, with nothing but sqlite3."""
        conn = sqlite3.connect(self.layout.db)
        conn.row_factory = sqlite3.Row
        try:
            rows = list(conn.execute(
                "SELECT id, uid, kind, title, body, props, tags_cache, "
                "created_at, updated_at FROM item WHERE deleted_at IS NULL"))
            self.assertEqual(len(rows), 3)
            # Look it up by title: SELECT without ORDER BY has no guaranteed
            # row order, and asserting on rows[0] would be a test that passes
            # for the wrong reason.
            by_title = {row["title"]: dict(row) for row in rows}
            item = by_title["Recoverable"]
            self.assertEqual(json.loads(item["props"] or "{}").get("amount"), 42)
            self.assertEqual((item["tags_cache"] or "").split(), ["work/finance"])
        finally:
            conn.close()

    def test_the_documented_edge_query_reads_links(self):
        conn = sqlite3.connect(self.layout.db)
        try:
            links = conn.execute(
                "SELECT e.rel, i.uid FROM edge e JOIN item i ON i.id = e.dst_id"
            ).fetchall()
            self.assertEqual(links[0][0], "mentions")
        finally:
            conn.close()

    def test_the_documented_revision_decoder_works(self):
        conn = sqlite3.connect(self.layout.db)
        try:
            item_id = conn.execute("SELECT item_id FROM revision LIMIT 1").fetchone()[0]
            blob = conn.execute(
                "SELECT doc_z FROM revision WHERE item_id=? ORDER BY rev DESC LIMIT 1",
                (item_id,)).fetchone()[0]
            doc = json.loads(zlib.decompress(blob).decode("utf-8"))
            self.assertIn("title", doc)
        finally:
            conn.close()

    def test_attachments_are_named_by_their_own_digest(self):
        """RECOVERY.md says the files are verifiable without the database."""
        stored = [p for p in self.layout.files.rglob("*") if p.is_file()]
        self.assertTrue(stored)
        for path in stored:
            digest = path.stem
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
            self.assertEqual(path.parent.parent.name, digest[:2])
            self.assertEqual(path.parent.name, digest[2:4])

    def test_the_tables_recovery_names_all_exist(self):
        conn = sqlite3.connect(self.layout.db)
        try:
            present = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        for table in ("item", "tag", "item_tag", "edge", "blob", "item_file",
                      "item_task", "item_event", "item_link", "item_person",
                      "revision", "change_log"):
            with self.subTest(table=table):
                self.assertIn(table, present)


class TestReadmeClaims(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.readme = (REPO / "README.md").read_text()
        cls.recovery = (REPO / "RECOVERY.md").read_text()
        cls.commands = set()
        for action in build_parser()._actions:
            if hasattr(action, "choices") and isinstance(action.choices, dict):
                cls.commands = set(action.choices.keys())
                break

    def test_both_documents_exist_and_are_substantial(self):
        self.assertGreater(len(self.readme), 3000)
        self.assertGreater(len(self.recovery), 2000)

    def test_every_vault_command_the_readme_shows_is_real(self):
        """A README that documents a command which does not exist is worse
        than one that documents nothing."""
        shown = set(re.findall(r"^\s*(?:\./)?vault ([a-z][a-z-]*)",
                               self.readme, re.M))
        unknown = sorted(shown - self.commands - {"add"})
        self.assertEqual(unknown, [], f"README shows commands that do not exist: {unknown}")

    def test_the_readme_states_the_known_limitations(self):
        """These are the things it would be easy and dishonest to omit."""
        for claim in ("No semantic search", "encryption at rest", "pdftotext"):
            with self.subTest(claim=claim):
                self.assertIn(claim, self.readme)

    def test_the_readme_warns_about_shell_redirection(self):
        """`vault find amount>5000` silently creates a file called 5000."""
        self.assertIn("redirect", self.readme)


if __name__ == "__main__":
    unittest.main()
