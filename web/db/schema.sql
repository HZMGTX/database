-- ===========================================================================
-- The database.
--
-- One table holds everything: a note, a task, a fish from VYREX's catalogue
-- and a source file are the same shape -- an id, a kind, a title, a body, a
-- JSON bag of whatever else that kind needs, and timestamps. That is what
-- lets search, tagging, linking and history be written once and be correct
-- for every kind, including kinds added later without a migration.
--
-- Applied by `POST /api/admin/migrate`, and safe to run again: every
-- statement is IF NOT EXISTS or CREATE OR REPLACE.
-- ===========================================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------------------
-- Items
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS item (
  id          BIGSERIAL PRIMARY KEY,
  uid         TEXT        NOT NULL UNIQUE,
  kind        TEXT        NOT NULL,
  title       TEXT        NOT NULL DEFAULT '',
  body        TEXT        NOT NULL DEFAULT '',

  -- Anything the kind needs that is not worth a column. A number in here is
  -- range-queryable through the GIN index below with no schema change, which
  -- is what makes `value > 5000` work over VYREX's item catalogue.
  props       JSONB       NOT NULL DEFAULT '{}'::jsonb,

  -- Slash-separated paths: 'work/finance' sits under 'work'. An array rather
  -- than a join table because the whole set is always read together and is
  -- never large.
  tags        TEXT[]      NOT NULL DEFAULT '{}',

  pinned      BOOLEAN     NOT NULL DEFAULT FALSE,
  rev         INTEGER     NOT NULL DEFAULT 1,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

  -- Soft delete. Nothing is destroyed by an ordinary delete, so an accident
  -- is recoverable; `DELETE /api/items/:ref?purge=1` is the one that is not.
  deleted_at  TIMESTAMPTZ,

  CONSTRAINT item_kind_shape CHECK (kind ~ '^[a-z][a-z0-9_]{0,39}$'),
  CONSTRAINT item_title_length CHECK (char_length(title) <= 500),
  CONSTRAINT item_props_is_object CHECK (jsonb_typeof(props) = 'object')
);

-- Ranked search.
--
-- Weighted by where the hit landed: title 'A', tags 'B', body 'C', and the
-- values inside props 'D'. props matters more here than it looks -- most of
-- what is in this table is catalogue data whose real content is
-- {"rarity": "Legendary", "value": 8800}, and leaving it out meant searching
-- `legendary` found nothing while 3,019 rows plainly were.
--
-- Two things make this awkward, both found by running it rather than reading
-- it:
--
--  1. A generated column must be IMMUTABLE, and `array_to_string` is only
--     STABLE, so Postgres rejects the whole table with "generation
--     expression is not immutable". Wrapping it is sound for text[]
--     specifically: text has no output function that varies with a setting.
--
--  2. Replacing the function does NOT recompute a stored generated column,
--     so the version lives in the function's NAME and the column is dropped
--     and rebuilt when it no longer refers to the current one. Bump the
--     suffix whenever the definition below changes.
--
-- The body is capped because a tsvector cannot exceed 1MB and this table
-- holds whole source files.
CREATE OR REPLACE FUNCTION item_search_vector_v2(
  p_title TEXT, p_body TEXT, p_tags TEXT[], p_props JSONB
) RETURNS tsvector
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT setweight(to_tsvector('english'::regconfig, coalesce(p_title, '')), 'A')
      || setweight(to_tsvector('english'::regconfig,
                               coalesce(array_to_string(p_tags, ' '), '')), 'B')
      || setweight(to_tsvector('english'::regconfig,
                               left(coalesce(p_body, ''), 200000)), 'C')
      || setweight(to_tsvector('english'::regconfig,
                               coalesce(left((SELECT string_agg(value, ' ')
                                                FROM jsonb_each_text(
                                                  coalesce(p_props, '{}'::jsonb))), 20000), '')), 'D')
$$;

DO $$
DECLARE expression TEXT;
BEGIN
  SELECT pg_get_expr(d.adbin, d.adrelid) INTO expression
    FROM pg_attrdef d
    JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
   WHERE d.adrelid = 'item'::regclass AND a.attname = 'search';

  IF expression IS NOT NULL AND expression NOT LIKE '%item_search_vector_v2%' THEN
    RAISE NOTICE 'search column was built from an older expression; rebuilding';
    ALTER TABLE item DROP COLUMN search;
  END IF;
