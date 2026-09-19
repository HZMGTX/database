"""The command line.

Design notes that matter more than they look:

* Every command takes ``--json``.  A tool that can only talk to humans
  cannot be scripted, and a personal database that cannot be scripted stops
  being useful the first time you want to do something its author did not
  think of.
* Exit codes are meaningful: 0 fine, 1 not found, 2 usage, 3 conflict,
  4 integrity, 5 refused.  ``&&`` in a shell should work.
* Colour is switched off when output is not a terminal, and when NO_COLOR is
  set.  Escape codes in a pipe are a bug.
* Anything destructive needs ``--yes`` when stdin is not a terminal, so a
  script cannot silently empty the trash.
"""

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

from vault import (SCHEMA_VERSION, __version__, dates, files, history, ids,
                   model, search)
from vault.db import Database, sqlite_capabilities
from vault.migrate import MigrationError, migrate
from vault.paths import DB_ENV_VAR, Layout, resolve

EXIT_OK = 0
EXIT_NOT_FOUND = 1
EXIT_USAGE = 2
EXIT_CONFLICT = 3
EXIT_INTEGRITY = 4
EXIT_REFUSED = 5

# FTS5's snippet() marks matches with these, chosen because they cannot occur
# in text: STX and ETX. The terminal layer turns them into colour, and --json
# strips them, so the same query works in both.
MARK_START, MARK_END = chr(2), chr(3)


class Style:
    """ANSI styling, or nothing at all when the output is not a terminal."""

    def __init__(self, stream=None) -> None:
        stream = stream or sys.stdout
        self.enabled = (
            hasattr(stream, "isatty") and stream.isatty()
            and not os.environ.get("NO_COLOR")
            and os.environ.get("TERM") != "dumb"
        )

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def dim(self, t): return self._wrap("2", t)
    def bold(self, t): return self._wrap("1", t)
    def cyan(self, t): return self._wrap("36", t)
    def green(self, t): return self._wrap("32", t)
    def yellow(self, t): return self._wrap("33", t)
    def red(self, t): return self._wrap("31", t)
    def magenta(self, t): return self._wrap("35", t)

    def highlight(self, text: str) -> str:
        """Render FTS5's snippet markers."""
        if not self.enabled:
            return text.replace(MARK_START, "").replace(MARK_END, "")
        return text.replace(MARK_START, "\033[1;33m").replace(MARK_END, "\033[0m")


STYLE = Style()

KIND_ICON = {"note": "●", "task": "□", "event": "◆", "link": "→",
             "file": "■", "person": "☺"}


def _out(text: str = "") -> None:
    print(text)


def _err(text: str) -> None:
    print(text, file=sys.stderr)


def _emit_json(payload: Any) -> None:
    def clean(value):
        if isinstance(value, str):
            return value.replace(MARK_START, "").replace(MARK_END, "")
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [clean(v) for v in value]
        return value

    print(json.dumps(clean(payload), indent=2, ensure_ascii=False, default=str))


def _confirm(prompt: str, assume_yes: bool) -> bool:
    """Ask before doing something irreversible.

    When stdin is not a terminal the answer is no unless --yes was passed:
    a script piping into Vault must not be able to destroy data by accident.
    """
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        _err("refusing: this is destructive and stdin is not a terminal. Pass --yes.")
        return False
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        _out()
        return False


def _open_db(args, *, migrate_if_needed: bool = True) -> Database:
    layout: Layout = resolve(getattr(args, "db", None))
    layout.ensure()
    db = Database(layout, actor=getattr(args, "actor", None) or "cli")
    if migrate_if_needed:
        migrate(db)
    return db


def _tzid(args, db: Optional[Database] = None) -> str:
    """The zone to interpret typed times in."""
    explicit = getattr(args, "tz", None)
    if explicit:
        return explicit
    if db is not None:
        row = db.conn().execute("SELECT value FROM app_meta WHERE key='tz'").fetchone()
        if row and row[0]:
            return row[0]
    env = os.environ.get("TZ")
    if env:
        return env
    try:
        import time as _time
        if _time.daylight and _time.tzname[1]:
            pass
    except Exception:
        pass
    return "UTC"


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def _render_hit(hit, *, show_kind: bool = True) -> str:
    icon = KIND_ICON.get(hit.kind, "●")
    handle = STYLE.dim(ids.short(hit.uid))
    title = STYLE.bold(hit.title or STYLE.dim("(untitled)"))
    bits = [f"{icon} {handle}  {title}"]
    if hit.pinned:
        bits.append(STYLE.yellow(" ★"))
    if hit.trashed:
        bits.append(STYLE.red(" [trashed]"))
    line = "".join(bits)
    if hit.tags:
        line += "  " + STYLE.cyan(" ".join("#" + t for t in hit.tags))
    out = [line]
    if hit.snippet:
        out.append("    " + STYLE.highlight(hit.snippet))
    return "\n".join(out)


def _facet_lines(doc: Dict[str, Any]) -> List[tuple]:
    """Present a facet the way a person reads it.

    The stored form is three columns -- local wall time, zone and epoch --
    because that is what makes sorting correct across timezones. Showing all
    three would be showing the user our bookkeeping.
    """
    facet = doc.get("facet") or {}
    if not facet:
        return []
    out: List[tuple] = []
    kind = doc.get("kind")

    if kind == "task":
        out.append(("status", facet.get("status", "todo")))
        if facet.get("priority"):
            out.append(("priority", "\u2605" * int(facet["priority"])))
        if facet.get("due_local"):
            when = facet["due_local"].replace("T", " ")
            out.append(("due", f"{when} {facet.get('due_tzid') or ''}".strip()))
        if facet.get("estimate_minutes"):
            out.append(("estimate", f"{facet['estimate_minutes']} min"))
        if facet.get("completed_at"):
            out.append(("completed", facet["completed_at"]))
    elif kind == "event":
        when = (facet.get("starts_local") or "").replace("T", " ")
        if facet.get("ends_local"):
            when += " to " + facet["ends_local"].replace("T", " ")
        if not facet.get("all_day") and facet.get("tzid"):
            when += f" {facet['tzid']}"
        out.append(("all day" if facet.get("all_day") else "when", when))
        if facet.get("location"):
            out.append(("location", facet["location"]))
        if facet.get("rrule"):
            out.append(("repeats", facet["rrule"]))
    elif kind == "link":
        out.append(("url", facet.get("url", "")))
    elif kind == "file":
        out.append(("file", facet.get("filename", "")))
        if facet.get("content_withheld"):
            out.append(("withheld", facet.get("withheld_reason") or "possible secret"))
    elif kind == "person":
        for key in ("org", "role", "birthday"):
            if facet.get(key):
                out.append((key, facet[key]))
    else:
        for key, value in facet.items():
            if value not in (None, "", 0):
                out.append((key, value))
    return out


