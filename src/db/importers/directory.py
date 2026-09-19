"""A folder of files.

Walks a directory, stores every file as an attachment and indexes whatever
text can be read from it. Skips the things nobody wants in a personal
database -- version control internals, dependency directories, build output,
lockfiles and anything over a size limit -- and runs the credential check on
everything it does take.
"""

from pathlib import Path
from typing import Any, List, Optional, Set

from db import files as files_mod
from db.db import Database
from db.importers.batch import Batch, BatchResult

__all__ = ["SKIP_DIRS", "load", "should_skip"]

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", ".next",
    ".nuxt", "target", "vendor", ".gradle", ".idea", ".vscode", "coverage",
    ".terraform", ".cache", "site-packages", ".tox", "bower_components",
}

SKIP_SUFFIXES = {
    ".pyc", ".pyo", ".class", ".o", ".so", ".dylib", ".dll", ".exe", ".bin",
    ".map", ".lock", ".min.js", ".min.css", ".woff", ".woff2", ".ttf", ".eot",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar", ".iso", ".dmg",
    ".mp4", ".mkv", ".mov", ".avi", ".mp3", ".wav", ".flac",
}

SKIP_NAMES = {
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "poetry.lock",
    "Cargo.lock", "composer.lock", "Gemfile.lock", ".DS_Store", "Thumbs.db",
}

DEFAULT_MAX_BYTES = 16 * 1024 * 1024


def should_skip(path: Path, *, max_bytes: int = DEFAULT_MAX_BYTES) -> Optional[str]:
    """Why this file should not be imported, or None to take it."""
    if path.name in SKIP_NAMES:
        return "lockfile or OS metadata"
    if path.suffix.lower() in SKIP_SUFFIXES:
        return f"{path.suffix} is not worth indexing"
    if any(part in SKIP_DIRS for part in path.parts):
        return "inside a skipped directory"
    if path.name.endswith((".min.js", ".min.css")):
        return "minified"
    try:
        if path.stat().st_size > max_bytes:
            return f"{path.stat().st_size / 1e6:.0f} MB is over the size limit"
    except OSError as exc:
        return str(exc)
    return None


def load(db: Database, source: "str | Path", *, dry_run: bool = False,
         tags: List[str] = (), max_bytes: int = DEFAULT_MAX_BYTES,
         extra_skip: Set[str] = frozenset(), **_: Any) -> BatchResult:
    root = Path(source).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"not a directory: {root}")

    with Batch(db, source=str(root), format="directory", dry_run=dry_run) as batch:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            reason = should_skip(path, max_bytes=max_bytes)
            if reason or path.name in extra_skip:
                batch.record("skipped", None, str(path.relative_to(root)), path.name)
                continue

            if dry_run:
                batch.record("created", None, str(path.relative_to(root)), path.name)
                continue

            try:
                relative = path.relative_to(root)
                result = files_mod.attach(
                    db, path, title=path.name,
                    tags=list(tags) + [f"folder/{relative.parent.as_posix()}"]
                    if relative.parent != Path(".") else list(tags),
                    source_path=str(relative))
                batch.record("withheld" if result["withheld"] else "created",
                             result["item"]["uid"], str(relative), path.name)
            except Exception as exc:                        # noqa: BLE001
                batch.fail(f"{path.name}: {exc}")
                batch.record("skipped", None, str(path), path.name)

        return batch.result()
