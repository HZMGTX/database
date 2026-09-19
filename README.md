# Vault

A self-contained personal database. One SQLite file, three interfaces, zero
dependencies.

```
./vault init && ./vault demo
./vault find budget
./vault serve
```

Everything you keep — notes, tasks, events, links, files, people, and any kind
you invent — is one row in one table. That is why search, tagging, links,
history, undo, import and export work the same way for all of it, including
kinds that did not exist when this was written.

---

## Why it is built this way

**Nothing to install.** Python 3.9+ and nothing else. No `pip install`, no
`npm install`, no build step, no Docker, no account, no network. It works
offline and will still work offline in ten years.

**Your data is never trapped.** `vault export` writes JSONL, Markdown, CSV,
a single self-contained HTML page, iCalendar and vCard. The JSONL round trip
is tested on every run: export everything, import it into an empty database,
compare every field. If anything were lost, that test would fail.

**One file to back up.** The database is `data/vault.db`. Copy it while
nothing is writing, or run `vault backup` at any time — that uses
`VACUUM INTO`, which is safe on a live database, and verifies the copy before
recording it.

**It tells you the truth.** `vault doctor` runs SQLite's integrity check, the
foreign key check, and FTS5's *content-aware* index check. Search says how it
understood your query. A file whose contents were withheld says so.

---

## Getting started

```sh
./vault init          # create the database
./vault demo          # load a few sample items to explore
./vault find budget
```

Put it on your `PATH` so it works from anywhere:

```sh
./vault install-cli   # symlinks into ~/.local/bin
```

### Capture

```sh
vault add note "Q3 planning" --body "Review the budget before Friday" --tag work/finance
vault add task "Ship the release" --due friday --priority 3
vault add event "Standup" --starts "monday 9:30am" --location "Room 2"
vault add link https://sqlite.org/fts5.html --title "FTS5 docs" --tag ref
vault add person --given Ada --family Lovelace --email ada@example.com
vault add file ~/Documents/contract.pdf --tag legal
```

Dates are read the way you write them: `friday`, `tomorrow 3pm`, `+7d`, `eod`,
`2026-05-01`.

### Find

```sh
vault find budget
vault find "kind:task status:todo due:<friday"
vault find "tag:work/* amount>5000"
vault find '"an exact phrase" -draft'
vault today
```

> Quote any query containing `>` or `<`. Otherwise your shell treats them as
> redirects and Vault never sees them.

| Operator | Meaning |
| --- | --- |
| `kind:task` | one kind, or several with commas |
| `tag:work` · `tag:work/*` | a tag, or a tag and everything under it |
| `status:todo,doing` | task status |
| `due:<friday` · `starts:today` | dates, in your timezone |
| `amount>5000` | any property, including ones you invented |
| `is:pinned` · `is:untagged` · `is:overdue` · `is:trashed` | flags |
| `has:file` · `has:link` | structure |
| `"exact phrase"` · `-excluded` | precision |
| `sort:recent` · `limit:50` | output |

Every command takes `--json`, and exit codes mean something: `0` fine,
`1` not found, `2` usage, `3` conflict, `4` integrity, `5` refused.

### The web UI

```sh
vault serve            # http://127.0.0.1:8787
vault serve --lan      # reachable from your network, with a token
```

The interface has no settings. The palette, spacing and depth are fixed:
a deep violet-cast void, translucent panels separated by a hairline rather
than a heavy border, and cyan and violet used strictly as signal — focus,
selection, live state — never as surface. Every text colour clears WCAG AA
against what it actually sits on.

`--lan` generates a token rather than defaulting to none, because the same
database that is harmless on loopback is not harmless on shared wifi.

---

## Import and export

```sh
vault import ~/notes --format md          # a folder of Markdown
vault import bookmarks.html               # any browser's export
vault import contacts.vcf
vault import ~/Documents --format dir     # a folder of files
vault import /path/to/repo --format repo  # a git repository

vault export --format jsonl --out backup.jsonl
vault export --format html  --out vault.html   # opens with just a browser
vault export --format md    --out ./markdown
```

Every import is one recorded batch: `vault import list` shows them, and
`vault import undo <batch>` reverses a thousand-file mistake in one command.
Add `--dry-run` to see exactly what would happen first.

### Indexing code

`--format repo` reads the file list from git, so `.gitignore` is honoured and
build output never arrives. It indexes source, docs and commit history, and
parses large structured data files as *data* rather than text — a 4.8 MB
JavaScript module of game items becomes 45 queryable kinds, so
`kind:weapons value>100000` answers in milliseconds.

**Credentials are never indexed.** A file that matches a credential filename,
contains a recognised token shape, or assigns a high-entropy literal to
something named like a secret is indexed by **path and metadata only**. Its
contents never reach the item body, the search index, the attribute
projection, a revision snapshot or an export. `vault doctor` lists what was
withheld, because being quietly protected is its own problem.