def _render_doc(doc: Dict[str, Any]) -> str:
    lines = []
    icon = KIND_ICON.get(doc["kind"], "●")
    lines.append(f"{icon} {STYLE.bold(doc['title'] or '(untitled)')}")
    lines.append(STYLE.dim(f"  {ids.short(doc['uid'])}  {doc['kind']}  rev {doc['rev']}"))
    if doc.get("tags"):
        lines.append("  " + STYLE.cyan(" ".join("#" + t for t in doc["tags"])))
    for label, value in _facet_lines(doc):
        lines.append(f"  {STYLE.dim(label + ':'):22} {value}")
    if doc.get("props"):
        for key, value in sorted(doc["props"].items()):
            lines.append(f"  {STYLE.dim(key + ':'):22} {value}")
    if doc.get("identities"):
        for ident in doc["identities"]:
            lines.append(f"  {STYLE.dim(ident['channel'] + ':'):22} {ident['value']}")
    if doc.get("body"):
        lines.append("")
        for line in doc["body"].splitlines():
            lines.append("  " + line)
    out_links = doc.get("links", {}).get("out", [])
    in_links = doc.get("links", {}).get("in", [])
    if out_links or in_links:
        lines.append("")
        for link in out_links:
            lines.append(f"  {STYLE.green('->')} {link['rel']:14} "
                         f"{STYLE.dim(ids.short(link['uid']))} {link['title']}")
        for link in in_links:
            lines.append(f"  {STYLE.magenta('<-')} {link['rel']:14} "
                         f"{STYLE.dim(ids.short(link['uid']))} {link['title']}")
    lines.append("")
    lines.append(STYLE.dim(f"  created {doc['created_at']}   updated {doc['updated_at']}"))
    if doc.get("deleted_at"):
        lines.append(STYLE.red(f"  trashed {doc['deleted_at']}"))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_init(args) -> int:
    caps = sqlite_capabilities()
    missing = [name for name in ("fts5", "json") if not caps[name]]
    if missing:
        _err(f"This Python's SQLite is missing {', '.join(missing)}, which Vault needs.")
        _err(f"  SQLite version: {caps['version']}")
        _err("  Install a Python built against a fuller SQLite, or use the system python3.")
        return EXIT_INTEGRITY

    layout = resolve(getattr(args, "db", None))
    existed = layout.db.exists()
    layout.ensure()
    db = Database(layout)
    try:
        applied = migrate(db, verbose=not args.json)
    except MigrationError as exc:
        _err(str(exc))
        return EXIT_INTEGRITY

    if args.json:
        _emit_json({"database": str(layout.db), "existed": existed,
                    "applied": applied, "schema_version": SCHEMA_VERSION,
                    "sqlite": caps["version"], "trigram": caps["trigram"]})
    else:
        _out(f"{STYLE.green('Ready.')}  {layout.db}")
        if not caps["trigram"]:
            _out(STYLE.yellow("  note: no trigram tokenizer; fuzzy substring search is reduced."))
        _out(STYLE.dim(f"  SQLite {caps['version']}, schema v{SCHEMA_VERSION}"))
        _out(STYLE.dim(f"  Back it up by copying that file while nothing is writing to it,"))
        _out(STYLE.dim(f"  or run `vault backup` at any time."))
    db.close()
    return EXIT_OK


def _parse_props(pairs: List[str]) -> Dict[str, Any]:
    """``--prop amount=420.5`` -> ``{"amount": 420.5}``.

    Numbers and booleans are recognised so that ``amount>5000`` can compare
    them later; everything else stays text.
    """
    props: Dict[str, Any] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise ValueError(f"--prop needs key=value, got {pair!r}")
        key, _, raw = pair.partition("=")
        key = key.strip()
        raw = raw.strip()
        if raw.lower() in ("true", "false"):
            value: Any = raw.lower() == "true"
        else:
            try:
                value = int(raw)
            except ValueError:
                try:
                    value = float(raw)
                except ValueError:
                    value = raw
        if key in props:
            existing = props[key]
            props[key] = existing + [value] if isinstance(existing, list) else [existing, value]
        else:
            props[key] = value
    return props


def _read_body(args) -> str:
    body = getattr(args, "body", None)
    if body == "-":
        return sys.stdin.read()
    return body or ""


def cmd_add_file(args) -> int:
    db = _open_db(args)
    from pathlib import Path as _Path
    target = _Path(args.path).expanduser()
    if not target.exists():
        _err(f"no such file: {target}")
        db.close()
        return EXIT_NOT_FOUND
    if target.is_dir():
        _err(f"{target} is a directory. Use `vault import` for a whole folder.")
        db.close()
        return EXIT_USAGE

    result = files.attach(db, target, title=args.title or None,
                          tags=args.tag or [], scan_secrets=not args.no_scan)
    if args.json:
        _emit_json(result)
    else:
        doc = result["item"]
        size = result["blob"]["size"]
        _out(f"{STYLE.green('Stored')} {STYLE.dim(ids.short(doc['uid']))}  "
             f"{doc['title']}  {STYLE.dim(format(size, ',') + ' bytes')}")
        if result["blob"]["deduplicated"]:
            _out(STYLE.dim("  identical bytes were already stored; not duplicated"))
        if result["withheld"]:
            _out(STYLE.yellow(f"  contents NOT indexed: {result['reason']}"))
            _out(STYLE.dim("  the file is stored and findable by name; its text is not searchable"))
    db.close()
    return EXIT_OK


