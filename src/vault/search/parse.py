"""The query language: text in, SQL and an FTS5 expression out.

**Rule one, and the reason this module exists: user text is never
concatenated into an FTS5 MATCH expression.** FTS5 has its own grammar, and
ordinary words collide with it. Verified against this SQLite build, each of
these is a *syntax error* when passed through raw::

    AND            fts5: syntax error near "AND"
    (              fts5: syntax error near ""
    "unclosed      unterminated string

Every term is wrapped in double quotes, with embedded quotes doubled, which
turns all of it into a literal. The same inputs then match harmlessly.

Rule two: SQL predicates are built as parameterised fragments. No value the
user typed is ever formatted into a SQL string.

The language accepts what people actually type::

    budget review                 both words
    "exact phrase"                a phrase
    -draft                        excluding a word
    kind:task status:todo,doing   facet filters
    tag:work/clients              a tag, or a subtree with tag:work/*
    due:<friday  due:today        dates, in the user's zone
    amount>5000                   any property, via the typed projection
    is:pinned  is:untagged        flags
    has:file   has:link           structure
    sort:recent  limit:50         output
"""

import re
from typing import Any, Dict, List, NamedTuple, Optional

from vault import dates

__all__ = ["Compiled", "QueryError", "compile_query", "fts_quote"]


class QueryError(ValueError):
    """The query could not be understood."""


class Compiled(NamedTuple):
    """A parsed query, ready to run."""

    fts: Optional[str]           # FTS5 MATCH expression, or None for no text search
    where: List[str]             # SQL predicate fragments, ANDed
    params: List[Any]            # bound values, in order
    sort: str                    # rank | recent | created | title | due
    limit: Optional[int]
    include_trashed: bool
    trashed_only: bool
    explain: List[str]           # what each clause was understood to mean
    use_trigram: bool            # short/CJK terms the word index cannot match


def fts_quote(term: str) -> str:
    """Make *term* a literal in an FTS5 expression.

    This is the whole safety story for MATCH. Doubling embedded quotes is
    what keeps an input like ``say "hi"`` from terminating the string early.
    """
    return '"' + term.replace('"', '""') + '"'


# Splits a query into bare words, "quoted phrases", field:value pairs and
# comparisons, keeping quoted runs intact.
_TOKEN_RE = re.compile(r'''
      (?P<neg>-)?
      (?:
          (?P<field>[a-zA-Z_][a-zA-Z0-9_]*)
          (?P<op>:|>=|<=|>|<|=)
          (?P<value>"[^"]*"|[^\s]+)
        | "(?P<phrase>[^"]*)"
        | (?P<word>[^\s]+)
      )
''', re.X)

# What `is:` and `has:` actually accept.  These sets are not documentation:
# they are what the error message offers when someone gets it wrong, so a
# name in here that the parser does not handle sends the user round in a
# circle -- told to try `is:attached`, then told `is:attached` is unknown.
# `test_search.py::test_every_advertised_flag_parses` walks both sets and
# runs each one, so they cannot drift from the branches below again.
FLAG_VALUES = {"pinned", "untagged", "trashed", "any", "done", "open",
               "overdue", "orphan", "withheld"}
STRUCTURE_VALUES = {"file", "link", "body", "tag", "due"}
SORTS = {"rank", "recent", "created", "title", "due", "oldest"}

# A term shorter than 3 characters cannot be found by the trigram index, and
# a script that does not separate words (CJK) tokenises poorly in the word
# index. Both route to a LIKE fallback over a narrowed candidate set.
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯]")


