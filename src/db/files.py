"""Attachments: a content-addressed store beside the database.

Bytes live on disk at ``files/ab/cd/<sha256><ext>``, never in the database.
Two levels of fan-out keep any one directory small enough to list quickly
even with hundreds of thousands of blobs, and content addressing means the
same attachment added twice is stored once.

Ingest is hash-then-rename: the bytes are streamed to a temporary file in a
sibling directory while the digest is computed, then renamed into place.
``os.replace`` within one filesystem is atomic, so a crash mid-ingest leaves
a stray temp file rather than a half-written blob that something claims is
complete. The temp directory is deliberately a sibling of ``files/`` rather
than the system temp directory, because a rename across filesystems is a
copy and is not atomic.
"""

import hashlib
import mimetypes
import os
import shutil
from pathlib import Path
from typing import BinaryIO, Dict, List, NamedTuple, Optional

from db import dates
from db.db import Database

__all__ = ["Ingested", "gc", "ingest_bytes", "ingest_file", "open_blob", "read_text"]

CHUNK = 1024 * 1024


class Ingested(NamedTuple):
    blob_id: int
    sha256: str
    size: int
    media_type: str
    deduplicated: bool


def _guess_media_type(filename: str) -> str:
    guess, _ = mimetypes.guess_type(filename)
    return guess or "application/octet-stream"


def _record(db: Database, digest: str, size: int, media_type: str, suffix: str) -> "tuple[int, bool]":
    conn = db.conn()
    row = conn.execute("SELECT id FROM blob WHERE sha256=?", (digest,)).fetchone()
    if row:
        return int(row[0]), True
    cur = conn.execute(
        "INSERT INTO blob(sha256, size_bytes, media_type, suffix, created_at) "
        "VALUES (?,?,?,?,?)", (digest, size, media_type, suffix, dates.utcnow()))
    return int(cur.lastrowid), False


def ingest_stream(db: Database, stream: BinaryIO, *, filename: str = "") -> Ingested:
    """Store the contents of *stream*, returning its blob."""
    layout = db.layout
    layout.ensure()
    suffix = Path(filename).suffix.lower()[:16]
    digest = hashlib.sha256()
    size = 0

    tmp = layout.tmp / f"ingest-{os.getpid()}-{id(stream):x}.part"
    with open(tmp, "wb") as out:
        while True:
            chunk = stream.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            out.write(chunk)

    sha = digest.hexdigest()
    target = layout.blob_path(sha, suffix)
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists():
        # Already stored. The bytes are identical by construction, so the
        # copy just made is redundant.
        tmp.unlink(missing_ok=True)
    else:
        os.replace(tmp, target)

    with db.write():
        blob_id, existed = _record(db, sha, size, _guess_media_type(filename), suffix)
    return Ingested(blob_id, sha, size, _guess_media_type(filename), existed)


def ingest_file(db: Database, path: "str | Path") -> Ingested:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"not a file: {path}")
    with open(path, "rb") as handle:
        return ingest_stream(db, handle, filename=path.name)


def ingest_bytes(db: Database, data: bytes, *, filename: str = "") -> Ingested:
    import io
    return ingest_stream(db, io.BytesIO(data), filename=filename)


def open_blob(db: Database, sha256: str) -> Path:
    row = db.conn().execute(
        "SELECT suffix FROM blob WHERE sha256=?", (sha256,)).fetchone()
    if row is None:
        raise FileNotFoundError(f"no blob {sha256}")
    path = db.layout.blob_path(sha256, row[0])
    if not path.exists():
        raise FileNotFoundError(f"blob {sha256[:12]} is recorded but its bytes are missing")
    return path


def read_text(db: Database, blob_id: int) -> Optional[str]:
    row = db.conn().execute(
        "SELECT extracted_text FROM blob WHERE id=?", (blob_id,)).fetchone()
    return row[0] if row else None


def gc(db: Database, *, dry_run: bool = True) -> Dict[str, object]:
    """Reclaim blobs nothing references any more.

    Defaults to a preview. Deleting a user's attachments is not something to
    do as a side effect of a maintenance command they ran to see what would
    happen.

    Also reports the opposite problem -- rows whose bytes have gone missing
    -- because that is a restore-from-backup situation and silence about it
    would be worse than useless.
    """
    conn = db.conn()
    unreferenced = conn.execute(
        "SELECT id, sha256, suffix, size_bytes FROM blob WHERE refcount = 0").fetchall()

    missing: List[str] = []
    for row in conn.execute("SELECT sha256, suffix FROM blob WHERE refcount > 0"):
        if not db.layout.blob_path(row[0], row[1]).exists():
            missing.append(row[0])

    reclaimed = sum(int(r[3]) for r in unreferenced)
    removed: List[str] = []

    if not dry_run and unreferenced:
        with db.write():
            for blob_id, sha, suffix, _size in unreferenced:
                path = db.layout.blob_path(sha, suffix)
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    continue
                conn.execute("DELETE FROM blob WHERE id=?", (blob_id,))
                removed.append(sha)

    # Stray .part files are crashed ingests; they reference nothing.
    stale_parts = 0
    if db.layout.tmp.exists():
        for part in db.layout.tmp.glob("*.part"):
            stale_parts += 1
            if not dry_run:
                part.unlink(missing_ok=True)

    return {
        "unreferenced": len(unreferenced),
        "bytes": reclaimed,
        "removed": removed,
        "missing_bytes": missing,
        "stale_partials": stale_parts,
        "dry_run": dry_run,
    }


def attach(db: Database, path: "str | Path", *, title: Optional[str] = None,
           tags=(), source_path: str = "", scan_secrets: bool = True,
           extract_text: bool = True) -> Dict[str, object]:
    """Store a file and create the item that points at it.

    The secret scan runs *before* any text is stored, not after. A file that
    trips it is still indexed -- by name, size, type and path, so you can
    find it -- but its contents are never written to the item body, the
    search index, the attribute projection, a revision snapshot or an export.
    The withholding is recorded on the item so ``db doctor`` can list what
    was held back, because being quietly protected is its own problem.
    """
    from db import extract as extract_mod
    from db import model, secretscan

    path = Path(path)
    ingested = ingest_file(db, path)

    withheld, reason = False, ""
    body = ""

    if scan_secrets:
        verdict = secretscan.scan_path(path)
        if verdict.withhold:
            withheld, reason = True, verdict.reason

    if not withheld and extract_text:
        result = extract_mod.extract_file(path)
        if result.text and scan_secrets:
            verdict = secretscan.scan(result.text, path=path)
            if verdict.withhold:
                withheld, reason = True, verdict.reason
        if not withheld:
            body = result.text
            with db.write():
                db.conn().execute(
                    "UPDATE blob SET extract_status=?, extract_note=?, extracted_text=? "
                    "WHERE id=?",
                    (result.status, result.note, result.text or None, ingested.blob_id))
        else:
            with db.write():
                db.conn().execute(
                    "UPDATE blob SET extract_status='skipped', extract_note=?, "
                    "extracted_text=NULL WHERE id=?", (reason, ingested.blob_id))

    doc = model.create(
        db, kind="file", title=title or path.name, body=body, tags=tags,
        facet={"blob_id": ingested.blob_id, "filename": path.name,
               "source_path": source_path or str(path),
               "content_withheld": withheld, "withheld_reason": reason},
        props={"size_bytes": ingested.size, "media_type": ingested.media_type},
        extra_search=[path.name, path.suffix.lstrip("."), ingested.media_type])

    return {"item": doc, "blob": ingested._asdict(),
            "withheld": withheld, "reason": reason}
