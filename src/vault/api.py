"""The HTTP API: a WSGI application over the same write path as the CLI.

Concurrency
-----------
Writes use ETags. ``GET`` returns ``ETag: "<uid>.<rev>"``; ``PATCH`` requires
``If-Match`` and answers 428 when it is missing, or 409 with the current
document and a field-level diff when it is stale. The revision is re-read
*inside* the write transaction, so nothing can commit between the check and
the write -- checking first leaves exactly the race the mechanism exists to
close.

Security
--------
A server on 127.0.0.1 is not private. Any page the user visits can point a
name it controls at 127.0.0.1 and have the browser send requests here, with
the browser's own idea of who is asking. Three checks together close that:

* ``Host`` must be a loopback name we recognise. DNS rebinding relies on the
  browser sending the attacker's hostname, so this alone stops the common case.
* ``Origin``, when present, must be one of ours.
* ``Sec-Fetch-Site: cross-site`` is refused outright.

Errors are ``application/problem+json`` (RFC 9457), so a client gets a
machine-readable ``type`` and a sentence a person can read.
"""

import hashlib
import json
import re
import sqlite3
import time
import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote

from vault import SCHEMA_VERSION, __version__, dates, files, history, ids, model, search
from vault.db import Database

__all__ = ["Api", "ProblemError", "build_app"]

JSON = "application/json; charset=utf-8"
PROBLEM = "application/problem+json; charset=utf-8"
MAX_BODY = 64 * 1024 * 1024


class ProblemError(Exception):
    """An error with an HTTP status and a readable explanation."""

    def __init__(self, status: int, title: str, detail: str = "",
                 type_: str = "about:blank", **extra: Any) -> None:
        super().__init__(detail or title)
        self.status = status
        self.title = title
        self.detail = detail
        self.type = type_
        self.extra = extra

    def body(self, path: str) -> Dict[str, Any]:
        payload = {"type": self.type, "title": self.title, "status": self.status,
                   "detail": self.detail, "instance": path}
        payload.update(self.extra)
        return payload


class Request:
    __slots__ = ("environ", "method", "path", "query", "headers", "_body")

    def __init__(self, environ: Dict[str, Any]) -> None:
        self.environ = environ
        self.method = environ.get("REQUEST_METHOD", "GET").upper()
        self.path = unquote(environ.get("PATH_INFO", "") or "/")
        self.query = parse_qs(environ.get("QUERY_STRING", "") or "", keep_blank_values=True)
        self.headers = {
            key[5:].replace("_", "-").lower(): value
            for key, value in environ.items() if key.startswith("HTTP_")
        }
        for key in ("CONTENT_TYPE", "CONTENT_LENGTH"):
            if environ.get(key):
                self.headers[key.replace("_", "-").lower()] = environ[key]
        self._body: Optional[bytes] = None

    def param(self, name: str, default: Optional[str] = None) -> Optional[str]:
        values = self.query.get(name)
        return values[0] if values else default

    def int_param(self, name: str, default: int, *, low: int = 0, high: int = 1000) -> int:
        raw = self.param(name)
        if raw is None or raw == "":
            return default
        try:
            return max(low, min(high, int(raw)))
        except ValueError:
            raise ProblemError(400, "Bad parameter", f"{name} must be a whole number")

    def body_bytes(self) -> bytes:
        if self._body is None:
            try:
                length = int(self.headers.get("content-length") or 0)
            except ValueError:
                length = 0
            if length > MAX_BODY:
                raise ProblemError(413, "Body too large",
                                   f"{length} bytes exceeds the {MAX_BODY} byte limit")
            self._body = self.environ["wsgi.input"].read(length) if length else b""
        return self._body

    def json(self) -> Dict[str, Any]:
        raw = self.body_bytes()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProblemError(400, "Malformed JSON", str(exc))
        if not isinstance(parsed, dict):
            raise ProblemError(400, "Malformed JSON", "the body must be a JSON object")
        return parsed


