# Getting your data out without Vault

This page assumes the worst: Vault will not start, or you no longer have it,
or it is 2036 and Python 3 is a curiosity. Your data is fine. Here is how to
reach it.

## What you have

One file: `data/vault.db`. It is an ordinary SQLite database — the most widely
deployed database format in existence, with readers in every language, a
published file format, and a stated commitment to support it until 2050.

Attachments are beside it in `data/files/`, laid out as
`ab/cd/<sha256><ext>`, where `ab` and `cd` are the first four characters of
the digest. Every file is stored under its own SHA-256, so you can verify any
of them and identify duplicates without Vault.

## Getting everything out, right now

If Vault runs at all, this is the shortest path:

```sh
./vault export --format jsonl --out everything.jsonl   # lossless
./vault export --format md --out ./markdown            # readable anywhere
./vault backup --bundle --out vault-bundle.zip         # database + attachments
```

If it does not run, everything below works without it.

## With the sqlite3 command line

```sh
sqlite3 data/vault.db

.headers on
.mode csv
.output items.csv
SELECT uid, kind, title, body, tags_cache, created_at, updated_at FROM item
 WHERE deleted_at IS NULL;
.output stdout
.quit
```

## With Python, which needs nothing installed

```python
import sqlite3, json

conn = sqlite3.connect("data/vault.db")
conn.row_factory = sqlite3.Row

with open("everything.jsonl", "w", encoding="utf-8") as out:
    for row in conn.execute(
        "SELECT id, uid, kind, title, body, props, tags_cache, "
        "created_at, updated_at FROM item WHERE deleted_at IS NULL"
    ):
        item = dict(row)
        item["props"] = json.loads(item["props"] or "{}")
        item["tags"] = (item.pop("tags_cache") or "").split()
        item["links"] = [
            {"rel": r[0], "to": r[1]}
            for r in conn.execute(
                "SELECT e.rel, i.uid FROM edge e JOIN item i ON i.id = e.dst_id "
                "WHERE e.src_id = ?", (item.pop("id"),))
        ]
        out.write(json.dumps(item, ensure_ascii=False) + "\n")
```

## The schema, in one paragraph

Everything is a row in **`item`** (`uid`, `kind`, `title`, `body`, `props` as
JSON, `created_at`, `updated_at`, `deleted_at` — non-null means trashed).
Tags are **`tag`** joined through **`item_tag`**; `item.tags_cache` already
holds them space-separated if you just want the text. Relationships are
**`edge`** (`src_id`, `rel`, `dst_id`), read from either end. Attachments are
**`blob`** (`sha256`, `size_bytes`, `media_type`, `extracted_text`) joined
through **`item_file`**. Kinds with extra structure have a facet table —
`item_task`, `item_event`, `item_link`, `item_person`, `item_file` — keyed by
`item_id`. Past versions are **`revision`**, one zlib-compressed JSON document
per revision. The audit trail is **`change_log`**.

Timestamps are UTC ISO 8601 (`2026-09-19T14:30:00Z`). Event and task times
additionally keep the local wall time and the IANA zone they were meant in,
because converting a date to UTC and back is how an all-day event lands on the
wrong day.

Read a stored revision like this:

```python
import zlib, json
blob = conn.execute(
    "SELECT doc_z FROM revision WHERE item_id=? ORDER BY rev DESC LIMIT 1",
    (item_id,)).fetchone()[0]
print(json.loads(zlib.decompress(blob).decode("utf-8")))
```

## Checking the file is sound

```sh
sqlite3 data/vault.db "PRAGMA integrity_check;"      # expect: ok
sqlite3 data/vault.db "PRAGMA foreign_key_check;"    # expect: nothing
```

If the first says anything other than `ok`, use a backup from
`data/backups/`. Every one was verified when it was written.

## If the file will not open at all

1. Look for `data/vault.db-wal`. If it is there, the last writes are in it.
   Copying `vault.db` **without** the `-wal` file loses them — copy both, or
   let any SQLite tool open the database once, which folds the log back in.
2. Try `data/backups/`, newest first. They are ordinary databases.
3. Recover what is readable:
   ```sh
   sqlite3 data/vault.db ".recover" | sqlite3 recovered.db
   ```
4. The attachments in `data/files/` are just files. Their names are their
   SHA-256 digests, so they are readable and verifiable with no database at
   all.
