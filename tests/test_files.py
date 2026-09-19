"""Attachments, text extraction, and the credential check.

The secret-exclusion tests are the important ones here. They exist because
this database is meant to index repositories, and one of the repositories it
will index has a committed .env holding a live API token.
"""

import sys
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import VaultTestCase  # noqa: E402

from vault import extract, files, model, search, secretscan  # noqa: E402

LIVE_TOKEN = "MTAxNzg0NTk5MDc2NTQzMjEwOQ.GxYzAb.3f9Xk2LmQpRsTuVwXyZ01234567890abcd"
HIGH_ENTROPY = "9f8Kx2mQzR4tY7uI0pA3sD6fG1hJ5kL8"


class TestSecretScanner(unittest.TestCase):

    def test_filenames_that_are_credentials_by_convention(self):
        for name in (".env", ".env.local", "id_rsa", "server.pem", "app.key",
                     "credentials.json", "secrets.yaml", ".netrc"):
            with self.subTest(name=name):
                self.assertTrue(secretscan.scan_path(name).withhold, name)

    def test_documentation_templates_are_not_credentials(self):
        for name in (".env.example", ".env.sample", ".env.template", "README.md",
                     "config.yaml", "notes.txt"):
            with self.subTest(name=name):
                self.assertFalse(secretscan.scan_path(name).withhold, name)

    def test_token_shapes_are_caught(self):
        for text in (f"DISCORD_BOT_TOKEN={LIVE_TOKEN}",
                     "token: ghp_16C7e42F292c6912E7710c838347Ae178B4a",
                     "aws_access_key_id = AKIAIOSFODNN7QW3RTYU",
                     "-----BEGIN RSA PRIVATE KEY-----",
                     "DATABASE_URL=postgres://user:s3cr3tp4ssw0rd@host/db",
                     f'API_SECRET="{HIGH_ENTROPY}"'):
            with self.subTest(text=text[:30]):
                self.assertTrue(secretscan.scan(text).withhold, text[:40])

    def test_source_code_is_not_mistaken_for_credentials(self):
        """Regression, found by indexing a real repository.

        An earlier value pattern accepted any run of non-whitespace, so
        ordinary JavaScript tripped it constantly: 23 legitimate source files
        were withheld over lines like `const titleTokens = words.map(...)`.
        Withholding real code defeats the point of indexing a codebase.
        """
        for text in ("const titleTokens = words.map(w => w.toLowerCase());",
                     "maxOutputTokens: config.limit ?? 2048,",
                     "const { apiKey } = options;",
                     "const token = process.env.DISCORD_BOT_TOKEN;",
                     "const apiKey = options.apiKey || '';",
                     "const authorName = message.author.username;",
                     "const auth = await getAuthorization(request, session);",
                     "headers: { Authorization: `Bearer ${token}` }",
                     "const DASHBOARD_TOKEN = coreEnv.dashboard.token;",
                     "allowQueryToken: DASHBOARD_ALLOW_QUERY_TOKEN,",
                     "tokens: Number.MAX_SAFE_INTEGER,",
                     "const apiKey = resolvedApiKey;",
                     "serverWith({ token: 'super-secret-token' });"):
            with self.subTest(text=text[:36]):
                self.assertFalse(secretscan.scan(text).withhold, text)

    def test_credentials_with_digits_are_still_caught(self):
        """The counterweight to the rule above.

        Identifier- and phrase-shaped values are excluded only when they
        contain no digits. Without that condition the camelCase pattern
        swallows real tokens, because a run of capitals matches it.
        """
        for text in ("PASSWORD='Tr0ub4dor3xKcdHorseBattery'",
                     "ACCESS_TOKEN=abcdefghij1234567890KLMNOPQRST",
                     'CLIENT_SECRET="k3Jx9vQ2mN8pL4tR7wZ1aB6cD0eF5gH"',
                     "SECRET=abcdefghijklmnopqrstuvwxyzabcd"):
            with self.subTest(text=text[:36]):
                self.assertTrue(secretscan.scan(text).withhold, text)

    def test_placeholders_and_paths_are_not_secrets(self):
        """False positives cost a search hit; being noisy about them costs
        the user's trust in the whole feature."""
        for text in ("API_KEY=your-api-key-here",
                     "SECRET_TOKEN=<insert-token>",
                     "API_KEY=${MY_API_KEY}",
                     "SECRET_KEY_FILE=/etc/app/secret.key",
                     "CREDENTIALS_PATH=./config/creds.json",
                     "AUTH_BACKEND=django.contrib.auth.backends.ModelBackend",
                     "API_ENDPOINT=https://api.example.com/v1/tokens",
                     "The budget review is on Friday.",
                     "def get_token(self):\n    return self.session.token"):
            with self.subTest(text=text[:30]):
                self.assertFalse(secretscan.scan(text).withhold, text[:40])


