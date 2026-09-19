-- Vault core schema, version 1.
--
-- Conventions used throughout:
--
--   * STRICT everywhere, so a column typed INTEGER cannot quietly hold 'abc'.
--   * Timestamps are TEXT, UTC, 'YYYY-MM-DDTHH:MM:SSZ', shape-checked by a
--     fully anchored GLOB.  Semantic validation (is month 13 real?) happens in
--     Python; the CHECK is a shape guard, and an anchored one -- in GLOB, '*'
--     matches any sequence, so a pattern like '[0-9]*' validates exactly one
--     character and then permits anything at all.
--   * Hex identifiers are validated as `length(x)=32 AND x NOT GLOB
--     '*[^0-9a-f]*'` -- "32 long and containing nothing but lowercase hex".
--   * No CHECK contains a subquery: SQLite prohibits them outright
--     ("subqueries prohibited in CHECK constraints").  Constraints that need
--     one live on the single write path in model.py instead.

-- ===========================================================================
-- Instance metadata
-- ===========================================================================

CREATE TABLE app_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
) STRICT, WITHOUT ROWID;

-- ===========================================================================
-- Type registry
--
-- A kind is just a row.  Inventing 'recipe' or 'invoice' is one INSERT, and
-- the new kind immediately gets search, tagging, relationships, history,
-- import and export, because those are implemented against `item` and know
-- nothing about specific kinds.
-- ===========================================================================

CREATE TABLE kind (
  name       TEXT PRIMARY KEY
             CHECK (length(name) BETWEEN 1 AND 40
                    AND name NOT GLOB '*[^a-z0-9_]*'),
  label      TEXT NOT NULL,
  plural     TEXT NOT NULL,
  icon       TEXT NOT NULL DEFAULT 'dot',
  builtin    INTEGER NOT NULL DEFAULT 0 CHECK (builtin IN (0, 1)),
  -- A facet kind has a companion table enforcing extra invariants.  Facet
  -- tables cannot be created for user-invented kinds, so this stays 0 there.
  has_facet  INTEGER NOT NULL DEFAULT 0 CHECK (has_facet IN (0, 1)),
  sort_order INTEGER NOT NULL DEFAULT 100,
  created_at TEXT NOT NULL
             CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
) STRICT;

-- Declares every legal property key for a kind.  One registry drives write
-- validation, the web UI's forms, CSV export headers and the query language's
-- field completion -- so they cannot disagree about what a field is.
CREATE TABLE kind_field (
  kind       TEXT NOT NULL REFERENCES kind(name) ON DELETE CASCADE,
  key        TEXT NOT NULL
             CHECK (length(key) BETWEEN 1 AND 60
                    AND key NOT GLOB '*[^a-z0-9_]*'),
  label      TEXT NOT NULL,
  type       TEXT NOT NULL
             CHECK (type IN ('text','number','bool','date','datetime','enum','url','email','json')),
  required   INTEGER NOT NULL DEFAULT 0 CHECK (required IN (0, 1)),
  multi      INTEGER NOT NULL DEFAULT 0 CHECK (multi IN (0, 1)),
  enum_values TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(enum_values)),
  default_value TEXT,
  hint       TEXT NOT NULL DEFAULT '',
  sort_order INTEGER NOT NULL DEFAULT 100,
  PRIMARY KEY (kind, key)
) STRICT;

-- ===========================================================================
-- The universal spine
--
-- Every note, task, event, file, link, person and user-invented thing is one
-- row here.  That single id space is what lets tagging, relationships,
-- history, trash, undo, search, import and export each be written once.
-- ===========================================================================

CREATE TABLE item (
  id     INTEGER PRIMARY KEY,
  uid    TEXT NOT NULL UNIQUE
         CHECK (length(uid) = 32 AND uid NOT GLOB '*[^0-9a-f]*'),
  kind   TEXT NOT NULL REFERENCES kind(name) ON DELETE RESTRICT,

  title  TEXT NOT NULL DEFAULT '',
  body   TEXT NOT NULL DEFAULT '',

  -- Free-form properties for anything not worth a facet column.  Validated
  -- against kind_field on write and projected into `attr` by trigger, so an
  -- invented field is range-queryable without a migration.
  props  TEXT NOT NULL DEFAULT '{}'
         CHECK (json_valid(props) AND json_type(props) = 'object'),

  -- Denormalised for search and for rendering a list without a join.
  -- Maintained by the item_tag triggers with a deterministic ORDER BY:
  -- group_concat ordering is plan-dependent, and comparing unordered
  -- concatenations makes `doctor` report corruption that is not there.
  tags_cache TEXT NOT NULL DEFAULT '',

  -- URLs, hostnames, email addresses and filenames, extracted on write.
  -- Keeping them in their own column is why the tokenizer does not need to
  -- treat '.' and '-' as word characters -- which would break ordinary prose
  -- search, because 'budget.' at the end of a sentence would stop matching
  -- the query 'budget'.
  search_extra TEXT NOT NULL DEFAULT '',

  -- Bumped for ANY change to the composed document, tags and edges included.
  -- The HTTP ETag is derived from it, so a tag edit must move it or a client
  -- can overwrite a concurrent change while holding a stale-but-matching tag.
  rev        INTEGER NOT NULL DEFAULT 1 CHECK (rev >= 1),
  pinned     INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),

  created_at TEXT NOT NULL
             CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
  updated_at TEXT NOT NULL
             CHECK (updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
  -- Soft delete.  NULL means live.  Trashed rows stay in the FTS index and
  -- are filtered at query time; excluding them would put the index
  -- permanently at odds with its content table and make FTS5's own
  -- integrity-check report 'database disk image is malformed'.
  deleted_at TEXT
             CHECK (deleted_at IS NULL OR deleted_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),

  -- The target of every facet table's composite foreign key.  This is what
  -- makes "an event facet may only hang off an item whose kind is 'event'"
  -- a database guarantee rather than an application convention, and what
  -- makes flipping an item's kind out from under its facet impossible.
  UNIQUE (id, kind)
) STRICT;

