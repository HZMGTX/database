"""Indexing a code repository.

Three things go in: the files, the commit history, and any large structured
data file that is really a database in disguise.

Files come from ``git ls-files`` where git is available, which means
``.gitignore`` is respected for free and build output never arrives. Every
file is checked for credentials before its contents are stored -- see
``vault.secretscan`` for why that matters here specifically.

A file like VYREX's ``src/data/data.js`` -- 4.8 MB on one line, about 50,000
game items across 45 categories, loaded into memory at boot -- is parsed as
*data* rather than indexed as text. Each category becomes a kind and each
entry an item, so "which fish is worth more than 5000?" becomes a query
instead of a grep.
"""

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from vault import files as files_mod
from vault import model, secretscan
from vault.db import Database
from vault.importers.batch import Batch, BatchResult
from vault.importers.directory import DEFAULT_MAX_BYTES, should_skip

__all__ = ["load", "parse_js_data", "tracked_files"]

LANGUAGE = {
    ".py": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".jsx": "javascript",
    ".go": "go", ".rs": "rust", ".rb": "ruby", ".php": "php", ".java": "java",
    ".kt": "kotlin", ".swift": "swift", ".c": "c", ".h": "c", ".cc": "cpp",
    ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp", ".lua": "lua", ".sh": "shell",
    ".bash": "shell", ".sql": "sql", ".html": "html", ".css": "css",
    ".scss": "css", ".md": "markdown", ".json": "json", ".yaml": "yaml",
    ".yml": "yaml", ".toml": "toml", ".xml": "xml",
}

# `module.exports = {...}` or `export default {...}` wrapping a JSON literal.
_JS_EXPORT = re.compile(
    r"^\s*(?:module\.exports\s*=|export\s+default|exports\s*=)\s*", re.M)


