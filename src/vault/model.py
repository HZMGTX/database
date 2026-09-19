"""The single write path.

Every mutation in Vault goes through this module -- CLI, REST API, web UI and
importers alike.  That is not tidiness for its own sake.  Three guarantees
depend on there being exactly one path:

* **The audit trail cannot be bypassed.**  ``change_log`` rows are written
  here, in the same transaction as the mutation.  They are not written by
  triggers, because SQLite refuses to create a trigger that reads transaction
  context from a temp table, and the user-defined-function alternative is
  foreclosed by ``trusted_schema=OFF``.
* **``rev`` moves whenever the document changes** -- including when only tags
  or links changed.  The HTTP ETag is derived from ``rev``, so if a tag edit
  left it alone, a client holding a stale copy could overwrite a concurrent
  change while its ETag still matched.
* **props is canonicalised before it is stored.**  ``json_valid`` accepts an
  object with a repeated key, but ``json_each`` then emits both and the
  ``attr`` projection dies on its primary key.  A CHECK constraint cannot
  catch this -- SQLite prohibits subqueries in CHECK -- so it is caught here.

``tests/test_lint.py`` enforces that no other module writes to these tables.
"""

import json
import re
import sqlite3
import zlib
from typing import Any, Dict, Iterable, List, Optional, Sequence

from vault import dates, ids
from vault.db import Database

__all__ = [
    "ItemNotFound",
    "ValidationError",
    "StaleWrite",
    "UNSET",
    "add_edge",
    "compose",
    "create",
    "purge",
    "remove_edge",
    "resolve",
    "restore",
    "set_tags",
    "trash",
    "update",
]


class ItemNotFound(LookupError):
    pass


class ValidationError(ValueError):
    pass


class StaleWrite(RuntimeError):
    """The caller's expected revision no longer matches.

    Raised from *inside* the write transaction.  Checking before opening the
    transaction leaves a window in which another writer commits between the
    check and the write, which is precisely the race optimistic concurrency
    is supposed to close.
    """

    def __init__(self, uid: str, expected: int, actual: int) -> None:
        super().__init__(f"{uid} is at revision {actual}, not {expected}")
        self.uid, self.expected, self.actual = uid, expected, actual