CREATE INDEX item_kind_updated ON item(kind, updated_at DESC) WHERE deleted_at IS NULL;
CREATE INDEX item_updated      ON item(updated_at DESC)       WHERE deleted_at IS NULL;
CREATE INDEX item_created      ON item(created_at DESC)       WHERE deleted_at IS NULL;
CREATE INDEX item_deleted      ON item(deleted_at)            WHERE deleted_at IS NOT NULL;
CREATE INDEX item_pinned       ON item(updated_at DESC)       WHERE pinned = 1 AND deleted_at IS NULL;
-- Reference resolution by title prefix, as-you-type suggestions and duplicate
-- detection all filter on title; without this every one of them is a scan.
CREATE INDEX item_title        ON item(title)                 WHERE deleted_at IS NULL;
-- Short handles are the TAIL of the uid, because the head is a timestamp and
-- barely varies -- 2,000 ids generated in a burst shared one 8-character
-- prefix.  This index is what makes `vault show 3f8a2b1c` a seek.
CREATE INDEX item_uid_suffix   ON item(substr(uid, -8));

-- ===========================================================================
-- Facets
--
-- The five built-in kinds with genuine invariants get a companion table.  The
-- composite foreign key (item_id, kind) -> item(id, kind) is the whole point:
-- it makes `starts_local NOT NULL` a real guarantee reachable from the CLI,
-- the API, the web UI or hand-typed SQL, and it makes changing an item's kind
-- out from under its facet impossible.  Verified: attaching an event facet to
-- a note fails with FOREIGN KEY constraint failed, and so does the kind flip.
-- ===========================================================================

CREATE TABLE item_task (
  item_id   INTEGER PRIMARY KEY,
  kind      TEXT NOT NULL CHECK (kind = 'task'),
  status    TEXT NOT NULL DEFAULT 'todo'
            CHECK (status IN ('todo','doing','blocked','done','cancelled')),
  priority  INTEGER NOT NULL DEFAULT 0 CHECK (priority BETWEEN 0 AND 5),

  -- Due is stored the way events are: local wall time, the zone it was meant
  -- in, and a derived UTC epoch computed in Python via zoneinfo.  A generated
  -- `unixepoch(due_at)` column would silently read the value as UTC, so a
  -- task due today at 18:00 in Los Angeles would sort and filter as overdue.
  due_local TEXT
            CHECK (due_local IS NULL OR
                   due_local GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]'),
  due_tzid  TEXT,
  due_epoch INTEGER,
  -- A date-only due means end of day in due_tzid; Python resolves that before
  -- it gets here, so due_epoch is always an instant.
  due_is_date INTEGER NOT NULL DEFAULT 0 CHECK (due_is_date IN (0, 1)),

  estimate_minutes INTEGER CHECK (estimate_minutes IS NULL OR estimate_minutes >= 0),
  completed_at TEXT
            CHECK (completed_at IS NULL OR
                   completed_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),

  CHECK (due_local IS NULL OR (due_tzid IS NOT NULL AND due_epoch IS NOT NULL)),
  CHECK ((status = 'done') = (completed_at IS NOT NULL)),
  FOREIGN KEY (item_id, kind) REFERENCES item(id, kind) ON DELETE CASCADE
) STRICT;

CREATE INDEX task_due      ON item_task(due_epoch) WHERE due_epoch IS NOT NULL AND status NOT IN ('done','cancelled');
CREATE INDEX task_status   ON item_task(status, priority DESC);