def tracked_files(root: Path) -> List[Path]:
    """Files git knows about, or a plain walk when git is unavailable.

    Using git means .gitignore is honoured without reimplementing it, so
    node_modules and build output never appear in the first place.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True, timeout=120)
        if result.returncode == 0 and result.stdout:
            return [root / name for name in
                    result.stdout.decode("utf-8", "replace").split("\0") if name]
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        pass
    return [p for p in sorted(root.rglob("*")) if p.is_file()]


def _commits(root: Path, limit: int) -> List[Dict[str, str]]:
    """Recent commits, as items in their own right.

    "When did we change the login flow?" is a question about history, and
    history is not in the working tree.
    """
    separator = "\x1e"
    fields = ["%H", "%an", "%aI", "%s", "%b"]
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "log", f"-{limit}",
             f"--pretty=format:{separator.join(fields)}\x1f", "--no-merges"],
            capture_output=True, timeout=120)
        if result.returncode != 0:
            return []
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        return []

    out = []
    for record in result.stdout.decode("utf-8", "replace").split("\x1f"):
        record = record.strip("\n")
        if not record:
            continue
        parts = record.split(separator)
        if len(parts) < 4:
            continue
        out.append({"sha": parts[0], "author": parts[1], "date": parts[2],
                    "subject": parts[3], "body": parts[4] if len(parts) > 4 else ""})
    return out


def parse_js_data(text: str) -> Optional[Dict[str, Any]]:
    """Read a JavaScript module whose whole body is a JSON object literal.

    Deliberately narrow: it strips the export prefix and a trailing
    semicolon, then hands the rest to json.loads. If what remains is not
    valid JSON -- because it has comments, trailing commas, unquoted keys or
    any real JavaScript in it -- this returns None and the file is treated as
    ordinary text. Guessing at almost-JSON is how importers corrupt data.
    """
    stripped = _JS_EXPORT.sub("", text or "", count=1).strip()
    stripped = re.sub(r";\s*$", "", stripped)
    if not stripped.startswith(("{", "[")):
        return None
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _ingest_structured(db: Database, batch: Batch, path: Path, relative: Path,
                       repo_name: str, data: Dict[str, Any], *,
                       dry_run: bool, max_rows: int) -> int:
    """Turn ``{category: [ {...}, ... ]}`` into items, one per entry."""
    taken = 0
    for category, entries in data.items():
        if not isinstance(entries, list) or not entries:
            continue
        if not isinstance(entries[0], dict):
            continue

        kind = re.sub(r"[^a-z0-9_]", "_", str(category).lower())[:40] or "record"
        if not dry_run:
            with db.write():
                db.conn().execute(
                    "INSERT OR IGNORE INTO kind(name, label, plural, icon, builtin, "
                    "sort_order, created_at) VALUES (?,?,?,?,0,500,?)",
                    (kind, str(category).title(), str(category).title() + "s",
                     "database", model.dates.utcnow()))

        for entry in entries:
            if taken >= max_rows:
                return taken
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("name") or entry.get("title")
                        or entry.get("id") or f"{category} entry")[:200]
            props = {k: v for k, v in entry.items()
                     if isinstance(v, (str, int, float, bool))
                     or (isinstance(v, list) and all(
                         isinstance(x, (str, int, float, bool)) for x in v))}
            taken += 1
            if dry_run:
                batch.record("created", None, f"{relative}:{category}", title)
                continue
            try:
                # Text values go into the full-text column as well as the
                # typed projection: `rarity:Legendary` is a field query, but
                # someone searching plain "Legendary" expects a hit too.
                searchable = [str(v) for v in props.values()
                              if isinstance(v, str) and 1 < len(v) <= 120][:40]
                doc = model.create(
                    db, kind=kind, title=title, props=props,
                    tags=[f"repo/{repo_name}", f"data/{kind}"],
                    extra_search=searchable)
                batch.record("created", doc["uid"], f"{relative}:{category}", title)
            except Exception as exc:                        # noqa: BLE001
                batch.fail(f"{relative}:{category}: {exc}")
                batch.record("skipped", None, str(relative), title)
    return taken


def load(db: Database, source: "str | Path", *, dry_run: bool = False,
         tags: List[str] = (), commits: int = 300,
         max_bytes: int = DEFAULT_MAX_BYTES, structured: bool = True,
         max_structured_rows: int = 60000, **_: Any) -> BatchResult:
    root = Path(source).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"not a directory: {root}")
    repo_name = root.name.lower()
    base_tags = list(tags) + [f"repo/{repo_name}"]

    with Batch(db, source=str(root), format="repo", dry_run=dry_run) as batch:
        # -- files ---------------------------------------------------------
        for path in tracked_files(root):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(root)
            reason = should_skip(path, max_bytes=max_bytes)
            if reason:
                batch.record("skipped", None, str(relative), path.name)
                continue

            # Judge the name before reading, so a key file is never loaded.
            verdict = secretscan.scan_path(path)
            if verdict.withhold:
                if not dry_run:
                    _metadata_only(db, batch, path, relative, base_tags, verdict.reason)
                else:
                    batch.record("withheld", None, str(relative), path.name)
                continue

            if structured and path.suffix.lower() in (".js", ".mjs", ".cjs", ".json") \
                    and path.stat().st_size > 512 * 1024:
                text = path.read_text("utf-8", errors="replace")
                data = parse_js_data(text) if path.suffix != ".json" else _try_json(text)
                if data:
                    _ingest_structured(db, batch, path, relative, repo_name, data,
                                       dry_run=dry_run, max_rows=max_structured_rows)
                    continue

            if dry_run:
                batch.record("created", None, str(relative), path.name)
                continue

            try:
                language = LANGUAGE.get(path.suffix.lower())
                top = relative.parts[0] if len(relative.parts) > 1 else ""
                file_tags = list(base_tags)
                if language:
                    file_tags.append(f"lang/{language}")
                if top:
                    file_tags.append(f"{repo_name}/{re.sub(r'[^a-z0-9_-]', '-', top.lower())}")

                result = files_mod.attach(
                    db, path, title=str(relative), tags=file_tags,
                    source_path=str(relative))
                batch.record("withheld" if result["withheld"] else "created",
                             result["item"]["uid"], str(relative), path.name)
            except Exception as exc:                        # noqa: BLE001
                batch.fail(f"{relative}: {exc}")
                batch.record("skipped", None, str(relative), path.name)

        # -- commits -------------------------------------------------------
        if commits:
            for entry in _commits(root, commits):
                title = entry["subject"][:200] or entry["sha"][:8]
                if dry_run:
                    batch.record("created", None, entry["sha"][:8], title)
                    continue
                try:
                    doc = model.create(
                        db, kind="note", title=title, body=entry["body"],
                        props={"sha": entry["sha"], "author": entry["author"],
                               "committed": entry["date"], "repo": repo_name},
                        tags=base_tags + ["commit"],
                        extra_search=[entry["sha"][:12], entry["author"]])
                    batch.record("created", doc["uid"], entry["sha"][:8], title)
                except Exception as exc:                    # noqa: BLE001
                    batch.fail(f"commit {entry['sha'][:8]}: {exc}")

        return batch.result()


def _try_json(text: str) -> Optional[Dict[str, Any]]:
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _metadata_only(db: Database, batch: Batch, path: Path, relative: Path,
                   tags: List[str], reason: str) -> None:
    """Record a credential file's existence without storing its bytes.

    Deliberately not an attachment: ``vault add file`` stores the bytes
    because you asked it to keep that file, but sweeping a repository is not
    the same request. Here the file is noted so you can see it was found and
    why it was skipped, and nothing of its content is kept anywhere.
    """
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    try:
        doc = model.create(
            db, kind="note", title=str(relative),
            body=f"Not indexed: {reason}.\n\n"
                 f"This file is recorded so you can see it exists and why it was "
                 f"skipped. Its contents were never read into the database.",
            props={"path": str(relative), "size_bytes": size,
                   "withheld_reason": reason, "indexed": False},
            tags=list(tags) + ["withheld"],
            extra_search=[path.name, path.suffix.lstrip(".")])
        batch.record("withheld", doc["uid"], str(relative), path.name)
    except Exception as exc:                                # noqa: BLE001
        batch.fail(f"{relative}: {exc}")
        batch.record("skipped", None, str(relative), path.name)