class Response:
    __slots__ = ("status", "headers", "payload", "raw")

    def __init__(self, status: int = 200, payload: Any = None, *,
                 headers: Optional[List[Tuple[str, str]]] = None,
                 raw: Optional[bytes] = None) -> None:
        self.status = status
        self.headers = headers or []
        self.payload = payload
        self.raw = raw

    def body(self) -> bytes:
        if self.raw is not None:
            return self.raw
        if self.payload is None:
            return b""
        return json.dumps(self.payload, ensure_ascii=False, default=str).encode("utf-8")


STATUS_TEXT = {
    200: "OK", 201: "Created", 204: "No Content", 301: "Moved Permanently",
    304: "Not Modified", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
    405: "Method Not Allowed", 409: "Conflict", 412: "Precondition Failed",
    413: "Payload Too Large", 415: "Unsupported Media Type",
    428: "Precondition Required", 500: "Internal Server Error",
}


def _etag(uid: str, rev: int) -> str:
    return f'"{uid}.{rev}"'


def _parse_if_match(value: Optional[str]) -> Optional[int]:
    """Pull the revision out of an If-Match header.

    Accepts the full ``"uid.rev"`` form and a bare revision, and tolerates
    the weak prefix some clients add.
    """
    if not value:
        return None
    token = value.strip()
    if token.startswith("W/"):
        token = token[2:]
    token = token.strip('"')
    if "." in token:
        token = token.rsplit(".", 1)[1]
    try:
        return int(token)
    except ValueError:
        raise ProblemError(400, "Bad If-Match", f"could not read a revision from {value!r}")


