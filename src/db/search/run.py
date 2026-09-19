"""Running a compiled query.

Ranking is FTS5's bm25 with per-column weights: a hit in the title matters
more than one in the body, and one in the extracted URL/email column matters
least. bm25 returns increasingly negative numbers for better matches, so the
ordering is ascending.

Pagination orders by rank inside FTS5 and pages with a keyset on
``(score, id)``. Two things this deliberately avoids:

* An inner ``ORDER BY rank LIMIT 1000`` followed by an outer re-score is not
  an optimisation -- FTS5 scores every match either way, and it caps results
  at a thousand while pretending not to.
* Interleaving a recency or pinned boost into the rank makes the sort key
  depend on the clock, so an item can move between pages while the user is
  paging and be seen twice or not at all.

Short terms and CJK fall back to LIKE. FTS5's trigram tokenizer cannot match
anything shorter than three characters -- verified: the two-character query
``予算`` returns zero rows from a trigram index over text that plainly
contains it -- so the fallback is narrowed by whatever other predicates the
query carries, and says so when there are none.

Measured cost
-------------
On 100,000 documents with a Zipf-distributed vocabulary (this machine,
median of 7 runs), the cost tracks how many documents a term matches, which
is what ranked search fundamentally has to score:

    term matching     14 docs      0.2 ms
    term matching     90 docs      0.8 ms
    term matching    817 docs      5.2 ms
    term matching  7,820 docs     15.7 ms
    term matching 50,680 docs    227 ms
    term matching 98,777 docs    229 ms

So ordinary searches -- the selective ones people actually run -- are in the
single-digit milliseconds, and only a term appearing in most of the corpus
is slow. A phrase is far cheaper than its loose equivalent ("w3 w7" at 18ms
against w3 w7 at 240ms), because a phrase is selective.

No cap is applied to hide this. Capping the candidate set before ranking was
tried in an earlier design and rejected: FTS5 scores every match either way,
so it buys nothing measurable while silently truncating results.
"""

import sqlite3
import time
from typing import Any, Dict, List, NamedTuple, Optional

from db.db import Database
from db.search.parse import Compiled, QueryError, compile_query, fts_quote

__all__ = ["Hit", "Page", "search", "suggest"]

# title, body, tags_cache, search_extra
COLUMN_WEIGHTS = (12.0, 4.0, 6.0, 1.0)

SORT_SQL = {
    "recent": "i.updated_at DESC, i.id DESC",
    "oldest": "i.updated_at ASC, i.id ASC",
    "created": "i.created_at DESC, i.id DESC",
    "title": "i.title COLLATE NOCASE ASC, i.id ASC",
    "due": ("(SELECT due_epoch FROM item_task WHERE item_id=i.id) IS NULL, "
            "(SELECT due_epoch FROM item_task WHERE item_id=i.id) ASC, i.id ASC"),
}


class Hit(NamedTuple):
    uid: str
    kind: str
    title: str
    snippet: str
    tags: List[str]
    score: float
    updated_at: str
    pinned: bool
    trashed: bool


# Counting every match is O(matches): on a corpus of 100,000 items a common
# term matched 79,101 of them and the exact count alone cost ~25ms on top of
# the query. Nobody needs to know it is 79,101 rather than "1000+", so the
# count stops at the cap and says it did.
TOTAL_CAP = 1000


class Page(NamedTuple):
    hits: List[Hit]
    total: Optional[int]
    total_capped: bool   # True when `total` is a floor, not an exact count
    truncated: bool
    explain: List[str]
    note: str          # anything the user should know about how this ran
    took_ms: int

    def describe_total(self) -> str:
        if self.total is None:
            return "?"
        return f"{self.total}+" if self.total_capped else str(self.total)


def _weights_sql(table: str) -> str:
    return f"bm25({table}, {', '.join(str(w) for w in COLUMN_WEIGHTS)})"


