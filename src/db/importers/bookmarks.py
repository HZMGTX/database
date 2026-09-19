"""Netscape bookmark HTML -- what every browser exports.

Folder structure becomes nested tags, so a bookmarks bar with folders arrives
as ``bookmarks/work/reference`` rather than a flat pile. Duplicates collapse
on the normalised URL.
"""

from html.parser import HTMLParser
from pathlib import Path
from typing import Any, List

from db import model
from db.db import Database
from db.importers.batch import Batch, BatchResult

__all__ = ["load"]


class _Bookmarks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.folders: List[str] = []
        self.entries: List[dict] = []
        self._current: dict = {}
        self._in_anchor = False
        self._in_folder = False
        self._pending_folder = ""

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "a" and attributes.get("href"):
            self._in_anchor = True
            self._current = {"url": attributes["href"], "title": "",
                             "folders": list(self.folders),
                             "add_date": attributes.get("add_date", "")}
        elif tag == "h3":
            self._in_folder = True
            self._pending_folder = ""
        elif tag == "dl":
            if self._pending_folder:
                self.folders.append(self._pending_folder)
                self._pending_folder = ""

    def handle_endtag(self, tag):
        if tag == "a" and self._in_anchor:
            self._in_anchor = False
            if self._current.get("url", "").startswith(("http://", "https://")):
                self.entries.append(self._current)
            self._current = {}
        elif tag == "h3":
            self._in_folder = False
        elif tag == "dl" and self.folders:
            self.folders.pop()

    def handle_data(self, data):
        if self._in_anchor:
            self._current["title"] = (self._current.get("title", "") + data).strip()
        elif self._in_folder:
            self._pending_folder = (self._pending_folder + data).strip()


def load(db: Database, source: "str | Path", *, dry_run: bool = False,
         tags: List[str] = (), **_: Any) -> BatchResult:
    path = Path(source)
    parser = _Bookmarks()
    parser.feed(path.read_text("utf-8", errors="replace"))

    with Batch(db, source=str(path), format="bookmarks", dry_run=dry_run) as batch:
        for entry in parser.entries:
            title = entry["title"] or entry["url"]
            folder_tags = ["/".join(["bookmarks"] + [
                _slug(f) for f in entry["folders"] if f])] if entry["folders"] else ["bookmarks"]

            if dry_run:
                batch.record("created", None, entry["url"], title)
                continue
            try:
                url_norm, _host = model.normalize_url(entry["url"])
            except model.ValidationError:
                batch.record("skipped", None, entry["url"], title)
                continue

            existing = db.conn().execute(
                "SELECT i.uid FROM item_link l JOIN item i ON i.id=l.item_id "
                "WHERE l.url_norm=? AND l.live=1", (url_norm,)).fetchone()
            if existing:
                batch.record("skipped", existing[0], entry["url"], title)
                continue

            try:
                doc = model.create(db, kind="link", title=title,
                                   tags=list(tags) + folder_tags,
                                   facet={"url": entry["url"]})
                batch.record("created", doc["uid"], entry["url"], title)
            except Exception as exc:                        # noqa: BLE001
                batch.fail(f"{entry['url']}: {exc}")
                batch.record("skipped", None, entry["url"], title)

        return batch.result()


def _slug(text: str) -> str:
    import re
    cleaned = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[\s_]+", "-", cleaned).strip("-") or "folder"
