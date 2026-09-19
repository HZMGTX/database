"""The one place The database opens a SQLite connection.

Everything else in the package asks this module for a connection.  ``tests/
test_lint.py`` enforces that mechanically: a stray ``sqlite3.connect`` anywhere
else fails the build.  Centralising it is what makes the pragma set, the
transaction shape and the audit context impossible to get wrong in one caller
and right in another.

Transactions
------------
Writes go through :meth:`Database.write`, which opens ``BEGIN IMMEDIATE``.
Taking the write lock up front means a writer either starts or waits -- it can
never do half its reads, discover a conflict at COMMIT and have to unwind.

Every write transaction carries a ``txn_id``.  ``model.py`` stamps it onto the
``change_log`` rows it writes, which is what lets ``db undo`` reverse one
logical operation -- a 200-item bulk retag included -- rather than one row.

Why the audit trail is written in Python and not by triggers
------------------------------------------------------------
The obvious design is a trigger that reads the current ``txn_id`` from a temp
table.  SQLite refuses to create such a trigger at all::

    sqlite3.OperationalError: trigger cl cannot reference objects in database temp

and the usual escape hatch -- a per-connection user-defined function -- is
foreclosed by ``PRAGMA trusted_schema=OFF``, which we want for its own sake.
So ``change_log`` rows are written from the single write path inside the same
transaction as the mutation.  Same atomicity, no trigger gymnastics.
"""

import contextlib
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterator, Optional

from db import ids
from db.paths import Layout

__all__ = [
    "Database",
    "TransactionContext",
    "connect_readonly",
    "sqlite_capabilities",
]

# ``synchronous=FULL`` costs an fsync per commit and is the reason a power cut
# cannot cost you the last few writes.  Bulk import lowers it deliberately --
# and always *before* BEGIN, because SQLite raises
# "Safety level may not be changed inside a transaction" otherwise.
DEFAULT_SYNCHRONOUS = "FULL"
DEFAULT_BUSY_TIMEOUT_MS = 5000
DEFAULT_CACHE_KB = 16_000
DEFAULT_MMAP_BYTES = 64 * 1024 * 1024


class TransactionContext:
    """Identity of the write transaction currently on this thread."""

    __slots__ = ("txn_id", "actor", "started_at")

    def __init__(self, txn_id: str, actor: str) -> None:
        self.txn_id = txn_id
        self.actor = actor
        self.started_at = time.time()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"TransactionContext(txn_id={self.txn_id!r}, actor={self.actor!r})"


def sqlite_capabilities() -> dict:
    """What this interpreter's SQLite can do.

    ``db init`` calls this and refuses to create a database when something
    load-bearing is missing, so the failure is one clear sentence at setup
    rather than a mystery the first time somebody searches.
    """
    caps = {"version": sqlite3.sqlite_version, "fts5": False, "trigram": False, "json": False}
    probe = sqlite3.connect(":memory:")
    try:
        try:
            probe.execute("CREATE VIRTUAL TABLE _p USING fts5(x)")
            caps["fts5"] = True
        except sqlite3.OperationalError:
            pass
        try:
            probe.execute("CREATE VIRTUAL TABLE _t USING fts5(x, tokenize='trigram')")
            caps["trigram"] = True
        except sqlite3.OperationalError:
            pass
        try:
            probe.execute("SELECT json_valid('{}')")
            caps["json"] = True
        except sqlite3.OperationalError:
            pass
    finally:
        probe.close()
    return caps


def _row_factory(cursor: sqlite3.Cursor, row: tuple) -> sqlite3.Row:
    return row


