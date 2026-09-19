"""Backups.

``VACUUM INTO`` is the mechanism: it produces a single, clean, self-contained
database file, it is safe to run while the database is in use, and the result
has no WAL to reconcile. Every backup is verified by opening it and running
``integrity_check`` before it is recorded -- a backup nobody checked is a
backup nobody has.

On copying the file by hand: it does work, and the README says so, but only
when nothing is writing. A copy taken mid-write can miss the tail of the
write-ahead log. ``vault backup`` is the version that is always safe, so it
is what the tool recommends, without pretending the simple thing is
impossible.

Rotation keeps 7 daily, 4 weekly and 12 monthly snapshots. The oldest
monthly is roughly a year back, which is the horizon at which "I deleted
something important months ago" stops being recoverable any other way.
"""

import os
import re
import shutil
import sqlite3
import threading
import time
import zipfile
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional

from vault import dates
from vault.db import Database

__all__ = ["BackupResult", "Scheduler", "bundle", "latest", "prune", "restore", "take"]

STAMP = re.compile(r"vault-(\d{8}T\d{6}Z)")


class BackupResult(NamedTuple):
    path: Path
    size: int
    verified: bool
    note: str


def _stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _verify(path: Path) -> "tuple[bool, str]":
    """Open the copy and check it, rather than trusting that it worked."""
    try:
        check = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return False, str(exc)
    try:
        result = check.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            return False, result
        if check.execute("PRAGMA foreign_key_check").fetchall():
            return False, "foreign key violations in the backup"
        count = check.execute("SELECT count(*) FROM item").fetchone()[0]
        return True, f"{count} items"
    except sqlite3.Error as exc:
        return False, str(exc)
    finally:
        check.close()


def take(db: Database, *, kind: str = "manual", target: Optional[Path] = None,
         note: str = "") -> BackupResult:
    """Snapshot the database, verify the snapshot, and record it."""
    layout = db.layout
    layout.backups.mkdir(parents=True, exist_ok=True)
    path = Path(target) if target else layout.backups / f"vault-{_stamp()}.db"
    if path.exists():
        path.unlink()

    db.conn().execute("VACUUM INTO ?", (str(path),))
    verified, detail = _verify(path)
    size = path.stat().st_size if path.exists() else 0

    with db.write():
        db.conn().execute(
            "INSERT INTO backup_log(path, at, size_bytes, verified, kind, note) "
            "VALUES (?,?,?,?,?,?)",
            (str(path), dates.utcnow(), size, 1 if verified else 0, kind,
             note or detail))

    return BackupResult(path, size, verified, detail)


def latest(db: Database) -> Optional[Dict[str, object]]:
    row = db.conn().execute(
        "SELECT path, at, size_bytes, verified, kind FROM backup_log "
        "ORDER BY at DESC LIMIT 1").fetchone()
    if row is None:
        return None
    return {"path": row[0], "at": row[1], "size": row[2],
            "verified": bool(row[3]), "kind": row[4]}


def age_hours(db: Database) -> Optional[float]:
    entry = latest(db)
    if not entry:
        return None
    then = time.strptime(entry["at"], "%Y-%m-%dT%H:%M:%SZ")
    return (time.time() - time.mktime(then) + time.timezone) / 3600.0