def _is_unsegmented(text: str) -> bool:
    return bool(_CJK.search(text))


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def compile_query(text: str, *, tzid: Optional[str] = None,
                  default_limit: int = 50) -> Compiled:
    """Parse *text* into an FTS expression plus SQL predicates."""
    fts_terms: List[str] = []
    where: List[str] = []
    params: List[Any] = []
    explain: List[str] = []
    sort = "rank"
    limit = default_limit
    include_trashed = False
    trashed_only = False
    use_trigram = False

    for match in _TOKEN_RE.finditer(text or ""):
        negate = bool(match.group("neg"))
        field = match.group("field")
        op = match.group("op")
        raw_value = match.group("value")
        phrase = match.group("phrase")
        word = match.group("word")

        if field and op:
            value = _strip_quotes(raw_value or "")
            handled = _apply_field(
                field.lower(), op, value, negate,
                where, params, explain, tzid)
            if handled == "sort":
                if value.lower() not in SORTS:
                    raise QueryError(
                        f"unknown sort {value!r}. Try: {', '.join(sorted(SORTS))}")
                sort = value.lower()
            elif handled == "limit":
                try:
                    limit = max(1, min(1000, int(value)))
                except ValueError as exc:
                    raise QueryError(f"limit must be a number, got {value!r}") from exc
            elif handled == "trashed":
                trashed_only, include_trashed = True, True
            elif handled == "any_state":
                include_trashed = True
            continue

        term = phrase if phrase is not None else word
        if not term:
            continue

        if phrase is not None:
            fts_terms.append(("NOT " if negate else "") + fts_quote(term))
            explain.append(f"{'without' if negate else 'containing'} the phrase {term!r}")
        else:
            if _is_unsegmented(term) or len(term) < 3:
                use_trigram = True
            fts_terms.append(("NOT " if negate else "") + fts_quote(term))
            explain.append(f"{'without' if negate else 'containing'} {term!r}")

    # FTS5 has no leading NOT: "NOT x" alone is a syntax error, because NOT is
    # a binary operator. A query that is only exclusions needs something to
    # subtract from, so those become SQL predicates instead.
    positives = [t for t in fts_terms if not t.startswith("NOT ")]
    negatives = [t[4:] for t in fts_terms if t.startswith("NOT ")]

    fts: Optional[str] = None
    if positives:
        fts = " AND ".join(positives)
        if negatives:
            fts += " NOT " + " NOT ".join(negatives)
    elif negatives:
        for term in negatives:
            where.append("i.id NOT IN (SELECT rowid FROM item_fts WHERE item_fts MATCH ?)")
            params.append(term)

    if not trashed_only and not include_trashed:
        where.append("i.deleted_at IS NULL")
    elif trashed_only:
        where.append("i.deleted_at IS NOT NULL")

    return Compiled(fts=fts, where=where, params=params, sort=sort, limit=limit,
                    include_trashed=include_trashed, trashed_only=trashed_only,
                    explain=explain, use_trigram=use_trigram)


