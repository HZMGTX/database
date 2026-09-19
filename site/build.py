#!/usr/bin/env python3
"""Generate the public site's demo data from the real Vault engine.

The demo page on the website has to answer queries without a server, so it
ships a corpus as JSON and a query engine in JavaScript.  That JavaScript is
a reimplementation, and a reimplementation is a chance to be subtly wrong.

This script is the defence.  It builds a throwaway Vault from
``site/corpus.json``, runs the demo's own example queries through the real
Python engine, and writes the answers out alongside the corpus.  The parity
test then replays those queries through the JavaScript and fails the build
if the two disagree about a single item.

So the demo is not "search-like".  Every query it advertises has been
checked against the engine it is imitating.

Run it with::

    python3 site/build.py

Nothing here touches your own vault: the corpus is synthetic and the
database is built in a temporary directory that is deleted on the way out.
"""

import datetime as _dt
import datetime as _dt
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))

from vault import migrate, model, search  # noqa: E402
from vault.db import Database  # noqa: E402
from vault.paths import Layout  # noqa: E402

# The queries the demo page offers as examples.  Every one is executed by the
# real engine here and by the JavaScript there, and they must agree.
DEMO_QUERIES = [
    "budget",
    "typography",
    "meridian",
    "contrast",
    "backup",
    "kind:task",
    "kind:task status:todo",
    "kind:task is:open",
    "kind:book",
    "kind:note,link",
    "tag:clients/meridian",
    "tag:craft/*",
    "tag:work/business",
    "is:pinned",
    "is:untagged",
    "is:done",
    "has:link",
    "has:due",
    "is:orphan",
    "pages>400",
    "pages<200",
    "rating=5",
    "year<1990 rating>3",
    "amount>1000",
    "budget=48000",
    "shelf:craft",
    'author:"Don Norman"',
    '"the brief"',
    '"dark background"',
    "design -book",
    "-kind:book -kind:person",
    "studio sort:title",
    "typography kind:link",
    "kind:book rating>4 sort:title",
    "kind:book rating>4 sort:title limit:5",
    "UI",
    "invoice",
    "lisbon",
    "sort:recent limit:8",
    "sort:created limit:6",
    "kind:task sort:due",
    "due<2026-10-01",
    "starts>2026-10-01",
    "created<2026-03-01",
    "updated>2026-06-01 kind:book",
    "kind:book sort:oldest limit:4",
    "client",
    "contrast ratio",
    "scope",
]


# The corpus is built in one burst, so without help every item carries the
# same second and a freshly minted random identifier. Neither is what a vault
# that grew over ten months looks like, and both make the generated data
# differ on every run -- which would leave the parity file comparing itself
# to a corpus that no longer exists.
#
# So both are derived from the item's position instead. The timestamps walk a
# stride coprime with the item count, which covers every slot exactly once
# while leaving the order on disk unrelated to the order in time; that is what
# makes sort:recent visibly different from sort:title. The span is chosen so
# the last slot lands a little before the day this was written rather than in
# the future, because a demo full of items created next year reads as broken.
# The identifiers keep
# UUIDv7's shape -- a millisecond timestamp in the leading twelve hex digits,
# then twenty digits that distinguish -- with the tail taken from a hash of
# the item's name rather than from the random pool, so a link to an item in
# the demo still resolves after the data is rebuilt.
EPOCH = _dt.datetime(2025, 12, 2, 9, 14, tzinfo=_dt.timezone.utc)
STRIDE = 23


def scheduled(position: int, slots: int) -> _dt.datetime:
    offset = (position * STRIDE) % slots
    return EPOCH + _dt.timedelta(days=offset * 4, minutes=offset * 37)


def stable_uid(key: str, when: _dt.datetime) -> str:
    millis = int(when.timestamp() * 1000)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"{millis:012x}"[-12:] + digest[:20]


