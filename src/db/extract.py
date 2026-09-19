"""Pulling readable text out of files, using only the standard library.

Office documents and EPUBs are zip archives of XML, and ``zipfile`` plus
``xml.etree`` reads them without a single dependency. That covers the formats
people actually keep: .docx, .xlsx, .pptx, .epub, .html, .eml, and every
plain-text format.

PDF is the honest gap. Parsing it properly needs a real library, so The database
uses ``pdftotext`` when it happens to be on PATH and otherwise stores the
file, indexes its name and metadata, and records ``unsupported`` so
``db doctor`` can say how many files are in that state. It does not
pretend to have read them.
"""

import json
import re
import subprocess
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import NamedTuple, Optional

__all__ = ["Extracted", "extract_bytes", "extract_file", "have_pdftotext"]

MAX_TEXT = 2_000_000          # what gets indexed from any one file
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANK = re.compile(r"\n{3,}")

TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json", ".jsonl",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env", ".sql", ".sh",
    ".bash", ".zsh", ".fish", ".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx",
    ".go", ".rs", ".rb", ".php", ".java", ".kt", ".swift", ".c", ".h", ".cc",
    ".cpp", ".hpp", ".cs", ".lua", ".pl", ".r", ".jl", ".scala", ".clj", ".ex",
    ".exs", ".erl", ".hs", ".ml", ".vim", ".el", ".tex", ".bib", ".css", ".scss",
    ".less", ".svg", ".xml", ".gitignore", ".dockerfile", ".makefile", ".gradle",
}


class Extracted(NamedTuple):
    text: str
    status: str        # ok | empty | unsupported | failed | skipped
    note: str


def _tidy(text: str) -> str:
    text = text.replace("\x00", "")
    text = _WS.sub(" ", text)
    text = _BLANK.sub("\n\n", text)
    return text.strip()[:MAX_TEXT]


class _HTMLText(HTMLParser):
    """Visible text from HTML, with script and style dropped."""

    SKIP = {"script", "style", "noscript", "template", "head"}
    BREAK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
             "section", "article", "blockquote", "pre"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list = []
        self._skip_depth = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in self.BREAK:
            self.parts.append("\n")
        if tag == "img":
            alt = dict(attrs).get("alt")
            if alt:
                self.parts.append(f" {alt} ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False
        if tag in self.BREAK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
        self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts)


