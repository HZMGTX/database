"""The public site, and the one claim it makes that could quietly be false.

The demo page answers queries in the browser with no server, so Vault's
query language exists there a second time, in JavaScript. Two implementations
of one grammar drift, and the drift is invisible: a query that should return
eleven items returns nine, and the page looks like it is working.

So the JavaScript is not trusted. ``site/build.py`` runs every query the demo
advertises through the real Python engine and records exactly which items
came back, in order. The test below replays those same queries through the
JavaScript with node and fails if a single item differs.

The rest checks the things a static site gets wrong: a page that references
a file that was not shipped, an external script, a stale generated corpus.
"""

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support import REPO

SITE = REPO / "site"
NODE = shutil.which("node")


def js_strings(source):
    """Every string literal in a fragment of JavaScript, in order.

    A regex over quoted runs gets this wrong the moment a double-quoted
    phrase sits inside a single-quoted literal, which is exactly what
    author:"Don Norman" is, so the quotes are tracked by hand.
    """
    out, i = [], 0
    while i < len(source):
        ch = source[i]
        if ch in "\"'":
            i += 1
            chars = []
            while i < len(source) and source[i] != ch:
                if source[i] == "\\":
                    i += 1
                chars.append(source[i])
                i += 1
            out.append("".join(chars))
        i += 1
    return out


def read(name):
    return (SITE / name).read_text("utf-8")


class SiteBuildTest(unittest.TestCase):
    """The generated data must exist and match the corpus it came from."""

    def test_generated_data_is_present(self) -> None:
        self.assertTrue((SITE / "data" / "corpus.js").exists(),
                        "run `python3 site/build.py`")
        self.assertTrue((SITE / "data" / "parity.json").exists(),
                        "run `python3 site/build.py`")

    def test_generated_corpus_is_current(self) -> None:
        """Rebuilding must not change the answers.

        This is what catches a corpus edited after the data was generated:
        the page would then advertise a query whose recorded answer no longer
        matches what the engine returns.
        """
        before = json.loads((SITE / "data" / "parity.json").read_text("utf-8"))
        result = subprocess.run(
            [sys.executable, str(SITE / "build.py")],
            capture_output=True, text=True, cwd=str(REPO))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        after = json.loads((SITE / "data" / "parity.json").read_text("utf-8"))
        self.assertEqual(before["queries"], after["queries"])
        for query in before["queries"]:
            self.assertEqual(before["answers"][query], after["answers"][query],
                             f"{query!r} changed; regenerate site/data/")

    def test_every_advertised_query_returns_something(self) -> None:
        parity = json.loads((SITE / "data" / "parity.json").read_text("utf-8"))
        empty = [q for q, hits in parity["answers"].items() if not hits]
        self.assertEqual(empty, [], "demo queries that return nothing")

    def test_every_example_the_demo_offers_is_verified(self) -> None:
        """The chips on the demo page are a promise, so they must be checked.

        A query advertised in the interface but missing from the parity list
        is one nobody has compared against the real engine, which is exactly
        the case where the JavaScript can be quietly wrong.
        """
        source = (SITE / "demo.js").read_text("utf-8")
        block = re.search(r"var EXAMPLES = \[(.*?)\n  \];", source, re.S)
        self.assertIsNotNone(block, "could not find EXAMPLES in demo.js")
        offered = js_strings(block.group(1))
        self.assertTrue(offered, "no examples parsed out of demo.js")
        verified = set(json.loads(
            (SITE / "data" / "parity.json").read_text("utf-8"))["queries"])
        unverified = [q for q in offered if q not in verified]
        self.assertEqual(unverified, [],
                         "add these to DEMO_QUERIES in site/build.py")

    def test_corpus_carries_no_real_data(self) -> None:
        """The corpus is published. Nothing from the real vault may be in it."""
        text = (SITE / "data" / "corpus.js").read_text("utf-8")
        for forbidden in ("DISCORD_BOT_TOKEN", "vyrex", "VYREX", "genesis-ai-dev",
                          "quan10042014", "sk-", "ghp_"):
            self.assertNotIn(forbidden, text)