def cmd_extract(args) -> int:
    """Run the extraction queue over blobs whose text was never read."""
    db = _open_db(args)
    from vault import extract as extract_mod
    conn = db.conn()
    pending = conn.execute(
        "SELECT b.id, b.sha256, b.suffix FROM blob b WHERE b.extract_status='pending' "
        "LIMIT ?", (args.limit,)).fetchall()
    done = {"ok": 0, "empty": 0, "unsupported": 0, "failed": 0, "skipped": 0}
    for blob_id, sha, suffix in pending:
        try:
            path = files.open_blob(db, sha)
        except FileNotFoundError:
            done["failed"] += 1
            continue
        result = extract_mod.extract_file(path)
        done[result.status] = done.get(result.status, 0) + 1
        with db.write():
            conn.execute(
                "UPDATE blob SET extract_status=?, extract_note=?, extracted_text=? WHERE id=?",
                (result.status, result.note, result.text or None, blob_id))
    if args.json:
        _emit_json({"processed": len(pending), "results": done})
    else:
        _out(f"  extracted {len(pending)} file(s): " +
             ", ".join(f"{k} {v}" for k, v in done.items() if v))
        if not extract_mod.have_pdftotext():
            unsupported = conn.execute(
                "SELECT count(*) FROM blob WHERE extract_status='unsupported'").fetchone()[0]
            if unsupported:
                _out(STYLE.dim(f"  {unsupported} file(s) need pdftotext on PATH to be indexed"))
    db.close()
    return EXIT_OK


def cmd_gc(args) -> int:
    db = _open_db(args)
    result = files.gc(db, dry_run=not args.apply)
    if args.json:
        _emit_json(result)
    else:
        verb = "would reclaim" if result["dry_run"] else "reclaimed"
        _out(f"  {verb} {result['unreferenced']} attachment(s), "
             f"{result['bytes'] / 1e6:.2f} MB")
        if result["stale_partials"]:
            _out(STYLE.dim(f"  {result['stale_partials']} interrupted upload(s)"))
        if result["missing_bytes"]:
            _out(STYLE.red(f"  {len(result['missing_bytes'])} attachment(s) are recorded "
                           f"but their bytes are missing from disk"))
            _out(STYLE.dim("  that is a restore-from-backup situation, not something gc fixes"))
        if result["dry_run"]:
            _out(STYLE.dim("  nothing removed; pass --apply to do it"))
    db.close()
    return EXIT_OK if not result["missing_bytes"] else EXIT_INTEGRITY


def cmd_add(args) -> int:
    db = _open_db(args)
    tz = _tzid(args, db)
    try:
        kind = args.kind
        facet: Dict[str, Any] = {}
        title = " ".join(args.title).strip() if isinstance(args.title, list) else (args.title or "")

        if kind == "task":
            facet = {"status": args.status, "priority": args.priority}
            if args.due:
                facet["due"] = args.due
        elif kind == "event":
            facet = {"starts": args.starts, "ends": args.ends,
                     "all_day": args.all_day, "location": args.location or "",
                     "rrule": args.rrule}
        elif kind == "link":
            url = title if title.startswith(("http://", "https://")) else args.url
            if not url:
                _err("a link needs a URL")
                return EXIT_USAGE
            facet = {"url": url}
            if url == title:
                title = args.title_for_link or ""
        elif kind == "person":
            facet = {"given_name": args.given or "", "family_name": args.family or "",
                     "org": args.org or "", "role": args.role or "",
                     "emails": args.email or [], "phones": args.phone or []}
            if not title and (args.given or args.family):
                title = f"{args.given or ''} {args.family or ''}".strip()

        doc = model.create(
            db, kind=kind, title=title, body=_read_body(args),
            props=_parse_props(getattr(args, "prop", [])),
            tags=getattr(args, "tag", []) or [], facet=facet or None,
            pinned=getattr(args, "pin", False), tzid=tz)
    except (model.ValidationError, ValueError, dates.ParseError) as exc:
        _err(str(exc))
        return EXIT_USAGE
    finally:
        pass

    if args.json:
        _emit_json(doc)
    else:
        _out(f"{STYLE.green('Added')} {STYLE.dim(ids.short(doc['uid']))}  {doc['title']}")
    db.close()
    return EXIT_OK


def cmd_find(args) -> int:
    db = _open_db(args)
    try:
        page = search.search(db, " ".join(args.query), tzid=_tzid(args, db),
                             limit=args.limit, offset=args.offset)
    except search.QueryError as exc:
        _err(str(exc))
        db.close()
        return EXIT_USAGE

    if args.json:
        _emit_json({
            "query": " ".join(args.query),
            "understood": page.explain,
            "total": page.total, "total_capped": page.total_capped,
            "took_ms": page.took_ms, "note": page.note,
            "hits": [h._asdict() for h in page.hits],
        })
        db.close()
        # Same exit code as the human output. An exit code that depends on
        # the output format makes `vault find x --json || ...` a trap.
        return EXIT_OK if page.hits else EXIT_NOT_FOUND

    if not page.hits:
        _out(STYLE.dim("Nothing matched."))
        if page.explain:
            _out(STYLE.dim("  read as: " + "; ".join(page.explain)))
        db.close()
        return EXIT_NOT_FOUND

    for hit in page.hits:
        _out(_render_hit(hit))
    _out()
    shown = f"{len(page.hits)} of {page.describe_total()}"
    _out(STYLE.dim(f"  {shown}   {page.took_ms} ms"))
    if page.note:
        _out(STYLE.yellow(f"  {page.note}"))
    db.close()
    return EXIT_OK


