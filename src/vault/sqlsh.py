"""A read-only SQL escape hatch.

The promise this database makes is that your data is never trapped. Part of
keeping that promise is letting you ask it anything SQL can express, without
having to trust that the query language covers your case.

Three independent limits, because one is a single point of failure:

1. The connection is opened ``mode=ro`` with ``query_only`` -- SQLite itself
   refuses writes, whatever the statement says.
2. An authorizer rejects everything but reads. This is what stops ``PRAGMA``,
   ``ATTACH`` and function calls that touch the filesystem, none of which
   ``mode=ro`` blocks on its own.
3. A progress handler aborts a query that runs too long, so a careless
   cartesian join cannot wedge the process.
"""

import csv
import io
import json
import sqlite3
import time
from typing import Any, Dict, List, Optional

from vault.db import Database, connect_readonly

__all__ = ["SqlError", "run"]

# Actions an authorizer may see. Only reads are permitted; everything else,
# including anything added to SQLite in future versions, is denied by default.
ALLOWED = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
}

DENIED_FUNCTIONS = {
    "readfile", "writefile", "edit", "load_extension", "fts5_decode", "sqlite_compileoption_get",
}


class SqlError(RuntimeError):
    pass


def _authorizer(action: int, arg1: Optional[str], arg2: Optional[str],
                database: Optional[str], trigger: Optional[str]) -> int:
    if action == sqlite3.SQLITE_FUNCTION:
        if (arg2 or "").lower() in DENIED_FUNCTIONS:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    if action in ALLOWED:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def run(db: Database, sql: str, *, limit: int = 500,
        timeout_ms: int = 5000) -> Dict[str, Any]:
    """Execute one read-only statement and return its rows."""
    statement = (sql or "").strip().rstrip(";")
    if not statement:
        raise SqlError("no query given")
    if ";" in statement:
        raise SqlError("one statement at a time, please")

    conn = connect_readonly(db.path)
    started = time.monotonic()
    deadline = started + (timeout_ms / 1000.0)

    def _watchdog() -> int:
        # Returning non-zero aborts the running statement.
        return 1 if time.monotonic() > deadline else 0

    try:
        conn.set_progress_handler(_watchdog, 10_000)
        conn.set_authorizer(_authorizer)
        try:
            cursor = conn.execute(statement)
        except sqlite3.DatabaseError as exc:
            message = str(exc)
            if "not authorized" in message:
                raise SqlError(
                    "that statement is not allowed here -- this sandbox is read-only. "
                    "Use the CLI or the API to change anything.") from exc
            if "interrupted" in message:
                raise SqlError(
                    f"the query ran longer than {timeout_ms} ms and was stopped. "
                    f"Add a WHERE clause or a LIMIT.") from exc
            raise SqlError(message) from exc

        columns = [d[0] for d in (cursor.description or [])]
        rows = []
        for row in cursor:
            rows.append(list(row))
            if len(rows) >= limit:
                break
        truncated = cursor.fetchone() is not None
    finally:
        conn.set_authorizer(None)
        conn.set_progress_handler(None, 0)
        conn.close()

    return {"columns": columns, "rows": rows, "truncated": truncated,
            "took_ms": int((time.monotonic() - started) * 1000)}


def to_csv(result: Dict[str, Any]) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(result["columns"])
    writer.writerows(result["rows"])
    return out.getvalue()


def to_table(result: Dict[str, Any], *, max_width: int = 40) -> str:
    """A plain aligned table, which is what a terminal wants."""
    columns = result["columns"]
    rows = result["rows"]
    if not columns:
        return "(no columns)"

    def cell(value):
        text = "" if value is None else str(value)
        text = text.replace("\n", " ")
        return text[: max_width - 1] + "…" if len(text) > max_width else text

    widths = [len(c) for c in columns]
    rendered = []
    for row in rows:
        cells = [cell(v) for v in row]
        widths = [max(w, len(c)) for w, c in zip(widths, cells)]
        rendered.append(cells)

    lines = ["  ".join(c.ljust(w) for c, w in zip(columns, widths)),
             "  ".join("-" * w for w in widths)]
    lines.extend("  ".join(c.ljust(w) for c, w in zip(cells, widths)) for cells in rendered)
    return "\n".join(lines)
