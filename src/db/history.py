"""History: revisions, diffs, revert, and undo.

Two mechanisms, answering two different questions.

``revision`` answers *what did this look like before?* -- a compressed
snapshot of the whole composed document at every rev. Reverting reads one.

``change_log`` answers *what did I just do, and can I take it back?* It is
keyed by transaction, so one ``undo`` reverses one logical operation even
when that operation touched two hundred items. It also outlives the rows it
describes, which is what lets a purge still be explained afterwards.

Revert is not a rollback: reverting to rev 3 writes a *new* rev. History is
append-only, so undoing a revert is just another revert.
"""

import difflib
import json
import zlib
from typing import Any, Dict, List, NamedTuple, Optional

from db import dates, ids, model
from db.db import Database

__all__ = ["Revision", "UndoError", "compact", "diff_text", "history", "read_revision",
           "revert", "undo", "undoable"]


class UndoError(RuntimeError):
    pass


class Revision(NamedTuple):
    rev: int
    at: str
    actor: str
    txn_id: str
    title: str
    size: int


def history(db: Database, item_id: int, *, limit: int = 50) -> List[Revision]:
    rows = db.conn().execute(
        "SELECT rev, at, actor, txn_id, doc_z FROM revision WHERE item_id=? "
        "ORDER BY rev DESC LIMIT ?", (item_id, limit)).fetchall()
    out = []
    for row in rows:
        doc = json.loads(zlib.decompress(row[4]).decode("utf-8"))
        out.append(Revision(rev=row[0], at=row[1], actor=row[2], txn_id=row[3],
                            title=doc.get("title", ""), size=len(row[4])))
    return out


def read_revision(db: Database, item_id: int, rev: int) -> Dict[str, Any]:
    row = db.conn().execute(
        "SELECT doc_z FROM revision WHERE item_id=? AND rev=?", (item_id, rev)).fetchone()
    if row is None:
        raise model.ItemNotFound(f"no revision {rev} for that item")
    return json.loads(zlib.decompress(row[0]).decode("utf-8"))


def _render(doc: Dict[str, Any]) -> List[str]:
    """A revision as readable lines, so a diff of two of them reads well."""
    lines = [
        f"title: {doc.get('title','')}",
        f"kind:  {doc.get('kind','')}",
        f"tags:  {' '.join(doc.get('tags') or [])}",
    ]
    for key, value in sorted((doc.get("props") or {}).items()):
        lines.append(f"prop {key}: {value}")
    for key, value in sorted((doc.get("facet") or {}).items()):
        if value not in (None, ""):
            lines.append(f"facet {key}: {value}")
    for link in (doc.get("links") or {}).get("out", []):
        lines.append(f"link -> {link['rel']} {link.get('title','')}")
    lines.append("")
    lines.extend((doc.get("body") or "").splitlines())
    return lines


def diff_text(db: Database, item_id: int, rev_a: int, rev_b: int) -> str:
    """A unified diff between two revisions."""
    a, b = read_revision(db, item_id, rev_a), read_revision(db, item_id, rev_b)
    return "\n".join(difflib.unified_diff(
        _render(a), _render(b),
        fromfile=f"rev {rev_a}  ({a.get('updated_at','')})",
        tofile=f"rev {rev_b}  ({b.get('updated_at','')})",
        lineterm=""))


def revert(db: Database, item_id: int, rev: int) -> Dict[str, Any]:
    """Restore an item's content from *rev*, as a new revision.

    Only the fields a person edits are restored. Identity, creation time and
    the revision counter are not: reverting is an edit, not time travel, and
    pretending otherwise would break the ETag contract and the audit trail.
    """
    old = read_revision(db, item_id, rev)
    with db.write():
        current = model.compose(db, item_id)
        if current["rev"] == rev:
            return current
        doc = model.update(
            db, item_id,
            title=old.get("title", ""), body=old.get("body", ""),
            props=old.get("props") or {}, tags=old.get("tags") or [])
        conn = db.conn()
        conn.execute(
            "UPDATE change_log SET op='revert', note=? WHERE seq=(SELECT max(seq) FROM change_log)",
            (f"reverted to rev {rev}",))
        return doc


def undoable(db: Database, *, limit: int = 20) -> List[Dict[str, Any]]:
    """Recent transactions, newest first, as candidates for undo."""
    rows = db.conn().execute(
        "SELECT txn_id, min(at) AS at, count(*) AS n, "
        "       group_concat(DISTINCT op) AS ops, max(actor) AS actor "
        "FROM change_log GROUP BY txn_id ORDER BY max(seq) DESC LIMIT ?", (limit,)).fetchall()
    return [{"txn_id": r[0], "at": r[1], "rows": r[2], "ops": (r[3] or "").split(","),
             "actor": r[4]} for r in rows]