def cmd_ls(args) -> int:
    parts = []
    if args.kind:
        parts.append(f"kind:{args.kind}")
    if args.tag:
        parts.append(f"tag:{args.tag}")
    if args.status:
        parts.append(f"status:{args.status}")
    if args.trashed:
        parts.append("is:trashed")
    if args.pinned:
        parts.append("is:pinned")
    parts.append(f"sort:{args.sort}")
    args.query = [" ".join(parts)]
    args.offset = 0
    return cmd_find(args)


def cmd_show(args) -> int:
    db = _open_db(args)
    try:
        item = model.resolve(db, args.ref, include_trashed=True)
        doc = model.compose(db, item)
    except model.ItemNotFound as exc:
        _err(str(exc))
        db.close()
        return EXIT_NOT_FOUND
    _emit_json(doc) if args.json else _out(_render_doc(doc))
    db.close()
    return EXIT_OK


def cmd_set(args) -> int:
    db = _open_db(args)
    tz = _tzid(args, db)
    try:
        item = model.resolve(db, args.ref)
        kwargs: Dict[str, Any] = {}
        if args.title is not None:
            kwargs["title"] = args.title
        if args.body is not None:
            kwargs["body"] = sys.stdin.read() if args.body == "-" else args.body
        if args.prop:
            current = model.compose(db, item)["props"]
            current.update(_parse_props(args.prop))
            kwargs["props"] = current
        if args.pin:
            kwargs["pinned"] = True
        if args.unpin:
            kwargs["pinned"] = False

        facet: Dict[str, Any] = {}
        if args.status:
            facet["status"] = args.status
        if args.due:
            facet["due"] = args.due
        if args.priority is not None:
            facet["priority"] = args.priority
        if facet:
            existing = model.compose(db, item).get("facet") or {}
            merged = {"status": existing.get("status", "todo"),
                      "priority": existing.get("priority", 0)}
            if existing.get("due_local"):
                merged["due"] = existing["due_local"]
            merged.update(facet)
            kwargs["facet"] = merged

        if not kwargs:
            _err("nothing to change")
            db.close()
            return EXIT_USAGE

        doc = model.update(db, item, tzid=tz, **kwargs)
    except model.ItemNotFound as exc:
        _err(str(exc)); db.close(); return EXIT_NOT_FOUND
    except model.StaleWrite as exc:
        _err(str(exc)); db.close(); return EXIT_CONFLICT
    except (model.ValidationError, ValueError, dates.ParseError) as exc:
        _err(str(exc)); db.close(); return EXIT_USAGE

    _emit_json(doc) if args.json else _out(
        f"{STYLE.green('Updated')} {STYLE.dim(ids.short(doc['uid']))}  {doc['title']}")
    db.close()
    return EXIT_OK


def cmd_tag(args) -> int:
    db = _open_db(args)
    try:
        item = model.resolve(db, args.ref)
        current = set(model.compose(db, item)["tags"])
        for token in args.tags:
            # '~' is what _escape_tag_removals rewrote a leading '-' into.
            if token.startswith(("-", "~")):
                current.discard(token[1:].strip("/").lower())
            else:
                current.add(token.lstrip("+").strip("/").lower())
        model.set_tags(db, item, current)
        doc = model.compose(db, item)
    except model.ItemNotFound as exc:
        _err(str(exc)); db.close(); return EXIT_NOT_FOUND
    _emit_json(doc) if args.json else _out(
        "  " + (STYLE.cyan(" ".join("#" + t for t in doc["tags"])) or STYLE.dim("(no tags)")))
    db.close()
    return EXIT_OK


def cmd_link(args) -> int:
    db = _open_db(args)
    try:
        src = model.resolve(db, args.src)
        dst = model.resolve(db, args.dst)
        if args.remove:
            model.remove_edge(db, src, args.rel, dst)
            action = "Unlinked"
        else:
            model.add_edge(db, src, args.rel, dst)
            action = "Linked"
    except model.ItemNotFound as exc:
        _err(str(exc)); db.close(); return EXIT_NOT_FOUND
    except model.ValidationError as exc:
        _err(str(exc)); db.close(); return EXIT_USAGE
    _out(f"{STYLE.green(action)} {args.src} {args.rel} {args.dst}")
    db.close()
    return EXIT_OK


def cmd_links(args) -> int:
    db = _open_db(args)
    try:
        doc = model.compose(db, model.resolve(db, args.ref))
    except model.ItemNotFound as exc:
        _err(str(exc)); db.close(); return EXIT_NOT_FOUND
    if args.json:
        _emit_json(doc["links"])
    else:
        for link in doc["links"]["out"]:
            _out(f"  {STYLE.green('->')} {link['rel']:16} "
                 f"{STYLE.dim(ids.short(link['uid']))} {link['title']}")
        for link in doc["links"]["in"]:
            _out(f"  {STYLE.magenta('<-')} {link['rel']:16} "
                 f"{STYLE.dim(ids.short(link['uid']))} {link['title']}")
        if not doc["links"]["out"] and not doc["links"]["in"]:
            _out(STYLE.dim("  (no links)"))
    db.close()
    return EXIT_OK


def cmd_rm(args) -> int:
    db = _open_db(args)
    try:
        item = model.resolve(db, args.ref)
        doc = model.compose(db, item)
        model.trash(db, item)
    except model.ItemNotFound as exc:
        _err(str(exc)); db.close(); return EXIT_NOT_FOUND
    _out(f"{STYLE.yellow('Trashed')} {STYLE.dim(ids.short(doc['uid']))}  {doc['title']}")
    _out(STYLE.dim(f"  restore it with: vault restore {ids.short(doc['uid'])}"))
    db.close()
    return EXIT_OK


def cmd_restore(args) -> int:
    db = _open_db(args)
    try:
        item = model.resolve(db, args.ref, include_trashed=True)
        model.restore(db, item)
        doc = model.compose(db, item)
    except model.ItemNotFound as exc:
        _err(str(exc)); db.close(); return EXIT_NOT_FOUND
    except model.ValidationError as exc:
        _err(str(exc)); db.close(); return EXIT_CONFLICT
    _out(f"{STYLE.green('Restored')} {STYLE.dim(ids.short(doc['uid']))}  {doc['title']}")
    db.close()
    return EXIT_OK