class _Unset:
    """Distinguishes "leave this alone" from "set this to None"."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET = _Unset()

_URL_RE = re.compile(r"https?://[^\s<>\"'\)\]]+", re.I)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")

# Facet columns that model.py owns, per kind.  Anything not listed here for a
# kind is a property, and lands in props.
FACET_TABLES = {
    "task": "item_task",
    "event": "item_event",
    "file": "item_file",
    "link": "item_link",
    "person": "item_person",
}


# ---------------------------------------------------------------------------
# props canonicalisation and validation
# ---------------------------------------------------------------------------

def canonical_props(props: Any) -> str:
    """Return *props* as a canonical JSON object string.

    A round-trip through Python's parser collapses duplicate keys the way
    every JSON consumer does -- last one wins.  Without this,
    ``{"a":1,"a":2}`` passes the column's ``json_valid`` CHECK and then kills
    the ``attr`` projection with a primary-key collision, reachable from the
    canonical JSONL importer, the API and hand-written SQL alike.

    Keys are sorted so that two equal documents serialise identically, which
    is what makes revision de-duplication and ``diff-db`` meaningful.
    """
    if props is None:
        return "{}"
    if isinstance(props, str):
        try:
            parsed = json.loads(props)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"props is not valid JSON: {exc}") from exc
    else:
        parsed = props

    if not isinstance(parsed, dict):
        raise ValidationError(f"props must be a JSON object, got {type(parsed).__name__}")

    return json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def validate_props(conn: sqlite3.Connection, kind: str, props: Dict[str, Any]) -> None:
    """Check declared fields against the ``kind_field`` registry.

    Undeclared keys are allowed on purpose: capturing something should never
    fail because a field has not been declared yet.  Declared ones are
    enforced, so an enum stays an enum.
    """
    rows = conn.execute(
        "SELECT key, type, required, multi, enum_values FROM kind_field WHERE kind=?", (kind,)
    ).fetchall()
    declared = {r[0]: r for r in rows}

    for key, spec in declared.items():
        _, ftype, required, multi, enum_values = spec
        if required and key not in props:
            # Facet-backed required fields are supplied through the facet, not
            # through props; only complain about ones nothing else provides.
            if kind not in FACET_TABLES:
                raise ValidationError(f"{kind}.{key} is required")
            continue
        if key not in props:
            continue

        value = props[key]
        if multi and isinstance(value, list):
            values = value
        else:
            values = [value]

        for v in values:
            if v is None:
                continue
            if ftype == "number" and not isinstance(v, (int, float)):
                raise ValidationError(f"{kind}.{key} must be a number, got {v!r}")
            if ftype == "bool" and not isinstance(v, bool) and v not in (0, 1):
                raise ValidationError(f"{kind}.{key} must be true or false, got {v!r}")
            if ftype == "enum":
                allowed = json.loads(enum_values or "[]")
                if allowed and v not in allowed:
                    raise ValidationError(
                        f"{kind}.{key} must be one of {', '.join(map(str, allowed))}, got {v!r}")


def build_search_extra(title: str, body: str, props: Dict[str, Any], extra: Sequence[str] = ()) -> str:
    """Terms that the word tokenizer would otherwise break apart.

    URLs, hostnames, emails and filenames live here as whole strings *and* as
    their components.  This column is why the tokenizer does not need '.' and
    '-' as word characters -- which would stop `budget` matching a sentence
    ending "the budget."
    """
    haystack = " ".join(
        [title or "", body or ""] + [str(v) for v in (props or {}).values() if not isinstance(v, (dict, list))]
    )
    terms: List[str] = []

    for url in _URL_RE.findall(haystack):
        terms.append(url)
        without_scheme = url.split("://", 1)[-1]
        host = without_scheme.split("/", 1)[0]
        terms.append(host)
        terms.extend(part for part in host.split(".") if part)
        path = without_scheme[len(host):]
        terms.extend(part for part in re.split(r"[/?&=#._-]", path) if len(part) > 1)

    for email in _EMAIL_RE.findall(haystack):
        local, _, domain = email.partition("@")
        terms.extend([email, local, domain])
        terms.extend(part for part in domain.split(".") if part)

    terms.extend(str(e) for e in extra if e)

    # De-duplicate while keeping order, so the column stays stable across
    # rewrites and revision diffs do not churn.
    seen = set()
    unique = []
    for term in terms:
        low = term.lower()
        if low not in seen:
            seen.add(low)
            unique.append(term)
    return " ".join(unique)


def wikilink_targets(body: str) -> List[str]:
    """``[[Some title]]`` references, for the importer to turn into edges."""
    return [m.group(1).strip() for m in _WIKILINK_RE.finditer(body or "")]


# ---------------------------------------------------------------------------
# reference resolution
# ---------------------------------------------------------------------------

def resolve(db: Database, ref: str, *, include_trashed: bool = False) -> int:
    """Turn what a person typed into an item id.

    Accepts a full uid, a unique uid prefix, or a unique title prefix.  An
    ambiguous prefix is an error rather than a guess: silently acting on the
    wrong item is worse than asking.
    """
    ref = (ref or "").strip()
    if not ref:
        raise ItemNotFound("no reference given")

    conn = db.conn()
    trashed_clause = "" if include_trashed else " AND deleted_at IS NULL"

    if ids.is_uid(ref):
        row = conn.execute(
            f"SELECT id FROM item WHERE uid=?{trashed_clause}", (ref,)).fetchone()
        if row:
            return int(row[0])
        redirect = _follow_tombstone(conn, ref)
        if redirect:
            return redirect
        raise ItemNotFound(f"no item with uid {ref}")

    if len(ref) >= 4 and all(c in "0123456789abcdef" for c in ref.lower()):
        lowered = ref.lower()
        # The short handle is the uid's tail, and substr(uid,-8) is indexed.
        # A leading prefix is almost useless for this: the first 12 characters
        # are a millisecond timestamp, so everything created in the same
        # minute shares them.
        if len(lowered) == 8:
            rows = conn.execute(
                f"SELECT id, uid FROM item WHERE substr(uid,-8)=?{trashed_clause} LIMIT 5",
                (lowered,)).fetchall()
            if len(rows) == 1:
                return int(rows[0][0])
        rows = conn.execute(
            f"SELECT id, uid FROM item WHERE uid LIKE ?{trashed_clause} LIMIT 5",
            ("%" + lowered,)).fetchall()
        if len(rows) == 1:
            return int(rows[0][0])
        if len(rows) > 1:
            raise ItemNotFound(
                f"{ref!r} matches {len(rows)} items: "
                + ", ".join(ids.short(r[1]) for r in rows))
        rows = conn.execute(
            f"SELECT id, uid FROM item WHERE uid GLOB ?{trashed_clause} LIMIT 5",
            (lowered + "*",)).fetchall()
        if len(rows) == 1:
            return int(rows[0][0])
        if len(rows) > 1:
            raise ItemNotFound(
                f"{ref!r} matches {len(rows)} items: "
                + ", ".join(ids.short(r[1]) for r in rows))

    rows = conn.execute(
        f"SELECT id, title FROM item WHERE title LIKE ?{trashed_clause} "
        f"ORDER BY length(title) LIMIT 5", (ref + "%",)).fetchall()
    if len(rows) == 1:
        return int(rows[0][0])
    if len(rows) > 1:
        exact = [r for r in rows if r[1].lower() == ref.lower()]
        if len(exact) == 1:
            return int(exact[0][0])
        raise ItemNotFound(
            f"{ref!r} matches {len(rows)} items: " + ", ".join(repr(r[1]) for r in rows))

    raise ItemNotFound(f"nothing matches {ref!r}")


def _follow_tombstone(conn: sqlite3.Connection, uid: str, depth: int = 8) -> Optional[int]:
    """Follow a merge redirect to whatever absorbed it.

    Depth-capped and cycle-safe: merging in a loop must not hang.
    """
    seen = set()
    for _ in range(depth):
        if uid in seen:
            return None
        seen.add(uid)
        row = conn.execute(
            "SELECT redirect_to_uid FROM tombstone WHERE uid=?", (uid,)).fetchone()
        if not row or not row[0]:
            return None
        uid = row[0]
        live = conn.execute("SELECT id FROM item WHERE uid=?", (uid,)).fetchone()
        if live:
            return int(live[0])
    return None


# ---------------------------------------------------------------------------
# reading a whole item
# ---------------------------------------------------------------------------

def compose(db: Database, item_id: int) -> Dict[str, Any]:
    """The full document: item, facet, tags, links both ways, attachment.

    This is what a revision snapshots, what the API returns and what the
    exporters write, so all three cannot disagree about what an item *is*.
    """
    conn = db.conn()
    row = conn.execute("SELECT * FROM item WHERE id=?", (item_id,)).fetchone()
    if row is None:
        raise ItemNotFound(f"no item with id {item_id}")

    doc: Dict[str, Any] = {
        "uid": row["uid"],
        "kind": row["kind"],
        "title": row["title"],
        "body": row["body"],
        "props": json.loads(row["props"]),
        "tags": [],
        "rev": row["rev"],
        "pinned": bool(row["pinned"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "deleted_at": row["deleted_at"],
    }

    doc["tags"] = [
        r[0] for r in conn.execute(
            "SELECT t.slug FROM item_tag it JOIN tag t ON t.id=it.tag_id "
            "WHERE it.item_id=? ORDER BY t.slug", (item_id,))
    ]

    table = FACET_TABLES.get(row["kind"])
    if table:
        facet = conn.execute(
            f"SELECT * FROM {table} WHERE item_id=?", (item_id,)).fetchone()
        if facet:
            doc["facet"] = {
                k: facet[k] for k in facet.keys() if k not in ("item_id", "kind")
            }

    if row["kind"] == "person":
        doc["identities"] = [
            {"channel": r[0], "label": r[1], "value": r[2], "primary": bool(r[3])}
            for r in conn.execute(
                "SELECT channel, label, value, is_primary FROM person_identity "
                "WHERE item_id=? ORDER BY channel, value_norm", (item_id,))
        ]

    doc["links"] = {
        "out": [
            {"rel": r[0], "uid": r[1], "title": r[2]}
            for r in conn.execute(
                "SELECT e.rel, i.uid, i.title FROM edge e JOIN item i ON i.id=e.dst_id "
                "WHERE e.src_id=? ORDER BY e.rel, i.title", (item_id,))
        ],
        "in": [
            {"rel": r[0], "uid": r[1], "title": r[2]}
            for r in conn.execute(
                "SELECT e.rel, i.uid, i.title FROM edge e JOIN item i ON i.id=e.src_id "
                "WHERE e.dst_id=? ORDER BY e.rel, i.title", (item_id,))
        ],
    }
    return doc


# ---------------------------------------------------------------------------
# audit and revisions
# ---------------------------------------------------------------------------

def _log(db: Database, op: str, item_id: Optional[int], row_uid: Optional[str],
         patch: Optional[Dict[str, Any]] = None, note: str = "") -> None:
    """Append to the audit ledger, inside the caller's transaction.

    The patch records only what changed, and only its *previous* value.  That
    is all ``undo`` needs, and it halves what history costs: storing a full
    before and after put roughly 2.6 copies of every body in the database.
    The full forward state is already in ``revision``, compressed.
    """
    txn = db.require_txn()
    db.conn().execute(
        "INSERT INTO change_log(txn_id, at, actor, op, item_id, row_uid, patch_json, note) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (txn.txn_id, dates.utcnow(), txn.actor, op, item_id, row_uid,
         json.dumps(patch or {}, sort_keys=True, separators=(",", ":")), note),
    )


def _snapshot(db: Database, item_id: int) -> None:
    """Store a compressed snapshot of the composed document at its new rev."""
    txn = db.require_txn()
    conn = db.conn()
    doc = compose(db, item_id)
    payload = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    blob = zlib.compress(payload.encode("utf-8"), 6)
    import hashlib
    conn.execute(
        "INSERT OR REPLACE INTO revision(item_id, rev, at, actor, txn_id, doc_z, doc_sha) "
        "VALUES (?,?,?,?,?,?,?)",
        (item_id, doc["rev"], dates.utcnow(), txn.actor, txn.txn_id, blob,
         hashlib.sha256(payload.encode("utf-8")).hexdigest()),
    )


def _touch(db: Database, item_id: int) -> int:
    """Bump ``rev`` and ``updated_at``.  Returns the new rev.

    Called for ANY change to the composed document, tags and links included.
    The ETag is ``uid.rev``, so a change that left rev alone would let a
    client overwrite a concurrent edit with a still-matching ETag.
    """
    conn = db.conn()
    conn.execute(
        "UPDATE item SET rev = rev + 1, updated_at = ? WHERE id = ?",
        (dates.utcnow(), item_id))
    return int(conn.execute("SELECT rev FROM item WHERE id=?", (item_id,)).fetchone()[0])


def _check_rev(db: Database, item_id: int, expected: Optional[int]) -> None:
    """Optimistic concurrency, validated inside the transaction."""
    if expected is None:
        return
    row = db.conn().execute("SELECT uid, rev FROM item WHERE id=?", (item_id,)).fetchone()
    if row is None:
        raise ItemNotFound(f"no item with id {item_id}")
    if int(row[1]) != int(expected):
        raise StaleWrite(row[0], int(expected), int(row[1]))


# ---------------------------------------------------------------------------
# tags
# ---------------------------------------------------------------------------

def _tag_id(db: Database, slug: str, *, create_missing: bool = True) -> Optional[int]:
    conn = db.conn()
    slug = slug.strip().strip("/").lower()
    if not slug:
        raise ValidationError("empty tag")
    row = conn.execute("SELECT id FROM tag WHERE slug=?", (slug,)).fetchone()
    if row:
        return int(row[0])
    if not create_missing:
        return None
    # Materialise ancestors too, so the tree view shows 'work' even when only
    # 'work/clients/acme' was ever typed.
    parts = slug.split("/")
    for depth in range(1, len(parts) + 1):
        ancestor = "/".join(parts[:depth])
        conn.execute(
            "INSERT OR IGNORE INTO tag(slug, label, created_at) VALUES (?,?,?)",
            (ancestor, parts[depth - 1], dates.utcnow()))
    return int(conn.execute("SELECT id FROM tag WHERE slug=?", (slug,)).fetchone()[0])


def set_tags(db: Database, item_id: int, tags: Iterable[str], *,
             expected_rev: Optional[int] = None) -> int:
    """Replace an item's whole tag set.  Returns the new rev."""
    with db.write():
        _check_rev(db, item_id, expected_rev)
        conn = db.conn()
        uid = conn.execute("SELECT uid FROM item WHERE id=?", (item_id,)).fetchone()
        if uid is None:
            raise ItemNotFound(f"no item with id {item_id}")
        before = [r[0] for r in conn.execute(
            "SELECT t.slug FROM item_tag it JOIN tag t ON t.id=it.tag_id "
            "WHERE it.item_id=? ORDER BY t.slug", (item_id,))]

        wanted = sorted({t.strip().strip("/").lower() for t in tags if t and t.strip()})
        if wanted == before:
            return int(conn.execute(
                "SELECT rev FROM item WHERE id=?", (item_id,)).fetchone()[0])

        conn.execute("DELETE FROM item_tag WHERE item_id=?", (item_id,))
        for slug in wanted:
            conn.execute("INSERT OR IGNORE INTO item_tag VALUES (?,?)",
                         (item_id, _tag_id(db, slug)))

        rev = _touch(db, item_id)
        _log(db, "tag", item_id, uid[0], {"changed": ["tags"], "before": {"tags": before}})
        _snapshot(db, item_id)
        return rev