CREATE TABLE item_event (
  item_id      INTEGER PRIMARY KEY,
  kind         TEXT NOT NULL CHECK (kind = 'event'),

  -- Timed events store local wall time plus the IANA zone they were meant in,
  -- plus a UTC epoch derived from both.  All-day events are floating dates
  -- and are NEVER converted: forcing one through a timezone renders
  -- 2026-09-19 in Auckland as 2026-09-18 elsewhere.  Hence the nullable
  -- epoch, guarded so only all-day rows may omit it.
  all_day      INTEGER NOT NULL DEFAULT 0 CHECK (all_day IN (0, 1)),
  starts_local TEXT NOT NULL,
  starts_epoch INTEGER,
  ends_local   TEXT,
  ends_epoch   INTEGER,
  tzid         TEXT,

  location     TEXT NOT NULL DEFAULT '',
  -- Documented RRULE subset; anything richer is stored verbatim and flagged
  -- rather than silently mis-expanded.
  rrule        TEXT,
  rrule_supported INTEGER NOT NULL DEFAULT 1 CHECK (rrule_supported IN (0, 1)),
  exdates      TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(exdates)),

  CHECK (all_day = 1 OR starts_epoch IS NOT NULL),
  CHECK (all_day = 1 OR tzid IS NOT NULL),
  CHECK (ends_epoch IS NULL OR starts_epoch IS NULL OR ends_epoch >= starts_epoch),
  CHECK (ends_local IS NULL OR ends_local >= starts_local),
  FOREIGN KEY (item_id, kind) REFERENCES item(id, kind) ON DELETE CASCADE
) STRICT;

CREATE INDEX event_starts     ON item_event(starts_epoch) WHERE starts_epoch IS NOT NULL;
CREATE INDEX event_starts_day ON item_event(starts_local);
CREATE INDEX event_recurring  ON item_event(item_id) WHERE rrule IS NOT NULL;

-- Bytes live on disk under files/ab/cd/<sha256>; this table is the ledger.
-- Content-addressed, so the same attachment added twice is stored once.
CREATE TABLE blob (
  id          INTEGER PRIMARY KEY,
  sha256      TEXT NOT NULL UNIQUE
              CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
  size_bytes  INTEGER NOT NULL CHECK (size_bytes >= 0),
  media_type  TEXT NOT NULL DEFAULT 'application/octet-stream',
  suffix      TEXT NOT NULL DEFAULT '',
  -- Explicit refcount, maintained by triggers on every column that can point
  -- here -- including the ones populated by UPDATE rather than INSERT, which
  -- is how an archived page body arrives after a link is created.
  refcount    INTEGER NOT NULL DEFAULT 0 CHECK (refcount >= 0),
  extract_status TEXT NOT NULL DEFAULT 'pending'
              CHECK (extract_status IN ('pending','ok','empty','unsupported','failed','skipped')),
  extract_note TEXT NOT NULL DEFAULT '',
  extracted_text TEXT,
  created_at  TEXT NOT NULL
              CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
) STRICT;

CREATE INDEX blob_unreferenced ON blob(id) WHERE refcount = 0;
CREATE INDEX blob_pending      ON blob(id) WHERE extract_status = 'pending';

CREATE TABLE item_file (
  item_id    INTEGER PRIMARY KEY,
  kind       TEXT NOT NULL CHECK (kind = 'file'),
  -- RESTRICT, not CASCADE: bytes a revision still references must not vanish
  -- because the current item stopped pointing at them.
  blob_id    INTEGER NOT NULL REFERENCES blob(id) ON DELETE RESTRICT,
  filename   TEXT NOT NULL,
  -- Where it came from, when it was imported from a folder or a repo.
  source_path TEXT NOT NULL DEFAULT '',
  -- Set when a file tripped the secret scan: it is indexed by path and
  -- metadata only and its contents are deliberately not stored.
  content_withheld INTEGER NOT NULL DEFAULT 0 CHECK (content_withheld IN (0, 1)),
  withheld_reason  TEXT NOT NULL DEFAULT '',
  FOREIGN KEY (item_id, kind) REFERENCES item(id, kind) ON DELETE CASCADE
) STRICT;

CREATE INDEX file_blob     ON item_file(blob_id);
CREATE INDEX file_withheld ON item_file(item_id) WHERE content_withheld = 1;

CREATE TABLE item_link (
  item_id   INTEGER PRIMARY KEY,
  kind      TEXT NOT NULL CHECK (kind = 'link'),
  url       TEXT NOT NULL,
  -- Scheme and host lowercased, default port dropped, trailing slash and
  -- tracking parameters stripped.  This is what "is this bookmark a
  -- duplicate?" compares.
  url_norm  TEXT NOT NULL,
  host      TEXT NOT NULL DEFAULT '',
  -- Denormalised liveness, maintained by a trigger on item.deleted_at.
  -- Uniqueness has to apply only to live rows: a soft delete leaves this row
  -- in place, so a global UNIQUE(url_norm) makes re-adding a trashed
  -- bookmark fail with IntegrityError.  A partial index cannot subquery
  -- another table, which is why the flag is denormalised here.
  live      INTEGER NOT NULL DEFAULT 1 CHECK (live IN (0, 1)),
  fetched_at TEXT,
  archive_blob_id INTEGER REFERENCES blob(id) ON DELETE RESTRICT,
  FOREIGN KEY (item_id, kind) REFERENCES item(id, kind) ON DELETE CASCADE
) STRICT;

CREATE UNIQUE INDEX link_url_norm_live ON item_link(url_norm) WHERE live = 1;
CREATE INDEX link_host    ON item_link(host);
CREATE INDEX link_archive ON item_link(archive_blob_id) WHERE archive_blob_id IS NOT NULL;