def _apply_field(field: str, op: str, value: str, negate: bool,
                 where: List[str], params: List[Any], explain: List[str],
                 tzid: Optional[str]) -> Optional[str]:
    """Translate one ``field:value`` clause. Returns a marker for the ones
    the caller has to act on (sort, limit, trashed)."""
    not_ = "NOT " if negate else ""

    if field == "sort":
        return "sort"
    if field == "limit":
        return "limit"

    if field == "kind":
        values = [v.strip() for v in value.split(",") if v.strip()]
        placeholders = ",".join("?" * len(values))
        where.append(f"i.kind {not_}IN ({placeholders})")
        params.extend(values)
        explain.append(f"{'not ' if negate else ''}of kind {' or '.join(values)}")
        return None

    if field == "tag":
        # A trailing /* means the whole subtree: tag:work/* also finds
        # work/clients/acme.
        if value.endswith("/*") or value == "*":
            prefix = value[:-2] if value.endswith("/*") else ""
            where.append(
                f"i.id {not_}IN (SELECT it.item_id FROM item_tag it JOIN tag t ON t.id=it.tag_id "
                f"WHERE t.slug = ? OR t.slug GLOB ?)")
            params.extend([prefix, prefix + "/*"])
            explain.append(f"{'not ' if negate else ''}tagged {prefix} or below")
        else:
            where.append(
                f"i.id {not_}IN (SELECT it.item_id FROM item_tag it JOIN tag t ON t.id=it.tag_id "
                f"WHERE t.slug = ?)")
            params.append(value.strip("/").lower())
            explain.append(f"{'not ' if negate else ''}tagged {value}")
        return None

    if field == "status":
        values = [v.strip() for v in value.split(",") if v.strip()]
        placeholders = ",".join("?" * len(values))
        where.append(
            f"i.id {not_}IN (SELECT item_id FROM item_task WHERE status IN ({placeholders}))")
        params.extend(values)
        explain.append(f"status {' or '.join(values)}")
        return None

    if field == "is":
        flag = value.lower()
        if flag == "pinned":
            where.append(f"i.pinned = {0 if negate else 1}")
            explain.append(f"{'not ' if negate else ''}pinned")
        elif flag == "untagged":
            where.append(f"i.tags_cache {'!=' if negate else '='} ''")
            explain.append(f"{'not ' if negate else ''}untagged")
        elif flag == "trashed":
            explain.append("in the trash")
            return "trashed"
        elif flag == "any":
            explain.append("including trashed")
            return "any_state"
        elif flag in ("done", "open"):
            wanted = "('done','cancelled')" if flag == "done" else "('todo','doing','blocked')"
            where.append(
                f"i.id {not_}IN (SELECT item_id FROM item_task WHERE status IN {wanted})")
            explain.append(flag)
        elif flag == "overdue":
            now = int(__import__("time").time())
            where.append(
                "i.id IN (SELECT item_id FROM item_task WHERE due_epoch IS NOT NULL "
                "AND due_epoch < ? AND status NOT IN ('done','cancelled'))")
            params.append(now)
            explain.append("overdue")
        elif flag == "orphan":
            where.append(
                "i.id NOT IN (SELECT src_id FROM edge UNION SELECT dst_id FROM edge)")
            explain.append("with no links")
        elif flag == "withheld":
            where.append("i.id IN (SELECT item_id FROM item_file WHERE content_withheld=1)")
            explain.append("content withheld as a possible secret")
        else:
            raise QueryError(
                f"unknown flag is:{value}. Try: {', '.join(sorted(FLAG_VALUES))}")
        return None

    if field == "has":
        what = value.lower()
        if what == "file":
            where.append(f"i.id {not_}IN (SELECT item_id FROM item_file)")
        elif what == "link":
            where.append(f"i.id {not_}IN (SELECT src_id FROM edge UNION SELECT dst_id FROM edge)")
        elif what == "body":
            where.append(f"i.body {'=' if negate else '!='} ''")
        elif what == "tag":
            where.append(f"i.tags_cache {'=' if negate else '!='} ''")
        elif what == "due":
            where.append(
                f"i.id {not_}IN (SELECT item_id FROM item_task WHERE due_epoch IS NOT NULL)")
        else:
            raise QueryError(
                f"unknown has:{value}. Try: {', '.join(sorted(STRUCTURE_VALUES))}")
        explain.append(f"{'without' if negate else 'with'} {what}")
        return None

    if field in ("due", "starts", "created", "updated"):
        _apply_date(field, op, value, negate, where, params, explain, tzid)
        return None

    if field == "uid":
        where.append("i.uid = ? OR substr(i.uid,-8) = ?")
        params.extend([value.lower(), value.lower()])
        return None

    # Anything else is a property, resolved through the typed projection.
    _apply_property(field, op, value, negate, where, params, explain)
    return None


