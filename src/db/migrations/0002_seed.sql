-- Seed data: the built-in kinds, their fields, the relationship vocabulary,
-- a small synonym set and the saved searches that make the sidebar useful on
-- day one rather than after an hour of setup.
--
-- Everything here is ordinary data.  A user can add kinds, fields, verbs and
-- synonyms without a migration; these rows are just a starting point.

-- ---------------------------------------------------------------------------
-- Kinds
-- ---------------------------------------------------------------------------

INSERT INTO kind(name, label, plural, icon, builtin, has_facet, sort_order, created_at) VALUES
  ('note',   'Note',   'Notes',   'note',   1, 0, 10, '2026-01-01T00:00:00Z'),
  ('task',   'Task',   'Tasks',   'check',  1, 1, 20, '2026-01-01T00:00:00Z'),
  ('event',  'Event',  'Events',  'calendar', 1, 1, 30, '2026-01-01T00:00:00Z'),
  ('link',   'Link',   'Links',   'link',   1, 1, 40, '2026-01-01T00:00:00Z'),
  ('file',   'File',   'Files',   'file',   1, 1, 50, '2026-01-01T00:00:00Z'),
  ('person', 'Person', 'People',  'person', 1, 1, 60, '2026-01-01T00:00:00Z');

-- ---------------------------------------------------------------------------
-- Fields
--
-- Facet-backed fields are declared here too, so that one registry drives
-- write validation, the web UI's forms, CSV export headers and query
-- completion.  They cannot disagree about what a field is.
-- ---------------------------------------------------------------------------

INSERT INTO kind_field(kind, key, label, type, required, multi, enum_values, hint, sort_order) VALUES
  ('task', 'status',   'Status',   'enum',     1, 0, '["todo","doing","blocked","done","cancelled"]', '', 10),
  ('task', 'priority', 'Priority', 'number',   0, 0, '[]', '0 (none) to 5 (urgent)', 20),
  ('task', 'due',      'Due',      'datetime', 0, 0, '[]', 'friday, tomorrow 3pm, +7d, 2026-05-01', 30),
  ('task', 'estimate', 'Estimate', 'text',     0, 0, '[]', '90m, 2h, 3d', 40),

  ('event', 'starts',   'Starts',   'datetime', 1, 0, '[]', '', 10),
  ('event', 'ends',     'Ends',     'datetime', 0, 0, '[]', '', 20),
  ('event', 'all_day',  'All day',  'bool',     0, 0, '[]', '', 30),
  ('event', 'tz',       'Time zone','text',     0, 0, '[]', 'IANA zone, e.g. Europe/London', 40),
  ('event', 'location', 'Location', 'text',     0, 0, '[]', '', 50),
  ('event', 'rrule',    'Repeats',  'text',     0, 0, '[]', 'FREQ=WEEKLY;BYDAY=MO', 60),

  ('link', 'url',   'URL',   'url',  1, 0, '[]', '', 10),

  ('file', 'filename', 'Filename', 'text', 1, 0, '[]', '', 10),

  ('person', 'given_name',  'First name', 'text',  0, 0, '[]', '', 10),
  ('person', 'family_name', 'Last name',  'text',  0, 0, '[]', '', 20),
  ('person', 'org',         'Organisation','text', 0, 0, '[]', '', 30),
  ('person', 'role',        'Role',       'text',  0, 0, '[]', '', 40),
  ('person', 'email',       'Email',      'email', 0, 1, '[]', '', 50),
  ('person', 'phone',       'Phone',      'text',  0, 1, '[]', '', 60),
  ('person', 'birthday',    'Birthday',   'date',  0, 0, '[]', '', 70);

-- ---------------------------------------------------------------------------
-- Relationship vocabulary
--
-- Each verb carries its inverse so a backlink renders as correct English
-- rather than an arrow: A "mentions" B, therefore B is "mentioned by" A.
-- ---------------------------------------------------------------------------