def cmd_purge(args) -> int:
    db = _open_db(args)
    try:
        item = model.resolve(db, args.ref, include_trashed=True)
        doc = model.compose(db, item)
    except model.ItemNotFound as exc:
        _err(str(exc)); db.close(); return EXIT_NOT_FOUND
    if not _confirm(f"Permanently delete {doc['title']!r}? This cannot be undone.", args.yes):
        db.close()
        return EXIT_REFUSED
    uid = model.purge(db, item)
    _out(f"{STYLE.red('Purged')} {STYLE.dim(ids.short(uid))}  {doc['title']}")
    db.close()
    return EXIT_OK


def cmd_today(args) -> int:
    db = _open_db(args)
    tz = _tzid(args, db)
    # Each section carries its own ceiling: "recently touched" is a glance,
    # not a list, and --limit should not turn it into one.
    sections = [
        ("Overdue", "is:overdue sort:due", STYLE.red, args.limit),
        ("Due today", "due:today kind:task sort:due", STYLE.yellow, args.limit),
        ("Today", "starts:today kind:event", STYLE.cyan, args.limit),
        ("Recently touched", "sort:recent", STYLE.dim, min(args.limit, 5)),
    ]
    payload: Dict[str, Any] = {}
    for label, query, colour, cap in sections:
        page = search.search(db, query, tzid=tz, limit=cap)
        payload[label] = [h._asdict() for h in page.hits]
        if args.json or not page.hits:
            continue
        _out(colour(STYLE.bold(label)))
        for hit in page.hits:
            _out(_render_hit(hit))
        _out()
    if args.json:
        _emit_json(payload)
    elif not any(payload.values()):
        _out(STYLE.dim("Nothing due, nothing scheduled, nothing recent."))
    db.close()
    return EXIT_OK


def cmd_stats(args) -> int:
    db = _open_db(args)
    conn = db.conn()
    stats = {
        "database": str(db.path),
        "size_bytes": os.path.getsize(db.path) if db.path.exists() else 0,
        "schema_version": SCHEMA_VERSION,
        "items": conn.execute("SELECT count(*) FROM item WHERE deleted_at IS NULL").fetchone()[0],
        "trashed": conn.execute("SELECT count(*) FROM item WHERE deleted_at IS NOT NULL").fetchone()[0],
        "by_kind": {r[0]: r[1] for r in conn.execute(
            "SELECT kind, count(*) FROM item WHERE deleted_at IS NULL "
            "GROUP BY kind ORDER BY count(*) DESC")},
        "tags": conn.execute("SELECT count(*) FROM tag").fetchone()[0],
        "links": conn.execute("SELECT count(*) FROM edge").fetchone()[0],
        "revisions": conn.execute("SELECT count(*) FROM revision").fetchone()[0],
        "changes": conn.execute("SELECT count(*) FROM change_log").fetchone()[0],
        "attachments": conn.execute("SELECT count(*) FROM blob").fetchone()[0],
    }
    if args.json:
        _emit_json(stats)
    else:
        _out(STYLE.bold(f"  {stats['items']:,} items") +
             STYLE.dim(f"   ({stats['trashed']:,} in the trash)"))
        for kind, count in stats["by_kind"].items():
            _out(f"    {KIND_ICON.get(kind, '*')} {kind:10} {count:>8,}")
        _out()
        for label in ("tags", "links", "revisions", "changes", "attachments"):
            _out(STYLE.dim(f"    {label:12} {stats[label]:>8,}"))
        _out()
        _out(STYLE.dim(f"    {stats['size_bytes'] / 1e6:.1f} MB   {db.path}"))
    db.close()
    return EXIT_OK


def cmd_doctor(args) -> int:
    db = _open_db(args)
    conn = db.conn()
    problems: List[str] = []
    checks: Dict[str, str] = {}

    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    checks["integrity_check"] = integrity
    if integrity != "ok":
        problems.append(f"integrity_check: {integrity}")

    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    checks["foreign_key_check"] = "ok" if not fk else f"{len(fk)} violations"
    if fk:
        problems.append(f"foreign_key_check: {len(fk)} violations")

    for table in ("item_fts", "item_trgm"):
        try:
            # rank=1 is the content-aware form. The cheap one does not detect
            # a drifted external-content index at all.
            conn.execute(f"INSERT INTO {table}({table}, rank) VALUES('integrity-check', 1)")
            checks[table] = "ok"
        except Exception as exc:
            checks[table] = str(exc)
            problems.append(f"{table}: {exc}")

    drift = conn.execute(
        "SELECT count(*) FROM item i WHERE i.tags_cache <> coalesce((SELECT "
        "group_concat(slug,' ') FROM (SELECT t.slug FROM item_tag it JOIN tag t "
        "ON t.id=it.tag_id WHERE it.item_id=i.id ORDER BY t.slug)), '')").fetchone()[0]
    checks["tags_cache"] = "ok" if not drift else f"{drift} items drifted"
    if drift:
        problems.append(f"tags_cache drifted on {drift} items")

    bad_refs = conn.execute(
        "SELECT count(*) FROM blob b WHERE b.refcount <> "
        "((SELECT count(*) FROM item_file f WHERE f.blob_id=b.id) + "
        " (SELECT count(*) FROM item_link l WHERE l.archive_blob_id=b.id))").fetchone()[0]
    checks["blob_refcounts"] = "ok" if not bad_refs else f"{bad_refs} wrong"
    if bad_refs:
        problems.append(f"{bad_refs} blob refcounts are wrong")

    withheld = conn.execute(
        "SELECT count(*) FROM item_file WHERE content_withheld=1").fetchone()[0]
    checks["withheld_files"] = str(withheld)

    if args.json:
        _emit_json({"ok": not problems, "checks": checks, "problems": problems})
    else:
        for name, result in checks.items():
            if name == "withheld_files":
                _out(f"  {name:22} {STYLE.dim(result + ' file(s)')}")
                continue
            mark = STYLE.green("ok") if result == "ok" else STYLE.red(result)
            _out(f"  {name:22} {mark}")
        _out()
        if problems:
            _out(STYLE.red(f"  {len(problems)} problem(s) found."))
        else:
            _out(STYLE.green("  Everything checks out."))
        if withheld:
            _out(STYLE.dim(f"  {withheld} file(s) indexed by path only "
                           f"(contents withheld as possible secrets)."))
    db.close()
    return EXIT_OK if not problems else EXIT_INTEGRITY


