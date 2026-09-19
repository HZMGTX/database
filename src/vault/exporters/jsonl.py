"""Canonical, lossless export: one JSON object per line.

Line-oriented on purpose. A 200,000-item export is written and read a line at
a time, so neither end needs the whole thing in memory, and a truncated file
still yields every complete line before the break.

The first line is a header describing the export; every line after it is one
item's composed document, with its attachment digest where it has one.
"""

import json
from typing import Any, Dict, Iterator, Optional, TextIO

from vault import SCHEMA_VERSION, __version__, dates, model
from vault.db import Database

__all__ = ["export", "iter_records"]


def iter_records(db: Database, *, query: Optional[str] = None,
                 include_trashed: bool = False) -> Iterator[Dict[str, Any]]:
    conn = db.conn()
    if query:
        from vault import search
        page = search.search(db, query, limit=1000000, want_total=False)
        ids = [conn.execute("SELECT id FROM item WHERE uid=?", (h.uid,)).fetchone()[0]
               for h in page.hits]
    else:
        clause = "" if include_trashed else " WHERE deleted_at IS NULL"
        ids = [r[0] for r in conn.execute(f"SELECT id FROM item{clause} ORDER BY id")]

    for item_id in ids:
        doc = model.compose(db, item_id)
        row = conn.execute(
            "SELECT b.sha256, f.filename FROM item_file f JOIN blob b ON b.id=f.blob_id "
            "WHERE f.item_id=?", (item_id,)).fetchone()
        if row:
            doc["attachment"] = {"sha256": row[0], "filename": row[1]}
        yield doc


def export(db: Database, out: TextIO, *, query: Optional[str] = None,
           include_trashed: bool = False, **_: Any) -> int:
    conn = db.conn()
    header = {
        "_vault": {
            "format": "jsonl", "version": 1, "app_version": __version__,
            "schema_version": SCHEMA_VERSION, "exported_at": dates.utcnow(),
            "items": conn.execute(
                "SELECT count(*) FROM item" +
                ("" if include_trashed else " WHERE deleted_at IS NULL")).fetchone()[0],
        },
        "tags": [
            {"slug": r[0], "label": r[1]}
            for r in conn.execute("SELECT slug, label FROM tag ORDER BY slug")
        ],
        "kinds": [
            {"name": r[0], "label": r[1], "plural": r[2]}
            for r in conn.execute("SELECT name, label, plural FROM kind ORDER BY name")
        ],
    }
    out.write(json.dumps(header, ensure_ascii=False, sort_keys=True) + "\n")

    count = 0
    for doc in iter_records(db, query=query, include_trashed=include_trashed):
        out.write(json.dumps(doc, ensure_ascii=False, sort_keys=True, default=str) + "\n")
        count += 1
    return count
