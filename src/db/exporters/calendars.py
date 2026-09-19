"""iCalendar and vCard, so events and people reach the apps you already use.

Both formats fold long lines at 75 octets and escape a small set of
characters. Getting either wrong produces a file that imports with silent
damage rather than an error, so both are done properly here.
"""

from typing import Any, Optional, TextIO

from db import __version__, dates
from db.db import Database
from db.exporters.jsonl import iter_records

__all__ = ["export_ics", "export_vcf"]


def _fold(line: str) -> str:
    """RFC 5545 line folding: 75 octets, continuations start with a space."""
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line
    chunks, current = [], b""
    for char in line:
        raw = char.encode("utf-8")
        limit = 75 if not chunks else 74
        if len(current) + len(raw) > limit:
            chunks.append(current)
            current = b""
        current += raw
    chunks.append(current)
    return "\r\n ".join(chunk.decode("utf-8") for chunk in chunks)


def _escape(text: str) -> str:
    return (str(text or "").replace("\\", "\\\\").replace(";", "\;")
            .replace(",", "\\,").replace("\n", "\\n"))


def export_ics(db: Database, out: TextIO, *, query: Optional[str] = None, **_: Any) -> int:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0",
             f"PRODID:-//The database//{__version__}//EN", "CALSCALE:GREGORIAN"]
    count = 0

    for doc in iter_records(db, query=query):
        if doc["kind"] != "event":
            continue
        facet = doc.get("facet") or {}
        starts = facet.get("starts_local")
        if not starts:
            continue

        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{doc['uid']}@hzdb.local")
        lines.append("DTSTAMP:" + dates.utcnow().replace("-", "").replace(":", ""))
        if facet.get("all_day"):
            # A floating date, deliberately with no timezone: an all-day
            # event must not shift for anyone who travels.
            lines.append("DTSTART;VALUE=DATE:" + starts.replace("-", "")[:8])
            if facet.get("ends_local"):
                lines.append("DTEND;VALUE=DATE:" + facet["ends_local"].replace("-", "")[:8])
        else:
            tzid = facet.get("tzid") or "UTC"
            stamp = starts.replace("-", "").replace(":", "")
            lines.append(f"DTSTART;TZID={tzid}:{stamp}")
            if facet.get("ends_local"):
                lines.append(f"DTEND;TZID={tzid}:"
                             + facet["ends_local"].replace("-", "").replace(":", ""))
        lines.append("SUMMARY:" + _escape(doc.get("title", "")))
        if doc.get("body"):
            lines.append("DESCRIPTION:" + _escape(doc["body"]))
        if facet.get("location"):
            lines.append("LOCATION:" + _escape(facet["location"]))
        if facet.get("rrule"):
            lines.append("RRULE:" + facet["rrule"])
        for tag in doc.get("tags") or []:
            lines.append("CATEGORIES:" + _escape(tag))
        lines.append("END:VEVENT")
        count += 1

    lines.append("END:VCALENDAR")
    out.write("\r\n".join(_fold(line) for line in lines) + "\r\n")
    return count


def export_vcf(db: Database, out: TextIO, *, query: Optional[str] = None, **_: Any) -> int:
    conn = db.conn()
    count = 0
    lines = []

    for doc in iter_records(db, query=query):
        if doc["kind"] != "person":
            continue
        facet = doc.get("facet") or {}
        lines.extend(["BEGIN:VCARD", "VERSION:3.0"])
        lines.append("FN:" + _escape(doc.get("title", "")))
        lines.append("N:" + _escape(facet.get("family_name", "")) + ";"
                     + _escape(facet.get("given_name", "")) + ";;;")
        if facet.get("org"):
            lines.append("ORG:" + _escape(facet["org"]))
        if facet.get("role"):
            lines.append("TITLE:" + _escape(facet["role"]))
        if facet.get("birthday"):
            lines.append("BDAY:" + facet["birthday"].replace("-", ""))

        for ident in doc.get("identities") or []:
            if ident["channel"] == "email":
                lines.append("EMAIL;TYPE=INTERNET:" + _escape(ident["value"]))
            elif ident["channel"] == "phone":
                lines.append("TEL:" + _escape(ident["value"]))
            elif ident["channel"] == "url":
                lines.append("URL:" + _escape(ident["value"]))

        if doc.get("body"):
            lines.append("NOTE:" + _escape(doc["body"]))
        for tag in doc.get("tags") or []:
            lines.append("CATEGORIES:" + _escape(tag))
        lines.append("UID:" + doc["uid"])
        lines.append("END:VCARD")
        count += 1

    out.write("\r\n".join(_fold(line) for line in lines) + ("\r\n" if lines else ""))
    return count
