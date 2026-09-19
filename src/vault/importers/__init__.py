"""Getting things in.

Every import is one recorded batch with a per-item ledger, so
``vault import undo <batch>`` reverses a thousand-file mistake in one
command.

That ledger is also what provides all-or-nothing semantics *without* wrapping
a bulk import in one enormous transaction. A single writer holding the WAL
lock for thirty seconds starves every other writer past its busy_timeout, and
a crash halfway through loses the lot. Chunked commits plus a ledger survive
both, and are recoverable after a crash in a way a giant transaction is not.
"""

from typing import Callable, Dict

from vault.importers import (bookmarks, calendars, csvfile, directory, jsonl,
                             markdown, repo)
from vault.importers.batch import Batch, BatchResult

FORMATS: Dict[str, Callable] = {
    "jsonl": jsonl.load,
    "md": markdown.load,
    "markdown": markdown.load,
    "csv": csvfile.load,
    "dir": directory.load,
    "directory": directory.load,
    "bookmarks": bookmarks.load,
    "ics": calendars.load_ics,
    "vcf": calendars.load_vcf,
    "vcard": calendars.load_vcf,
    "repo": repo.load,
}

__all__ = ["Batch", "BatchResult", "FORMATS"]