def _xml_text(blob: bytes) -> str:
    """All character data from an XML document, tags discarded."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(blob)
    except ET.ParseError:
        return ""
    return " ".join(t.strip() for t in root.itertext() if t and t.strip())


def _from_zip_xml(path: Path, members) -> str:
    out = []
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        for pattern in members:
            for name in sorted(names):
                if re.fullmatch(pattern, name):
                    try:
                        out.append(_xml_text(archive.read(name)))
                    except (KeyError, zipfile.BadZipFile):
                        continue
    return "\n".join(p for p in out if p)


def _docx(path: Path) -> str:
    return _from_zip_xml(path, [r"word/document\.xml", r"word/footnotes\.xml",
                                r"word/endnotes\.xml", r"word/header\d*\.xml",
                                r"word/footer\d*\.xml"])


def _pptx(path: Path) -> str:
    return _from_zip_xml(path, [r"ppt/slides/slide\d+\.xml",
                                r"ppt/notesSlides/notesSlide\d+\.xml"])


def _xlsx(path: Path) -> str:
    """Cell text from a workbook.

    Strings live in a shared table and cells reference them by index, so the
    table is read first and the sheets resolved against it.
    """
    import xml.etree.ElementTree as ET
    NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    out = []
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        shared: list = []
        if "xl/sharedStrings.xml" in names:
            try:
                root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                for si in root.findall(f"{NS}si"):
                    shared.append("".join(t.text or "" for t in si.iter(f"{NS}t")))
            except ET.ParseError:
                pass
        for name in sorted(n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)):
            try:
                root = ET.fromstring(archive.read(name))
            except ET.ParseError:
                continue
            for cell in root.iter(f"{NS}c"):
                value = cell.find(f"{NS}v")
                if value is None or value.text is None:
                    continue
                if cell.get("t") == "s":
                    index = int(value.text)
                    if 0 <= index < len(shared):
                        out.append(shared[index])
                else:
                    out.append(value.text)
    return " ".join(out)


def _epub(path: Path) -> str:
    out = []
    with zipfile.ZipFile(path) as archive:
        for name in sorted(archive.namelist()):
            if name.lower().endswith((".xhtml", ".html", ".htm")):
                parser = _HTMLText()
                try:
                    parser.feed(archive.read(name).decode("utf-8", "replace"))
                except Exception:
                    continue
                out.append(parser.text())
    return "\n".join(out)


def _eml(blob: bytes) -> str:
    from email import policy
    from email.parser import BytesParser

    message = BytesParser(policy=policy.default).parsebytes(blob)
    header_lines = [f"{h}: {message.get(h, '')}"
                    for h in ("From", "To", "Cc", "Subject", "Date") if message.get(h)]
    body = ""
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain":
                body += part.get_content()
            elif part.get_content_type() == "text/html" and not body:
                parser = _HTMLText()
                parser.feed(part.get_content())
                body += parser.text()
    else:
        try:
            body = message.get_content()
        except Exception:
            body = ""
    return "\n".join(header_lines) + "\n\n" + str(body)


def have_pdftotext() -> bool:
    import shutil as _shutil
    return _shutil.which("pdftotext") is not None


def _pdf(path: Path) -> Extracted:
    if not have_pdftotext():
        return Extracted("", "unsupported",
                         "PDF text needs pdftotext on PATH; the file is stored and "
                         "searchable by name, but its text is not indexed")
    try:
        result = subprocess.run(
            ["pdftotext", "-q", "-enc", "UTF-8", str(path), "-"],
            capture_output=True, timeout=60)
        text = result.stdout.decode("utf-8", "replace")
        return Extracted(_tidy(text), "ok" if text.strip() else "empty", "")
    except (subprocess.SubprocessError, OSError) as exc:
        return Extracted("", "failed", f"pdftotext failed: {exc}")


def extract_file(path: "str | Path", *, max_bytes: int = 64 * 1024 * 1024) -> Extracted:
    """Read *path* as text, however it is encoded."""
    path = Path(path)
    if not path.is_file():
        return Extracted("", "failed", "not a file")

    size = path.stat().st_size
    if size == 0:
        return Extracted("", "empty", "")
    if size > max_bytes:
        return Extracted("", "skipped", f"{size / 1e6:.0f} MB is over the extraction limit")

    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            return _pdf(path)
        if suffix == ".docx":
            return Extracted(_tidy(_docx(path)), "ok", "")
        if suffix == ".pptx":
            return Extracted(_tidy(_pptx(path)), "ok", "")
        if suffix == ".xlsx":
            return Extracted(_tidy(_xlsx(path)), "ok", "")
        if suffix == ".epub":
            return Extracted(_tidy(_epub(path)), "ok", "")
        if suffix in (".eml", ".mbox"):
            return Extracted(_tidy(_eml(path.read_bytes())), "ok", "")
        if suffix in (".html", ".htm", ".xhtml"):
            parser = _HTMLText()
            parser.feed(path.read_text("utf-8", errors="replace"))
            return Extracted(_tidy(parser.text()), "ok", "")

        blob = path.read_bytes()
        return extract_bytes(blob, filename=path.name)
    except zipfile.BadZipFile:
        return Extracted("", "failed", "not a readable archive")
    except (OSError, UnicodeError) as exc:
        return Extracted("", "failed", str(exc))


def extract_bytes(blob: bytes, *, filename: str = "") -> Extracted:
    """Read raw bytes as text when they plausibly are text."""
    if not blob:
        return Extracted("", "empty", "")

    suffix = Path(filename).suffix.lower()

    # A NUL byte in the first block is the oldest and most reliable binary
    # test there is.
    if b"\x00" in blob[:8192] and suffix not in TEXT_SUFFIXES:
        return Extracted("", "unsupported", "looks like binary")

    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            text = blob.decode(encoding)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        return Extracted("", "unsupported", "no usable text encoding")

    if suffix in (".json", ".jsonl"):
        try:
            parsed = json.loads(text)
            text = json.dumps(parsed, indent=1, ensure_ascii=False)
        except json.JSONDecodeError:
            pass

    tidied = _tidy(text)
    return Extracted(tidied, "ok" if tidied else "empty", "")