class TestExtraction(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="vault-extract-"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_plain_text(self):
        path = self.tmp / "a.txt"
        path.write_text("budget report")
        self.assertEqual(extract.extract_file(path).text, "budget report")

    def test_html_drops_script_and_style_but_keeps_alt_text(self):
        path = self.tmp / "a.html"
        path.write_text("<html><head><style>p{color:red}</style></head><body>"
                        "<script>secret()</script><p>Visible</p>"
                        "<img alt='a diagram'></body></html>")
        text = extract.extract_file(path).text
        self.assertIn("Visible", text)
        self.assertIn("a diagram", text)
        self.assertNotIn("secret()", text)
        self.assertNotIn("color:red", text)

    def test_docx_is_read_with_no_dependencies(self):
        path = self.tmp / "a.docx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml",
                             '<?xml version="1.0"?><w:document xmlns:w="x"><w:body>'
                             '<w:p><w:r><w:t>Quarterly report</w:t></w:r></w:p>'
                             '</w:body></w:document>')
        self.assertIn("Quarterly report", extract.extract_file(path).text)

    def test_xlsx_resolves_the_shared_string_table(self):
        """Cells reference strings by index, so reading the sheet alone gives
        numbers and nothing else."""
        path = self.tmp / "a.xlsx"
        ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("xl/sharedStrings.xml",
                             f'<?xml version="1.0"?><sst xmlns="{ns}">'
                             f'<si><t>Client</t></si><si><t>Acme Ltd</t></si></sst>')
            archive.writestr("xl/worksheets/sheet1.xml",
                             f'<?xml version="1.0"?><worksheet xmlns="{ns}"><sheetData><row>'
                             f'<c t="s"><v>0</v></c><c t="s"><v>1</v></c><c><v>8200</v></c>'
                             f'</row></sheetData></worksheet>')
        text = extract.extract_file(path).text
        self.assertIn("Acme Ltd", text)
        self.assertIn("8200", text)

    def test_binary_is_recognised_rather_than_mangled(self):
        path = self.tmp / "a.bin"
        path.write_bytes(bytes(range(256)) * 20)
        self.assertEqual(extract.extract_file(path).status, "unsupported")

    def test_pdf_without_the_tool_says_so_instead_of_pretending(self):
        path = self.tmp / "a.pdf"
        path.write_bytes(b"%PDF-1.4\n...")
        result = extract.extract_file(path)
        if not extract.have_pdftotext():
            self.assertEqual(result.status, "unsupported")
            self.assertIn("pdftotext", result.note)

    def test_empty_file(self):
        path = self.tmp / "empty.txt"
        path.write_text("")
        self.assertEqual(extract.extract_file(path).status, "empty")