---

## Connecting to other applications

Vault connects to things; it does not take them over. Two projects are wired
up, both additively — neither one's data layer was touched.

### The client modules

`clients/` holds the canonical copy of each, and each is installed in its own
project, where it is the file that runs:

| Project | Installed as |
| --- | --- |
| VYREX | `src/services/vaultService.js` |
| genesis-ai-dev | `lib/vault-client/`, the package `@workspace/vault-client` |

They speak HTTP to a running `vault serve`, so neither project's own database
is involved. Nothing in either project imports its client until you add an
import, so both are inert until you use them.

Every call fails soft. Vault is a separate process somebody has to start, so
it will be down more often than it is up, and a sidecar must not be able to
fail a request that was not about it — an unreachable server gives back an
empty result rather than throwing. Pass `strict` where you would rather
handle the error; you get the HTTP status and the problem document.

See [clients/README.md](clients/README.md).

### Reading another application's database

```
vault connect vyrex --db-path /path/to/vyrex.db --describe
vault connect vyrex --db-path /path/to/vyrex.db
```

The connection is opened `mode=ro` with `query_only`, which SQLite enforces
itself: this cannot write to the other application's database even if asked
to. WAL means it does not block that application's own writer either, so it
is safe to run while the thing is live. Re-running reads only what is new.

## Looking after it

```sh
vault backup                  # verified snapshot
vault backup --bundle         # one zip: database + every attachment
vault backups                 # what exists, and how old
vault restore-backup <path>

vault doctor                  # every integrity check
vault optimize --vacuum       # statistics, index compaction, reclaim space
vault gc                      # attachments nothing references (preview first)
vault stats

vault sql "SELECT kind, count(*) FROM item GROUP BY kind"
```

`vault sql` is read-only three times over: a `mode=ro` connection, an
authorizer that permits only reads, and a timeout that stops a runaway query.
Ask the database anything; you cannot damage it from there.

### History

```sh
vault history <ref>
vault diff <ref> 1 4
vault revert <ref> 3          # writes a NEW revision; history is append-only
vault undo                    # reverses the last change, however many items
```

`undo` is keyed by transaction, not by row, so retagging two hundred items and
regretting it is one command.

---

## The public site

`site/` is a static showcase: a landing page and a browser-only search demo.
It is deployed to Vercel, and it is deliberately **not** Vault.

Vault cannot run on a serverless host. The whole design is a database file you
own, and a serverless filesystem is thrown away between requests, so a SQLite
file written there is gone by the next one. What the site ships instead is a
read-only copy of the query engine over 70 synthetic items baked into the
page, and it says so at the top rather than letting a visitor assume the
running thing is the product.

That read-only engine is the interesting part. The query language exists there
a second time, in JavaScript, and two implementations of one grammar drift
silently: a query that should return eleven items returns nine, and the page
still looks like it works. So it is checked instead of trusted:

```
python3 site/build.py        # build a throwaway vault, export it, and record
                             # what the real engine answers for every query
                             # the demo advertises
cd tests && python3 -m unittest test_site
                             # replay those queries through the JavaScript and
                             # fail if a single item differs, in content or order
```

`tests/test_site.py` also asserts that every example offered in the interface
is one of the queries that was checked, that the published corpus contains
nothing from a real vault, and that no page fetches anything from a third
party — the site makes the same offline promise the application does.

To change the demo data, edit `site/corpus.json` and re-run `site/build.py`.

---

## Honest limitations

**No semantic search.** Real embeddings need a model download or an external
API, and both are ruled out by the no-dependency, offline, no-account design.
What ships is BM25 ranking, trigram fuzzy matching and synonym expansion:
genuinely strong lexical search, which is not the same thing. An `embedding`
table and an opt-in provider seam exist and stay empty.

**CJK search under three characters** falls back to a substring scan, because
FTS5's trigram tokenizer cannot match terms that short. The result says so
when there is nothing else to narrow by.

**PDF text needs `pdftotext`** on your `PATH`. Without it, PDFs are stored and
findable by name, and `doctor` reports how many are unindexed. Vault does not
pretend to have read them.

**No encryption at rest.** It is a plain SQLite file. Use disk encryption.

**Very common search terms are slow.** Measured on 100,000 documents: a term
in 14 documents takes 0.2 ms, one in 7,820 takes 15 ms, one in half the corpus
takes 227 ms. That is inherent to ranked retrieval — bm25 must score every
match.

---

## If this program ever stops working

See [RECOVERY.md](RECOVERY.md). Your data is in a standard SQLite database
that any tool in any language can read, and that file is the thing to keep.