CREATE TABLE item_person (
  item_id   INTEGER PRIMARY KEY,
  kind      TEXT NOT NULL CHECK (kind = 'person'),
  given_name  TEXT NOT NULL DEFAULT '',
  family_name TEXT NOT NULL DEFAULT '',
  org       TEXT NOT NULL DEFAULT '',
  role      TEXT NOT NULL DEFAULT '',
  birthday  TEXT
            CHECK (birthday IS NULL OR birthday GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  FOREIGN KEY (item_id, kind) REFERENCES item(id, kind) ON DELETE CASCADE
) STRICT;

CREATE INDEX person_org ON item_person(org) WHERE org <> '';

-- People have several emails and phone numbers.  A single generated column
-- cannot answer "whose number is this?"; this table can, and indexes it.
CREATE TABLE person_identity (
  id        INTEGER PRIMARY KEY,
  item_id   INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
  channel   TEXT NOT NULL CHECK (channel IN ('email','phone','handle','url')),
  label     TEXT NOT NULL DEFAULT '',
  value     TEXT NOT NULL,
  -- Lowercased email, digits-only phone: what duplicate detection compares.
  value_norm TEXT NOT NULL,
  is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
  UNIQUE (item_id, channel, value_norm)
) STRICT;

CREATE INDEX identity_lookup ON person_identity(channel, value_norm);

-- ===========================================================================
-- Typed projection of props
--
-- props is JSON, which SQLite can query but not index usefully.  These two
-- tables are a trigger-maintained projection of it, with partial indexes per
-- value type.  That is what makes a field the user invented on Tuesday
-- range-queryable on Wednesday without a migration.
-- ===========================================================================

CREATE TABLE attr (
  item_id INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
  key     TEXT NOT NULL,
  vtext   TEXT,
  vnum    REAL,
  PRIMARY KEY (item_id, key)
) STRICT, WITHOUT ROWID;

CREATE INDEX attr_text ON attr(key, vtext) WHERE vtext IS NOT NULL;
CREATE INDEX attr_num  ON attr(key, vnum)  WHERE vnum IS NOT NULL;

-- Array-valued properties exploded one row per element.  Without this a JSON
-- array projects as a single key-only row and `authors:ada` never matches.
CREATE TABLE attr_multi (
  item_id INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
  key     TEXT NOT NULL,
  ord     INTEGER NOT NULL,
  vtext   TEXT,
  vnum    REAL,
  PRIMARY KEY (item_id, key, ord)
) STRICT, WITHOUT ROWID;

CREATE INDEX attr_multi_text ON attr_multi(key, vtext) WHERE vtext IS NOT NULL;
CREATE INDEX attr_multi_num  ON attr_multi(key, vnum)  WHERE vnum IS NOT NULL;

-- Rebuild both projections whenever props changes.  Both statements appear in
-- both triggers: the DELETE in the UPDATE trigger is what stops a shrinking
-- array from leaving orphaned elements behind.
CREATE TRIGGER attr_ai AFTER INSERT ON item BEGIN
  INSERT INTO attr(item_id, key, vtext, vnum)
    SELECT new.id, j.key,
           CASE WHEN j.type IN ('text') THEN j.value END,
           CASE WHEN j.type IN ('integer','real') THEN j.value END
      FROM json_each(new.props) j
     WHERE j.type NOT IN ('object','array');
  INSERT INTO attr_multi(item_id, key, ord, vtext, vnum)
    SELECT new.id, j.key, e.key,
           CASE WHEN e.type IN ('text') THEN e.value END,
           CASE WHEN e.type IN ('integer','real') THEN e.value END
      FROM json_each(new.props) j, json_each(j.value) e
     WHERE j.type = 'array' AND e.type NOT IN ('object','array');
END;

CREATE TRIGGER attr_au AFTER UPDATE OF props ON item
WHEN old.props IS NOT new.props BEGIN
  DELETE FROM attr       WHERE item_id = new.id;
  DELETE FROM attr_multi WHERE item_id = new.id;
  INSERT INTO attr(item_id, key, vtext, vnum)
    SELECT new.id, j.key,
           CASE WHEN j.type IN ('text') THEN j.value END,
           CASE WHEN j.type IN ('integer','real') THEN j.value END
      FROM json_each(new.props) j
     WHERE j.type NOT IN ('object','array');
  INSERT INTO attr_multi(item_id, key, ord, vtext, vnum)
    SELECT new.id, j.key, e.key,
           CASE WHEN e.type IN ('text') THEN e.value END,
           CASE WHEN e.type IN ('integer','real') THEN e.value END
      FROM json_each(new.props) j, json_each(j.value) e
     WHERE j.type = 'array' AND e.type NOT IN ('object','array');
END;

-- ===========================================================================
-- Tags
--
-- Hierarchical, encoded as a slash path.  The path is the ONLY representation
-- of parenthood: carrying both a path and a parent pointer gives two sources
-- of truth that nothing forces to agree, and a rename updates one of them.
-- With the path authoritative, renaming a subtree is one prefix rewrite.
-- ===========================================================================

CREATE TABLE tag (
  id    INTEGER PRIMARY KEY,
  slug  TEXT NOT NULL UNIQUE
        CHECK (length(slug) BETWEEN 1 AND 200
               AND slug NOT GLOB '*[^a-z0-9/_-]*'
               AND slug NOT GLOB '/*' AND slug NOT GLOB '*/'
               AND slug NOT GLOB '*//*'),
  label TEXT NOT NULL,
  color TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
        CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
) STRICT;

CREATE TABLE item_tag (
  item_id INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
  tag_id  INTEGER NOT NULL REFERENCES tag(id)  ON DELETE CASCADE,
  PRIMARY KEY (item_id, tag_id)
) STRICT, WITHOUT ROWID;

CREATE INDEX item_tag_by_tag ON item_tag(tag_id, item_id);

-- tags_cache is rebuilt with an explicit ORDER BY.  group_concat's order
-- follows the query plan, which changes with ANALYZE or a SQLite upgrade, and
-- `doctor` compares this string -- an unordered one reports phantom drift.
CREATE TRIGGER item_tag_ai AFTER INSERT ON item_tag BEGIN
  UPDATE item SET tags_cache = (
    SELECT coalesce(group_concat(slug, ' '), '')
      FROM (SELECT t.slug FROM item_tag it JOIN tag t ON t.id = it.tag_id
             WHERE it.item_id = new.item_id ORDER BY t.slug)
  ) WHERE id = new.item_id;
END;

CREATE TRIGGER item_tag_ad AFTER DELETE ON item_tag BEGIN
  UPDATE item SET tags_cache = (
    SELECT coalesce(group_concat(slug, ' '), '')
      FROM (SELECT t.slug FROM item_tag it JOIN tag t ON t.id = it.tag_id
             WHERE it.item_id = old.item_id ORDER BY t.slug)
  ) WHERE id = old.item_id;
END;

-- Renaming a tag has to refresh every item carrying it.
CREATE TRIGGER tag_slug_au AFTER UPDATE OF slug ON tag
WHEN old.slug IS NOT new.slug BEGIN
  UPDATE item SET tags_cache = (
    SELECT coalesce(group_concat(slug, ' '), '')
      FROM (SELECT t.slug FROM item_tag it JOIN tag t ON t.id = it.tag_id
             WHERE it.item_id = item.id ORDER BY t.slug)
  ) WHERE id IN (SELECT item_id FROM item_tag WHERE tag_id = new.id);
END;

-- ===========================================================================
-- Relationships
--
-- A controlled vocabulary of verbs with inverses, so a backlink renders as
-- correct English ("attends" / "attended by") rather than an arrow.
-- ===========================================================================

CREATE TABLE rel (
  name       TEXT PRIMARY KEY
             CHECK (length(name) BETWEEN 1 AND 40 AND name NOT GLOB '*[^a-z0-9_]*'),
  label      TEXT NOT NULL,
  inverse    TEXT NOT NULL,
  inverse_label TEXT NOT NULL,
  symmetric  INTEGER NOT NULL DEFAULT 0 CHECK (symmetric IN (0, 1)),
  builtin    INTEGER NOT NULL DEFAULT 0 CHECK (builtin IN (0, 1))
) STRICT;

-- The reverse index IS the backlink index, so a backlink cannot go stale:
-- there is only one row and it is read from both ends.
CREATE TABLE edge (
  id     INTEGER PRIMARY KEY,
  src_id INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
  rel    TEXT NOT NULL REFERENCES rel(name) ON DELETE RESTRICT,
  dst_id INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL
         CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
  CHECK (src_id <> dst_id),
  UNIQUE (src_id, rel, dst_id)
) STRICT;

CREATE INDEX edge_in  ON edge(dst_id, rel, src_id);
CREATE INDEX edge_rel ON edge(rel);

-- ===========================================================================
-- History
--
-- Two layers answering two different questions.
--
--   change_log  -- "what happened, and can I take it back?"  Append-only,
--                  transaction-scoped, and it OUTLIVES the rows it describes,
--                  which is what lets `vault undo` reverse a bulk retag and
--                  lets a purge still be explainable afterwards.
--   revision    -- "what did this look like on Tuesday?"  A compressed
--                  snapshot of the whole composed document.
--
-- change_log rows are written from model.py inside the same transaction as
-- the mutation, NOT by triggers.  SQLite refuses to create a trigger that
-- reads transaction context from a temp table, and the user-defined-function
-- alternative is foreclosed by trusted_schema=OFF.
-- ===========================================================================

CREATE TABLE change_log (
  seq        INTEGER PRIMARY KEY,
  txn_id     TEXT NOT NULL
             CHECK (length(txn_id) = 32 AND txn_id NOT GLOB '*[^0-9a-f]*'),
  at         TEXT NOT NULL
             CHECK (at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
  actor      TEXT NOT NULL,
  op         TEXT NOT NULL
             CHECK (op IN ('create','update','trash','restore','purge','tag','untag',
                           'link','unlink','merge','import','revert')),
  -- Deliberately NOT a foreign key.  An audit ledger must have no referential
  -- dependency on the live tables it audits: with ON DELETE SET NULL plus an
  -- AFTER DELETE trigger, SQLite runs the FK action first and the delete dies
  -- with "FOREIGN KEY constraint failed".  Verified.
  item_id    INTEGER,
  -- Denormalised so the trail still identifies the item after a hard purge.
  row_uid    TEXT,
  -- A diff, not two full copies.  Storing before+after put roughly 2.6 copies
  -- of every body in the database with no expiry.
  patch_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(patch_json)),
  note       TEXT NOT NULL DEFAULT ''
) STRICT;

CREATE INDEX cl_txn  ON change_log(txn_id, seq);
CREATE INDEX cl_item ON change_log(item_id, seq DESC) WHERE item_id IS NOT NULL;
CREATE INDEX cl_at   ON change_log(at);

CREATE TABLE revision (
  id       INTEGER PRIMARY KEY,
  item_id  INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
  rev      INTEGER NOT NULL CHECK (rev >= 1),
  at       TEXT NOT NULL
           CHECK (at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
  actor    TEXT NOT NULL,
  txn_id   TEXT NOT NULL,
  -- zlib-compressed JSON of the composed document: item + facet + tags +
  -- edges (both directions) + blob digest.
  doc_z    BLOB NOT NULL,
  doc_sha  TEXT NOT NULL,
  UNIQUE (item_id, rev)
) STRICT;

CREATE INDEX rev_item ON revision(item_id, rev DESC);
CREATE INDEX rev_at   ON revision(at);

-- Survives a hard purge and a merge, holding the last known snapshot plus a
-- redirect.  redirect_to is a uid, not a foreign key to item(id): with an FK
-- and ON DELETE SET NULL, merging A into C after B was merged into A nulls
-- B's redirect and the promised permanent 301 becomes a 404.  Resolution
-- follows the chain with a depth-capped recursive CTE instead.
CREATE TABLE tombstone (
  uid         TEXT PRIMARY KEY
              CHECK (length(uid) = 32 AND uid NOT GLOB '*[^0-9a-f]*'),
  kind        TEXT NOT NULL,
  title       TEXT NOT NULL DEFAULT '',
  reason      TEXT NOT NULL CHECK (reason IN ('purge','merge')),
  redirect_to_uid TEXT,
  at          TEXT NOT NULL
              CHECK (at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
  doc_z       BLOB
) STRICT, WITHOUT ROWID;

CREATE INDEX tomb_redirect ON tombstone(redirect_to_uid) WHERE redirect_to_uid IS NOT NULL;

-- ===========================================================================
-- Search
--
-- One ranked word index and one trigram index, both external-content over
-- `item`.  External content rather than contentless because snippet() and
-- highlight() return real text only on external-content tables, and a result
-- list you can scan is the difference between search that gets used and
-- search that gets abandoned.
--
-- Two decisions worth stating, both settled by testing this SQLite build:
--
--  1. tokenchars is '_@' and NOT '_@.-'.  Making '.' and '-' word characters
--     breaks the commonest case in prose: with them, the body "Please review
--     the budget." returns ZERO hits for the query `budget`, because the
--     trailing period is part of the token.  Same for both halves of any
--     hyphenated word.  URLs, hosts, emails and filenames are already served
--     by the search_extra column and by the trigram index.
--
--  2. Soft-deleted rows ARE indexed, and filtered at query time.  Excluding
--     them puts the index permanently at odds with its content table, and
--     FTS5's own content-aware integrity check then reports "database disk
--     image is malformed" on a perfectly healthy database.
-- ===========================================================================

CREATE VIRTUAL TABLE item_fts USING fts5(
  title, body, tags_cache, search_extra,
  content = 'item',
  content_rowid = 'id',
  tokenize = "unicode61 remove_diacritics 2 tokenchars '_@'",
  prefix = '2 3 4'
);

CREATE VIRTUAL TABLE item_trgm USING fts5(
  title, body, tags_cache, search_extra,
  content = 'item',
  content_rowid = 'id',
  tokenize = 'trigram'
);

-- Corpus statistics for more-like-this.
CREATE VIRTUAL TABLE item_vocab USING fts5vocab('item_fts', 'row');

-- Index maintenance.  The delete side is unconditional: the index may hold a
-- row for reasons a WHEN clause cannot see (a rebuild, an import), and
-- passing delete values for a row FTS5 does not hold is harmless, while
-- failing to delete one it does hold is corruption.
CREATE TRIGGER item_fts_ai AFTER INSERT ON item BEGIN
  INSERT INTO item_fts(rowid, title, body, tags_cache, search_extra)
    VALUES (new.id, new.title, new.body, new.tags_cache, new.search_extra);
  INSERT INTO item_trgm(rowid, title, body, tags_cache, search_extra)
    VALUES (new.id, new.title, new.body, new.tags_cache, new.search_extra);
END;

CREATE TRIGGER item_fts_ad AFTER DELETE ON item BEGIN
  INSERT INTO item_fts(item_fts, rowid, title, body, tags_cache, search_extra)
    VALUES ('delete', old.id, old.title, old.body, old.tags_cache, old.search_extra);
  INSERT INTO item_trgm(item_trgm, rowid, title, body, tags_cache, search_extra)
    VALUES ('delete', old.id, old.title, old.body, old.tags_cache, old.search_extra);
END;

CREATE TRIGGER item_fts_au AFTER UPDATE OF title, body, tags_cache, search_extra ON item BEGIN
  INSERT INTO item_fts(item_fts, rowid, title, body, tags_cache, search_extra)
    VALUES ('delete', old.id, old.title, old.body, old.tags_cache, old.search_extra);
  INSERT INTO item_fts(rowid, title, body, tags_cache, search_extra)
    VALUES (new.id, new.title, new.body, new.tags_cache, new.search_extra);
  INSERT INTO item_trgm(item_trgm, rowid, title, body, tags_cache, search_extra)
    VALUES ('delete', old.id, old.title, old.body, old.tags_cache, old.search_extra);
  INSERT INTO item_trgm(rowid, title, body, tags_cache, search_extra)
    VALUES (new.id, new.title, new.body, new.tags_cache, new.search_extra);
END;

-- Query-time term expansion, seeded small and editable by the user.
CREATE TABLE synonym (
  term    TEXT NOT NULL,
  synonym TEXT NOT NULL,
  weight  REAL NOT NULL DEFAULT 1.0 CHECK (weight > 0),
  source  TEXT NOT NULL DEFAULT 'seed' CHECK (source IN ('seed','user','mined')),
  PRIMARY KEY (term, synonym)
) STRICT, WITHOUT ROWID;

CREATE TABLE saved_search (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL UNIQUE,
  query      TEXT NOT NULL,
  icon       TEXT NOT NULL DEFAULT 'search',
  pinned     INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
  sort_order INTEGER NOT NULL DEFAULT 100,
  builtin    INTEGER NOT NULL DEFAULT 0 CHECK (builtin IN (0, 1)),
  created_at TEXT NOT NULL
             CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
) STRICT;

-- ===========================================================================
-- Recurring events, expanded
--
-- A calendar view must not re-expand RRULEs on every request, so occurrences
-- are materialised over a rolling window.
--
-- Both epochs are stored, and the calendar query is
--   starts_epoch BETWEEN :from - :max_duration AND :to AND ends_epoch >= :from
-- rather than a plain range on starts_epoch.  A plain range silently drops
-- any occurrence that began before the window and is still running inside it
-- -- a week-long trip is invisible in the view of its own middle day.
--
-- all_day and starts_local ride along so a floating date round-trips as a
-- date instead of being forced through a timezone and landing a day off.
-- ===========================================================================

CREATE TABLE event_instance (
  item_id      INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
  seq          INTEGER NOT NULL,
  all_day      INTEGER NOT NULL DEFAULT 0 CHECK (all_day IN (0, 1)),
  starts_local TEXT NOT NULL,
  ends_local   TEXT,
  starts_epoch INTEGER,
  ends_epoch   INTEGER,
  CHECK (all_day = 1 OR starts_epoch IS NOT NULL),
  PRIMARY KEY (item_id, seq)
) STRICT, WITHOUT ROWID;

CREATE INDEX ei_starts     ON event_instance(starts_epoch, ends_epoch) WHERE starts_epoch IS NOT NULL;
CREATE INDEX ei_starts_day ON event_instance(starts_local);

-- ===========================================================================
-- Import ledger
--
-- Every import is one recorded batch with a per-item ledger, so undoing a
-- thousand-file mistake is one command.  This is also what provides
-- all-or-nothing semantics for a bulk import WITHOUT wrapping it in a single
-- giant transaction: one writer holding the WAL lock for 30 seconds starves
-- every other writer past its busy_timeout, and a crash halfway through an
-- enormous transaction loses the lot.  Chunked commits plus this ledger
-- survive both.
-- ===========================================================================

CREATE TABLE import_batch (
  uid        TEXT PRIMARY KEY
             CHECK (length(uid) = 32 AND uid NOT GLOB '*[^0-9a-f]*'),
  source     TEXT NOT NULL,
  format     TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status     TEXT NOT NULL DEFAULT 'running'
             CHECK (status IN ('running','done','failed','undone')),
  created_count INTEGER NOT NULL DEFAULT 0,
  updated_count INTEGER NOT NULL DEFAULT 0,
  skipped_count INTEGER NOT NULL DEFAULT 0,
  withheld_count INTEGER NOT NULL DEFAULT 0,
  note       TEXT NOT NULL DEFAULT ''
) STRICT, WITHOUT ROWID;

CREATE TABLE import_item (
  batch_uid TEXT NOT NULL REFERENCES import_batch(uid) ON DELETE CASCADE,
  item_uid  TEXT NOT NULL,
  action    TEXT NOT NULL CHECK (action IN ('created','updated','skipped','withheld')),
  source_ref TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (batch_uid, item_uid)
) STRICT, WITHOUT ROWID;

-- ===========================================================================
-- Operational tables
-- ===========================================================================

-- Sidebar and facet counts, maintained incrementally.  Recomputing them from
-- a join across item_tag on every keystroke measured 300ms+ at 100k items,
-- in a single-threaded server -- the omnibox would stutter on every character.
CREATE TABLE counts (
  bucket TEXT NOT NULL,
  key    TEXT NOT NULL,
  n      INTEGER NOT NULL DEFAULT 0 CHECK (n >= 0),
  PRIMARY KEY (bucket, key)
) STRICT, WITHOUT ROWID;

-- A replayed Idempotency-Key must return the original response rather than
-- create a second item.  That requires storing the response.
CREATE TABLE idempotency (
  key         TEXT PRIMARY KEY,
  method      TEXT NOT NULL,
  path        TEXT NOT NULL,
  request_sha TEXT NOT NULL,
  status      INTEGER NOT NULL,
  response_json TEXT NOT NULL,
  at          TEXT NOT NULL
) STRICT, WITHOUT ROWID;

CREATE INDEX idem_at ON idempotency(at);

CREATE TABLE backup_log (
  id        INTEGER PRIMARY KEY,
  path      TEXT NOT NULL,
  at        TEXT NOT NULL,
  size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
  verified  INTEGER NOT NULL DEFAULT 0 CHECK (verified IN (0, 1)),
  kind      TEXT NOT NULL DEFAULT 'auto' CHECK (kind IN ('auto','manual','pre-migration','pre-purge','pre-import')),
  note      TEXT NOT NULL DEFAULT ''
) STRICT;

CREATE INDEX backup_at ON backup_log(at DESC);

-- Reserved and intentionally empty.  Nothing that ships writes to it.
--
-- Real semantic search needs an embedding model, which means either a model
-- download or an external API -- both ruled out by the no-dependency,
-- offline, no-account constraints this database is built on.  Rather than
-- pretend otherwise, the table exists so that opting in later
-- (`vault extend embeddings --provider <script>`) is a configuration change
-- and not a migration.  What ships is BM25 ranking, trigram fuzzy matching
-- and synonym expansion: strong lexical search, honestly labelled.
CREATE TABLE embedding (
  item_id   INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
  model     TEXT NOT NULL,
  dim       INTEGER NOT NULL CHECK (dim > 0),
  vec       BLOB NOT NULL,
  at        TEXT NOT NULL,
  PRIMARY KEY (item_id, model)
) STRICT, WITHOUT ROWID;

-- ===========================================================================
-- Blob refcounting
--
-- Triggers cover INSERT, DELETE *and* UPDATE.  The UPDATE case is the one
-- that matters and the one most easily forgotten: a link item is created
-- first and its archived copy attached later by UPDATE, so an INSERT-only
-- trigger leaves refcount at 0 and the next `vault gc` deletes bytes that
-- are very much in use.
-- ===========================================================================

CREATE TRIGGER blob_ref_file_ai AFTER INSERT ON item_file BEGIN
  UPDATE blob SET refcount = refcount + 1 WHERE id = new.blob_id;
END;

CREATE TRIGGER blob_ref_file_ad AFTER DELETE ON item_file BEGIN
  UPDATE blob SET refcount = refcount - 1 WHERE id = old.blob_id AND refcount > 0;
END;

CREATE TRIGGER blob_ref_file_au AFTER UPDATE OF blob_id ON item_file
WHEN old.blob_id IS NOT new.blob_id BEGIN
  UPDATE blob SET refcount = refcount - 1 WHERE id = old.blob_id AND refcount > 0;
  UPDATE blob SET refcount = refcount + 1 WHERE id = new.blob_id;
END;

CREATE TRIGGER blob_ref_link_ai AFTER INSERT ON item_link
WHEN new.archive_blob_id IS NOT NULL BEGIN
  UPDATE blob SET refcount = refcount + 1 WHERE id = new.archive_blob_id;
END;

CREATE TRIGGER blob_ref_link_ad AFTER DELETE ON item_link
WHEN old.archive_blob_id IS NOT NULL BEGIN
  UPDATE blob SET refcount = refcount - 1 WHERE id = old.archive_blob_id AND refcount > 0;
END;

CREATE TRIGGER blob_ref_link_au AFTER UPDATE OF archive_blob_id ON item_link
WHEN old.archive_blob_id IS NOT new.archive_blob_id BEGIN
  UPDATE blob SET refcount = refcount - 1 WHERE id = old.archive_blob_id AND old.archive_blob_id IS NOT NULL AND refcount > 0;
  UPDATE blob SET refcount = refcount + 1 WHERE id = new.archive_blob_id AND new.archive_blob_id IS NOT NULL;
END;

-- ===========================================================================
-- Link liveness
--
-- Mirrors item.deleted_at onto item_link so that UNIQUE(url_norm) can be a
-- partial index over live rows only.
-- ===========================================================================

CREATE TRIGGER link_live_au AFTER UPDATE OF deleted_at ON item
WHEN old.deleted_at IS NOT new.deleted_at BEGIN
  UPDATE item_link SET live = CASE WHEN new.deleted_at IS NULL THEN 1 ELSE 0 END
   WHERE item_id = new.id;
END;
