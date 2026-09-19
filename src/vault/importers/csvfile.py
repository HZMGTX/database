"""A CSV file, one item per row.

Column names map to fields by default; ``--map`` overrides where they do not
match. Unmapped columns become properties rather than being discarded, so a
spreadsheet nobody designed for this still imports usefully.
"""

import csv
from pathlib import Path
from typing import Any, Dict, List

from vault import model
from vault.db import Database
from vault.importers.batch import Batch, BatchResult

__all__ = ["load"]

KNOWN = {"title", "name", "subject", "body", "notes", "note", "description",
         "tags", "kind", "uid"}


def load(db: Database, source: "str | Path", *, dry_run: bool = False,
         mapping: Dict[str, str] = None, kind: str = "note",
         tags: List[str] = (), **_: Any) -> BatchResult:
    path = Path(source)
    mapping = {k.lower(): v for k, v in (mapping or {}).items()}

    with Batch(db, source=str(path), format="csv", dry_run=dry_run) as batch:
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as handle:
            sample = handle.read(8192)
            handle.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            except csv.Error:
                dialect = csv.excel
            reader = csv.DictReader(handle, dialect=dialect)

            for number, row in enumerate(reader, start=2):
                fields: Dict[str, Any] = {}
                props: Dict[str, Any] = {}
                for column, value in row.items():
                    if column is None or value in (None, ""):
                        continue
                    target = mapping.get((column or "").lower(), (column or "").lower())
                    if target in KNOWN:
                        fields[target] = value
                    else:
                        props[column.strip()] = _coerce(value)

                title = (fields.get("title") or fields.get("name")
                         or fields.get("subject") or "").strip()
                body = (fields.get("body") or fields.get("notes")
                        or fields.get("note") or fields.get("description") or "")
                if not title and not body and not props:
                    batch.record("skipped", None, f"row {number}")
                    continue
                if not title:
                    title = (body.strip().splitlines() or ["(untitled)"])[0][:80]

                row_tags = list(tags)
                if fields.get("tags"):
                    row_tags.extend(t.strip() for t in
                                    str(fields["tags"]).replace(",", " ").split())

                if dry_run:
                    batch.record("created", None, f"row {number}", title)
                    continue
                try:
                    doc = model.create(db, kind=fields.get("kind", kind), title=title,
                                       body=body, props=props, tags=row_tags)
                    batch.record("created", doc["uid"], f"row {number}", title)
                except Exception as exc:                    # noqa: BLE001
                    batch.fail(f"row {number}: {exc}")
                    batch.record("skipped", None, f"row {number}", title)

        return batch.result()


def _coerce(value: str) -> Any:
    text = str(value).strip()
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    # Tolerate 1,234.56 and £1,234.56 in spreadsheet columns.
    cleaned = text.replace(",", "").lstrip("$£€")
    try:
        return int(cleaned)
    except ValueError:
        pass
    try:
        return float(cleaned)
    except ValueError:
        return text
