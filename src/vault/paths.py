"""Where Vault keeps things.

The database lives beside the code, in ``<repo>/data/vault.db``, so that
"back it up" really is "copy that one file".  ``data/`` is gitignored, so the
file is never committed -- it holds personal data and the repository is not
the right place for it.

Resolution order, highest priority first:

1. an explicit ``--db PATH`` passed on the command line,
2. the ``VAULT_DB`` environment variable,
3. ``<repo>/data/vault.db``.
"""

import os
from pathlib import Path

__all__ = [
    "DB_ENV_VAR",
    "Layout",
    "default_db_path",
    "package_root",
    "repo_root",
    "resolve",
]

DB_ENV_VAR = "VAULT_DB"

# Directories that are notorious for breaking SQLite's locking: a file-sync
# client can copy the .db out from under a live WAL and produce a database
# that passes integrity_check but has lost the tail of the write-ahead log.
_CLOUD_MARKERS = (
    "dropbox",
    "google drive",
    "googledrive",
    "onedrive",
    "icloud",
    "com~apple~clouddocs",
    "nextcloud",
    "owncloud",
    "sync.com",
    "pcloud",
    "mega",
    "yandexdisk",
)


def package_root() -> Path:
    """The directory holding the ``vault`` package itself."""
    return Path(__file__).resolve().parent


def repo_root() -> Path:
    """The checkout that contains the ``vault`` package.

    The package lives at ``<repo>/src/vault`` so that the launcher script at
    ``<repo>/vault`` and the package directory can share the name ``vault``
    without colliding on the filesystem.
    """
    return package_root().parent.parent


def default_db_path() -> Path:
    """``<repo>/data/vault.db`` -- the database unless told otherwise."""
    return repo_root() / "data" / "vault.db"


def looks_cloud_synced(path: Path) -> bool:
    """True if *path* appears to sit inside a file-sync folder.

    Not authoritative -- it is a name check, and a user can sync any directory
    by other means.  It exists to print a warning, never to refuse to run.
    """
    haystack = str(path).lower()
    return any(marker in haystack for marker in _CLOUD_MARKERS)


class Layout:
    """Every path Vault uses, derived from the database file's location.

    Keeping these together means there is exactly one answer to "where do
    attachments go?", and that backup/restore, ``doctor`` and ``gc`` cannot
    drift apart about it.
    """

    __slots__ = ("db",)

    def __init__(self, db: Path) -> None:
        self.db = db

    # -- derived locations ---------------------------------------------------

    @property
    def root(self) -> Path:
        """The directory holding the database and everything beside it."""
        return self.db.parent

    @property
    def files(self) -> Path:
        """Content-addressed attachment store: ``files/ab/cd/<sha256><ext>``."""
        return self.root / "files"

    @property
    def backups(self) -> Path:
        return self.root / "backups"

    @property
    def inbox(self) -> Path:
        """Watched drop folder: anything landing here becomes an item."""
        return self.root / "inbox"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def tmp(self) -> Path:
        """Staging for blob ingest.

        Deliberately a sibling of ``files/`` rather than the system temp
        directory, so that the hash-then-rename ingest is a rename within one
        filesystem and therefore atomic.
        """
        return self.root / "tmp"

    # -- operations ----------------------------------------------------------

    def blob_path(self, sha256: str, suffix: str = "") -> Path:
        """Where the bytes for *sha256* live.

        Two levels of fan-out keep any single directory well under the size at
        which listing it gets slow, even with a few hundred thousand blobs.
        """
        if len(sha256) < 4:
            raise ValueError(f"not a sha256 digest: {sha256!r}")
        return self.files / sha256[:2] / sha256[2:4] / f"{sha256}{suffix}"

    def ensure(self) -> "Layout":
        """Create every directory Vault needs.  Idempotent."""
        for directory in (self.root, self.files, self.backups, self.inbox, self.logs, self.tmp):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Layout(db={str(self.db)!r})"


def resolve(explicit: "str | os.PathLike[str] | None" = None) -> Layout:
    """Work out which database to use and return its :class:`Layout`.

    Does not touch the filesystem -- call :meth:`Layout.ensure` for that.
    """
    if explicit is not None:
        db = Path(explicit)
    else:
        from_env = os.environ.get(DB_ENV_VAR, "").strip()
        db = Path(from_env) if from_env else default_db_path()

    # expanduser() first so that VAULT_DB=~/notes.db behaves as typed.
    return Layout(db.expanduser().resolve())
