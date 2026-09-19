# Database

A self-contained personal database. One SQLite file, three interfaces, zero
dependencies.

```
./db init && ./db demo
./db find budget
./db serve
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

**Your data is never trapped.** `db export` writes JSONL, Markdown, CSV,
a single self-contained HTML page, iCalendar and vCard. The JSONL round trip
is tested on every run: export everything, import it into an empty database,
compare every field. If anything were lost, that test would fail.

**One file to back up.** The database is `data/data.db`. Copy it while
nothing is writing, or run `db backup` at any time — that uses
`VACUUM INTO`, which is safe on a live database, and verifies the copy before
recording it.

**It tells you the truth.** `db doctor` runs SQLite's integrity check, the
foreign key check, and FTS5's *content-aware* index check. Search says how it
understood your query. A file whose contents were withheld says so.

---

## Getting started

```sh
./db init          # create the database
./db demo          # load a few sample items to explore
./db find budget
```

Put it on your `PATH` so it works from anywhere:

```sh
./db install-cli   # symlinks into ~/.local/bin
```

### Capture

```sh
db add note "Q3 planning" --body "Review the budget before Friday" --tag work/finance
db add task "Ship the release" --due friday --priority 3
db add event "Standup" --starts "monday 9:30am" --location "Room 2"
db add link https://sqlite.org/fts5.html --title "FTS5 docs" --tag ref
db add person --given Ada --family Lovelace --email ada@example.com
db add file ~/Documents/contract.pdf --tag legal
```

Dates are read the way you write them: `friday`, `tomorrow 3pm`, `+7d`, `eod`,
`2026-05-01`.

### Find

```sh
db find budget
db find "kind:task status:todo due:<friday"
db find "tag:work/* amount>5000"
db find '"an exact phrase" -draft'
db today
```

> Quote any query containing `>` or `<`. Otherwise your shell treats them as
> redirects and the database never sees them.

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
db serve            # http://127.0.0.1:8787
db serve --lan      # reachable from your network, with a token
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
db import ~/notes --format md          # a folder of Markdown
db import bookmarks.html               # any browser's export
db import contacts.vcf
db import ~/Documents --format dir     # a folder of files
db import /path/to/repo --format repo  # a git repository

db export --format jsonl --out backup.jsonl
db export --format html  --out database.html   # opens with just a browser
db export --format md    --out ./markdown
```

Every import is one recorded batch: `db import list` shows them, and
`db import undo <batch>` reverses a thousand-file mistake in one command.
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
projection, a revision snapshot or an export. `db doctor` lists what was
withheld, because being quietly protected is its own problem.

---

## Connecting to other applications

the database connects to things; it does not take them over. Two projects are wired
up, both additively — neither one's data layer was touched.

### The client modules

`clients/` holds the canonical copy of each, and each is installed in its own
project, where it is the file that runs:

| Project | Installed as |
| --- | --- |
| VYREX | `src/services/remoteDbService.js` |
| genesis-ai-dev | `lib/remote-db-client/`, the package `@workspace/remote-db-client` |

They speak HTTP to a running `db serve`, so neither project's own database
is involved. Nothing in either project imports its client until you add an
import, so both are inert until you use them.

Every call fails soft. the database is a separate process somebody has to start, so
it will be down more often than it is up, and a sidecar must not be able to
fail a request that was not about it — an unreachable server gives back an
empty result rather than throwing. Pass `strict` where you would rather
handle the error; you get the HTTP status and the problem document.

See [clients/README.md](clients/README.md).

### Reading another application's database

```
db connect vyrex --db-path /path/to/vyrex.db --describe
db connect vyrex --db-path /path/to/vyrex.db
```

The connection is opened `mode=ro` with `query_only`, which SQLite enforces
itself: this cannot write to the other application's database even if asked
to. WAL means it does not block that application's own writer either, so it
is safe to run while the thing is live. Re-running reads only what is new.

## Looking after it

```sh
db backup                  # verified snapshot
db backup --bundle         # one zip: database + every attachment
db backups                 # what exists, and how old
db restore-backup <path>

db doctor                  # every integrity check
db optimize --vacuum       # statistics, index compaction, reclaim space
db gc                      # attachments nothing references (preview first)
db stats

db sql "SELECT kind, count(*) FROM item GROUP BY kind"
```

`db sql` is read-only three times over: a `mode=ro` connection, an
authorizer that permits only reads, and a timeout that stops a runaway query.
Ask the database anything; you cannot damage it from there.

### History

```sh
db history <ref>
db diff <ref> 1 4
db revert <ref> 3          # writes a NEW revision; history is append-only
db undo                    # reverses the last change, however many items
```

`undo` is keyed by transaction, not by row, so retagging two hundred items and
regretting it is one command.

---

## The hosted database

`web/` is a Postgres database with a web interface and an HTTP API, deployed
on Vercel. It holds the same shape of data as the local tool and is reachable
from anywhere, which the local one is not.

It is **not** a demo. It starts with whatever you put in it, or with the
existing data loaded through `POST /api/admin/import`, and everything written
to it is kept.

```
GET    /api/health                  up? attached? how many items?
GET    /api/stats                   counts by kind and tag
GET    /api/items?q=...             search
POST   /api/items                   write one
GET    /api/items/:ref              read one
PATCH  /api/items/:ref              change only the fields you send
DELETE /api/items/:ref              to the trash
```

Everything but `/api/health` requires `Authorization: Bearer $API_TOKEN`, and
the API refuses every request when `API_TOKEN` is unset rather than defaulting
to open.

Setup and the full API are in [web/README.md](web/README.md).

### This does not replace anything

VYREX keeps its own `better-sqlite3` store. That one is synchronous because
the file is on the same disk, and 899 call sites across 84 files depend on
that being synchronous. No network-backed database can be synchronous in
Node, so swapping it would mean rewriting all of them into async and turning
every query into a network round trip. The projects talk to this database
over HTTP instead, asynchronously and fail-soft, at whatever few places are
worth it — see [clients/](clients/).

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
findable by name, and `doctor` reports how many are unindexed. the database does not
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