def cmd_history(args) -> int:
    db = _open_db(args)
    try:
        item = model.resolve(db, args.ref, include_trashed=True)
        entries = history.history(db, item, limit=args.limit)
    except model.ItemNotFound as exc:
        _err(str(exc)); db.close(); return EXIT_NOT_FOUND
    if args.json:
        _emit_json([e._asdict() for e in entries])
    else:
        for entry in entries:
            _out(f"  {STYLE.bold('rev ' + str(entry.rev)):>12}  "
                 f"{STYLE.dim(entry.at)}  {entry.title}")
        if entries:
            _out()
            _out(STYLE.dim(f"  vault diff {args.ref} {entries[-1].rev} {entries[0].rev}"))
            _out(STYLE.dim(f"  vault revert {args.ref} <rev>"))
    db.close()
    return EXIT_OK


def cmd_diff(args) -> int:
    db = _open_db(args)
    try:
        item = model.resolve(db, args.ref, include_trashed=True)
        text = history.diff_text(db, item, args.from_rev, args.to_rev)
    except model.ItemNotFound as exc:
        _err(str(exc)); db.close(); return EXIT_NOT_FOUND
    if args.json:
        _emit_json({"diff": text})
    else:
        for line in text.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                _out(STYLE.green(line))
            elif line.startswith("-") and not line.startswith("---"):
                _out(STYLE.red(line))
            elif line.startswith("@@"):
                _out(STYLE.cyan(line))
            else:
                _out(STYLE.dim(line))
    db.close()
    return EXIT_OK


def cmd_revert(args) -> int:
    db = _open_db(args)
    try:
        item = model.resolve(db, args.ref, include_trashed=True)
        doc = history.revert(db, item, args.rev)
    except model.ItemNotFound as exc:
        _err(str(exc)); db.close(); return EXIT_NOT_FOUND
    _emit_json(doc) if args.json else _out(
        f"{STYLE.green('Reverted')} to rev {args.rev}; now at rev {doc['rev']}  {doc['title']}")
    db.close()
    return EXIT_OK


def cmd_undo(args) -> int:
    db = _open_db(args)
    if args.list:
        entries = history.undoable(db, limit=args.limit)
        if args.json:
            _emit_json(entries)
        else:
            for entry in entries:
                _out(f"  {STYLE.dim(entry['txn_id'][:12])}  {entry['at']}  "
                     f"{entry['rows']:>4} change(s)  {','.join(entry['ops'])}")
        db.close()
        return EXIT_OK
    try:
        result = history.undo(db, args.txn)
    except history.UndoError as exc:
        _err(str(exc)); db.close(); return EXIT_NOT_FOUND
    if args.json:
        _emit_json(result)
    else:
        _out(f"{STYLE.green('Undid')} {len(result['reversed'])} change(s).")
        for line in result["reversed"][:10]:
            _out(STYLE.dim(f"    {line}"))
        if len(result["reversed"]) > 10:
            _out(STYLE.dim(f"    ... and {len(result['reversed']) - 10} more"))
        for line in result["skipped"]:
            _out(STYLE.yellow(f"    skipped: {line}"))
    db.close()
    return EXIT_OK


def cmd_compact_history(args) -> int:
    db = _open_db(args)
    result = history.compact(db, keep_days=args.keep_days,
                             keep_per_item=args.keep_per_item, dry_run=not args.apply)
    if args.json:
        _emit_json(result)
    else:
        verb = "would remove" if result["dry_run"] else "removed"
        _out(f"  {verb} {result['change_log']:,} audit rows older than {result['cutoff']}")
        _out(f"  {verb} {result['revisions']:,} revisions beyond the most recent "
             f"{args.keep_per_item} per item")
        if result["dry_run"]:
            _out(STYLE.dim("  nothing changed; pass --apply to do it"))
    db.close()
    return EXIT_OK


def cmd_demo(args) -> int:
    """Load a small, realistic dataset so the tool is explorable immediately."""
    db = _open_db(args)
    tz = _tzid(args, db)
    made = []
    made.append(model.create(
        db, kind="note", title="Welcome to Vault",
        body="Everything you keep is one row in one table, so search, tags, links,\n"
             "history and export work the same for all of it.\n\n"
             "Try:  vault find budget\n"
             "      vault find kind:task status:todo\n"
             "      vault today",
        tags=["vault/help"], pinned=True))
    made.append(model.create(
        db, kind="note", title="Q3 planning", body="Please review the budget before Friday.",
        tags=["work/finance"]))
    made.append(model.create(
        db, kind="task", title="Review the Q3 budget",
        facet={"status": "todo", "due": "friday", "priority": 3},
        tags=["work/finance"], tzid=tz))
    made.append(model.create(
        db, kind="task", title="Book the venue",
        facet={"status": "done"}, tags=["work"], tzid=tz))
    made.append(model.create(
        db, kind="event", title="Team standup",
        facet={"starts": "tomorrow 9am", "location": "Room 2"}, tags=["work"], tzid=tz))
    made.append(model.create(
        db, kind="link", title="SQLite FTS5 documentation",
        facet={"url": "https://sqlite.org/fts5.html"}, tags=["work/ref"]))
    made.append(model.create(
        db, kind="person", title="Ada Lovelace",
        facet={"given_name": "Ada", "family_name": "Lovelace", "org": "Analytical Engines",
               "emails": ["ada@example.com"]}, tags=["people"]))
    made.append(model.create(
        db, kind="note", title="Invoice 118", props={"amount": 8200, "client": "acme"},
        tags=["work/clients"]))

    note_id = model.resolve(db, made[1]["uid"])
    model.add_edge(db, note_id, "mentions", model.resolve(db, made[2]["uid"]))
    model.add_edge(db, model.resolve(db, made[4]["uid"]), "attended_by",
                   model.resolve(db, made[6]["uid"]))

    if args.json:
        _emit_json({"created": [d["uid"] for d in made]})
    else:
        _out(f"{STYLE.green('Loaded')} {len(made)} sample items.")
        _out(STYLE.dim("  vault find budget"))
        _out(STYLE.dim("  vault find kind:task status:todo"))
        _out(STYLE.dim("  vault today"))
    db.close()
    return EXIT_OK


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------

