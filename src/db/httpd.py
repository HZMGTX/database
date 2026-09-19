"""The server: ThreadingHTTPServer plus the static file handler.

Binds to 127.0.0.1 by default. ``--lan`` opens it to the local network and
requires a token, because the same database that is harmless on loopback is
not harmless on a shared wifi network.
"""

import mimetypes
import os
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Callable, Optional
from wsgiref.handlers import SimpleHandler

from db.api import ProblemError, Request, Response, build_app
from db.db import Database

__all__ = ["Server", "static_handler"]

WEB_ROOT = Path(__file__).resolve().parent / "web"


def static_handler(root: Path) -> Callable:
    """Serve the web UI from *root*, and nothing outside it."""

    def serve(request: Request) -> Response:
        rel = request.path.lstrip("/") or "index.html"
        # resolve() then a containment check: the one way a static handler
        # gets exploited is a path that escapes its root.
        target = (root / rel).resolve()
        try:
            target.relative_to(root.resolve())
        except ValueError:
            raise ProblemError(403, "Outside the web root", rel)

        if target.is_dir():
            target = target / "index.html"

        if not target.is_file():
            # A missing *asset* is a 404. Only extensionless paths fall
            # through to the single-page app.
            #
            # Falling back to index.html for everything is the usual SPA
            # arrangement and it is wrong for assets: a stale cached page
            # asking for a module that has since been deleted gets HTML with
            # a 200, and the browser reports "Unexpected token '<'" from a
            # file the developer no longer has. A 404 says what actually
            # happened.
            if PurePosixPath(rel).suffix:
                raise ProblemError(
                    404, "No such file",
                    f"{request.path} is not part of this build. "
                    f"If a page is asking for it, that page is cached -- reload it.")
            target = root / "index.html"
            if not target.is_file():
                raise ProblemError(404, "Not found", request.path)

        media, _ = mimetypes.guess_type(str(target))
        data = target.read_bytes()
        etag = f'"{hash((target.name, len(data), int(target.stat().st_mtime)))}"'
        if request.headers.get("if-none-match") == etag:
            return Response(304, None, headers=[("ETag", etag)])
        return Response(200, None, raw=data, headers=[
            ("Content-Type", media or "application/octet-stream"),
            ("ETag", etag),
            ("Cache-Control", "no-cache"),
        ])

    return serve


class _Handler(BaseHTTPRequestHandler):
    """Bridges BaseHTTPRequestHandler to the WSGI application."""

    protocol_version = "HTTP/1.1"
    server_version = "database"
    sys_version = ""

    def _run(self) -> None:
        environ = {
            "REQUEST_METHOD": self.command,
            "PATH_INFO": self.path.split("?", 1)[0],
            "QUERY_STRING": self.path.split("?", 1)[1] if "?" in self.path else "",
            "SERVER_PROTOCOL": self.request_version,
            "wsgi.input": self.rfile,
            "wsgi.errors": self.server.error_log,       # type: ignore[attr-defined]
            "wsgi.version": (1, 0),
            "wsgi.multithread": True,
            "wsgi.multiprocess": False,
            "wsgi.run_once": False,
            "wsgi.url_scheme": "http",
        }
        for name, value in self.headers.items():
            key = "HTTP_" + name.upper().replace("-", "_")
            environ[key] = value if key not in environ else environ[key] + "," + value
        if self.headers.get("content-type"):
            environ["CONTENT_TYPE"] = self.headers["content-type"]
        if self.headers.get("content-length"):
            environ["CONTENT_LENGTH"] = self.headers["content-length"]

        handler = SimpleHandler(self.rfile, self.wfile, self.server.error_log,  # type: ignore
                                environ, multithread=True)
        handler.request_handler = self          # type: ignore[attr-defined]
        handler.run(self.server.wsgi_app)       # type: ignore[attr-defined]

    do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = do_OPTIONS = do_HEAD = _run

    def log_message(self, fmt, *args):   # noqa: A003 - quiet by default
        pass


class Server:
    """A running The database server."""

    def __init__(self, db: Database, *, host: str = "127.0.0.1", port: int = 8787,
                 token: Optional[str] = None, lan: bool = False,
                 web_root: Optional[Path] = None,
                 log: Optional[Callable[[str], None]] = None) -> None:
        self.db = db
        self.host = "0.0.0.0" if lan else host
        self.port = port
        # A token is optional on loopback and mandatory off it: the same
        # database that is harmless on 127.0.0.1 is not harmless on shared wifi.
        self.token = token or (secrets.token_urlsafe(24) if lan else None)
        self.lan = lan
        self.web_root = web_root if web_root is not None else WEB_ROOT
        self.log = log
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def base_url(self) -> str:
        host = self.lan_address() if self.lan else "127.0.0.1"
        suffix = f"?token={self.token}" if self.token else ""
        return f"http://{host}:{self.port}/{suffix}"

    def lan_address(self) -> str:
        """This machine's address on the local network, best effort."""
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 1))     # TEST-NET-1; never actually sent
            return probe.getsockname()[0]
        except OSError:
            return "127.0.0.1"
        finally:
            probe.close()

    def _allowed_hosts(self) -> set:
        hosts = set()
        if self.lan:
            address = self.lan_address()
            hosts |= {address, f"{address}:{self.port}"}
        return hosts

    def start(self, *, background: bool = False) -> "Server":
        static = static_handler(self.web_root) if self.web_root.is_dir() else None
        app = build_app(self.db, port=self.port, token=self.token,
                        allow_hosts=self._allowed_hosts(), static_root=static,
                        log=self.log)

        httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        httpd.daemon_threads = True
        httpd.wsgi_app = app                 # type: ignore[attr-defined]
        httpd.error_log = _ErrorLog(self.log)  # type: ignore[attr-defined]
        self._httpd = httpd

        if background:
            self._thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            self._thread.start()
        return self

    def serve_forever(self) -> None:
        if self._httpd is None:
            self.start()
        assert self._httpd is not None
        self._httpd.serve_forever()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "Server":
        return self.start(background=True)

    def __exit__(self, *exc) -> None:
        self.stop()


class _ErrorLog:
    """Minimal file-like sink so wsgiref has somewhere to write."""

    def __init__(self, log: Optional[Callable[[str], None]]) -> None:
        self._log = log

    def write(self, text: str) -> None:
        if self._log and text.strip():
            self._log(text.rstrip())

    def flush(self) -> None:
        pass
