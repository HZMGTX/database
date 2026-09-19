/**
 * Reading and writing items. Every statement here is parameterised.
 */
import { query, transaction } from "./db.js";
import { compile } from "./query.js";

const FIELDS = [
  "id", "uid", "kind", "title", "body", "props", "tags",
  "pinned", "rev", "created_at", "updated_at", "deleted_at",
];

/** For a SELECT that aliases the table `i`. */
const COLUMNS = FIELDS.map((f) => `i.${f}`).join(", ");

/**
 * For RETURNING, which has no alias in scope.
 *
 * INSERT and UPDATE do not alias their target, so `RETURNING i.uid` fails
 * with "missing FROM-clause entry for table i" -- the same list cannot serve
 * both, and reusing it silently broke every write.
 */
const RETURNING = FIELDS.join(", ");

/** A stable, sortable, unique id. First 12 hex are a millisecond clock. */
export function newUid() {
  const millis = Date.now();
  const stamp = millis.toString(16).padStart(12, "0").slice(-12);
  let tail = "";
  const bytes = new Uint8Array(10);
  (globalThis.crypto || require("node:crypto").webcrypto).getRandomValues(bytes);
  for (const byte of bytes) tail += byte.toString(16).padStart(2, "0");
  return stamp + tail;
}

function shape(row) {
  return {
    uid: row.uid,
    kind: row.kind,
    title: row.title,
    body: row.body,
    props: row.props ?? {},
    tags: row.tags ?? [],
    pinned: row.pinned,
    rev: row.rev,
    created_at: row.created_at,
    updated_at: row.updated_at,
    deleted_at: row.deleted_at,
  };
}

/** Runs a query and returns a page of ranked hits. */
export async function search(text, options = {}) {
  const started = Date.now();
  const q = compile(text, options);

  const sql =
    `SELECT ${COLUMNS},
            CASE WHEN i.body = '' THEN ''
                 ELSE left(i.body, 240) END AS preview
       FROM item i
      WHERE ${q.where}
      ORDER BY ${q.order}
      LIMIT ${q.limit + 1} OFFSET ${q.offset}`;

  const { rows } = await query(sql, q.params);
  const truncated = rows.length > q.limit;
  const page = rows.slice(0, q.limit);

  // Counting is capped: past a thousand nobody needs the exact figure, and
  // getting it means visiting every match.
  const countSql =
    `SELECT count(*)::bigint AS n FROM (
       SELECT 1 FROM item i WHERE ${q.where} LIMIT 1001) c`;
  // Only the parameters WHERE actually references -- the ranking one is not
  // in this statement, and passing it makes Postgres reject the statement.
  const counted = await query(countSql, q.params.slice(0, q.whereParamCount));
  const total = Number(counted.rows[0].n);

  return {
    query: text,
    understood: q.understood,
    hits: page.map((row) => ({ ...shape(row), preview: row.preview })),
    total: total > 1000 ? 1000 : total,
    total_capped: total > 1000,
    truncated,
    limit: q.limit,
    offset: q.offset,
    took_ms: Date.now() - started,
  };
}

/** One item by full uid or eight-character tail, with its links. */
export async function get(ref) {
  const wanted = String(ref || "").toLowerCase();
  const { rows } = await query(
    `SELECT ${COLUMNS} FROM item i
      WHERE i.uid = $1 OR right(i.uid, 8) = $1
      LIMIT 2`,
    [wanted]);

  if (!rows.length) return null;
  if (rows.length > 1) {
    const error = new Error(`'${ref}' matches more than one item; use the full uid`);
    error.status = 409;
    throw error;
  }

  const item = shape(rows[0]);
  const links = await query(
    `SELECT 'out' AS direction, e.rel, d.uid, d.title, d.kind
       FROM edge e JOIN item d ON d.id = e.dst_id WHERE e.src_id = $1
      UNION ALL
     SELECT 'in', e.rel, s.uid, s.title, s.kind
       FROM edge e JOIN item s ON s.id = e.src_id WHERE e.dst_id = $1`,
    [rows[0].id]);

  item.links = {
    out: links.rows.filter((l) => l.direction === "out")
                   .map(({ rel, uid, title, kind }) => ({ rel, uid, title, kind })),
    in: links.rows.filter((l) => l.direction === "in")
                  .map(({ rel, uid, title, kind }) => ({ rel, uid, title, kind })),
  };
  return item;
}

function clean(input) {
  const kind = String(input.kind || "note").toLowerCase().trim();
  if (!/^[a-z][a-z0-9_]{0,39}$/.test(kind)) {
    const error = new Error(
      `'${kind}' is not a usable kind. Lowercase letters, digits and ` +
      `underscores, starting with a letter, up to 40 characters.`);
    error.status = 400;
    throw error;
  }

  const title = String(input.title ?? "").trim();
  if (title.length > 500) {
    const error = new Error("A title is at most 500 characters.");
    error.status = 400;
    throw error;
  }

  let props = input.props ?? {};
  if (typeof props !== "object" || props === null || Array.isArray(props)) {
    const error = new Error("props must be a JSON object.");
    error.status = 400;
    throw error;
  }
  // A round trip through JSON drops duplicate keys and anything that cannot
  // be stored, so the value written is exactly the value that reads back.
  props = JSON.parse(JSON.stringify(props));

  const tags = [...new Set(
    (Array.isArray(input.tags) ? input.tags : [])
      .map((t) => String(t).trim().replace(/^\/+|\/+$/g, "").toLowerCase())
      .filter(Boolean),
  )];

  return { kind, title, body: String(input.body ?? ""), props, tags,
           pinned: Boolean(input.pinned) };
}