@unittest.skipIf(NODE is None, "node is not installed")
class QueryParityTest(unittest.TestCase):
    """Every advertised query, answered twice, compared item by item."""

    @classmethod
    def setUpClass(cls) -> None:
        script = r"""
const path = require('path');
const site = process.argv[2];
const Engine = require(path.join(site, 'demo-engine.js'));
global.window = {};
require(path.join(site, 'data', 'corpus.js'));
const corpus = global.window.CORPUS;
const parity = require(path.join(site, 'data', 'parity.json'));
const index = Engine.buildIndex(corpus.items);
const out = {};
for (const query of parity.queries) {
  try {
    out[query] = Engine.run(index, query, {now: 0})
                       .hits.map(h => h.uid);
  } catch (e) {
    out[query] = {error: String(e && e.message || e)};
  }
}
process.stdout.write(JSON.stringify(out));
"""
        with tempfile.TemporaryDirectory() as tmp:
            runner = Path(tmp) / "parity.js"
            runner.write_text(script, "utf-8")
            result = subprocess.run([NODE, str(runner), str(SITE)],
                                    capture_output=True, text=True)
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        cls.js = json.loads(result.stdout)
        cls.parity = json.loads((SITE / "data" / "parity.json").read_text("utf-8"))

    def test_same_items_in_the_same_order(self) -> None:
        disagreements = []
        for query in self.parity["queries"]:
            expected = self.parity["answers"][query]
            got = self.js.get(query)
            if isinstance(got, dict):
                disagreements.append(f"{query!r}: JavaScript raised {got['error']}")
            elif got != expected:
                missing = [u for u in expected if u not in (got or [])]
                extra = [u for u in (got or []) if u not in expected]
                if missing or extra:
                    disagreements.append(
                        f"{query!r}: {len(missing)} missing, {len(extra)} extra")
                else:
                    disagreements.append(f"{query!r}: same items, different order")
        self.assertEqual(disagreements, [], "\n".join(disagreements))


class SiteAssetsTest(unittest.TestCase):
    """A static site fails by shipping a page that references a missing file."""

    PAGES = ["index.html", "demo.html"]

    def test_pages_exist(self) -> None:
        for page in self.PAGES:
            self.assertTrue((SITE / page).exists(), page)

    def test_every_local_reference_resolves(self) -> None:
        pattern = re.compile(r'(?:src|href)="([^"#?:]+)"')
        for page in self.PAGES:
            for ref in pattern.findall(read(page)):
                if ref.startswith(("http", "//", "mailto:", "#")):
                    continue
                target = ref.lstrip("/")
                if not target or target.endswith("/"):
                    continue
                if not (SITE / target).exists():
                    # Clean URLs: /demo resolves to demo.html.
                    self.assertTrue((SITE / (target + ".html")).exists(),
                                    f"{page} references missing {ref}")

    def test_nothing_is_fetched_from_a_third_party(self) -> None:
        """The site must work with no network beyond its own origin.

        The same promise the application makes. A CDN font or an analytics
        script would make the showcase contradict the thing it showcases.
        """
        for page in self.PAGES + ["site.css", "demo.css", "demo.js", "demo-engine.js"]:
            text = read(page)
            for url in re.findall(r'(?:src|href)="(https?://[^"]+)"', text):
                self.assertNotIn("cdn", url.lower(), f"{page} loads {url}")
                self.assertFalse(url.startswith("https://fonts."), f"{page}: {url}")
            self.assertNotIn("googletagmanager", text)
            self.assertNotIn("analytics.js", text)

    def test_pages_declare_a_title_and_a_description(self) -> None:
        for page in self.PAGES:
            text = read(page)
            self.assertRegex(text, r"<title>[^<]{4,}</title>", page)
            self.assertIn('name="description"', text, page)
            self.assertIn('name="viewport"', text, page)

    def test_the_demo_says_what_it_is(self) -> None:
        """A demo that looks like the real app must say that it is not.

        Vault stores a file you own. Nothing on a serverless host can do
        that, and a visitor who does not read carefully would assume the
        page in front of them is the product.
        """
        text = read("demo.html").lower()
        self.assertIn("read-only", text)
        self.assertIn("synthetic", text)

    def test_build_inputs_are_not_published(self) -> None:
        """Vercel serves the whole root directory, so exclusions are explicit.

        The generator and the corpus it reads are repository files, not site
        files. site/data/ already holds everything a visitor's browser loads.
        """
        ignored = [line.strip() for line in read(".vercelignore").splitlines()
                   if line.strip() and not line.startswith("#")]
        self.assertIn("build.py", ignored)
        self.assertIn("corpus.json", ignored)
        # Whatever is excluded must not be something a page fetches. Prose
        # may still name build.py -- the footer does -- so this looks at
        # src and href values rather than at the text.
        pattern = re.compile(r'(?:src|href)="([^"]+)"')
        for page in self.PAGES:
            for ref in pattern.findall(read(page)):
                self.assertNotIn(ref.lstrip("/"), ignored, f"{page} fetches {ref}")

    def test_vercel_config_is_valid_json(self) -> None:
        config = json.loads(read("vercel.json"))
        self.assertIn("headers", config)


if __name__ == "__main__":
    unittest.main()