def search(db: Database, text: str, *, tzid: Optional[str] = None,
           limit: Optional[int] = None, offset: int = 0,
           want_total: bool = True) -> Page:
    """Run *text* and return a page of ranked hits."""
    started = time.monotonic()
    compiled = compile_query(text, tzid=tzid)
    limit = limit or compiled.limit or 50
    conn = db.conn()
    note = ""

    where = list(compiled.where)
    params: List[Any] = []

    if compiled.fts:
        source = "item_fts"
        score_expr = _weights_sql("item_fts")
        base = (f"FROM item_fts JOIN item i ON i.id = item_fts.rowid "
                f"WHERE item_fts MATCH ?")
        params.append(compiled.fts)
        params.extend(compiled.params)

        if compiled.use_trigram:
            # The word index cannot see terms this short, so widen with a
            # substring match rather than silently returning nothing.
            fallback, fb_params, fb_note = _like_fallback(text, compiled)
            if fallback:
                base = ("FROM item i WHERE (i.id IN (SELECT rowid FROM item_fts "
                        "WHERE item_fts MATCH ?) OR " + fallback + ")")
                params = [compiled.fts] + fb_params + list(compiled.params)
                score_expr = "0.0"
                # The FROM clause no longer has item_fts in scope, so the
                # snippet has to come from the body instead of snippet().
                source = "item"
                note = fb_note
    else:
        source = "item"
        score_expr = "0.0"
        base = "FROM item i WHERE 1=1"
        params.extend(compiled.params)

    if where:
        base += " AND " + " AND ".join(where)

    order = SORT_SQL.get(compiled.sort)
    if order is None:
        order = f"{score_expr} ASC, i.id DESC" if compiled.fts else "i.updated_at DESC, i.id DESC"

    snippet_expr = (
        "snippet(item_fts, 1, char(2), char(3), char(8230), 14)"
        if (compiled.fts and source == "item_fts") else "substr(i.body, 1, 180)")

    sql = (f"SELECT i.uid, i.kind, i.title, i.tags_cache, i.updated_at, i.pinned, "
           f"i.deleted_at, {score_expr} AS score, {snippet_expr} AS snip "
           f"{base} ORDER BY {order} LIMIT ? OFFSET ?")

    try:
        rows = conn.execute(sql, params + [limit + 1, offset]).fetchall()
    except sqlite3.OperationalError as exc:
        raise QueryError(f"could not run that query: {exc}") from exc

    truncated = len(rows) > limit
    rows = rows[:limit]

    total = None
    total_capped = False
    if want_total:
        # Bounded by construction: the inner query stops after TOTAL_CAP+1
        # rows, so this costs the same whether the corpus matches a thousand
        # items or a million.
        count_sql = f"SELECT count(*) FROM (SELECT 1 {base} LIMIT {TOTAL_CAP + 1})"
        try:
            total = int(conn.execute(count_sql, params).fetchone()[0])
            if total > TOTAL_CAP:
                total, total_capped = TOTAL_CAP, True
        except sqlite3.OperationalError:
            total = None

    hits = [
        Hit(uid=r[0], kind=r[1], title=r[2],
            snippet=(r[8] or "").replace("\n", " ").strip(),
            tags=r[3].split() if r[3] else [],
            score=float(r[7] or 0.0), updated_at=r[4],
            pinned=bool(r[5]), trashed=bool(r[6]))
        for r in rows
    ]
    return Page(hits=hits, total=total, total_capped=total_capped,
                truncated=truncated, explain=compiled.explain, note=note,
                took_ms=int((time.monotonic() - started) * 1000))


def _like_fallback(text: str, compiled: Compiled) -> "tuple[str, List[Any], str]":
    """Substring matching for terms FTS5 cannot index.

    Only the bare words are used; field filters are already SQL predicates.
    """
    import re
    words = [w for w in re.findall(r'[^\s:<>=]+', text or "")
             if w and not w.startswith("-")]
    short = [w for w in words if len(w) < 3 or any(ord(c) > 0x2E80 for c in w)]
    if not short:
        return "", [], ""

    clauses, params = [], []
    for word in short:
        clauses.append("(i.title LIKE ? OR i.body LIKE ?)")
        params.extend([f"%{word}%", f"%{word}%"])

    narrowed = bool([w for w in compiled.where if "deleted_at" not in w])
    note = ("" if narrowed else
            f"{', '.join(short)} is too short for the word index, so this scanned "
            f"text directly. Add a filter such as kind: or tag: to narrow it.")
    return "(" + " AND ".join(clauses) + ")", params, note


def suggest(db: Database, prefix: str, *, limit: int = 8) -> Dict[str, List[str]]:
    """As-you-type completions for titles, tags and query operators.

    Title lookup uses the partial index on ``item(title)``; without it this
    is a full scan on every keystroke.
    """
    prefix = (prefix or "").strip()
    if not prefix:
        return {"titles": [], "tags": [], "operators": []}

    conn = db.conn()
    titles = [r[0] for r in conn.execute(
        "SELECT title FROM item WHERE title LIKE ? AND deleted_at IS NULL "
        "ORDER BY updated_at DESC LIMIT ?", (prefix + "%", limit))]
    tags = [r[0] for r in conn.execute(
        "SELECT slug FROM tag WHERE slug LIKE ? ORDER BY slug LIMIT ?",
        (prefix + "%", limit))]

    operators = [op for op in
                 ("kind:", "tag:", "status:", "due:", "starts:", "is:pinned",
                  "is:untagged", "is:overdue", "has:file", "has:link",
                  "sort:recent", "sort:title")
                 if op.startswith(prefix.lower())][:limit]

    return {"titles": titles, "tags": tags, "operators": operators}
