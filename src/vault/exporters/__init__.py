"""Getting everything back out.

The promise is that your data is never trapped, and a promise like that is
only worth anything if it is tested. ``tests/test_roundtrip.py`` exports to
JSONL, imports into an empty database and compares the two row by row; if
anything is lost, that test fails.

Six formats, for three different reasons:

* **jsonl** is canonical and lossless. It is what round-trips, and what a
  future version of Vault reads.
* **markdown**, **csv** and **html** are for leaving. Markdown opens in any
  editor and any notes app; CSV opens in a spreadsheet; the HTML export is a
  single self-contained file with a working search box that needs no server,
  no Python and no Vault.
* **ics** and **vcf** are for the calendar and address book you already use.
"""

from typing import Callable, Dict

from vault.exporters import calendars, csvfile, htmlfile, jsonl, markdown

FORMATS: Dict[str, Callable] = {
    "jsonl": jsonl.export,
    "md": markdown.export,
    "markdown": markdown.export,
    "csv": csvfile.export,
    "html": htmlfile.export,
    "ics": calendars.export_ics,
    "vcf": calendars.export_vcf,
    "vcard": calendars.export_vcf,
}

__all__ = ["FORMATS", "calendars", "csvfile", "htmlfile", "jsonl", "markdown"]
