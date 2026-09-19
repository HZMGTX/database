"""Forward-only migrations with a checksummed ledger.

Rules this enforces, in order of how much damage they prevent:

1. **An applied migration may never change.**  Every file's SHA-256 is
   recorded when it runs.  If a file that already ran no longer matches, the
   database refuses to open.  Editing a migration that has run on real data
   produces two databases with the same version number and different shapes,
   which is the kind of bug that is found months later.

2. **A database from the future is never opened.**  If ``user_version``
   exceeds what this code knows, a newer Vault has written columns this one
   would silently drop on the next write.

3. **A backup is taken before any migration touches data.**  ``VACUUM INTO``
   produces a clean, self-contained copy, and it is verified before the
   migration proceeds.

4. **Rebuilding a table always reindexes its FTS tables.**  Dropping ``item``
   drops its triggers, so a 12-step rebuild copies rows with the FTS sync
   absent and leaves the search index describing the old data.  The rebuild
   helper ends with a driven reindex and a real integrity check, in the same
   transaction, so a migration that would corrupt search fails instead.
"""

import contextlib
import hashlib
import sqlite3
import time
from pathlib import Path
from typing import List, NamedTuple, Optional

from vault import SCHEMA_VERSION
from vault.db import Database

__all__ = [
    "MigrationError",
    "Migration",
    "applied_versions",
    "current_version",
    "discover",
    "migrate",
    "rebuild_table",
]

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# Bootstrap: the ledger has to exist before it can record anything, so it is
# created here rather than in a migration file.
_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migration (
  version     INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,
  checksum    TEXT NOT NULL,
  applied_at  TEXT NOT NULL,
  duration_ms INTEGER NOT NULL
) STRICT;
"""


class MigrationError(RuntimeError):
    """A migration could not be applied, or the database is not one we can open."""


class Migration(NamedTuple):
    version: int
    name: str
    path: Path
    sql: str
    checksum: str


def _utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _checksum(text: str) -> str:
    # Normalise line endings so a checkout with CRLF does not look like
    # tampering.
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def discover(directory: "Path | None" = None) -> List[Migration]:
    """Every migration on disk, ordered by version.

    Files are named ``NNNN_name.sql``.  Duplicated version numbers are an
    error rather than a coin toss about which one runs.
    """
    directory = directory or MIGRATIONS_DIR
    found: List[Migration] = []
    seen: dict = {}

    for path in sorted(directory.glob("*.sql")):
        stem = path.stem
        head, _, name = stem.partition("_")
        if not head.isdigit():
            raise MigrationError(f"migration filename must start with a number: {path.name}")
        version = int(head)
        if version in seen:
            raise MigrationError(
                f"two migrations share version {version}: {seen[version]} and {path.name}"
            )
        seen[version] = path.name
        sql = path.read_text(encoding="utf-8")
        found.append(Migration(version, name or stem, path, sql, _checksum(sql)))

    return sorted(found, key=lambda m: m.version)


def current_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def applied_versions(conn: sqlite3.Connection) -> dict:
    """``{version: (name, checksum)}`` for everything already applied."""
    try:
        rows = conn.execute(
            "SELECT version, name, checksum FROM schema_migration ORDER BY version"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {int(r[0]): (r[1], r[2]) for r in rows}


def _database_has_content(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchone()
    return int(row[0]) > 0


def _backup_before_migrating(db: Database, label: str) -> Optional[Path]:
    """Snapshot the database with ``VACUUM INTO`` and verify the copy.

    VACUUM INTO is safe on a live database and produces a single clean file,
    which is exactly what someone wants when a migration goes wrong.
    """
    layout = db.layout
    layout.backups.mkdir(parents=True, exist_ok=True)
    target = layout.backups / f"pre-{label}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.db"
    if target.exists():  # pragma: no cover - same-second reruns
        target.unlink()

    db.conn().execute("VACUUM INTO ?", (str(target),))

    # A backup nobody checked is a backup nobody has.
    check = sqlite3.connect(target)
    try:
        result = check.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise MigrationError(f"pre-migration backup failed its integrity check: {result}")
    finally:
        check.close()
    return target


def migrate(db: Database, *, directory: "Path | None" = None, verbose: bool = False) -> List[int]:
    """Bring the database up to date.  Returns the versions applied."""
    conn = db.conn()
    migrations = discover(directory)
    if not migrations:
        raise MigrationError(f"no migrations found in {directory or MIGRATIONS_DIR}")

    highest = migrations[-1].version
    if highest > SCHEMA_VERSION:
        raise MigrationError(
            f"migration {highest} exists but this code only knows schema version "
            f"{SCHEMA_VERSION}; upgrade Vault"
        )

    version_now = current_version(conn)
    if version_now > SCHEMA_VERSION:
        raise MigrationError(
            f"this database is at schema version {version_now}, but this copy of Vault "
            f"only understands {SCHEMA_VERSION}.\n"
            f"A newer Vault has written it. Upgrade rather than risk dropping data."
        )

    conn.executescript(_LEDGER_DDL)
    already = applied_versions(conn)

    # Rule 1: nothing that has already run may have changed.
    for m in migrations:
        if m.version in already:
            name, checksum = already[m.version]
            if checksum != m.checksum:
                raise MigrationError(
                    f"migration {m.version:04d}_{name} has changed since it was applied.\n"
                    f"  recorded: {checksum[:16]}...\n"
                    f"  on disk : {m.checksum[:16]}...\n"
                    f"Applied migrations are immutable. Add a new migration instead."
                )

    pending = [m for m in migrations if m.version not in already]
    if not pending:
        return []

    # Only back up when there is something to lose: a fresh database has no
    # content, and a backup of nothing is noise in the backups directory.
    if _database_has_content(conn) and already:
        backup = _backup_before_migrating(db, f"migration-{pending[0].version:04d}")
        if verbose and backup:
            print(f"  backup: {backup}")

    applied: List[int] = []
    for m in pending:
        started = time.monotonic()
        try:
            _apply_one(conn, m, started)
        except Exception as exc:
            # The transaction lives inside the script text, so a mid-script
            # failure leaves it open; roll it back here.  Verified atomic:
            # tables created earlier in the script do not survive.
            if conn.in_transaction:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
            raise MigrationError(f"migration {m.version:04d}_{m.name} failed: {exc}") from exc

        applied.append(m.version)
        if verbose:
            print(f"  applied {m.version:04d}_{m.name}")

    return applied


def _sql_str(value: str) -> str:
    """A SQL string literal.  Values here are all internally generated -- a
    version number, a filename stem, a hex digest, a timestamp -- but this is
    the one place migration text is assembled, so it escapes anyway."""
    return "'" + str(value).replace("'", "''") + "'"


def _apply_one(conn: sqlite3.Connection, m: Migration, started: float) -> None:
    """Apply one migration atomically.

    The transaction has to be written *into* the script rather than opened
    around it: Python's ``executescript`` issues an implicit COMMIT before it
    runs, which silently ends any transaction the caller opened.  Verified --
    a BEGIN IMMEDIATE taken beforehand is gone by the time the script starts,
    and the matching COMMIT then fails with "cannot commit - no transaction
    is active".

    Writing BEGIN/COMMIT inside the script keeps the DDL, the ledger row and
    user_version in one unit.  All three roll back together; user_version is
    transactional, so a failed migration does not leave the version bumped.
    """
    duration_ms = int((time.monotonic() - started) * 1000)
    ledger = (
        "INSERT INTO schema_migration(version, name, checksum, applied_at, duration_ms) "
        f"VALUES ({m.version:d}, {_sql_str(m.name)}, {_sql_str(m.checksum)}, "
        f"{_sql_str(_utcnow())}, {duration_ms:d});"
    )
    script = (
        "BEGIN IMMEDIATE;\n"
        f"{m.sql}\n"
        f"{ledger}\n"
        f"PRAGMA user_version = {m.version:d};\n"
        "COMMIT;"
    )
    conn.executescript(script)


def rebuild_table(
    conn: sqlite3.Connection,
    *,
    name: str,
    new_ddl: str,
    copy_sql: str,
    recreate: "List[str]",
    fts_tables: "List[str]",
) -> None:
    """The 12-step table rebuild, with the FTS step that is easy to forget.

    SQLite cannot drop or retype a column in place, so a real schema change
    means: build the new table, copy, drop the old, rename, then put back
    everything that was attached to the old one.

    Dropping the table also drops its triggers, so the copy runs with the FTS
    sync absent -- and any copy that transforms an indexed column leaves the
    search index describing data that no longer exists.  This always ends by
    rebuilding the index from the table and verifying it, inside the caller's
    transaction, so a migration that would corrupt search fails loudly.

    The caller owns the transaction, and must have turned foreign keys off
    before opening it (SQLite ignores the pragma inside a transaction).
    """
    if conn.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise MigrationError(
            "rebuild_table needs foreign_keys OFF, set before the transaction opened"
        )

    conn.execute(new_ddl)
    conn.execute(copy_sql)
    conn.execute(f"DROP TABLE {name}")
    conn.execute(f"ALTER TABLE {name}_new RENAME TO {name}")

    for statement in recreate:
        conn.execute(statement)

    for fts in fts_tables:
        cols = [
            r[1]
            for r in conn.execute(f"PRAGMA table_info({fts})")
            if r[1] not in (fts, "rank")
        ]
        column_list = ", ".join(cols)
        conn.execute(f"DELETE FROM {fts}")
        conn.execute(
            f"INSERT INTO {fts}(rowid, {column_list}) SELECT id, {column_list} FROM {name}"
        )
        # rank=1 is the content-aware form.  The cheap
        # `VALUES('integrity-check')` does NOT detect a drifted external
        # content table -- verified: it passes happily after the content
        # table changed underneath it.
        conn.execute(f"INSERT INTO {fts}({fts}, rank) VALUES('integrity-check', 1)")

    for check in ("foreign_key_check",):
        problems = conn.execute(f"PRAGMA {check}").fetchall()
        if problems:
            raise MigrationError(f"{check} failed after rebuilding {name}: {problems[:5]}")
