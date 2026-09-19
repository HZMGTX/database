"""Comparing two databases row by row.

This is what turns "your data is never trapped" into something checked
rather than claimed: export everything, import it into an empty database,
and compare. If the two differ anywhere that matters, this says exactly
where.

Identity, revision numbers and timestamps are deliberately not compared --
a re-import legitimately produces new ones. What must match is the content:
title, kind, body, properties, tags and links.
"""

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

__all__ = ["compare"]

COMPARED = ("kind", "title", "body", "props", "tags", "links")


def _load(path: Path) -> Dict[str, Dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"no such database: {path}")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.execute("SELECT 1 FROM item LIMIT 1")
    except sqlite3.DatabaseError as exc:
        raise OSError(f"{path} is not a readable The database database: {exc}") from exc
    conn.row_factory = sqlite3.Row
    try:
        out: Dict[str, Dict[str, Any]] = {}
        for row in conn.execute(
                "SELECT id, uid, kind, title, body, props FROM item "
                "WHERE deleted_at IS NULL"):
            tags = [r[0] for r in conn.execute(
                "SELECT t.slug FROM item_tag it JOIN tag t ON t.id=it.tag_id "
                "WHERE it.item_id=? ORDER BY t.slug", (row["id"],))]
            links = sorted(
                f"{r[0]}:{r[1]}" for r in conn.execute(
                    "SELECT e.rel, i.uid FROM edge e JOIN item i ON i.id=e.dst_id "
                    "WHERE e.src_id=?", (row["id"],)))
            out[row["uid"]] = {
                "kind": row["kind"], "title": row["title"], "body": row["body"],
                "props": json.loads(row["props"] or "{}"),
                "tags": tags, "links": links,
            }
        return out
    finally:
        conn.close()


def compare(left: "str | Path", right: "str | Path") -> Dict[str, Any]:
    """Compare two The database databases. Empty ``differences`` means identical."""
    a, b = _load(Path(left)), _load(Path(right))

    only_left = sorted(set(a) - set(b))
    only_right = sorted(set(b) - set(a))
    differences: List[Dict[str, Any]] = []

    for uid in sorted(set(a) & set(b)):
        changed = [field for field in COMPARED if a[uid][field] != b[uid][field]]
        if changed:
            differences.append({
                "uid": uid, "title": a[uid]["title"], "fields": changed,
                "left": {f: a[uid][f] for f in changed},
                "right": {f: b[uid][f] for f in changed},
            })

    return {
        "identical": not (only_left or only_right or differences),
        "left_items": len(a), "right_items": len(b),
        "only_in_left": only_left, "only_in_right": only_right,
        "differences": differences,
    }
