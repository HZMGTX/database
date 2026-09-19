"""A folder of Markdown files.

Front matter becomes tags and properties; ``[[wikilinks]]`` become real edge
rows, resolved by title after every file has been read. That two-pass order
matters: a note linking forward to one imported later would otherwise lose
the link.
"""

import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from vault import model
from vault.db import Database
from vault.importers.batch import Batch, BatchResult

__all__ = ["load", "parse"]

FRONT = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.S)
WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")


def _coerce(value: str) -> Any:
    text = value.strip().strip('"').strip("'")
    lowered = text.lower()
    if lowered in ("true", "yes"):
        return True
    if lowered in ("false", "no"):
        return False
    if text.startswith("[") and text.endswith("]"):
        return [part.strip() for part in text[1:-1].split(",") if part.strip()]
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def parse(text: str) -> Tuple[Dict[str, Any], str]:
    """Split YAML-ish front matter from the body.

    Deliberately not a YAML parser: the subset people actually write in front
    matter is key/value pairs and simple lists, and a real parser would be a
    dependency this project does not take.
    """
    match = FRONT.match(text or "")
    if not match:
        return {}, text or ""
    meta: Dict[str, Any] = {}
    for line in match.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = _coerce(value)
    return meta, text[match.end():]


def load(db: Database, source: "str | Path", *, dry_run: bool = False,
         tags: List[str] = (), **_: Any) -> BatchResult:
    root = Path(source)
    paths = sorted(root.rglob("*.md")) if root.is_dir() else [root]
    by_title: Dict[str, str] = {}
    links: List[Tuple[str, str]] = []

    with Batch(db, source=str(root), format="markdown", dry_run=dry_run) as batch:
        for path in paths:
            try:
                raw = path.read_text("utf-8", errors="replace")
            except OSError as exc:
                batch.fail(f"{path.name}: {exc}")
                batch.record("skipped", None, str(path))
                continue

            meta, body = parse(raw)
            title = str(meta.pop("title", "") or "").strip()
            if not title:
                heading = re.search(r"^#\s+(.+)$", body, re.M)
                title = heading.group(1).strip() if heading else path.stem

            kind = str(meta.pop("kind", "note"))
            item_tags = list(tags)
            raw_tags = meta.pop("tags", [])
            if isinstance(raw_tags, str):
                raw_tags = [t.strip() for t in raw_tags.replace(",", " ").split()]
            item_tags.extend(str(t).lstrip("#") for t in raw_tags or [])

            uid = meta.pop("uid", None)
            meta.pop("created", None)
            meta.pop("updated", None)

            if dry_run:
                batch.record("created", None, str(path), title)
                continue

            try:
                doc = model.create(db, kind=kind if kind in ("note",) else "note",
                                   title=title, body=body.strip(),
                                   props={k: v for k, v in meta.items()},
                                   tags=item_tags,
                                   uid=uid if uid and model.ids.is_uid(str(uid)) else None)
            except Exception as exc:                        # noqa: BLE001
                batch.fail(f"{path.name}: {exc}")
                batch.record("skipped", None, str(path), title)
                continue

            by_title[title.lower()] = doc["uid"]
            batch.record("created", doc["uid"], str(path), title)
            for target in WIKILINK.findall(body):
                links.append((doc["uid"], target.strip().lower()))

        if not dry_run:
            for src_uid, target_title in links:
                dst_uid = by_title.get(target_title)
                if not dst_uid:
                    continue
                try:
                    model.add_edge(db, model.resolve(db, src_uid), "mentions",
                                   model.resolve(db, dst_uid))
                except (model.ItemNotFound, model.ValidationError):
                    continue

        return batch.result()