# ---------------------------------------------------------------------------
# relationships
# ---------------------------------------------------------------------------

def add_edge(db: Database, src_id: int, rel: str, dst_id: int) -> None:
    """Create a typed link.  Bumps both endpoints: both documents changed."""
    with db.write():
        conn = db.conn()
        if src_id == dst_id:
            raise ValidationError("an item cannot link to itself")
        if not conn.execute("SELECT 1 FROM rel WHERE name=?", (rel,)).fetchone():
            known = ", ".join(r[0] for r in conn.execute(
                "SELECT name FROM rel ORDER BY name LIMIT 12"))
            raise ValidationError(f"unknown relationship {rel!r}. Known verbs: {known}")
        for ident in (src_id, dst_id):
            if not conn.execute("SELECT 1 FROM item WHERE id=?", (ident,)).fetchone():
                raise ItemNotFound(f"no item with id {ident}")

        existing = conn.execute(
            "SELECT 1 FROM edge WHERE src_id=? AND rel=? AND dst_id=?",
            (src_id, rel, dst_id)).fetchone()
        if existing:
            return

        conn.execute(
            "INSERT INTO edge(src_id, rel, dst_id, created_at) VALUES (?,?,?,?)",
            (src_id, rel, dst_id, dates.utcnow()))
        for ident in (src_id, dst_id):
            _touch(db, ident)
            uid = conn.execute("SELECT uid FROM item WHERE id=?", (ident,)).fetchone()[0]
            _log(db, "link", ident, uid, {"changed": ["links"]},
                 note=f"{src_id} {rel} {dst_id}")
            _snapshot(db, ident)