class Database:
    """A handle on one The database database file.

    Connections are per-thread: the HTTP server runs a thread per request and
    SQLite connections are not safe to share across threads.  Threads are
    cheap here; the write lock below is what actually serialises writers.
    """

    def __init__(
        self,
        layout: Layout,
        *,
        synchronous: str = DEFAULT_SYNCHRONOUS,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        cache_kb: int = DEFAULT_CACHE_KB,
        mmap_bytes: int = DEFAULT_MMAP_BYTES,
        actor: str = "cli",
    ) -> None:
        self.layout = layout
        self.synchronous = synchronous.upper()
        self.busy_timeout_ms = int(busy_timeout_ms)
        self.cache_kb = int(cache_kb)
        self.mmap_bytes = int(mmap_bytes)
        self.actor = actor

        # Serialises this process's own writers.  Cross-process serialisation
        # is SQLite's job, via BEGIN IMMEDIATE plus busy_timeout.  Both are
        # needed: the lock avoids burning the timeout on our own threads.
        self._write_lock = threading.RLock()
        self._local = threading.local()
        self._all_conns: "list[sqlite3.Connection]" = []
        self._conns_lock = threading.Lock()

    # -- connections ---------------------------------------------------------

    @property
    def path(self) -> Path:
        return self.layout.db

    def conn(self) -> sqlite3.Connection:
        """This thread's connection, opened on first use."""
        existing = getattr(self._local, "conn", None)
        if existing is not None:
            return existing

        conn = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000.0,
            # Manual transaction control.  Without this, Python's sqlite3
            # opens transactions implicitly at times of its own choosing,
            # which makes BEGIN IMMEDIATE unreliable.
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        self._apply_pragmas(conn)

        self._local.conn = conn
        with self._conns_lock:
            self._all_conns.append(conn)
        return conn

    def _apply_pragmas(self, conn: sqlite3.Connection) -> None:
        # Order matters.  busy_timeout first so that every statement after it
        # waits rather than failing instantly if another process holds a lock.
        conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")

        # Must be set outside a transaction, and must be on: half this schema's
        # integrity guarantees are foreign keys.
        conn.execute("PRAGMA foreign_keys = ON")

        # WAL lets readers run while a writer is active, which is what makes a
        # CLI query against a live server work.  The setting is persistent, so
        # this is a no-op after the first time.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(f"PRAGMA synchronous = {self.synchronous}")

        # Off, deliberately, and verified: a trigger on item_tag that updates
        # item still fires item's own triggers with this off (cross-table
        # cascade does not need it), while item->item re-entrancy stays
        # impossible.  That is exactly the pairing the FTS sync wants.
        conn.execute("PRAGMA recursive_triggers = OFF")

        # The schema is not trusted to invoke application-defined functions.
        # Verified not to interfere with FTS5, its tokenizers, snippet() or
        # integrity-check.
        conn.execute("PRAGMA trusted_schema = OFF")

        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute(f"PRAGMA cache_size = -{self.cache_kb}")
        if self.mmap_bytes > 0:
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute(f"PRAGMA mmap_size = {self.mmap_bytes}")

    def set_synchronous(self, level: str) -> None:
        """Change durability for subsequent transactions.

        Only legal outside a transaction -- SQLite raises "Safety level may
        not be changed inside a transaction" otherwise.  Bulk import uses this
        before BEGIN and restores it afterwards.
        """
        level = level.upper()
        if level not in ("OFF", "NORMAL", "FULL", "EXTRA"):
            raise ValueError(f"invalid synchronous level: {level!r}")
        if self.in_transaction:
            raise RuntimeError("cannot change synchronous inside a transaction")
        self.synchronous = level
        with self._conns_lock:
            for conn in self._all_conns:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute(f"PRAGMA synchronous = {level}")

    # -- transaction context -------------------------------------------------

    @property
    def txn(self) -> Optional[TransactionContext]:
        """The write transaction on this thread, if one is open."""
        return getattr(self._local, "txn", None)

    @property
    def in_transaction(self) -> bool:
        return self.txn is not None

    def require_txn(self) -> TransactionContext:
        """The current write transaction, or raise.

        ``model.py`` calls this before writing, so that a mutation outside a
        transaction is a loud programming error rather than a silent
        autocommit that cannot be undone as a unit.
        """
        txn = self.txn
        if txn is None:
            raise RuntimeError(
                "no write transaction open -- wrap this in `with db.write():`"
            )
        return txn

    # -- transactions --------------------------------------------------------

    @contextlib.contextmanager
    def write(self, actor: "str | None" = None) -> Iterator[sqlite3.Connection]:
        """Open a write transaction: ``BEGIN IMMEDIATE`` .. ``COMMIT``.

        Re-entrant.  A nested ``with db.write()`` joins the transaction
        already open on this thread instead of starting a second one, so a
        high-level operation can call lower-level ones without either having
        to know whether it is the outermost.
        """
        if self.in_transaction:
            # Already inside one on this thread -- join it.  The outermost
            # block owns COMMIT/ROLLBACK.
            yield self.conn()
            return

        self._write_lock.acquire()
        conn = self.conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._local.txn = TransactionContext(
                txn_id=ids.uuid7(), actor=actor or self.actor
            )
            try:
                yield conn
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
        finally:
            self._local.txn = None
            self._write_lock.release()

    @contextlib.contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """A read-only view.

        In WAL mode this sees a consistent snapshot without blocking, and
        without blocking a concurrent writer.
        """
        conn = self.conn()
        if self.in_transaction:
            # Reading inside our own write transaction: already consistent.
            yield conn
            return
        conn.execute("BEGIN DEFERRED")
        try:
            yield conn
        finally:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")

    # -- lifecycle -----------------------------------------------------------

    def executescript(self, script: str) -> None:
        """Run a multi-statement script.

        Not wrapped in a transaction here: ``migrate.py`` owns the transaction
        boundary so that a migration's DDL and its data step commit together.
        """
        self.conn().executescript(script)

    def close(self) -> None:
        with self._conns_lock:
            conns, self._all_conns = self._all_conns, []
        for conn in conns:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
        self._local = threading.local()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Database({str(self.path)!r})"


def connect_readonly(path: "str | Path", *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    """Open *path* strictly read-only.

    Used for two things that must never be able to write: the ``db sql``
    sandbox, and the VYREX connector reading a live bot database.  ``mode=ro``
    is enforced by SQLite itself, so it holds even if calling code is wrong.

    In WAL mode a reader does not block the writer, so pointing this at a
    running application's database is safe.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"no such database: {path}")
    uri = f"file:{path.as_uri()[7:]}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=busy_timeout_ms / 1000.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
    conn.execute("PRAGMA query_only = ON")
    return conn
