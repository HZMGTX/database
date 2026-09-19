"""Reading VYREX's live database.

VYREX is a Discord bot with its own SQLite store -- 50 tables covering
users, economy, moderation, tickets and analytics. This makes that data
searchable alongside everything else in The database **without touching it**.

Three things make that safe:

* the connection is ``mode=ro`` plus ``query_only``, enforced by SQLite;
* WAL means a reader never blocks the bot's writer, so this can run while
  the bot is live;
* nothing here issues a write against the source, and there is a test that
  asserts a write attempt through this connection raises.

Tables are discovered from ``sqlite_master`` rather than hardcoded. A bot
that gains a table next month is picked up without a The database release, and a
schema that has drifted from ``applySchema.js`` does not produce confident
nonsense.

Ingest is incremental: the highest rowid seen per table is remembered, so
re-running after the bot has been busy only reads what is new.
"""

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from db import dates, model
from db.db import Database, connect_readonly
from db.importers.batch import Batch, BatchResult

__all__ = ["connect", "describe", "synthesize_from_schema"]

# Columns worth using as an item's title, best first.
TITLE_COLUMNS = ("username", "name", "title", "tag", "label", "guild_name",
                 "reason", "content", "message", "question", "item", "id")

# Tables that are pure churn: high-volume append-only logs whose individual
# rows are not things anyone searches for by hand.
NOISY = {"command_analytics", "error_analytics", "performance_metrics",
         "assistant_audit_logs", "ws_outbox"}

WATERMARK_KEY = "connector.vyrex.watermark"


def describe(path: "str | Path") -> Dict[str, Any]:
    """What is in the source database, without importing anything."""
    conn = connect_readonly(path)
    try:
        tables = []
        for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            try:
                count = conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
            except sqlite3.DatabaseError:
                count = -1
            columns = [r[1] for r in conn.execute(f'PRAGMA table_info("{name}")')]
            tables.append({"name": name, "rows": count, "columns": columns,
                           "noisy": name in NOISY})
        return {"path": str(path), "tables": tables,
                "total_rows": sum(t["rows"] for t in tables if t["rows"] > 0)}
    finally:
        conn.close()


def _title_for(table: str, row: sqlite3.Row, columns: List[str]) -> str:
    for candidate in TITLE_COLUMNS:
        if candidate in columns and row[candidate] not in (None, ""):
            text = str(row[candidate]).strip().replace("\n", " ")
            if text:
                return text[:180]
    return f"{table} row"