def prune(db: Database, *, keep_daily: int = 7, keep_weekly: int = 4,
          keep_monthly: int = 12, dry_run: bool = False) -> Dict[str, object]:
    """Thin out old snapshots on a daily/weekly/monthly ladder."""
    layout = db.layout
    if not layout.backups.is_dir():
        return {"kept": [], "removed": [], "dry_run": dry_run}

    snapshots: List[tuple] = []
    for path in layout.backups.glob("vault-*.db"):
        match = STAMP.search(path.name)
        if not match:
            continue
        snapshots.append((match.group(1), path))
    snapshots.sort(reverse=True)

    keep: set = set()
    seen_days: set = set()
    seen_weeks: set = set()
    seen_months: set = set()

    for stamp, path in snapshots:
        day = stamp[:8]
        parsed = time.strptime(stamp, "%Y%m%dT%H%M%SZ")
        week = time.strftime("%Y-%W", parsed)
        month = stamp[:6]

        if len(seen_days) < keep_daily and day not in seen_days:
            seen_days.add(day); keep.add(path); continue
        if len(seen_weeks) < keep_weekly and week not in seen_weeks:
            seen_weeks.add(week); keep.add(path); continue
        if len(seen_months) < keep_monthly and month not in seen_months:
            seen_months.add(month); keep.add(path); continue

    removed = []
    for _stamp_value, path in snapshots:
        if path in keep:
            continue
        removed.append(str(path))
        if not dry_run:
            path.unlink(missing_ok=True)
            with db.write():
                db.conn().execute("DELETE FROM backup_log WHERE path=?", (str(path),))

    return {"kept": [str(p) for p in keep], "removed": removed, "dry_run": dry_run}


def bundle(db: Database, target: Path) -> Path:
    """One zip holding the database and every attachment.

    This is the "move to a new machine" artefact. A database without its
    attachments restores to a set of file items pointing at nothing.
    """
    layout = db.layout
    target = Path(target)
    snapshot = layout.tmp / f"bundle-{_stamp()}.db"
    layout.tmp.mkdir(parents=True, exist_ok=True)
    db.conn().execute("VACUUM INTO ?", (str(snapshot),))

    try:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.write(snapshot, "vault.db")
            if layout.files.is_dir():
                for path in sorted(layout.files.rglob("*")):
                    if path.is_file():
                        archive.write(path, str(Path("files") / path.relative_to(layout.files)))
            archive.writestr("RESTORE.txt",
                             "This bundle holds a complete Vault.\n\n"
                             "  vault.db   the database\n"
                             "  files/     attachments, laid out as Vault expects\n\n"
                             "To restore: unzip it, put vault.db and files/ in your data\n"
                             "directory, and run `vault doctor` to confirm.\n")
    finally:
        snapshot.unlink(missing_ok=True)
    return target


def restore(db: Database, source: Path, *, confirm: bool = False) -> Dict[str, object]:
    """Replace the live database with a snapshot.

    Refuses unless the snapshot verifies, and always sets the current
    database aside first rather than overwriting it -- a restore from the
    wrong file should not be the end of the story.
    """
    source = Path(source)
    if not source.is_file():
        raise FileNotFoundError(f"no such backup: {source}")
    verified, detail = _verify(source)
    if not verified:
        raise ValueError(f"that backup does not verify ({detail}); refusing to restore it")
    if not confirm:
        return {"would_restore": str(source), "detail": detail, "confirmed": False}

    layout = db.layout
    aside = layout.backups / f"replaced-{_stamp()}.db"
    db.conn().execute("VACUUM INTO ?", (str(aside),))
    db.close()

    for suffix in ("-wal", "-shm"):
        Path(str(layout.db) + suffix).unlink(missing_ok=True)
    shutil.copy2(source, layout.db)
    return {"restored": str(source), "previous_saved_to": str(aside),
            "detail": detail, "confirmed": True}


class Scheduler:
    """Takes a backup every *interval_hours*, in a daemon thread.

    Exists because a backup command the user has to remember is not a
    backup strategy. It runs while the server runs, and prunes as it goes.
    """

    def __init__(self, db: Database, *, interval_hours: float = 6.0,
                 log=None) -> None:
        self.db = db
        self.interval = max(0.25, float(interval_hours)) * 3600
        self.log = log
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _run(self) -> None:
        while not self._stop.is_set():
            due = age_hours(self.db)
            if due is None or due * 3600 >= self.interval:
                try:
                    result = take(self.db, kind="auto")
                    prune(self.db)
                    if self.log:
                        state = "verified" if result.verified else "FAILED VERIFICATION"
                        self.log(f"backup {result.path.name} ({state})")
                except Exception as exc:                 # pragma: no cover
                    if self.log:
                        self.log(f"scheduled backup failed: {exc}")
            self._stop.wait(min(self.interval, 900))

    def start(self) -> "Scheduler":
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