def undo(db: Database, txn_id: Optional[str] = None) -> Dict[str, Any]:
    """Reverse one transaction.

    Because change_log is keyed by transaction rather than by row, this
    reverses a bulk retag across two hundred items as one operation -- which
    is the whole reason the txn_id exists.

    The undo is itself a transaction, and is itself recorded, so undoing an
    undo works.
    """
    conn = db.conn()
    if txn_id is None:
        row = conn.execute(
            "SELECT txn_id FROM change_log WHERE op != 'undo' "
            "ORDER BY seq DESC LIMIT 1").fetchone()
        if row is None:
            raise UndoError("nothing to undo")
        txn_id = row[0]

    entries = conn.execute(
        "SELECT seq, op, item_id, row_uid, patch_json, note FROM change_log "
        "WHERE txn_id=? ORDER BY seq DESC", (txn_id,)).fetchall()
    if not entries:
        raise UndoError(f"no transaction {txn_id}")

    reversed_ops: List[str] = []
    skipped: List[str] = []

    with db.write():
        for seq, op, item_id, row_uid, patch_json, note in entries:
            patch = json.loads(patch_json or "{}")
            before = patch.get("before") or {}

            live = None
            if row_uid:
                found = conn.execute("SELECT id FROM item WHERE uid=?", (row_uid,)).fetchone()
                live = int(found[0]) if found else None

            if op == "create":
                if live is not None:
                    model.trash(db, live)
                    reversed_ops.append(f"un-created {ids.short(row_uid)}")
                continue

            if op in ("update", "tag"):
                if live is None:
                    skipped.append(f"{op} on a purged item")
                    continue
                kwargs: Dict[str, Any] = {}
                for field in ("title", "body", "props"):
                    if field in before:
                        kwargs[field] = before[field]
                if "tags" in before:
                    kwargs["tags"] = before["tags"]
                if "pinned" in before:
                    kwargs["pinned"] = before["pinned"]
                if kwargs:
                    model.update(db, live, **kwargs)
                    reversed_ops.append(f"restored {', '.join(kwargs)} on {ids.short(row_uid)}")
                continue

            if op == "trash":
                if live is not None:
                    model.restore(db, live)
                    reversed_ops.append(f"restored {ids.short(row_uid)}")
                continue

            if op == "restore":
                if live is not None:
                    model.trash(db, live)
                    reversed_ops.append(f"re-trashed {ids.short(row_uid)}")
                continue

            if op in ("link", "unlink"):
                parts = (note or "").split()
                if len(parts) == 3:
                    src, rel, dst = int(parts[0]), parts[1], int(parts[2])
                    try:
                        if op == "link":
                            model.remove_edge(db, src, rel, dst)
                        else:
                            model.add_edge(db, src, rel, dst)
                        reversed_ops.append(f"{'removed' if op == 'link' else 'restored'} link {rel}")
                    except (model.ItemNotFound, model.ValidationError):
                        skipped.append(f"{op} referencing a missing item")
                continue

            if op == "purge":
                # The tombstone holds the full document, so the content comes
                # back -- but under a new row. Anything that pointed at it is
                # gone, because the cascade already removed those edges.
                restored = _restore_from_tombstone(db, row_uid)
                if restored:
                    reversed_ops.append(f"restored purged {ids.short(row_uid)} (links not recovered)")
                else:
                    skipped.append("purge with no tombstone")
                continue

            skipped.append(f"{op} is not reversible")

        model._log(db, "update", None, None,
                   {"changed": ["undo"]}, note=f"undid transaction {txn_id}")

    return {"txn_id": txn_id, "reversed": reversed_ops, "skipped": skipped}


def _restore_from_tombstone(db: Database, uid: str) -> bool:
    conn = db.conn()
    row = conn.execute(
        "SELECT kind, doc_z FROM tombstone WHERE uid=? AND reason='purge'", (uid,)).fetchone()
    if row is None or row[1] is None:
        return False
    doc = json.loads(zlib.decompress(row[1]).decode("utf-8"))
    facet = None
    if doc.get("facet"):
        facet = dict(doc["facet"])
        if doc["kind"] == "event":
            facet["starts"] = facet.get("starts_local")
            facet["all_day"] = bool(facet.get("all_day"))
        elif doc["kind"] == "task" and facet.get("due_local"):
            facet["due"] = facet["due_local"]
    model.create(db, kind=doc["kind"], title=doc.get("title", ""), body=doc.get("body", ""),
                 props=doc.get("props") or {}, tags=doc.get("tags") or [],
                 facet=facet, uid=uid)
    conn.execute("DELETE FROM tombstone WHERE uid=?", (uid,))
    return True


def compact(db: Database, *, keep_days: int = 90, keep_per_item: int = 30,
            dry_run: bool = True) -> Dict[str, int]:
    """Trim history.

    Two separate problems. ``change_log`` grows without bound and its older
    entries are redundant once a backup exists, so it gets a time window.
    ``revision`` is per item, so it keeps the most recent N for each -- the
    first and the last are always kept, because "what did this start as?" is
    a question people actually ask.

    Never runs on its own. Losing history silently is worse than a large
    database.
    """
    conn = db.conn()
    cutoff = dates.utcnow()[:10]
    import datetime as _dt
    cutoff = (_dt.date.fromisoformat(cutoff) - _dt.timedelta(days=keep_days)).isoformat()

    log_rows = conn.execute(
        "SELECT count(*) FROM change_log WHERE at < ?", (cutoff + "T00:00:00Z",)).fetchone()[0]

    rev_rows = conn.execute(
        "SELECT count(*) FROM revision r WHERE r.rev NOT IN ("
        "  SELECT rev FROM revision WHERE item_id=r.item_id ORDER BY rev DESC LIMIT ?"
        ") AND r.rev <> (SELECT min(rev) FROM revision WHERE item_id=r.item_id)",
        (keep_per_item,)).fetchone()[0]

    if not dry_run:
        with db.write():
            conn.execute("DELETE FROM change_log WHERE at < ?", (cutoff + "T00:00:00Z",))
            conn.execute(
                "DELETE FROM revision WHERE rowid IN (SELECT r.rowid FROM revision r "
                "WHERE r.rev NOT IN (SELECT rev FROM revision WHERE item_id=r.item_id "
                "ORDER BY rev DESC LIMIT ?) AND r.rev <> "
                "(SELECT min(rev) FROM revision WHERE item_id=r.item_id))", (keep_per_item,))

    return {"change_log": log_rows, "revisions": rev_rows, "cutoff": cutoff,
            "dry_run": dry_run}