END $$;

ALTER TABLE item
  ADD COLUMN IF NOT EXISTS search tsvector
  GENERATED ALWAYS AS (item_search_vector_v2(title, body, tags, props)) STORED;

CREATE INDEX IF NOT EXISTS item_search_idx  ON item USING GIN (search);
CREATE INDEX IF NOT EXISTS item_tags_idx    ON item USING GIN (tags);
CREATE INDEX IF NOT EXISTS item_props_idx   ON item USING GIN (props jsonb_path_ops);
CREATE INDEX IF NOT EXISTS item_title_trgm  ON item USING GIN (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS item_kind_idx    ON item (kind)       WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS item_updated_idx ON item (updated_at DESC) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS item_live_idx    ON item (id)         WHERE deleted_at IS NULL;

-- The short handle people actually type is the TAIL of the uid, not the head:
-- these ids begin with a millisecond timestamp, so thousands written in the
-- same burst share a prefix and differ only at the end.
CREATE INDEX IF NOT EXISTS item_short_idx ON item (right(uid, 8));

-- ---------------------------------------------------------------------------
-- Links
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS edge (
  src_id  BIGINT NOT NULL REFERENCES item(id) ON DELETE CASCADE,
  rel     TEXT   NOT NULL,
  dst_id  BIGINT NOT NULL REFERENCES item(id) ON DELETE CASCADE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (src_id, rel, dst_id),
  CONSTRAINT edge_not_self CHECK (src_id <> dst_id)
);

CREATE INDEX IF NOT EXISTS edge_dst_idx ON edge (dst_id);

-- ---------------------------------------------------------------------------
-- History
--
-- Append-only, and deliberately holding NO foreign key to item: a ledger that
-- cascades away with the thing it was recording is not a ledger. The uid is
-- denormalised so a purged item still has a readable trail.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS revision (
  id         BIGSERIAL PRIMARY KEY,
  item_uid   TEXT        NOT NULL,
  rev        INTEGER     NOT NULL,
  doc        JSONB       NOT NULL,
  action     TEXT        NOT NULL,
  actor      TEXT        NOT NULL DEFAULT 'api',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS revision_item_idx ON revision (item_uid, rev DESC);
CREATE INDEX IF NOT EXISTS revision_time_idx ON revision (created_at DESC);

-- ---------------------------------------------------------------------------
-- Kinds seen, maintained by trigger so the sidebar never has to scan.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS kind_count (
  kind  TEXT PRIMARY KEY,
  n     BIGINT NOT NULL DEFAULT 0
);

CREATE OR REPLACE FUNCTION bump_kind_count() RETURNS TRIGGER AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.deleted_at IS NULL THEN
      INSERT INTO kind_count(kind, n) VALUES (NEW.kind, 1)
        ON CONFLICT (kind) DO UPDATE SET n = kind_count.n + 1;
    END IF;
  ELSIF TG_OP = 'DELETE' THEN
    IF OLD.deleted_at IS NULL THEN
      UPDATE kind_count SET n = GREATEST(n - 1, 0) WHERE kind = OLD.kind;
    END IF;
  ELSE
    -- An update can move a row between kinds, into the trash, or back out.
    IF OLD.deleted_at IS NULL THEN
      UPDATE kind_count SET n = GREATEST(n - 1, 0) WHERE kind = OLD.kind;
    END IF;
    IF NEW.deleted_at IS NULL THEN
      INSERT INTO kind_count(kind, n) VALUES (NEW.kind, 1)
        ON CONFLICT (kind) DO UPDATE SET n = kind_count.n + 1;
    END IF;
  END IF;
  RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS item_kind_count ON item;
CREATE TRIGGER item_kind_count
  AFTER INSERT OR UPDATE OR DELETE ON item
  FOR EACH ROW EXECUTE FUNCTION bump_kind_count();

-- ---------------------------------------------------------------------------
-- Bookkeeping for the one-time import, so it can resume after a timeout
-- rather than starting over or writing everything twice.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS import_progress (
  name        TEXT PRIMARY KEY,
  cursor      BIGINT      NOT NULL DEFAULT 0,
  total       BIGINT      NOT NULL DEFAULT 0,
  finished_at TIMESTAMPTZ
);
