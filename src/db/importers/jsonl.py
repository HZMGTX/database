"""Canonical import: the counterpart to the JSONL export.

Idempotent by uid, so re-importing the same file updates rather than
duplicating. This is the path ``tests/test_roundtrip.py`` exercises, which is
what turns "your data is never trapped" from a claim into something checked
on every run.
"""

import json
from pathlib import Path
from typing import Any, Dict, Optional

from db import model
from db.db import Database
from db.importers.batch import Batch, BatchResult

__all__ = ["load"]


def _facet_for(kind: str, facet: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Turn a stored facet back into the shape model.create expects.

    The stored form splits a time into local/zone/epoch; the write path takes
    a single value and derives the other two, so the round trip has to hand
    back the local value and let it be recomputed.
    """
    if not facet:
        return None
    out = dict(facet)
    if kind == "event":
        out["starts"] = facet.get("starts_local")
        out["ends"] = facet.get("ends_local")
        out["all_day"] = bool(facet.get("all_day"))
    elif kind == "task" and facet.get("due_local"):
        out["due"] = facet["due_local"]
    return out


def load(db: Database, source: "str | Path", *, dry_run: bool = False,
         **_: Any) -> BatchResult:
    path = Path(source)
    deferred_links = []

    with Batch(db, source=str(path), format="jsonl", dry_run=dry_run) as batch:
        with open(path, "r", encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    doc = json.loads(line)
                except json.JSONDecodeError as exc:
                    batch.fail(f"line {number}: {exc}")
                    batch.record("skipped", None, f"line {number}")
                    continue

                # "_vault" is what the header was called before the rename; a file
                # exported then still has to import now.
                if "_db" in doc or "_vault" in doc:   # the header line
                    continue
                uid = doc.get("uid")
                if not uid:
                    batch.record("skipped", None, f"line {number}", "(no uid)")
                    continue

                existing = db.conn().execute(
                    "SELECT id FROM item WHERE uid=?", (uid,)).fetchone()
                if dry_run:
                    batch.record("updated" if existing else "created", uid,
                                 f"line {number}", doc.get("title", ""))
                    continue

                try:
                    if existing:
                        model.update(db, int(existing[0]),
                                     title=doc.get("title", ""), body=doc.get("body", ""),
                                     props=doc.get("props") or {},
                                     tags=doc.get("tags") or [])
                        batch.record("updated", uid, f"line {number}", doc.get("title", ""))
                    else:
                        model.create(db, kind=doc.get("kind", "note"),
                                     title=doc.get("title", ""), body=doc.get("body", ""),
                                     props=doc.get("props") or {},
                                     tags=doc.get("tags") or [],
                                     facet=_facet_for(doc.get("kind", ""), doc.get("facet") or {}),
                                     uid=uid, pinned=bool(doc.get("pinned")))
                        batch.record("created", uid, f"line {number}", doc.get("title", ""))
                except (model.ValidationError, Exception) as exc:  # noqa: B014
                    batch.fail(f"line {number}: {exc}")
                    batch.record("skipped", None, f"line {number}", doc.get("title", ""))
                    continue

                for link in (doc.get("links") or {}).get("out", []):
                    deferred_links.append((uid, link.get("rel"), link.get("uid")))

        # Links go in last: the other end may not have existed yet when the
        # first item was read.
        if not dry_run:
            for src_uid, rel, dst_uid in deferred_links:
                try:
                    src = model.resolve(db, src_uid, include_trashed=True)
                    dst = model.resolve(db, dst_uid, include_trashed=True)
                    model.add_edge(db, src, rel, dst)
                except (model.ItemNotFound, model.ValidationError):
                    continue

        return batch.result()
