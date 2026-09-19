"""Vault — a self-contained personal database.

One SQLite file, three interfaces (CLI, REST API, web UI), zero third-party
dependencies.  Everything the user keeps is a row in ``item``, so search,
tagging, relationships, history, trash, undo, import and export are each
implemented once and are correct for every kind of thing -- including kinds
that do not exist yet.
"""

__all__ = ["__version__", "SCHEMA_VERSION"]

# Package version.  Bumped by hand.
__version__ = "0.1.0"

# The schema version this code knows how to speak.  ``migrate.py`` refuses to
# open a database whose ``user_version`` is higher than this, because a newer
# Vault may have written columns this code would silently drop.
SCHEMA_VERSION = 1