def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", metavar="PATH",
                        help=f"database file (default: <repo>/data/vault.db, or ${DB_ENV_VAR})")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--tz", metavar="ZONE", help="IANA zone for times you type")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vault",
        description="A self-contained personal database. One file, everything searchable.",
        epilog="Run `vault demo` to load sample data, then `vault find budget`.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version",
                        version=f"vault {__version__} (schema v{SCHEMA_VERSION})")
    _add_common(parser)
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p = sub.add_parser("init", help="create the database")
    _add_common(p); p.set_defaults(func=cmd_init)

    # -- add ----------------------------------------------------------------
    add = sub.add_parser("add", help="capture something")
    add_sub = add.add_subparsers(dest="kind", metavar="<kind>")

    def _add_shared(sp):
        _add_common(sp)
        sp.add_argument("--body", help="body text, or - to read stdin")
        sp.add_argument("--tag", action="append", default=[], help="repeatable")
        sp.add_argument("--prop", action="append", default=[], metavar="KEY=VALUE",
                        help="repeatable; numbers stay comparable")
        sp.add_argument("--pin", action="store_true")

    sp = add_sub.add_parser("note", help="a note")
    sp.add_argument("title", nargs="*"); _add_shared(sp); sp.set_defaults(func=cmd_add)

    sp = add_sub.add_parser("task", help="a task")
    sp.add_argument("title", nargs="*")
    sp.add_argument("--due", help="friday, tomorrow 3pm, +7d, 2026-05-01")
    sp.add_argument("--status", default="todo",
                    choices=["todo", "doing", "blocked", "done", "cancelled"])
    sp.add_argument("--priority", type=int, default=0, choices=range(6))
    _add_shared(sp); sp.set_defaults(func=cmd_add)

    sp = add_sub.add_parser("event", help="an event")
    sp.add_argument("title", nargs="*")
    sp.add_argument("--starts", required=True, help="monday 9:30am, 2026-05-01T09:00:00")
    sp.add_argument("--ends")
    sp.add_argument("--all-day", dest="all_day", action="store_true")
    sp.add_argument("--location")
    sp.add_argument("--rrule", help="FREQ=WEEKLY;BYDAY=MO")
    _add_shared(sp); sp.set_defaults(func=cmd_add)

    sp = add_sub.add_parser("link", help="a bookmark")
    sp.add_argument("title", nargs="*", help="the URL, or a title with --url")
    sp.add_argument("--url")
    sp.add_argument("--title", dest="title_for_link", help="title when the URL is positional")
    _add_shared(sp); sp.set_defaults(func=cmd_add)

    sp = add_sub.add_parser("file", help="store a file and index its text")
    sp.add_argument("path")
    sp.add_argument("--title")
    sp.add_argument("--tag", action="append", default=[])
    sp.add_argument("--no-scan", action="store_true",
                    help="skip the credential check (not recommended)")
    _add_common(sp); sp.set_defaults(func=cmd_add_file)

    sp = add_sub.add_parser("person", help="a person")
    sp.add_argument("title", nargs="*")
    sp.add_argument("--given"); sp.add_argument("--family")
    sp.add_argument("--org"); sp.add_argument("--role")
    sp.add_argument("--email", action="append", default=[])
    sp.add_argument("--phone", action="append", default=[])
    _add_shared(sp); sp.set_defaults(func=cmd_add)

    # -- finding ------------------------------------------------------------
    p = sub.add_parser("find", help="search everything",
                       description="Examples:\n"
                                   "  vault find budget\n"
                                   '  vault find "kind:task status:todo due:<friday"\n'
                                   '  vault find "tag:work/* amount>5000"\n'
                                   '  vault find "an exact phrase" -draft\n'
                                   "\n"
                                   "Quote any query containing > or < -- otherwise the\n"
                                   "shell treats them as redirects and Vault never sees them.",
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("query", nargs="+")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--offset", type=int, default=0)
    _add_common(p); p.set_defaults(func=cmd_find)

    p = sub.add_parser("ls", help="list by filter")
    p.add_argument("--kind"); p.add_argument("--tag"); p.add_argument("--status")
    p.add_argument("--trashed", action="store_true")
    p.add_argument("--pinned", action="store_true")
    p.add_argument("--sort", default="recent",
                   choices=["rank", "recent", "oldest", "created", "title", "due"])
    p.add_argument("--limit", type=int, default=20)
    _add_common(p); p.set_defaults(func=cmd_ls)

    p = sub.add_parser("show", help="show one item in full")
    p.add_argument("ref", help="short handle, uid, or a unique title prefix")
    _add_common(p); p.set_defaults(func=cmd_show)

    p = sub.add_parser("today", help="what needs attention")
    p.add_argument("--limit", type=int, default=10)
    _add_common(p); p.set_defaults(func=cmd_today)

    # -- editing ------------------------------------------------------------
    p = sub.add_parser("set", help="change fields on an item")
    p.add_argument("ref")
    p.add_argument("--title"); p.add_argument("--body", help="text, or - for stdin")
    p.add_argument("--prop", action="append", default=[], metavar="KEY=VALUE")
    p.add_argument("--status", choices=["todo", "doing", "blocked", "done", "cancelled"])
    p.add_argument("--due"); p.add_argument("--priority", type=int, choices=range(6))
    p.add_argument("--pin", action="store_true"); p.add_argument("--unpin", action="store_true")
    _add_common(p); p.set_defaults(func=cmd_set)

    p = sub.add_parser("tag", help="add or remove tags (+work -personal)")
    p.add_argument("ref")
    p.add_argument("tags", nargs="+",
                   help="+tag to add, -tag to remove; a bare word adds")
    _add_common(p); p.set_defaults(func=cmd_tag)

    p = sub.add_parser("link", help="relate two items")
    p.add_argument("src"); p.add_argument("rel"); p.add_argument("dst")
    p.add_argument("--remove", action="store_true")
    _add_common(p); p.set_defaults(func=cmd_link)

    p = sub.add_parser("links", help="show what an item is connected to")
    p.add_argument("ref"); _add_common(p); p.set_defaults(func=cmd_links)

    # -- lifecycle ----------------------------------------------------------
    p = sub.add_parser("rm", help="move to the trash")
    p.add_argument("ref"); _add_common(p); p.set_defaults(func=cmd_rm)

    p = sub.add_parser("restore", help="bring it back")
    p.add_argument("ref"); _add_common(p); p.set_defaults(func=cmd_restore)

    p = sub.add_parser("purge", help="delete permanently")
    p.add_argument("ref"); p.add_argument("--yes", action="store_true")
    _add_common(p); p.set_defaults(func=cmd_purge)

    # -- operations ---------------------------------------------------------
    p = sub.add_parser("extract", help="read text from stored files not yet indexed")
    p.add_argument("--limit", type=int, default=500)
    _add_common(p); p.set_defaults(func=cmd_extract)

    p = sub.add_parser("gc", help="reclaim attachments nothing references")
    p.add_argument("--apply", action="store_true")
    _add_common(p); p.set_defaults(func=cmd_gc)

    p = sub.add_parser("history", help="revisions of an item")
    p.add_argument("ref"); p.add_argument("--limit", type=int, default=20)
    _add_common(p); p.set_defaults(func=cmd_history)

    p = sub.add_parser("diff", help="compare two revisions")
    p.add_argument("ref")
    p.add_argument("from_rev", type=int, metavar="FROM")
    p.add_argument("to_rev", type=int, metavar="TO")
    _add_common(p); p.set_defaults(func=cmd_diff)

    p = sub.add_parser("revert", help="restore an item's content from a revision")
    p.add_argument("ref"); p.add_argument("rev", type=int)
    _add_common(p); p.set_defaults(func=cmd_revert)

    p = sub.add_parser("undo", help="reverse the last change, however many items it touched")
    p.add_argument("--txn", help="a specific transaction id")
    p.add_argument("--list", action="store_true", help="show what could be undone")
    p.add_argument("--limit", type=int, default=15)
    _add_common(p); p.set_defaults(func=cmd_undo)

    p = sub.add_parser("compact-history", help="trim old history (never automatic)")
    p.add_argument("--keep-days", type=int, default=90, dest="keep_days")
    p.add_argument("--keep-per-item", type=int, default=30, dest="keep_per_item")
    p.add_argument("--apply", action="store_true", help="actually do it")
    _add_common(p); p.set_defaults(func=cmd_compact_history)

    p = sub.add_parser("stats", help="what is in here")
    _add_common(p); p.set_defaults(func=cmd_stats)

    p = sub.add_parser("doctor", help="check the database is sound")
    p.add_argument("--deep", action="store_true")
    _add_common(p); p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("demo", help="load sample data")
    _add_common(p); p.set_defaults(func=cmd_demo)

    return parser


def _overview(args) -> int:
    """What bare `vault` shows.

    Until the web server lands this is a status page plus the shortest route
    to doing something useful, which beats an argparse usage dump.
    """
    layout = resolve(getattr(args, "db", None))
    if not layout.db.exists():
        _out(STYLE.bold("Vault") + STYLE.dim(f"  {__version__}"))
        _out()
        _out("  No database yet. Create one with:")
        _out(STYLE.cyan("    vault init"))
        _out(STYLE.dim("    vault demo      # and load some sample data to explore"))
        return EXIT_OK

    args.json = False
    args.limit = 5
    db = _open_db(args)
    count = db.conn().execute(
        "SELECT count(*) FROM item WHERE deleted_at IS NULL").fetchone()[0]
    db.close()
    _out(STYLE.bold("Vault") + STYLE.dim(f"   {count:,} items   {layout.db}"))
    _out()
    cmd_today(args)
    _out(STYLE.dim("  vault find <query>     vault add note \"...\"     vault --help"))
    return EXIT_OK


# Tag removals are written -personal, which argparse reads as an option and
# rejects. REMAINDER would fix that but swallows every later option too --
# including --db, which then silently uses the wrong database. So the tokens
# are rewritten before parsing and read back in cmd_tag.
_TAG_REMOVAL = __import__("re").compile(r"^-(?![-])[A-Za-z0-9][A-Za-z0-9/_-]*$")


def _escape_tag_removals(argv: List[str]) -> List[str]:
    if not argv or argv[0] != "tag":
        return argv
    out = list(argv[:2])          # 'tag' and the reference
    for token in argv[2:]:
        out.append("~" + token[1:] if _TAG_REMOVAL.match(token) else token)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    raw = argv if argv is not None else sys.argv[1:]
    args = parser.parse_args(_escape_tag_removals(list(raw)))

    if not getattr(args, "command", None):
        return _overview(args)
    if getattr(args, "command", None) == "add" and not getattr(args, "kind", None):
        parser.parse_args(["add", "--help"])
        return EXIT_USAGE
    if not hasattr(args, "func"):
        parser.print_help()
        return EXIT_USAGE

    # Titles arrive as a list from nargs="*" so that quoting is optional.
    if isinstance(getattr(args, "title", None), list):
        args.title = " ".join(args.title).strip()

    try:
        return args.func(args)
    except MigrationError as exc:
        _err(str(exc))
        return EXIT_INTEGRITY
    except KeyboardInterrupt:
        _err("interrupted")
        return EXIT_REFUSED
    except BrokenPipeError:
        # `vault find x | head` is a normal thing to do.
        try:
            sys.stdout.close()
        except Exception:
            pass
        return EXIT_OK