INSERT INTO rel(name, label, inverse, inverse_label, symmetric, builtin) VALUES
  ('mentions',   'mentions',    'mentioned_by', 'mentioned by',  0, 1),
  ('relates_to', 'relates to',  'relates_to',   'relates to',    1, 1),
  ('child_of',   'child of',    'parent_of',    'parent of',     0, 1),
  ('parent_of',  'parent of',   'child_of',     'child of',      0, 1),
  ('blocks',     'blocks',      'blocked_by',   'blocked by',    0, 1),
  ('blocked_by', 'blocked by',  'blocks',       'blocks',        0, 1),
  ('attends',    'attends',     'attended_by',  'attended by',   0, 1),
  ('attended_by','attended by', 'attends',      'attends',       0, 1),
  ('authored',   'authored',    'authored_by',  'authored by',   0, 1),
  ('authored_by','authored by', 'authored',     'authored',      0, 1),
  ('about',      'about',       'subject_of',   'subject of',    0, 1),
  ('subject_of', 'subject of',  'about',        'about',         0, 1),
  ('duplicates', 'duplicates',  'duplicates',   'duplicates',    1, 1),
  ('derived_from','derived from','source_of',   'source of',     0, 1),
  ('source_of',  'source of',   'derived_from', 'derived from',  0, 1),
  ('mentioned_by','mentioned by','mentions',    'mentions',      0, 1),
  ('contains',   'contains',    'contained_in', 'contained in',  0, 1),
  ('contained_in','contained in','contains',    'contains',      0, 1);

-- ---------------------------------------------------------------------------
-- Synonyms
--
-- Deliberately small and conservative.  Aggressive expansion makes results
-- worse, not better -- it is easy to turn a precise query into a vague one.
-- The user can edit these, and the miner adds more from their own corpus.
-- ---------------------------------------------------------------------------

INSERT INTO synonym(term, synonym, weight, source) VALUES
  ('todo','task',0.8,'seed'),      ('task','todo',0.8,'seed'),
  ('doc','document',0.9,'seed'),   ('document','doc',0.9,'seed'),
  ('img','image',0.9,'seed'),      ('image','img',0.9,'seed'),
  ('pic','image',0.8,'seed'),      ('photo','image',0.8,'seed'),
  ('db','database',0.9,'seed'),    ('database','db',0.9,'seed'),
  ('config','configuration',0.9,'seed'), ('configuration','config',0.9,'seed'),
  ('repo','repository',0.9,'seed'),('repository','repo',0.9,'seed'),
  ('auth','authentication',0.8,'seed'),
  ('bug','defect',0.7,'seed'),     ('issue','bug',0.7,'seed'),
  ('meeting','meet',0.7,'seed'),
  ('ref','reference',0.8,'seed');

-- ---------------------------------------------------------------------------
-- Saved searches
--
-- These become the sidebar.  They are worth seeding because an empty sidebar
-- teaches nothing about the query language, and these double as worked
-- examples of it.
-- ---------------------------------------------------------------------------

INSERT INTO saved_search(name, query, icon, pinned, sort_order, builtin, created_at) VALUES
  ('Inbox',      'is:untagged',                'inbox',    1, 10, 1, '2026-01-01T00:00:00Z'),
  ('Today',      'due:today OR starts:today',  'sun',      1, 20, 1, '2026-01-01T00:00:00Z'),
  ('Overdue',    'kind:task status:todo due:<today', 'alert', 1, 30, 1, '2026-01-01T00:00:00Z'),
  ('This week',  'starts:this-week',           'calendar', 0, 40, 1, '2026-01-01T00:00:00Z'),
  ('Open tasks', 'kind:task status:todo,doing','check',    0, 50, 1, '2026-01-01T00:00:00Z'),
  ('Pinned',     'is:pinned',                  'pin',      0, 60, 1, '2026-01-01T00:00:00Z'),
  ('Recent',     'sort:recent',                'clock',    0, 70, 1, '2026-01-01T00:00:00Z'),
  ('Read later', 'kind:link tag:read-later',   'link',     0, 80, 1, '2026-01-01T00:00:00Z'),
  ('People',     'kind:person',                'person',   0, 90, 1, '2026-01-01T00:00:00Z'),
  ('Trash',      'is:trashed',                 'trash',    0, 100, 1, '2026-01-01T00:00:00Z');