/** Writes a new item, and its first revision, in one transaction. */
export async function create(input, actor = "api") {
  const value = clean(input);
  if (!value.title && !value.body) {
    const error = new Error("An item needs at least a title or a body.");
    error.status = 400;
    throw error;
  }
  const uid = String(input.uid || newUid()).toLowerCase();
  if (!/^[0-9a-f]{32}$/.test(uid)) {
    const error = new Error("A uid is 32 lowercase hex characters.");
    error.status = 400;
    throw error;
  }

  return transaction(async (client) => {
    const { rows } = await client.query(
      `INSERT INTO item (uid, kind, title, body, props, tags, pinned)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6::text[], $7)
       RETURNING ${RETURNING}`,
      [uid, value.kind, value.title, value.body,
       JSON.stringify(value.props), value.tags, value.pinned]);

    const item = shape(rows[0]);
    await client.query(
      "INSERT INTO revision (item_uid, rev, doc, action, actor) VALUES ($1,$2,$3::jsonb,$4,$5)",
      [item.uid, item.rev, JSON.stringify(item), "create", actor]);
    return item;
  });
}

/**
 * Changes an item. Only the fields present in `patch` are touched.
 *
 * `expected_rev` is checked inside the transaction, on a locked row, so no
 * other writer can slip between the check and the write.
 */
export async function update(ref, patch, actor = "api") {
  const wanted = String(ref || "").toLowerCase();

  return transaction(async (client) => {
    const found = await client.query(
      `SELECT ${COLUMNS} FROM item i
        WHERE i.uid = $1 OR right(i.uid, 8) = $1
        FOR UPDATE`,
      [wanted]);

    if (!found.rows.length) return null;
    if (found.rows.length > 1) {
      const error = new Error(`'${ref}' matches more than one item; use the full uid`);
      error.status = 409;
      throw error;
    }

    const before = shape(found.rows[0]);
    if (patch.expected_rev != null && Number(patch.expected_rev) !== before.rev) {
      const error = new Error(
        `That item has changed since you read it (you have revision ` +
        `${patch.expected_rev}, it is now ${before.rev}).`);
      error.status = 409;
      error.current = before;
      throw error;
    }

    const merged = clean({
      kind: patch.kind ?? before.kind,
      title: patch.title ?? before.title,
      body: patch.body ?? before.body,
      props: patch.props ?? before.props,
      tags: patch.tags ?? before.tags,
      pinned: patch.pinned ?? before.pinned,
    });

    const { rows } = await client.query(
      `UPDATE item
          SET kind = $2, title = $3, body = $4, props = $5::jsonb,
              tags = $6::text[], pinned = $7,
              rev = rev + 1, updated_at = now(),
              deleted_at = CASE WHEN $8::boolean THEN NULL ELSE deleted_at END
        WHERE id = $1
    RETURNING ${RETURNING}`,
      [found.rows[0].id, merged.kind, merged.title, merged.body,
       JSON.stringify(merged.props), merged.tags, merged.pinned,
       patch.restore === true]);

    const item = shape(rows[0]);
    await client.query(
      "INSERT INTO revision (item_uid, rev, doc, action, actor) VALUES ($1,$2,$3::jsonb,$4,$5)",
      [item.uid, item.rev, JSON.stringify(item), "update", actor]);
    return item;
  });
}

/**
 * Moves an item to the trash, or destroys it.
 *
 * The default is reversible. `purge` is not, and takes the item's links and
 * its whole revision history with it.
 */
export async function remove(ref, { purge = false } = {}, actor = "api") {
  const wanted = String(ref || "").toLowerCase();

  return transaction(async (client) => {
    const found = await client.query(
      `SELECT ${COLUMNS} FROM item i
        WHERE i.uid = $1 OR right(i.uid, 8) = $1 FOR UPDATE`,
      [wanted]);
    if (!found.rows.length) return null;

    const item = shape(found.rows[0]);

    if (purge) {
      // The tombstone is written first: after the DELETE there is nothing
      // left to read the item from.
      await client.query(
        "INSERT INTO revision (item_uid, rev, doc, action, actor) VALUES ($1,$2,$3::jsonb,$4,$5)",
        [item.uid, item.rev, JSON.stringify(item), "purge", actor]);
      await client.query("DELETE FROM item WHERE id = $1", [found.rows[0].id]);
      return { ...item, purged: true };
    }

    const { rows } = await client.query(
      `UPDATE item SET deleted_at = now(), rev = rev + 1, updated_at = now()
        WHERE id = $1 RETURNING ${RETURNING}`,
      [found.rows[0].id]);
    const trashed = shape(rows[0]);
    await client.query(
      "INSERT INTO revision (item_uid, rev, doc, action, actor) VALUES ($1,$2,$3::jsonb,$4,$5)",
      [trashed.uid, trashed.rev, JSON.stringify(trashed), "trash", actor]);
    return { ...trashed, purged: false };
  });
}