def remove_edge(db: Database, src_id: int, rel: str, dst_id: int) -> None:
    with db.write():
        conn = db.conn()
        cur = conn.execute(
            "DELETE FROM edge WHERE src_id=? AND rel=? AND dst_id=?", (src_id, rel, dst_id))
        if not cur.rowcount:
            return
        for ident in (src_id, dst_id):
            _touch(db, ident)
            row = conn.execute("SELECT uid FROM item WHERE id=?", (ident,)).fetchone()
            if row:
                _log(db, "unlink", ident, row[0], {"changed": ["links"]},
                     note=f"{src_id} {rel} {dst_id}")
                _snapshot(db, ident)


# ---------------------------------------------------------------------------
# facets
# ---------------------------------------------------------------------------

def normalize_url(url: str) -> "tuple[str, str]":
    """Return ``(normalised, host)``.

    Scheme and host lowercased, default port dropped, fragment and tracking
    parameters stripped, trailing slash removed.  Two bookmarks that differ
    only by a campaign tag are the same bookmark.
    """
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

    raw = (url or "").strip()
    if not raw:
        raise ValidationError("a link needs a URL")
    if "://" not in raw:
        raw = "https://" + raw

    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    if (scheme == "http" and parts.port == 80) or (scheme == "https" and parts.port == 443):
        netloc = host
    else:
        netloc = f"{host}:{parts.port}" if parts.port else host

    tracking = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
                "utm_id", "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src",
                "igshid", "si", "s", "_ga"}
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if k.lower() not in tracking])

    path = parts.path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, query, "")), host