class TestAttachments(VaultTestCase):

    def setUp(self):
        super().setUp()
        self.src = self._tmp / "src"
        self.src.mkdir()

    def _write(self, name, text):
        path = self.src / name
        path.write_text(text)
        return path

    def test_a_file_is_stored_and_its_text_is_searchable(self):
        self._write("report.txt", "The Q3 budget shows revenue grew.")
        files.attach(self.db, self.src / "report.txt")
        self.assertEqual(len(search.search(self.db, "revenue").hits), 1)

    def test_identical_bytes_are_stored_once(self):
        self._write("a.txt", "same content")
        self._write("b.txt", "same content")
        first = files.attach(self.db, self.src / "a.txt")
        second = files.attach(self.db, self.src / "b.txt")
        self.assertFalse(first["blob"]["deduplicated"])
        self.assertTrue(second["blob"]["deduplicated"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM blob").fetchone()[0], 1)
        self.assertEqual(
            self.conn.execute("SELECT refcount FROM blob").fetchone()[0], 2)

    def test_a_credential_file_is_indexed_by_name_only(self):
        """The whole point. The file is findable; its contents never enter
        the item body, the search index, the projection or a revision."""
        self._write(".env", f"DISCORD_BOT_TOKEN={LIVE_TOKEN}\nPREFIX=!")
        result = files.attach(self.db, self.src / ".env")
        self.assertTrue(result["withheld"])

        self.assertEqual(len(search.search(self.db, LIVE_TOKEN[:24]).hits), 0)
        for table, column in (("item", "body"), ("item", "search_extra"),
                              ("blob", "extracted_text")):
            found = self.conn.execute(
                f"SELECT count(*) FROM {table} WHERE {column} LIKE ?",
                (f"%{LIVE_TOKEN[:16]}%",)).fetchone()[0]
            self.assertEqual(found, 0, f"the token leaked into {table}.{column}")

        # ... but it is still findable, which is the other half of the deal.
        self.assertEqual(len(search.search(self.db, "env").hits), 1)

    def test_a_secret_found_in_content_is_withheld_too(self):
        """Not just by filename: a credential in an innocuously named file
        has to be caught by reading it."""
        self._write("notes.txt", f"API_SECRET={HIGH_ENTROPY}")
        result = files.attach(self.db, self.src / "notes.txt")
        self.assertTrue(result["withheld"])
        self.assertEqual(len(search.search(self.db, HIGH_ENTROPY).hits), 0)

    def test_withholding_is_recorded_for_doctor(self):
        self._write(".env", f"TOKEN={LIVE_TOKEN}")
        files.attach(self.db, self.src / ".env")
        row = self.conn.execute(
            "SELECT content_withheld, withheld_reason FROM item_file").fetchone()
        self.assertEqual(row[0], 1)
        self.assertTrue(row[1])

    def test_ordinary_files_are_not_withheld(self):
        self._write("README.md", "# Project\n\nRun `npm install` to begin.")
        result = files.attach(self.db, self.src / "README.md")
        self.assertFalse(result["withheld"])
        self.assertEqual(len(search.search(self.db, "npm").hits), 1)

    def test_gc_previews_before_it_deletes(self):
        self._write("x.txt", "content")
        result = files.attach(self.db, self.src / "x.txt")
        model.purge(self.db, model.resolve(self.db, result["item"]["uid"]))
        preview = files.gc(self.db)
        self.assertEqual(preview["unreferenced"], 1)
        self.assertTrue(preview["dry_run"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM blob").fetchone()[0], 1)

    def test_gc_reclaims_when_asked(self):
        self._write("x.txt", "content")
        result = files.attach(self.db, self.src / "x.txt")
        sha = result["blob"]["sha256"]
        model.purge(self.db, model.resolve(self.db, result["item"]["uid"]))
        files.gc(self.db, dry_run=False)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM blob").fetchone()[0], 0)
        self.assertFalse(self.layout.blob_path(sha, ".txt").exists())

    def test_gc_reports_bytes_that_went_missing(self):
        """A recorded blob whose file has vanished is a restore situation,
        and silence about it would be worse than useless."""
        self._write("x.txt", "content")
        result = files.attach(self.db, self.src / "x.txt")
        self.layout.blob_path(result["blob"]["sha256"], ".txt").unlink()
        self.assertEqual(len(files.gc(self.db)["missing_bytes"]), 1)

    def test_everything_stays_sound_after_attaching_and_collecting(self):
        self._write("a.txt", "one")
        self._write("b.txt", "two")
        files.attach(self.db, self.src / "a.txt")
        second = files.attach(self.db, self.src / "b.txt")
        model.purge(self.db, model.resolve(self.db, second["item"]["uid"]))
        files.gc(self.db, dry_run=False)
        self.assert_all_integrity_clean()


if __name__ == "__main__":
    unittest.main()