def build_corpus(db: Database) -> Dict[str, int]:
    """Create every item in ``corpus.json``.  Returns id-by-slug."""
    spec = json.loads((HERE / "corpus.json").read_text("utf-8"))
    conn = db.conn()

    with db.write():
        for k in spec.get("custom_kinds", []):
            conn.execute(
                "INSERT OR IGNORE INTO kind(name, label, plural, icon, builtin, "
                "sort_order, created_at) VALUES (?,?,?,?,0,500,?)",
                (k["name"], k["label"], k["plural"], k.get("icon", "note"),
                 model.dates.utcnow()))
        for f in spec.get("custom_fields", []):
            conn.execute(
                "INSERT OR IGNORE INTO kind_field(kind, key, label, type, required, "
                "multi, enum_values, hint, sort_order) VALUES (?,?,?,?,0,0,'[]','',100)",
                (f["kind"], f["key"], f["label"], f["type"]))

    ids: Dict[str, int] = {}
    pending: List[tuple] = []
    slots = len(spec["items"]) + len(spec["books"])
    position = 0

    for entry in spec["items"]:
        when = scheduled(position, slots)
        row = model.create(
            db,
            kind=entry["kind"],
            title=entry["title"],
            body=entry.get("body", ""),
            props=entry.get("props"),
            tags=entry.get("tags", []),
            facet=entry.get("facet"),
            pinned=entry.get("pinned", False),
            uid=stable_uid(entry["id"], when),
            tzid="UTC",
        )
        ids[entry["id"]] = conn.execute(
            "SELECT id FROM item WHERE uid=?", (row["uid"],)).fetchone()[0]
        for rel, target in entry.get("links", []):
            pending.append((entry["id"], rel, target))
        position += 1

    for source, rel, target in pending:
        model.add_edge(db, ids[source], rel, ids[target])

    for book in spec["books"]:
        when = scheduled(position, slots)
        props = {k: book[k] for k in ("author", "pages", "year", "rating", "shelf")}
        model.create(db, kind="book", title=book["title"], body=book.get("body", ""),
                     props=props, tags=book.get("tags", []),
                     uid=stable_uid("book:" + book["title"], when), tzid="UTC")
        position += 1

    return ids


def stagger_timestamps(db: Database) -> None:
    """Apply the schedule the identifiers were minted against.

    This writes ``created_at`` and ``updated_at`` directly rather than going
    through ``model.update``, which would be the wrong thing to do to a real
    vault: it skips the change log. It is safe here because neither column is
    indexed by FTS, so no trigger has anything to say about it, and this
    database is deleted before the script returns.
    """
    conn = db.conn()
    ids = [r[0] for r in conn.execute("SELECT id FROM item ORDER BY id")]
    with db.write():
        for position, item_id in enumerate(ids):
            stamp = scheduled(position, len(ids)).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute("UPDATE item SET created_at=?, updated_at=? WHERE id=?",
                         (stamp, stamp, item_id))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="vault-site-"))
    try:
        layout = Layout(tmp / "demo.db")
        layout.ensure()
        db = Database(layout)
        migrate.migrate(db)
        build_corpus(db)
        stagger_timestamps(db)

        conn = db.conn()
        records = []
        for (item_id,) in conn.execute(
                "SELECT id FROM item WHERE deleted_at IS NULL ORDER BY id"):
            doc = model.compose(db, item_id)
            doc.pop("rev", None)
            records.append(doc)

        # The JavaScript ranks with the same bm25 column weights, so it needs
        # the same four columns the index is built over.
        for doc, row in zip(records, conn.execute(
                "SELECT tags_cache, search_extra FROM item "
                "WHERE deleted_at IS NULL ORDER BY id")):
            doc["_tags_cache"] = row[0]
            doc["_search_extra"] = row[1]

        answers = {}
        for query in DEMO_QUERIES:
            # No limit override: the query's own limit: clause decides, so
            # the JavaScript has to get that right too.
            page = search.search(db, query, tzid="UTC", want_total=False)
            answers[query] = [h.uid for h in page.hits]

        kinds = [dict(name=r[0], label=r[1], plural=r[2]) for r in conn.execute(
            "SELECT name, label, plural FROM kind WHERE name IN "
            "(SELECT DISTINCT kind FROM item WHERE deleted_at IS NULL) ORDER BY sort_order")]
        tags = [r[0] for r in conn.execute(
            "SELECT slug FROM tag WHERE id IN (SELECT tag_id FROM item_tag) ORDER BY slug")]

        out = HERE / "data"
        out.mkdir(exist_ok=True)
        payload = {"items": records, "kinds": kinds, "tags": tags,
                   "generated_by": "site/build.py", "count": len(records)}
        (out / "corpus.js").write_text(
            "window.CORPUS = " + json.dumps(payload, separators=(",", ":")) + ";\n",
            "utf-8")
        (out / "parity.json").write_text(
            json.dumps({"queries": DEMO_QUERIES, "answers": answers}, indent=1) + "\n",
            "utf-8")

        print(f"{len(records)} items -> site/data/corpus.js")
        print(f"{len(DEMO_QUERIES)} queries -> site/data/parity.json")
        empty = [q for q, a in answers.items() if not a]
        if empty:
            print("WARNING: these demo queries return nothing: " + ", ".join(empty))
            return 1
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