def _normalize_identity(channel: str, value: str) -> str:
    value = (value or "").strip()
    if channel == "email":
        return value.lower()
    if channel == "phone":
        digits = re.sub(r"[^\d+]", "", value)
        return digits
    return value.lower()


def _apply_facet(db: Database, item_id: int, kind: str, facet: Dict[str, Any],
                 tzid: Optional[str]) -> None:
    """Write the facet row for *kind*.

    Every timestamp that reaches here goes through dates.parse, so the local
    wall time, its zone and the derived instant are always written together
    and always agree.
    """
    conn = db.conn()
    table = FACET_TABLES.get(kind)
    if not table:
        return

    if kind == "task":
        due = facet.get("due")
        due_local = due_tzid = None
        due_epoch = None
        due_is_date = 0
        if due:
            moment = due if isinstance(due, dates.Moment) else dates.parse(str(due), tzid=tzid)
            due_local, due_tzid, due_epoch = moment.local, moment.tzid, moment.epoch
            due_is_date = 1 if moment.is_date else 0
        status = facet.get("status", "todo")
        completed = dates.utcnow() if status == "done" else None
        conn.execute(
            "INSERT INTO item_task(item_id, kind, status, priority, due_local, due_tzid, "
            "due_epoch, due_is_date, estimate_minutes, completed_at) "
            "VALUES (?,'task',?,?,?,?,?,?,?,?) "
            "ON CONFLICT(item_id) DO UPDATE SET status=excluded.status, "
            "priority=excluded.priority, due_local=excluded.due_local, "
            "due_tzid=excluded.due_tzid, due_epoch=excluded.due_epoch, "
            "due_is_date=excluded.due_is_date, estimate_minutes=excluded.estimate_minutes, "
            "completed_at=excluded.completed_at",
            (item_id, status, int(facet.get("priority", 0) or 0), due_local, due_tzid,
             due_epoch, due_is_date, facet.get("estimate_minutes"), completed))

    elif kind == "event":
        all_day = 1 if facet.get("all_day") else 0
        starts = facet.get("starts")
        if not starts:
            raise ValidationError("an event needs a start time")
        start = starts if isinstance(starts, dates.Moment) else dates.parse(
            str(starts), tzid=tzid, prefer_date=bool(all_day))
        all_day = 1 if start.is_date else all_day
        end = None
        if facet.get("ends"):
            ends = facet["ends"]
            end = ends if isinstance(ends, dates.Moment) else dates.parse(
                str(ends), tzid=tzid, prefer_date=bool(all_day))
        conn.execute(
            "INSERT INTO item_event(item_id, kind, all_day, starts_local, starts_epoch, "
            "ends_local, ends_epoch, tzid, location, rrule, rrule_supported) "
            "VALUES (?,'event',?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(item_id) DO UPDATE SET all_day=excluded.all_day, "
            "starts_local=excluded.starts_local, starts_epoch=excluded.starts_epoch, "
            "ends_local=excluded.ends_local, ends_epoch=excluded.ends_epoch, "
            "tzid=excluded.tzid, location=excluded.location, rrule=excluded.rrule, "
            "rrule_supported=excluded.rrule_supported",
            (item_id, all_day, start.local, start.epoch,
             end.local if end else None, end.epoch if end else None,
             start.tzid, facet.get("location", "") or "", facet.get("rrule"),
             1 if facet.get("rrule_supported", True) else 0))

    elif kind == "link":
        url_norm, host = normalize_url(facet.get("url", ""))
        live = 0 if conn.execute(
            "SELECT deleted_at FROM item WHERE id=?", (item_id,)).fetchone()[0] else 1
        conn.execute(
            "INSERT INTO item_link(item_id, kind, url, url_norm, host, live) "
            "VALUES (?,'link',?,?,?,?) "
            "ON CONFLICT(item_id) DO UPDATE SET url=excluded.url, "
            "url_norm=excluded.url_norm, host=excluded.host",
            (item_id, facet.get("url", ""), url_norm, host, live))

    elif kind == "file":
        if not facet.get("blob_id"):
            raise ValidationError("a file item needs stored bytes (blob_id)")
        conn.execute(
            "INSERT INTO item_file(item_id, kind, blob_id, filename, source_path, "
            "content_withheld, withheld_reason) VALUES (?,'file',?,?,?,?,?) "
            "ON CONFLICT(item_id) DO UPDATE SET blob_id=excluded.blob_id, "
            "filename=excluded.filename, source_path=excluded.source_path, "
            "content_withheld=excluded.content_withheld, "
            "withheld_reason=excluded.withheld_reason",
            (item_id, facet["blob_id"], facet.get("filename", ""),
             facet.get("source_path", "") or "",
             1 if facet.get("content_withheld") else 0,
             facet.get("withheld_reason", "") or ""))

    elif kind == "person":
        conn.execute(
            "INSERT INTO item_person(item_id, kind, given_name, family_name, org, role, birthday) "
            "VALUES (?,'person',?,?,?,?,?) "
            "ON CONFLICT(item_id) DO UPDATE SET given_name=excluded.given_name, "
            "family_name=excluded.family_name, org=excluded.org, role=excluded.role, "
            "birthday=excluded.birthday",
            (item_id, facet.get("given_name", "") or "", facet.get("family_name", "") or "",
             facet.get("org", "") or "", facet.get("role", "") or "", facet.get("birthday")))

        if any(k in facet for k in ("emails", "phones", "handles")):
            conn.execute("DELETE FROM person_identity WHERE item_id=?", (item_id,))
            for channel, key in (("email", "emails"), ("phone", "phones"), ("handle", "handles")):
                for index, value in enumerate(facet.get(key) or []):
                    if not str(value).strip():
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO person_identity"
                        "(item_id, channel, label, value, value_norm, is_primary) "
                        "VALUES (?,?,?,?,?,?)",
                        (item_id, channel, "", str(value).strip(),
                         _normalize_identity(channel, str(value)), 1 if index == 0 else 0))