def _searchable_text(props: Dict[str, Any]) -> List[str]:
    """Text values from a row, for the full-text index.

    Properties are projected into `attr`, which makes `reward>100` an indexed
    range scan -- but `attr` is not a full-text column, so without this a row
    is findable by field and invisible to a plain word search. Someone
    looking for a username they half-remember is doing a word search.
    """
    out: List[str] = []
    for key, value in props.items():
        if isinstance(value, str) and 1 < len(value) <= 200:
            out.append(value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            out.append(str(value))
    return out[:60]


def _props_for(row: sqlite3.Row, columns: List[str]) -> Dict[str, Any]:
    props: Dict[str, Any] = {}
    for column in columns:
        value = row[column]
        if value is None or value == "":
            continue
        if isinstance(value, bytes):
            props[column] = f"<{len(value)} bytes>"
        elif isinstance(value, str) and len(value) > 2000:
            props[column] = value[:2000]
        else:
            props[column] = value
    return props


def _watermarks(db: Database) -> Dict[str, int]:
    row = db.conn().execute(
        "SELECT value FROM app_meta WHERE key=?", (WATERMARK_KEY,)).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return {}


def _save_watermarks(db: Database, marks: Dict[str, int]) -> None:
    with db.write():
        db.conn().execute(
            "INSERT INTO app_meta(key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (WATERMARK_KEY, json.dumps(marks, sort_keys=True)))


def connect(db: Database, source: "str | Path", *, dry_run: bool = False,
            tables: Optional[List[str]] = None, include_noisy: bool = False,
            max_rows_per_table: int = 20000, incremental: bool = True,
            tags: List[str] = (), **_: Any) -> BatchResult:
    """Ingest VYREX's database into The database, read-only."""
    path = Path(source).expanduser()
    if not path.is_file():
        raise FileNotFoundError(
            f"no VYREX database at {path}. It is gitignored, so it will not be in a "
            f"checkout -- point --db at the running bot's vyrex.db.")

    reader = connect_readonly(path)
    marks = _watermarks(db) if incremental else {}
    new_marks = dict(marks)
    base_tags = list(tags) + ["vyrex"]

    try:
        with Batch(db, source=str(path), format="vyrex", dry_run=dry_run) as batch:
            wanted = tables or [
                name for (name,) in reader.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name")]

            for table in wanted:
                if table in NOISY and not include_noisy:
                    batch.record("skipped", None, table, f"{table} (high-volume log)")
                    continue
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
                    batch.record("skipped", None, table, table)
                    continue

                try:
                    columns = [r[1] for r in reader.execute(f'PRAGMA table_info("{table}")')]
                    if not columns:
                        continue
                    since = marks.get(table, 0)
                    rows = reader.execute(
                        f'SELECT rowid AS _rowid, * FROM "{table}" '
                        f"WHERE rowid > ? ORDER BY rowid LIMIT ?",
                        (since, max_rows_per_table)).fetchall()
                except sqlite3.DatabaseError as exc:
                    # A WITHOUT ROWID table has no rowid; fall back to a plain read.
                    try:
                        rows = reader.execute(
                            f'SELECT * FROM "{table}" LIMIT ?',
                            (max_rows_per_table,)).fetchall()
                        columns = [r[1] for r in reader.execute(f'PRAGMA table_info("{table}")')]
                    except sqlite3.DatabaseError:
                        batch.fail(f"{table}: {exc}")
                        continue

                if not rows:
                    continue

                kind = f"vyrex_{table}"[:40]
                if not dry_run:
                    with db.write():
                        db.conn().execute(
                            "INSERT OR IGNORE INTO kind(name, label, plural, icon, "
                            "builtin, sort_order, created_at) VALUES (?,?,?,?,0,600,?)",
                            (kind, table.replace("_", " ").title(),
                             table.replace("_", " ").title(), "database", dates.utcnow()))

                highest = marks.get(table, 0)
                for row in rows:
                    keys = row.keys()
                    highest = max(highest, int(row["_rowid"])) if "_rowid" in keys else highest
                    data_columns = [c for c in columns if c in keys]
                    title = _title_for(table, row, data_columns)
                    if dry_run:
                        batch.record("created", None, table, title)
                        continue
                    try:
                        props = _props_for(row, data_columns)
                        doc = model.create(
                            db, kind=kind, title=title, props=props,
                            tags=base_tags + [f"vyrex/{table}"],
                            extra_search=[table] + _searchable_text(props))
                        batch.record("created", doc["uid"], table, title)
                    except Exception as exc:                # noqa: BLE001
                        batch.fail(f"{table}: {exc}")
                        batch.record("skipped", None, table, title)

                new_marks[table] = highest

            if not dry_run and incremental:
                _save_watermarks(db, new_marks)
            return batch.result()
    finally:
        reader.close()


# ---------------------------------------------------------------------------
# Test fixture
# ---------------------------------------------------------------------------

_CREATE = re.compile(
    r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+`?(\w+)`?\s*\((.*?)\);", re.S | re.I)


def synthesize_from_schema(schema_js: "str | Path", target: "str | Path", *,
                           rows_per_table: int = 5) -> Dict[str, int]:
    """Build a stand-in VYREX database from ``applySchema.js``.

    The real ``vyrex.db`` is gitignored and so is never present in a
    checkout. Rather than skip the connector's tests, they run against a
    database built from the same file the bot builds its own from -- which is
    a better fixture anyway, since it can be seeded to any size.
    """
    source = Path(schema_js).read_text(errors="replace")
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()

    conn = sqlite3.connect(target)
    created: Dict[str, int] = {}
    try:
        for match in _CREATE.finditer(source):
            name, body = match.group(1), match.group(2)
            # The schema lives in JS template literals; drop anything that
            # interpolates, since it is not valid SQL on its own.
            if "${" in body:
                continue
            try:
                conn.execute(f"CREATE TABLE IF NOT EXISTS {name} ({body})")
            except sqlite3.DatabaseError:
                continue

            columns = [(r[1], (r[2] or "TEXT").upper()) for r in
                       conn.execute(f"PRAGMA table_info({name})")]
            if not columns:
                continue
            placeholders = ", ".join("?" * len(columns))
            values = []
            for index in range(rows_per_table):
                row = []
                for column, ctype in columns:
                    if "INT" in ctype:
                        row.append(index + 1)
                    elif "REAL" in ctype or "FLOA" in ctype or "DOUB" in ctype:
                        row.append(float(index) + 0.5)
                    elif column.lower() in ("id", "user_id", "userid"):
                        row.append(f"user{index}")
                    else:
                        row.append(f"{column}-{index}")
                values.append(row)
            try:
                conn.executemany(
                    f"INSERT INTO {name} VALUES ({placeholders})", values)
                created[name] = rows_per_table
            except sqlite3.DatabaseError:
                created[name] = 0
        conn.commit()
    finally:
        conn.close()
    return created
