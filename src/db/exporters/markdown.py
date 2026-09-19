"""Markdown export: one file per item, organised by kind.

Front matter carries the structured fields so the export is not lossy in
practice, and ``[[wikilinks]]`` carry the relationships, because that is what
every Markdown notes app understands. Filenames are derived from titles and
deduplicated, so the result is browsable in a file manager as well as an
editor.
"""

import re
from pathlib import Path
from typing import Any, Dict, Optional

from db import model
from db.db import Database
from db.exporters.jsonl import iter_records

__all__ = ["export", "slugify"]

_UNSAFE = re.compile(r"[^\w\s.-]", re.UNICODE)
_SPACES = re.compile(r"[\s_]+")


def slugify(text: str, *, fallback: str = "untitled", limit: int = 60) -> str:
    cleaned = _UNSAFE.sub("", (text or "").strip())
    cleaned = _SPACES.sub("-", cleaned).strip("-.")
    return (cleaned[:limit] or fallback).lower()


def _front_matter(doc: Dict[str, Any]) -> str:
    lines = ["---", f"uid: {doc['uid']}", f"kind: {doc['kind']}",
             f'title: "{(doc.get("title") or "").replace(chr(34), chr(39))}"',
             f"created: {doc['created_at']}", f"updated: {doc['updated_at']}"]
    if doc.get("tags"):
        lines.append("tags: [" + ", ".join(doc["tags"]) + "]")
    if doc.get("pinned"):
        lines.append("pinned: true")
    for key, value in sorted((doc.get("props") or {}).items()):
        lines.append(f"{key}: {value}")
    for key, value in sorted((doc.get("facet") or {}).items()):
        if value not in (None, ""):
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines)


def export(db: Database, out, *, root: Optional[Path] = None,
           query: Optional[str] = None, include_trashed: bool = False, **_: Any) -> int:
    """Write one .md per item under *root*, grouped into a folder per kind."""
    if root is None:
        raise ValueError("markdown export needs a directory (--out)")
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)

    titles: Dict[str, str] = {}
    for doc in iter_records(db, query=query, include_trashed=include_trashed):
        titles[doc["uid"]] = doc.get("title") or doc["uid"][:8]

    used: Dict[str, int] = {}
    count = 0
    for doc in iter_records(db, query=query, include_trashed=include_trashed):
        folder = root / (doc["kind"] + "s")
        folder.mkdir(parents=True, exist_ok=True)

        stem = slugify(doc.get("title") or "", fallback=doc["uid"][-8:])
        key = f"{folder}/{stem}"
        if key in used:
            used[key] += 1
            stem = f"{stem}-{used[key]}"
        else:
            used[key] = 0

        body = doc.get("body") or ""
        links = doc.get("links", {}).get("out", [])
        if links:
            body += "\n\n## Links\n\n" + "\n".join(
                f"- {l['rel']}: [[{titles.get(l['uid'], l['uid'][:8])}]]" for l in links)
        backlinks = doc.get("links", {}).get("in", [])
        if backlinks:
            body += "\n\n## Referenced by\n\n" + "\n".join(
                f"- {l['rel']}: [[{titles.get(l['uid'], l['uid'][:8])}]]" for l in backlinks)

        heading = doc.get("title") or "(untitled)"
        (folder / f"{stem}.md").write_text(
            f"{_front_matter(doc)}\n\n# {heading}\n\n{body}\n", encoding="utf-8")
        count += 1

    return count