# ---------------------------------------------------------------------------
# create / update / delete
# ---------------------------------------------------------------------------

def create(db: Database, *, kind: str = "note", title: str = "", body: str = "",
           props: Any = None, tags: Iterable[str] = (), facet: Optional[Dict[str, Any]] = None,
           uid: Optional[str] = None, pinned: bool = False,
           tzid: Optional[str] = None, extra_search: Sequence[str] = ()) -> Dict[str, Any]:
    """Create an item and everything attached to it, in one transaction."""
    with db.write():
        conn = db.conn()
        if not conn.execute("SELECT 1 FROM kind WHERE name=?", (kind,)).fetchone():
            known = ", ".join(r[0] for r in conn.execute("SELECT name FROM kind ORDER BY name"))
            raise ValidationError(f"unknown kind {kind!r}. Known kinds: {known}")

        props_json = canonical_props(props)
        parsed_props = json.loads(props_json)
        validate_props(conn, kind, parsed_props)

        uid = uid or ids.uuid7()
        now = dates.utcnow()
        search_extra = build_search_extra(title, body, parsed_props, extra_search)

        cur = conn.execute(
            "INSERT INTO item(uid, kind, title, body, props, search_extra, pinned, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (uid, kind, title, body, props_json, search_extra, 1 if pinned else 0, now, now))
        item_id = int(cur.lastrowid)

        # Always write the facet row for a facet kind, even when the caller
        # passed nothing.  An absent row is an event with no start time, and a
        # composite foreign key cannot constrain a row that does not exist --
        # so the check has to happen here, on the way in.
        if kind in FACET_TABLES:
            _apply_facet(db, item_id, kind, facet or {}, tzid)

        for slug in sorted({t.strip().strip("/").lower() for t in tags if t and t.strip()}):
            conn.execute("INSERT OR IGNORE INTO item_tag VALUES (?,?)",
                         (item_id, _tag_id(db, slug)))

        _log(db, "create", item_id, uid, {"changed": ["*"]})
        _snapshot(db, item_id)
        return compose(db, item_id)


def update(db: Database, item_id: int, *, title: Any = UNSET, body: Any = UNSET,
           props: Any = UNSET, facet: Any = UNSET, pinned: Any = UNSET,
           tags: Any = UNSET, expected_rev: Optional[int] = None,
           tzid: Optional[str] = None) -> Dict[str, Any]:
    """Change an item.  Only the fields passed are touched.

    ``expected_rev`` is checked *inside* the transaction, so no other writer
    can slip in between the check and the write.
    """
    with db.write():
        conn = db.conn()
        _check_rev(db, item_id, expected_rev)
        row = conn.execute("SELECT * FROM item WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise ItemNotFound(f"no item with id {item_id}")

        changed: List[str] = []
        before: Dict[str, Any] = {}
        sets: List[str] = []
        params: List[Any] = []

        new_title = row["title"] if title is UNSET else str(title)
        new_body = row["body"] if body is UNSET else str(body)
        new_props_json = row["props"] if props is UNSET else canonical_props(props)

        if title is not UNSET and new_title != row["title"]:
            changed.append("title"); before["title"] = row["title"]
            sets.append("title=?"); params.append(new_title)
        if body is not UNSET and new_body != row["body"]:
            changed.append("body"); before["body"] = row["body"]
            sets.append("body=?"); params.append(new_body)
        if props is not UNSET and new_props_json != row["props"]:
            validate_props(conn, row["kind"], json.loads(new_props_json))
            changed.append("props"); before["props"] = json.loads(row["props"])
            sets.append("props=?"); params.append(new_props_json)
        if pinned is not UNSET and bool(pinned) != bool(row["pinned"]):
            changed.append("pinned"); before["pinned"] = bool(row["pinned"])
            sets.append("pinned=?"); params.append(1 if pinned else 0)

        # search_extra is derived, so it is recomputed whenever a source of it
        # moves rather than being something a caller can set.
        if changed and {"title", "body", "props"} & set(changed):
            extra = build_search_extra(new_title, new_body, json.loads(new_props_json))
            if extra != row["search_extra"]:
                sets.append("search_extra=?"); params.append(extra)

        if sets:
            conn.execute(f"UPDATE item SET {', '.join(sets)} WHERE id=?", params + [item_id])

        if facet is not UNSET and facet:
            _apply_facet(db, item_id, row["kind"], facet, tzid)
            changed.append("facet")

        if tags is not UNSET:
            current = [r[0] for r in conn.execute(
                "SELECT t.slug FROM item_tag it JOIN tag t ON t.id=it.tag_id "
                "WHERE it.item_id=? ORDER BY t.slug", (item_id,))]
            wanted = sorted({t.strip().strip("/").lower() for t in tags if t and t.strip()})
            if wanted != current:
                conn.execute("DELETE FROM item_tag WHERE item_id=?", (item_id,))
                for slug in wanted:
                    conn.execute("INSERT OR IGNORE INTO item_tag VALUES (?,?)",
                                 (item_id, _tag_id(db, slug)))
                changed.append("tags"); before["tags"] = current

        if not changed:
            return compose(db, item_id)

        _touch(db, item_id)
        _log(db, "update", item_id, row["uid"], {"changed": changed, "before": before})
        _snapshot(db, item_id)
        return compose(db, item_id)


def trash(db: Database, item_id: int, *, expected_rev: Optional[int] = None) -> None:
    """Soft delete.  The row stays, and stays in the search index."""
    with db.write():
        _check_rev(db, item_id, expected_rev)
        conn = db.conn()
        row = conn.execute("SELECT uid, deleted_at FROM item WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise ItemNotFound(f"no item with id {item_id}")
        if row[1]:
            return
        conn.execute("UPDATE item SET deleted_at=? WHERE id=?", (dates.utcnow(), item_id))
        _touch(db, item_id)
        _log(db, "trash", item_id, row[0], {"changed": ["deleted_at"], "before": {"deleted_at": None}})


def restore(db: Database, item_id: int) -> None:
    """Bring an item back out of the trash.

    A restored bookmark can collide with one added while it was gone: live
    URLs are unique.  That surfaces as a clear error rather than a raw
    IntegrityError, because the user has to choose which one to keep.
    """
    with db.write():
        conn = db.conn()
        row = conn.execute("SELECT uid, kind, deleted_at FROM item WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise ItemNotFound(f"no item with id {item_id}")
        if not row[2]:
            return

        if row[1] == "link":
            clash = conn.execute(
                "SELECT i.uid FROM item_link l JOIN item i ON i.id=l.item_id "
                "WHERE l.live=1 AND l.url_norm=(SELECT url_norm FROM item_link WHERE item_id=?)",
                (item_id,)).fetchone()
            if clash:
                raise ValidationError(
                    f"cannot restore: {ids.short(clash[0])} already bookmarks that URL. "
                    f"Trash that one first, or merge them.")

        conn.execute("UPDATE item SET deleted_at=NULL WHERE id=?", (item_id,))
        _touch(db, item_id)
        _log(db, "restore", item_id, row[0], {"changed": ["deleted_at"]})


def purge(db: Database, item_id: int) -> str:
    """Delete permanently, leaving a tombstone and a full audit record.

    Inbound edges are captured *before* the delete: ON DELETE CASCADE removes
    edges belonging to surviving items, and without this the other end's
    stored revision would list a link that no longer exists.
    """
    with db.write():
        conn = db.conn()
        row = conn.execute("SELECT uid, kind, title FROM item WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise ItemNotFound(f"no item with id {item_id}")

        doc = compose(db, item_id)
        inbound = [
            {"rel": r[0], "uid": r[1]} for r in conn.execute(
                "SELECT e.rel, i.uid FROM edge e JOIN item i ON i.id=e.src_id WHERE e.dst_id=?",
                (item_id,))
        ]
        doc["inbound_at_purge"] = inbound
        payload = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

        conn.execute(
            "INSERT OR REPLACE INTO tombstone(uid, kind, title, reason, redirect_to_uid, at, doc_z) "
            "VALUES (?,?,?,'purge',NULL,?,?)",
            (row[0], row[1], row[2], dates.utcnow(), zlib.compress(payload.encode("utf-8"), 6)))

        _log(db, "purge", None, row[0],
             {"changed": ["*"], "before": {"title": row[2], "kind": row[1]}},
             note=f"{len(inbound)} inbound link(s) removed by cascade")

        conn.execute("DELETE FROM item WHERE id=?", (item_id,))
        return row[0]
