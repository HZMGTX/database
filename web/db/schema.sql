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
-- Projects
--
-- What a row is FOR. Everything in here comes from somewhere -- one repo,
-- one piece of work, or nothing in particular -- and without a column that
-- says so you are left reading tag conventions to tell 53,442 rows of game
-- data apart from 665 rows of something else.
--
-- A plain slug on the item rather than a foreign key: an import must never
-- fail because a project row was not created first, and a project with no
-- items left should not block deleting it. The table below carries the
-- label and colour; the item carries the slug.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS project (
  slug       TEXT PRIMARY KEY,
  label      TEXT        NOT NULL,
  colour     TEXT,
  note       TEXT        NOT NULL DEFAULT '',
  sort_order INTEGER     NOT NULL DEFAULT 100,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT project_slug_shape CHECK (slug ~ '^[a-z0-9][a-z0-9_-]{0,48}$'),
  CONSTRAINT project_colour_shape CHECK (colour IS NULL OR colour ~ '^#[0-9a-fA-F]{6}$')
);

-- '' means "not filed under anything", which is a real answer and not a
-- missing one, so the column is NOT NULL and the empty string is the
-- unfiled bucket. NULL would make every query need IS NULL handling.
ALTER TABLE item ADD COLUMN IF NOT EXISTS project TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS item_project_idx
  ON item (project, updated_at DESC) WHERE deleted_at IS NULL;

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

CREATE TABLE IF NOT EXISTS project_count (
  project TEXT PRIMARY KEY,
  n       BIGINT NOT NULL DEFAULT 0
);

-- One trigger keeps both tallies, because an update can move a row between
-- kinds, between projects, into the trash or back out, and two triggers
-- reading the same OLD/NEW would have to agree about all of it.
CREATE OR REPLACE FUNCTION bump_counts() RETURNS TRIGGER AS $$
BEGIN
  IF TG_OP <> 'INSERT' AND OLD.deleted_at IS NULL THEN
    UPDATE kind_count    SET n = GREATEST(n - 1, 0) WHERE kind = OLD.kind;
    UPDATE project_count SET n = GREATEST(n - 1, 0) WHERE project = OLD.project;
  END IF;

  IF TG_OP <> 'DELETE' AND NEW.deleted_at IS NULL THEN
    INSERT INTO kind_count(kind, n) VALUES (NEW.kind, 1)
      ON CONFLICT (kind) DO UPDATE SET n = kind_count.n + 1;
    INSERT INTO project_count(project, n) VALUES (NEW.project, 1)
      ON CONFLICT (project) DO UPDATE SET n = project_count.n + 1;
  END IF;

  RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS item_kind_count ON item;
DROP TRIGGER IF EXISTS item_counts ON item;
CREATE TRIGGER item_counts
  AFTER INSERT OR UPDATE OR DELETE ON item
  FOR EACH ROW EXECUTE FUNCTION bump_counts();

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

-- ---------------------------------------------------------------------------
-- Reconcile
--
-- Files anything unfiled and rebuilds the derived counts.
--
-- A function rather than loose statements because it has two callers that
-- must not disagree: applying the schema, and finishing an import. The
-- importer files each row from its `repo/<x>` tag as it goes, which leaves
-- behind every row that names its project some other way -- so without this
-- running afterwards, a fresh import ends with a couple of hundred rows
-- sitting in Unfiled that plainly belong somewhere.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION reconcile_projects() RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  -- Pass one: the repository indexer's tag. `FROM unnest(i.tags)` cannot be
  -- used here -- an UPDATE may not re-reference its own target in the FROM
  -- clause -- so the row reads its own array through a correlated subquery.
  UPDATE item
     SET project = (
           SELECT substring(t FROM 6)
             FROM unnest(tags) AS t
            WHERE t LIKE 'repo/%'
              AND substring(t FROM 6) ~ '^[a-z0-9][a-z0-9_-]{0,48}$'
            ORDER BY t
            LIMIT 1)
   WHERE project = ''
     AND EXISTS (
           SELECT 1 FROM unnest(tags) AS t
            WHERE t LIKE 'repo/%'
              AND substring(t FROM 6) ~ '^[a-z0-9][a-z0-9_-]{0,48}$');

  -- Known projects get a real name and colour. The WHERE is what makes this
  -- safe to re-run: it only replaces a label still equal to the auto-derived
  -- guess, so a name edited by hand is never overwritten.
  INSERT INTO project (slug, label, colour, sort_order) VALUES
    ('vyrex',          'VYREX',          '#00f8ff', 10),
    ('genesis-ai-dev', 'Genesis AI Dev', '#ba00ff', 20)
  ON CONFLICT (slug) DO UPDATE
     SET label      = EXCLUDED.label,
         colour     = COALESCE(project.colour, EXCLUDED.colour),
         sort_order = EXCLUDED.sort_order
   WHERE project.label = initcap(replace(project.slug, '-', ' '));

  INSERT INTO project (slug, label)
  SELECT DISTINCT i.project, initcap(replace(i.project, '-', ' '))
    FROM item i
   WHERE i.project <> ''
  ON CONFLICT (slug) DO NOTHING;

  -- Pass two: rows that name their project some other way. Data read out of
  -- another application's own database arrives tagged `vyrex` and
  -- `vyrex/warnings`, never `repo/vyrex`.
  --
  -- The match is against project slugs that ALREADY EXIST, which is the
  -- whole point: `vyrex` is a project because pass one established it, while
  -- `data/fish` and `lang/js` are just tags and must not become projects.
  UPDATE item
     SET project = (
           SELECT p.slug FROM project p
            WHERE p.slug = ANY(ARRAY(SELECT split_part(t, '/', 1) FROM unnest(tags) t))
            ORDER BY p.sort_order, p.slug
            LIMIT 1)
   WHERE project = ''
     AND EXISTS (
           SELECT 1 FROM project p
            WHERE p.slug = ANY(ARRAY(SELECT split_part(t, '/', 1) FROM unnest(tags) t)));

  -- The counts are derived, so they are rebuilt rather than trusted: a bulk
  -- import or the passes above move rows the per-row trigger never sees in
  -- their final state.
  TRUNCATE kind_count;
  INSERT INTO kind_count (kind, n)
  SELECT kind, count(*) FROM item WHERE deleted_at IS NULL GROUP BY kind;

  TRUNCATE project_count;
  INSERT INTO project_count (project, n)
  SELECT project, count(*) FROM item WHERE deleted_at IS NULL GROUP BY project;
END;
$$;

SELECT reconcile_projects();
