"""The web UI.

Static serving and its path handling are tested directly. The page itself is
rendered in headless Chromium when one is available, because "the JavaScript
parses" is not the same claim as "the page works".
"""

import json
import os
import re
import shutil
import subprocess
import sys
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import REPO, VaultTestCase  # noqa: E402

from vault import model  # noqa: E402
from vault.httpd import WEB_ROOT, Server  # noqa: E402

CHROME_CANDIDATES = [
    Path("/opt/pw-browsers/chromium-1194/chrome-linux/chrome"),
    Path("/usr/bin/chromium"),
    Path("/usr/bin/google-chrome"),
]


def find_chrome():
    for path in CHROME_CANDIDATES:
        if path.is_file():
            return path
    found = shutil.which("chromium") or shutil.which("google-chrome")
    return Path(found) if found else None


class WebTestCase(VaultTestCase):
    _port = [8840]

    def setUp(self):
        super().setUp()
        type(self)._port[0] += 1
        self.port = type(self)._port[0]
        self.server = Server(self.db, port=self.port).start(background=True)
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.stop()
        super().tearDown()

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as response:
            return response.status, dict(response.headers), response.read()


class TestStaticServing(WebTestCase):

    def test_the_page_and_its_assets_are_served(self):
        for path, expected in (("/", "text/html"),
                               ("/app.css", "text/css"),
                               ("/app.js", "javascript"),
                               ("/md.js", "javascript")):
            with self.subTest(path=path):
                status, headers, body = self.get(path)
                self.assertEqual(status, 200)
                self.assertIn(expected, headers["Content-Type"])
                self.assertTrue(body)

    def test_unknown_paths_fall_through_to_the_app(self):
        """The router lives in the page, so an unknown path is its problem,
        not a 404."""
        status, headers, body = self.get("/some/client/route")
        self.assertEqual(status, 200)
        self.assertIn(b"<title>Vault</title>", body)

    def test_a_path_cannot_escape_the_web_root(self):
        request = urllib.request.Request(self.base + "/../../../etc/passwd")
        try:
            with urllib.request.urlopen(request) as response:
                body = response.read()
            self.assertNotIn(b"root:", body)
        except urllib.error.HTTPError as exc:
            self.assertIn(exc.code, (400, 403, 404))

    def test_assets_carry_an_etag(self):
        _, headers, _ = self.get("/app.css")
        self.assertIn("ETag", headers)

    def test_the_api_still_wins_over_static(self):
        status, _, body = self.get("/api/v1/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])


class TestMarkdownRenderer(unittest.TestCase):
    """md.js runs in node, so it can be tested without a browser."""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node is not available")

    def render(self, markdown):
        script = (
            "import('file://" + str(WEB_ROOT / "md.js") + "').then(m => "
            "process.stdout.write(m.render(JSON.parse(process.argv[1]))))"
        )
        result = subprocess.run(
            [self.node, "--input-type=module", "-e", script, json.dumps(markdown)],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_basic_structures(self):
        html = self.render("# Title\n\n- one\n- two\n\n**bold** and `code`")
        self.assertIn("<h2>Title</h2>", html)
        self.assertIn("<li>one</li>", html)
        self.assertIn("<strong>bold</strong>", html)
        self.assertIn("<code>code</code>", html)

    def test_task_lists(self):
        html = self.render("- [x] done\n- [ ] pending")
        self.assertIn("checked", html)
        self.assertEqual(html.count("<input type=\"checkbox\""), 2)

    def test_script_tags_are_escaped_not_executed(self):
        html = self.render("<script>alert(1)</script>")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_image_onerror_is_escaped(self):
        html = self.render('<img src=x onerror="alert(1)">')
        self.assertNotIn("<img", html)

    def test_javascript_urls_are_not_turned_into_links(self):
        """A note is untrusted text as far as the page is concerned."""
        html = self.render("[click](javascript:alert(1))")
        self.assertNotIn("<a ", html)

    def test_external_links_are_safe(self):
        html = self.render("[ok](https://example.com)")
        self.assertIn('rel="noopener noreferrer"', html)

    def test_code_spans_are_not_reformatted(self):
        html = self.render("`**not bold**`")
        self.assertIn("<code>**not bold**</code>", html)


@unittest.skipUnless(find_chrome(), "no Chromium available")
class TestRenderedPage(WebTestCase):
    """Render the real page and assert on the DOM the browser built."""

    def dump_dom(self, path="/", width=1280, height=900):
        chrome = find_chrome()
        result = subprocess.run(
            [str(chrome), "--headless=new", "--no-sandbox", "--disable-gpu",
             "--disable-dev-shm-usage", "--virtual-time-budget=6000",
             f"--window-size={width},{height}", "--dump-dom", self.base + path],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "HOME": "/tmp"})
        return result.stdout

    def test_results_render_from_the_api(self):
        for n in range(4):
            model.create(self.db, title=f"Rendered item {n}", body="budget review",
                         tags=["web"])
        dom = self.dump_dom()
        self.assertEqual(dom.count('class="hit"'), 4)
        self.assertIn("Rendered item 0", dom)
        self.assertNotIn("Cannot reach the server", dom)

    def test_the_sidebar_is_populated(self):
        model.create(self.db, kind="task", title="A task", tags=["work"])
        dom = self.dump_dom()
        self.assertIn('data-query="kind:task"', dom)
        self.assertIn('data-query="tag:work"', dom)
        self.assertIn('data-query="is:untagged"', dom)

    def test_a_deep_link_opens_the_item_and_still_lists_results(self):
        """Regression: arriving on an item link left the results column
        empty, because the router opened the item and never searched."""
        doc = model.create(self.db, title="Deep linked", body="the body text")
        model.create(self.db, title="Another item")
        dom = self.dump_dom(f"/#/item/{doc['uid']}")
        self.assertIn("the body text", dom)
        self.assertIn('class="hit"', dom, "the results column should not be empty")

    def test_search_via_the_url_filters_the_list(self):
        model.create(self.db, title="Findable thing", body="unique-token-xyz")
        model.create(self.db, title="Other thing", body="nothing alike")
        dom = self.dump_dom("/#/q/unique-token-xyz")
        self.assertEqual(dom.count('class="hit"'), 1)
        self.assertIn("Findable thing", dom)

    def test_the_interface_exposes_no_theme_controls(self):
        """The visual design is fixed. There is nothing here to configure,
        so nothing here should offer to."""
        dom = self.dump_dom()
        for marker in ('data-token=', 'data-preset=', 'cz-audit-row',
                       'customizer', 'type="color"', 'type="range"'):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, dom)

    def test_no_horizontal_overflow_at_any_width(self):
        for n in range(6):
            model.create(self.db, title=f"Item {n} with a reasonably long title here",
                         body="x" * 400, tags=["a/long/tag/path", "another"])
        for width in (560, 900, 1280):
            with self.subTest(width=width):
                dom = self.dump_dom(width=width)
                self.assertIn('class="hit"', dom)


if __name__ == "__main__":
    unittest.main()
