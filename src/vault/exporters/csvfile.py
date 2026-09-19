"""CSV export: one file per kind, columns driven by the field registry.

A single CSV across every kind would be mostly empty cells, so each kind gets
its own file with the columns that kind actually declares. That is what opens
usefully in a spreadsheet.
"""

import csv
from pathlib import Path
from typing import Any, Dict, List, Optional

from vault.db import Database
from vault.exporters.jsonl import iter_records

__all__ = ["export"]

BASE_COLUMNS = ["uid", "kind", "title", "tags", "created_at", "updated_at"]


def export(db: Database, out, *, root: Optional[Path] = None,
           query: Optional[str] = None, include_trashed: bool = False, **_: Any) -> int:
    conn = db.conn()
    by_kind: Dict[str, List[Dict[str, Any]]] = {}
    for doc in iter_records(db, query=query, include_trashed=include_trashed):
        by_kind.setdefault(doc["kind"], []).append(doc)

    single = root is None
    count = 0

    for kind, docs in sorted(by_kind.items()):
        declared = [r[0] for r in conn.execute(
            "SELECT key FROM kind_field WHERE kind=? ORDER BY sort_order", (kind,))]
        extra = sorted({k for d in docs for k in (d.get("props") or {})} - set(declared))
        columns = BASE_COLUMNS + declared + extra + ["body"]

        if single:
            handle = out
        else:
            Path(root).mkdir(parents=True, exist_ok=True)
            handle = open(Path(root) / f"{kind}.csv", "w", newline="", encoding="utf-8")

        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for doc in docs:
            facet = doc.get("facet") or {}
            props = doc.get("props") or {}
            row = {
                "uid": doc["uid"], "kind": doc["kind"], "title": doc.get("title", ""),
                "tags": " ".join(doc.get("tags") or []),
                "created_at": doc["created_at"], "updated_at": doc["updated_at"],
                "body": (doc.get("body") or "").replace("\r\n", "\n"),
            }
            for column in declared + extra:
                if column in props:
                    row[column] = props[column]
                elif column in facet:
                    row[column] = facet[column]
                elif f"{column}_local" in facet:
                    row[column] = facet[f"{column}_local"]
            writer.writerow(row)
            count += 1

        if not single:
            handle.close()
        elif len(by_kind) > 1:
            handle.write("\n")

    return count
