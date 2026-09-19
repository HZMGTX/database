"""iCalendar and vCard import.

Both formats unfold continuation lines before anything else -- a folded line
split mid-word is the classic way these get mangled -- and both keep values
they do not understand rather than dropping them, so nothing is lost on the
way in even where The database cannot act on it.
"""

import re
from pathlib import Path
from typing import Any, Dict, Iterator, List

from db import model
from db.db import Database
from db.importers.batch import Batch, BatchResult

__all__ = ["load_ics", "load_vcf"]

SUPPORTED_FREQ = {"DAILY", "WEEKLY", "MONTHLY", "YEARLY"}


def _unfold(text: str) -> List[str]:
    """Join continuation lines, which begin with a space or tab."""
    lines: List[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _unescape(value: str) -> str:
    return (value.replace("\\n", "\n").replace("\\N", "\n")
            .replace("\\,", ",").replace("\;", ";").replace("\\\\", "\\"))


def _blocks(lines: List[str], name: str) -> Iterator[List[tuple]]:
    current: List[tuple] = []
    inside = False
    for line in lines:
        if line.upper().startswith(f"BEGIN:{name}"):
            inside, current = True, []
            continue
        if line.upper().startswith(f"END:{name}"):
            if inside:
                yield current
            inside = False
            continue
        if inside and ":" in line:
            head, _, value = line.partition(":")
            key, _, params = head.partition(";")
            current.append((key.upper(), params, _unescape(value)))
    if inside and current:
        yield current


def _param(params: str, name: str) -> str:
    match = re.search(rf"{name}=([^;]+)", params, re.I)
    return match.group(1) if match else ""


def _to_local(value: str) -> "tuple[str, bool]":
    """Return (local timestamp, is_date)."""
    raw = value.strip().rstrip("Z")
    if len(raw) == 8:
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}", True
    if len(raw) >= 15:
        return (f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}T"
                f"{raw[9:11]}:{raw[11:13]}:{raw[13:15]}"), False
    return raw, False


def load_ics(db: Database, source: "str | Path", *, dry_run: bool = False,
             tags: List[str] = (), **_: Any) -> BatchResult:
    path = Path(source)
    lines = _unfold(path.read_text("utf-8", errors="replace"))

    with Batch(db, source=str(path), format="ics", dry_run=dry_run) as batch:
        for block in _blocks(lines, "VEVENT"):
            fields: Dict[str, Any] = {}
            props: Dict[str, Any] = {}
            event_tags = list(tags)

            for key, params, value in block:
                if key == "SUMMARY":
                    fields["title"] = value
                elif key == "DESCRIPTION":
                    fields["body"] = value
                elif key == "LOCATION":
                    fields["location"] = value
                elif key == "DTSTART":
                    local, is_date = _to_local(value)
                    fields["starts"] = local
                    fields["all_day"] = is_date or _param(params, "VALUE").upper() == "DATE"
                    fields["tzid"] = _param(params, "TZID") or None
                elif key == "DTEND":
                    fields["ends"] = _to_local(value)[0]
                elif key == "RRULE":
                    freq = _param(value.replace(";", ";FREQPARAM="), "FREQ") or ""
                    match = re.search(r"FREQ=([A-Z]+)", value.upper())
                    freq = match.group(1) if match else ""
                    fields["rrule"] = value
                    # Keep an unsupported rule verbatim and flag it rather
                    # than silently expanding it wrongly.
                    fields["rrule_supported"] = freq in SUPPORTED_FREQ
                    if freq not in SUPPORTED_FREQ:
                        props["rrule_unsupported"] = value
                elif key == "CATEGORIES":
                    event_tags.extend(t.strip() for t in value.split(",") if t.strip())
                elif key == "UID":
                    props["ics_uid"] = value

            if not fields.get("starts"):
                batch.record("skipped", None, "", fields.get("title", ""))
                continue
            title = fields.get("title") or "(untitled event)"
            if dry_run:
                batch.record("created", None, "", title)
                continue

            try:
                doc = model.create(
                    db, kind="event", title=title, body=fields.get("body", ""),
                    props=props, tags=event_tags,
                    facet={"starts": fields["starts"], "ends": fields.get("ends"),
                           "all_day": bool(fields.get("all_day")),
                           "location": fields.get("location", ""),
                           "rrule": fields.get("rrule"),
                           "rrule_supported": fields.get("rrule_supported", True)},
                    tzid=fields.get("tzid"))
                batch.record("created", doc["uid"], "", title)
            except Exception as exc:                        # noqa: BLE001
                batch.fail(f"{title}: {exc}")
                batch.record("skipped", None, "", title)

        return batch.result()


def load_vcf(db: Database, source: "str | Path", *, dry_run: bool = False,
             tags: List[str] = (), **_: Any) -> BatchResult:
    path = Path(source)
    lines = _unfold(path.read_text("utf-8", errors="replace"))

    with Batch(db, source=str(path), format="vcard", dry_run=dry_run) as batch:
        for block in _blocks(lines, "VCARD"):
            facet: Dict[str, Any] = {"emails": [], "phones": [], "handles": []}
            title = ""
            body = ""
            card_tags = list(tags)

            for key, params, value in block:
                if key == "FN":
                    title = value
                elif key == "N":
                    parts = value.split(";")
                    facet["family_name"] = parts[0] if parts else ""
                    facet["given_name"] = parts[1] if len(parts) > 1 else ""
                elif key == "ORG":
                    facet["org"] = value.split(";")[0]
                elif key == "TITLE":
                    facet["role"] = value
                elif key == "BDAY":
                    raw = value.replace("-", "")
                    if len(raw) >= 8:
                        facet["birthday"] = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
                elif key == "EMAIL":
                    facet["emails"].append(value)
                elif key == "TEL":
                    facet["phones"].append(value)
                elif key == "URL":
                    facet["handles"].append(value)
                elif key == "NOTE":
                    body = value
                elif key == "CATEGORIES":
                    card_tags.extend(t.strip() for t in value.split(",") if t.strip())

            if not title:
                title = " ".join(
                    filter(None, [facet.get("given_name"), facet.get("family_name")])).strip()
            if not title:
                batch.record("skipped", None, "", "(no name)")
                continue
            if dry_run:
                batch.record("created", None, "", title)
                continue

            try:
                doc = model.create(db, kind="person", title=title, body=body,
                                   tags=card_tags, facet=facet)
                batch.record("created", doc["uid"], "", title)
            except Exception as exc:                        # noqa: BLE001
                batch.fail(f"{title}: {exc}")
                batch.record("skipped", None, "", title)

        return batch.result()