class Api:
    """The routes. One instance per server, shared across request threads."""

    def __init__(self, db: Database, *, token: Optional[str] = None,
                 allowed_hosts: Optional[set] = None) -> None:
        self.db = db
        self.token = token
        self.allowed_hosts = allowed_hosts or set()
        self.started = time.time()
        self.routes: List[Tuple[str, Any, Callable]] = []
        self._register()

    # -- routing -----------------------------------------------------------

    def route(self, method: str, pattern: str):
        compiled = re.compile("^" + pattern + "$")

        def decorate(func):
            self.routes.append((method, compiled, func))
            return func

        return decorate

    def dispatch(self, request: Request) -> Response:
        allowed: List[str] = []
        for method, pattern, handler in self.routes:
            match = pattern.match(request.path)
            if not match:
                continue
            if method != request.method:
                allowed.append(method)
                continue
            return handler(request, *match.groups())
        if allowed:
            raise ProblemError(405, "Method not allowed",
                               f"{request.path} accepts {', '.join(sorted(set(allowed)))}",
                               allow=sorted(set(allowed)))
        raise ProblemError(404, "No such endpoint", f"nothing is routed at {request.path}")

    # -- helpers -----------------------------------------------------------

    def _resolve(self, ref: str, *, trashed: bool = True) -> int:
        try:
            return model.resolve(self.db, ref, include_trashed=trashed)
        except model.ItemNotFound as exc:
            raise ProblemError(404, "No such item", str(exc))

    def _doc_response(self, item_id: int, status: int = 200) -> Response:
        doc = model.compose(self.db, item_id)
        return Response(status, doc, headers=[("ETag", _etag(doc["uid"], doc["rev"]))])

    # -- registration ------------------------------------------------------

    def _register(self) -> None:
        V = "/api/v1"

        @self.route("GET", r"/api/v1/health")
        def health(request):
            conn = self.db.conn()
            last_backup = conn.execute(
                "SELECT at FROM backup_log ORDER BY at DESC LIMIT 1").fetchone()
            return Response(200, {
                "ok": True, "version": __version__, "schema_version": SCHEMA_VERSION,
                "sqlite": sqlite3.sqlite_version,
                "items": conn.execute(
                    "SELECT count(*) FROM item WHERE deleted_at IS NULL").fetchone()[0],
                "uptime_seconds": int(time.time() - self.started),
                "last_backup": last_backup[0] if last_backup else None,
            })

        @self.route("GET", r"/api/v1/schema")
        def schema(request):
            conn = self.db.conn()
            kinds = []
            for row in conn.execute(
                    "SELECT name, label, plural, icon, has_facet FROM kind ORDER BY sort_order"):
                fields = [
                    {"key": f[0], "label": f[1], "type": f[2], "required": bool(f[3]),
                     "multi": bool(f[4]), "enum": json.loads(f[5] or "[]"), "hint": f[6]}
                    for f in conn.execute(
                        "SELECT key, label, type, required, multi, enum_values, hint "
                        "FROM kind_field WHERE kind=? ORDER BY sort_order", (row[0],))
                ]
                kinds.append({"name": row[0], "label": row[1], "plural": row[2],
                              "icon": row[3], "has_facet": bool(row[4]), "fields": fields})
            rels = [{"name": r[0], "label": r[1], "inverse": r[2], "inverse_label": r[3]}
                    for r in conn.execute(
                        "SELECT name, label, inverse, inverse_label FROM rel ORDER BY name")]
            return Response(200, {"kinds": kinds, "relationships": rels})

        @self.route("GET", r"/api/v1/search")
        def do_search(request):
            query = request.param("q", "") or ""
            try:
                page = search.search(
                    self.db, query, tzid=request.param("tz"),
                    limit=request.int_param("limit", 25, low=1, high=200),
                    offset=request.int_param("offset", 0, high=100000))
            except search.QueryError as exc:
                raise ProblemError(400, "Bad query", str(exc),
                                   type_="https://vault.local/problem/bad-query")
            return Response(200, {
                "query": query, "understood": page.explain, "note": page.note,
                "total": page.total, "total_capped": page.total_capped,
                "took_ms": page.took_ms,
                "hits": [h._asdict() for h in page.hits],
            })

        @self.route("GET", r"/api/v1/suggest")
        def do_suggest(request):
            return Response(200, search.suggest(
                self.db, request.param("q", "") or "",
                limit=request.int_param("limit", 8, low=1, high=50)))

        @self.route("GET", r"/api/v1/items")
        def list_items(request):
            parts = []
            for field in ("kind", "tag", "status"):
                value = request.param(field)
                if value:
                    parts.append(f"{field}:{value}")
            if request.param("trashed") == "1":
                parts.append("is:trashed")
            if request.param("q"):
                parts.append(request.param("q"))
            parts.append(f"sort:{request.param('sort', 'recent')}")
            try:
                page = search.search(
                    self.db, " ".join(parts), tzid=request.param("tz"),
                    limit=request.int_param("limit", 50, low=1, high=200),
                    offset=request.int_param("offset", 0, high=100000))
            except search.QueryError as exc:
                raise ProblemError(400, "Bad query", str(exc))
            return Response(200, {"items": [h._asdict() for h in page.hits],
                                  "total": page.total, "total_capped": page.total_capped})

        @self.route("POST", r"/api/v1/items")
        def create_item(request):
            payload = request.json()
            idem = request.headers.get("idempotency-key")
            if idem:
                replay = self._replay(idem, request)
                if replay is not None:
                    return replay
            try:
                doc = model.create(
                    self.db,
                    kind=payload.get("kind", "note"),
                    title=payload.get("title", ""), body=payload.get("body", ""),
                    props=payload.get("props"), tags=payload.get("tags") or [],
                    facet=payload.get("facet"), pinned=bool(payload.get("pinned")),
                    tzid=payload.get("tz") or request.param("tz"))
            except model.ValidationError as exc:
                raise ProblemError(422, "Could not create that", str(exc),
                                   type_="https://vault.local/problem/validation")
            except dates.ParseError as exc:
                raise ProblemError(422, "Could not read a date", str(exc))
            response = Response(201, doc, headers=[
                ("Location", f"{V}/items/{doc['uid']}"),
                ("ETag", _etag(doc["uid"], doc["rev"])),
            ])
            if idem:
                self._remember(idem, request, response)
            return response

        @self.route("GET", r"/api/v1/items/([0-9a-fA-F]{4,32})")
        def get_item(request, ref):
            conn = self.db.conn()
            row = conn.execute("SELECT id FROM item WHERE uid=?", (ref.lower(),)).fetchone()
            if row is None:
                redirect = conn.execute(
                    "SELECT redirect_to_uid FROM tombstone WHERE uid=?", (ref.lower(),)).fetchone()
                if redirect and redirect[0]:
                    return Response(301, {"moved_to": redirect[0]},
                                    headers=[("Location", f"{V}/items/{redirect[0]}")])
                item_id = self._resolve(ref)
            else:
                item_id = int(row[0])

            doc = model.compose(self.db, item_id)
            tag = _etag(doc["uid"], doc["rev"])
            if request.headers.get("if-none-match", "").strip() == tag:
                return Response(304, None, headers=[("ETag", tag)])
            return Response(200, doc, headers=[("ETag", tag)])

        @self.route("PATCH", r"/api/v1/items/([0-9a-fA-F]{4,32})")
        def patch_item(request, ref):
            item_id = self._resolve(ref, trashed=False)
            if "if-match" not in request.headers:
                current = model.compose(self.db, item_id)
                raise ProblemError(
                    428, "If-Match required",
                    "Send If-Match with the ETag you last read, so a concurrent "
                    "edit cannot be overwritten silently.",
                    type_="https://vault.local/problem/if-match-required",
                    current_etag=_etag(current["uid"], current["rev"]))

            expected = _parse_if_match(request.headers["if-match"])
            payload = request.json()
            kwargs: Dict[str, Any] = {}
            for field in ("title", "body", "props", "facet", "pinned", "tags"):
                if field in payload:
                    kwargs[field] = payload[field]
            try:
                doc = model.update(self.db, item_id, expected_rev=expected,
                                   tzid=payload.get("tz"), **kwargs)
            except model.StaleWrite as exc:
                current = model.compose(self.db, item_id)
                raise ProblemError(
                    409, "That item changed while you were editing",
                    f"You have revision {exc.expected}; it is now at {exc.actual}.",
                    type_="https://vault.local/problem/stale-write",
                    expected_rev=exc.expected, current_rev=exc.actual,
                    current=current, changed=self._changed_fields(item_id, exc.expected))
            except model.ValidationError as exc:
                raise ProblemError(422, "Could not apply that change", str(exc))
            return Response(200, doc, headers=[("ETag", _etag(doc["uid"], doc["rev"]))])

        @self.route("DELETE", r"/api/v1/items/([0-9a-fA-F]{4,32})")
        def delete_item(request, ref):
            item_id = self._resolve(ref, trashed=False)
            model.trash(self.db, item_id)
            return Response(204)

        @self.route("POST", r"/api/v1/items/([0-9a-fA-F]{4,32})/restore")
        def restore_item(request, ref):
            item_id = self._resolve(ref)
            try:
                model.restore(self.db, item_id)
            except model.ValidationError as exc:
                raise ProblemError(409, "Could not restore that", str(exc))
            return self._doc_response(item_id)

        @self.route("POST", r"/api/v1/items/([0-9a-fA-F]{4,32})/purge")
        def purge_item(request, ref):
            item_id = self._resolve(ref)
            if request.json().get("confirm") is not True:
                raise ProblemError(428, "Confirmation required",
                                   'Send {"confirm": true}. A purge cannot be undone '
                                   'except from the tombstone.')
            uid = model.purge(self.db, item_id)
            return Response(200, {"purged": uid})

        @self.route("GET", r"/api/v1/items/([0-9a-fA-F]{4,32})/revisions")
        def list_revisions(request, ref):
            item_id = self._resolve(ref)
            return Response(200, {"revisions": [
                r._asdict() for r in history.history(
                    self.db, item_id, limit=request.int_param("limit", 50, low=1, high=500))]})

        @self.route("GET", r"/api/v1/items/([0-9a-fA-F]{4,32})/revisions/(\d+)")
        def get_revision(request, ref, rev):
            item_id = self._resolve(ref)
            try:
                return Response(200, history.read_revision(self.db, item_id, int(rev)))
            except model.ItemNotFound as exc:
                raise ProblemError(404, "No such revision", str(exc))

        @self.route("GET", r"/api/v1/items/([0-9a-fA-F]{4,32})/diff")
        def diff_revisions(request, ref):
            item_id = self._resolve(ref)
            try:
                return Response(200, {"diff": history.diff_text(
                    self.db, item_id,
                    request.int_param("from", 1, low=1, high=10**9),
                    request.int_param("to", 2, low=1, high=10**9))})
            except model.ItemNotFound as exc:
                raise ProblemError(404, "No such revision", str(exc))

        @self.route("POST", r"/api/v1/items/([0-9a-fA-F]{4,32})/revert/(\d+)")
        def revert_item(request, ref, rev):
            item_id = self._resolve(ref)
            try:
                doc = history.revert(self.db, item_id, int(rev))
            except model.ItemNotFound as exc:
                raise ProblemError(404, "No such revision", str(exc))
            return Response(200, doc, headers=[("ETag", _etag(doc["uid"], doc["rev"]))])

        @self.route("PUT", r"/api/v1/items/([0-9a-fA-F]{4,32})/tags")
        def put_tags(request, ref):
            item_id = self._resolve(ref, trashed=False)
            payload = request.json()
            expected = _parse_if_match(request.headers.get("if-match"))
            try:
                model.set_tags(self.db, item_id, payload.get("tags") or [],
                               expected_rev=expected)
            except model.StaleWrite as exc:
                raise ProblemError(409, "That item changed while you were editing",
                                   str(exc), expected_rev=exc.expected, current_rev=exc.actual)
            return self._doc_response(item_id)

        @self.route("GET", r"/api/v1/items/([0-9a-fA-F]{4,32})/edges")
        def get_edges(request, ref):
            item_id = self._resolve(ref)
            return Response(200, model.compose(self.db, item_id)["links"])

        @self.route("POST", r"/api/v1/items/([0-9a-fA-F]{4,32})/edges")
        def post_edge(request, ref):
            src = self._resolve(ref, trashed=False)
            payload = request.json()
            dst = self._resolve(payload.get("dst", ""), trashed=False)
            try:
                model.add_edge(self.db, src, payload.get("rel", "relates_to"), dst)
            except model.ValidationError as exc:
                raise ProblemError(422, "Could not link those", str(exc))
            return self._doc_response(src, 201)

        @self.route("DELETE", r"/api/v1/edges/([0-9a-fA-F]{4,32})/([a-z_]+)/([0-9a-fA-F]{4,32})")
        def delete_edge(request, src_ref, rel, dst_ref):
            src, dst = self._resolve(src_ref), self._resolve(dst_ref)
            model.remove_edge(self.db, src, rel, dst)
            return Response(204)

        @self.route("GET", r"/api/v1/tags")
        def get_tags(request):
            rows = self.db.conn().execute(
                "SELECT t.slug, count(it.item_id) FROM tag t "
                "LEFT JOIN item_tag it ON it.tag_id = t.id "
                "LEFT JOIN item i ON i.id = it.item_id AND i.deleted_at IS NULL "
                "GROUP BY t.slug ORDER BY t.slug")
            return Response(200, {"tags": [{"slug": r[0], "count": r[1]} for r in rows]})

        @self.route("GET", r"/api/v1/stats")
        def get_stats(request):
            conn = self.db.conn()
            return Response(200, {
                "items": conn.execute(
                    "SELECT count(*) FROM item WHERE deleted_at IS NULL").fetchone()[0],
                "trashed": conn.execute(
                    "SELECT count(*) FROM item WHERE deleted_at IS NOT NULL").fetchone()[0],
                "by_kind": {r[0]: r[1] for r in conn.execute(
                    "SELECT kind, count(*) FROM item WHERE deleted_at IS NULL GROUP BY kind")},
                "tags": conn.execute("SELECT count(*) FROM tag").fetchone()[0],
                "links": conn.execute("SELECT count(*) FROM edge").fetchone()[0],
                "attachments": conn.execute("SELECT count(*) FROM blob").fetchone()[0],
                "withheld": conn.execute(
                    "SELECT count(*) FROM item_file WHERE content_withheld=1").fetchone()[0],
            })

        @self.route("GET", r"/api/v1/saved-searches")
        def saved_searches(request):
            rows = self.db.conn().execute(
                "SELECT name, query, icon, pinned FROM saved_search ORDER BY sort_order, name")
            return Response(200, {"searches": [
                {"name": r[0], "query": r[1], "icon": r[2], "pinned": bool(r[3])} for r in rows]})

        @self.route("POST", r"/api/v1/undo")
        def do_undo(request):
            try:
                return Response(200, history.undo(self.db, request.json().get("txn_id")))
            except history.UndoError as exc:
                raise ProblemError(404, "Nothing to undo", str(exc))

        @self.route("GET", r"/api/v1/files/([0-9a-f]{64})")
        def get_file(request, sha):
            conn = self.db.conn()
            row = conn.execute(
                "SELECT media_type, size_bytes, suffix FROM blob WHERE sha256=?", (sha,)).fetchone()
            if row is None:
                raise ProblemError(404, "No such attachment", sha[:12])
            try:
                path = files.open_blob(self.db, sha)
            except FileNotFoundError as exc:
                raise ProblemError(410, "Attachment bytes are missing", str(exc))
            name = conn.execute(
                "SELECT filename FROM item_file WHERE blob_id=(SELECT id FROM blob "
                "WHERE sha256=?) LIMIT 1", (sha,)).fetchone()
            return Response(200, None, raw=path.read_bytes(), headers=[
                ("Content-Type", row[0]),
                ("ETag", f'"{sha}"'),
                ("Cache-Control", "private, max-age=31536000, immutable"),
                ("Content-Disposition",
                 f'inline; filename="{(name[0] if name else sha)[:200]}"'),
            ])

    # -- concurrency helpers ------------------------------------------------

    def _changed_fields(self, item_id: int, since_rev: int) -> List[str]:
        """Which fields moved since the client's revision.

        A 409 that says only "it changed" makes the client re-fetch and
        guess; naming the fields lets it merge.
        """
        try:
            old = history.read_revision(self.db, item_id, since_rev)
        except model.ItemNotFound:
            return []
        new = model.compose(self.db, item_id)
        changed = []
        for field in ("title", "body", "props", "tags", "facet", "pinned"):
            if old.get(field) != new.get(field):
                changed.append(field)
        return changed

    def _replay(self, key: str, request: Request) -> Optional[Response]:
        row = self.db.conn().execute(
            "SELECT method, path, request_sha, status, response_json FROM idempotency "
            "WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        digest = hashlib.sha256(request.body_bytes()).hexdigest()
        if row[0] != request.method or row[1] != request.path or row[2] != digest:
            raise ProblemError(
                409, "Idempotency-Key reused with a different request",
                "That key was used for a different call. Use a fresh key.")
        payload = json.loads(row[4])
        return Response(int(row[3]), payload,
                        headers=[("Idempotency-Replayed", "true")])

    def _remember(self, key: str, request: Request, response: Response) -> None:
        with self.db.write():
            self.db.conn().execute(
                "INSERT OR REPLACE INTO idempotency"
                "(key, method, path, request_sha, status, response_json, at) "
                "VALUES (?,?,?,?,?,?,?)",
                (key, request.method, request.path,
                 hashlib.sha256(request.body_bytes()).hexdigest(),
                 response.status,
                 json.dumps(response.payload, ensure_ascii=False, default=str),
                 dates.utcnow()))


# ---------------------------------------------------------------------------
# WSGI
# ---------------------------------------------------------------------------

def _loopback_hosts(port: int) -> set:
    names = {"127.0.0.1", "localhost", "[::1]", "::1", "0.0.0.0"}
    return {f"{name}:{port}" for name in names} | names


def build_app(db: Database, *, port: int = 8787, token: Optional[str] = None,
              allow_hosts: Optional[set] = None, static_root=None,
              log: Optional[Callable[[str], None]] = None) -> Callable:
    """Return a WSGI application serving the API and, optionally, the web UI."""
    api = Api(db, token=token,
              allowed_hosts=(allow_hosts or set()) | _loopback_hosts(port))

    def _origin_allowed(origin: str) -> bool:
        if not origin:
            return True
        for host in api.allowed_hosts:
            if origin in (f"http://{host}", f"https://{host}"):
                return True
        return False

    def _guard(request: Request) -> None:
        """Refuse anything that might be a browser on someone else's page.

        The Host check is the important one: DNS rebinding works precisely
        because the browser sends the attacker's hostname, so a server that
        only answers to names it knows is not reachable that way.
        """
        host = (request.headers.get("host") or "").lower()
        if api.allowed_hosts and host not in api.allowed_hosts:
            raise ProblemError(
                403, "Host not allowed",
                f"This server answers to {', '.join(sorted(api.allowed_hosts))[:120]}, "
                f"not {host!r}. This is what stops a web page you visit from "
                f"reaching your database through your browser.",
                type_="https://vault.local/problem/host-not-allowed")

        if request.headers.get("sec-fetch-site") == "cross-site":
            raise ProblemError(403, "Cross-site request refused",
                               "A page on another site tried to call this server.")

        origin = (request.headers.get("origin") or "").lower()
        if origin and not _origin_allowed(origin):
            raise ProblemError(403, "Origin not allowed", f"{origin} is not this server.")

        if api.token:
            supplied = (request.headers.get("authorization", "")
                        .removeprefix("Bearer ").strip()) or request.param("token", "")
            if supplied != api.token:
                raise ProblemError(401, "Token required",
                                   "Pass Authorization: Bearer <token>.")

    def application(environ, start_response):
        request = Request(environ)
        started = time.monotonic()
        try:
            if request.method == "OPTIONS":
                response = Response(204, headers=[
                    ("Allow", "GET, POST, PATCH, PUT, DELETE, OPTIONS")])
            else:
                _guard(request)
                if static_root is not None and not request.path.startswith("/api/"):
                    response = static_root(request)
                else:
                    response = api.dispatch(request)
        except ProblemError as problem:
            response = Response(problem.status, problem.body(request.path),
                                headers=[("Content-Type", PROBLEM)])
        except Exception:
            # Never leak a traceback over HTTP; log it and say so plainly.
            if log:
                log(traceback.format_exc())
            response = Response(500, {
                "type": "about:blank", "title": "Something went wrong",
                "status": 500,
                "detail": "The server logged the details. This is a bug in Vault.",
                "instance": request.path,
            }, headers=[("Content-Type", PROBLEM)])

        body = response.body()
        headers = list(response.headers)
        names = {name.lower() for name, _ in headers}
        if "content-type" not in names and body:
            headers.append(("Content-Type", JSON))
        headers.append(("Content-Length", str(len(body))))
        # A local database has no business being framed, sniffed or referred.
        headers.extend([
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "no-referrer"),
            ("Cache-Control", dict(headers).get("Cache-Control", "no-store")),
        ])
        if log:
            log(f"{request.method} {request.path} -> {response.status} "
                f"{int((time.monotonic() - started) * 1000)}ms")

        status_line = f"{response.status} {STATUS_TEXT.get(response.status, 'Unknown')}"
        start_response(status_line, headers)
        return [body]

    application.api = api  # type: ignore[attr-defined]
    return application
