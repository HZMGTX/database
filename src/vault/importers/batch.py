"""The import ledger.

Records what an import did, item by item, so it can be undone wholesale and
so a dry run can report exactly what would happen before anything is written.
"""

import json
from typing import Any, Dict, List, NamedTuple, Optional

from vault import dates, ids, model
from vault.db import Database

__all__ = ["Batch", "BatchResult"]

# Commit every this many items. Small enough that other writers are never
# starved for long; large enough that per-transaction overhead disappears.
CHUNK = 500


class BatchResult(NamedTuple):
    batch_uid: Optional[str]
    created: int
    updated: int
    skipped: int
    withheld: int
    dry_run: bool
    preview: List[Dict[str, Any]]
    errors: List[str]

    @property
    def total(self) -> int:
        return self.created + self.updated + self.skipped + self.withheld


class Batch:
    """Context manager around one import run."""

    def __init__(self, db: Database, *, source: str, format: str,
                 dry_run: bool = False, preview_limit: int = 20) -> None:
        self.db = db
        self.source = source
        self.format = format
        self.dry_run = dry_run
        self.preview_limit = preview_limit
        self.uid = ids.uuid7()
        self.created = self.updated = self.skipped = self.withheld = 0
        self.preview: List[Dict[str, Any]] = []
        self.errors: List[str] = []
        self._pending: List[tuple] = []

    def __enter__(self) -> "Batch":
        if not self.dry_run:
            with self.db.write():
                self.db.conn().execute(
                    "INSERT INTO import_batch(uid, source, format, started_at, status) "
                    "VALUES (?,?,?,?,'running')",
                    (self.uid, self.source, self.format, dates.utcnow()))
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._flush()
        if self.dry_run:
            return
        with self.db.write():
            self.db.conn().execute(
                "UPDATE import_batch SET finished_at=?, status=?, created_count=?, "
                "updated_count=?, skipped_count=?, withheld_count=?, note=? WHERE uid=?",
                (dates.utcnow(), "failed" if exc_type else "done", self.created,
                 self.updated, self.skipped, self.withheld,
                 "; ".join(self.errors[:5]), self.uid))

    # -- recording ----------------------------------------------------------

    def record(self, action: str, item_uid: Optional[str], source_ref: str = "",
               title: str = "") -> None:
        if action == "created":
            self.created += 1
        elif action == "updated":
            self.updated += 1
        elif action == "withheld":
            self.withheld += 1
        else:
            self.skipped += 1

        if len(self.preview) < self.preview_limit:
            self.preview.append({"action": action, "title": title, "source": source_ref})

        if self.dry_run or item_uid is None:
            return
        self._pending.append((self.uid, item_uid, action, source_ref[:500]))
        if len(self._pending) >= CHUNK:
            self._flush()

    def _flush(self) -> None:
        if self.dry_run or not self._pending:
            self._pending.clear()
            return
        rows, self._pending = self._pending, []
        with self.db.write():
            self.db.conn().executemany(
                "INSERT OR REPLACE INTO import_item(batch_uid, item_uid, action, source_ref) "
                "VALUES (?,?,?,?)", rows)

    def fail(self, message: str) -> None:
        self.errors.append(message)

    def result(self) -> BatchResult:
        return BatchResult(
            batch_uid=None if self.dry_run else self.uid,
            created=self.created, updated=self.updated, skipped=self.skipped,
            withheld=self.withheld, dry_run=self.dry_run,
            preview=self.preview, errors=self.errors)


def undo(db: Database, batch_uid: str) -> Dict[str, Any]:
    """Reverse an import.

    Items the import created are trashed rather than purged -- an undo that
    destroys is not something to reach for casually. Items it merely updated
    are left alone, because their previous state belongs to ``vault undo``.
    """
    conn = db.conn()
    batch = conn.execute(
        "SELECT status, source FROM import_batch WHERE uid=?", (batch_uid,)).fetchone()
    if batch is None:
        raise model.ItemNotFound(f"no import batch {batch_uid}")
    if batch[0] == "undone":
        return {"batch": batch_uid, "already_undone": True, "trashed": 0}

    rows = conn.execute(
        "SELECT item_uid FROM import_item WHERE batch_uid=? AND action='created'",
        (batch_uid,)).fetchall()

    trashed = 0
    with db.write():
        for (item_uid,) in rows:
            found = conn.execute("SELECT id FROM item WHERE uid=?", (item_uid,)).fetchone()
            if found:
                model.trash(db, int(found[0]))
                trashed += 1
        conn.execute("UPDATE import_batch SET status='undone' WHERE uid=?", (batch_uid,))

    return {"batch": batch_uid, "source": batch[1], "trashed": trashed,
            "note": "items are in the trash, not purged"}


def listing(db: Database, *, limit: int = 20) -> List[Dict[str, Any]]:
    return [
        {"uid": r[0], "source": r[1], "format": r[2], "at": r[3], "status": r[4],
         "created": r[5], "updated": r[6], "skipped": r[7], "withheld": r[8]}
        for r in db.conn().execute(
            "SELECT uid, source, format, started_at, status, created_count, "
            "updated_count, skipped_count, withheld_count FROM import_batch "
            "ORDER BY started_at DESC LIMIT ?", (limit,))
    ]
