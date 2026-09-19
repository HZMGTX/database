"""Connectors: reading databases that belong to other applications.

These are strictly read-only. The database opens the other application's database
with ``mode=ro`` and ``query_only``, which SQLite enforces itself rather than
trusting this code to behave. A bug here cannot corrupt a live bot's data,
because the connection it holds is physically incapable of writing.
"""

from db.connectors import vyrex

FORMATS = {"vyrex": vyrex.connect}

__all__ = ["FORMATS", "vyrex"]