def _apply_date(field: str, op: str, value: str, negate: bool,
                where: List[str], params: List[Any], explain: List[str],
                tzid: Optional[str]) -> None:
    """Date comparisons, in the user's zone.

    ``due:today`` means the whole of today locally, not the instant of
    midnight UTC -- which is the bug that makes everything due today look
    overdue west of Greenwich.
    """
    import datetime as _dt

    column = {
        "due": ("item_task", "due_epoch"),
        "starts": ("item_event", "starts_epoch"),
    }.get(field)

    bare = value.lstrip("<>=")
    operator = value[:len(value) - len(bare)] or (op if op != ":" else "")

    if field in ("created", "updated"):
        col = "i.created_at" if field == "created" else "i.updated_at"
        try:
            moment = dates.parse(bare, tzid=tzid)
        except dates.ParseError as exc:
            raise QueryError(str(exc)) from exc
        iso = _dt.datetime.utcfromtimestamp(moment.epoch).strftime("%Y-%m-%dT%H:%M:%SZ")
        sql_op = {"<": "<", ">": ">", "<=": "<=", ">=": ">=", "": ">="}.get(operator, ">=")
        where.append(f"{col} {sql_op} ?")
        params.append(iso)
        explain.append(f"{field} {sql_op} {bare}")
        return

    if column is None:
        raise QueryError(f"cannot compare {field} as a date")

    table, epoch_col = column
    try:
        moment = dates.parse(bare, tzid=tzid)
    except dates.ParseError as exc:
        raise QueryError(str(exc)) from exc

    if operator in ("", ":") and bare.lower() in ("today", "tomorrow", "yesterday"):
        # A bare day means the whole local day, both ends.
        day = _dt.datetime.fromtimestamp(moment.epoch, dates.zone(tzid)).date()
        low, high = dates.day_bounds(day, tzid)
        where.append(
            f"i.id IN (SELECT item_id FROM {table} WHERE {epoch_col} BETWEEN ? AND ?)")
        params.extend([low, high])
        explain.append(f"{field} on {day.isoformat()}")
        return

    sql_op = {"<": "<", ">": ">", "<=": "<=", ">=": ">="}.get(operator, "<=")
    where.append(
        f"i.id IN (SELECT item_id FROM {table} WHERE {epoch_col} IS NOT NULL "
        f"AND {epoch_col} {sql_op} ?)")
    params.append(moment.epoch)
    explain.append(f"{field} {sql_op} {bare}")


def _apply_property(field: str, op: str, value: str, negate: bool,
                    where: List[str], params: List[Any], explain: List[str]) -> None:
    """Query a property through the typed projection.

    ``attr`` and ``attr_multi`` are trigger-maintained and indexed per type,
    so ``amount>5000`` on a field nobody declared is still an index range
    scan rather than a scan plus json_extract on every row.
    """
    not_ = "NOT " if negate else ""
    numeric: Optional[float]
    try:
        numeric = float(value)
    except ValueError:
        numeric = None

    if op in (">", "<", ">=", "<="):
        if numeric is None:
            raise QueryError(f"{field}{op}{value} needs a number on the right")
        where.append(
            f"i.id {not_}IN (SELECT item_id FROM attr WHERE key=? AND vnum IS NOT NULL "
            f"AND vnum {op} ? UNION SELECT item_id FROM attr_multi WHERE key=? "
            f"AND vnum IS NOT NULL AND vnum {op} ?)")
        params.extend([field, numeric, field, numeric])
        explain.append(f"{field} {op} {value}")
        return

    if numeric is not None:
        where.append(
            f"i.id {not_}IN (SELECT item_id FROM attr WHERE key=? AND (vnum = ? OR vtext = ?) "
            f"UNION SELECT item_id FROM attr_multi WHERE key=? AND (vnum = ? OR vtext = ?))")
        params.extend([field, numeric, value, field, numeric, value])
    else:
        where.append(
            f"i.id {not_}IN (SELECT item_id FROM attr WHERE key=? AND vtext = ? COLLATE NOCASE "
            f"UNION SELECT item_id FROM attr_multi WHERE key=? AND vtext = ? COLLATE NOCASE)")
        params.extend([field, value, field, value])
    explain.append(f"{field} is {value}")
