/**
 * Loads the shipped data file into the database, a chunk per call.
 *
 * 54,311 items will not import inside one function invocation, so this keeps
 * a cursor in `import_progress` and does a slice each time. Call it until it
 * answers `done: true`. Re-running after it finishes is a no-op, and a call
 * that dies halfway loses only the chunk it was on -- the cursor advances
 * inside the same transaction as the rows.
 *
 * Every row is an upsert on uid, so importing twice cannot double anything.
 */
import { createReadStream } from "node:fs";
import { createGunzip } from "node:zlib";
import { createInterface } from "node:readline";
import path from "node:path";
import { handler, send } from "../_lib/http.js";
import { pool } from "../_lib/db.js";

const NAME = "seed";
const DEFAULT_CHUNK = 4000;

export default handler(
  async (req, res) => {
    const url = new URL(req.url, "http://localhost");
    const chunk = Math.min(
      Math.max(Number(url.searchParams.get("chunk")) || DEFAULT_CHUNK, 100), 10000);
    const file = path.join(process.cwd(), "db", "seed.jsonl.gz");

    const client = await pool().connect();
    try {
      await client.query(
        "INSERT INTO import_progress(name) VALUES ($1) ON CONFLICT (name) DO NOTHING",
        [NAME]);
      const state = await client.query(
        "SELECT cursor, total, finished_at FROM import_progress WHERE name = $1 FOR UPDATE",
        [NAME]);
      const row = state.rows[0];

      if (row.finished_at) {
        return send(res, 200, {
          done: true, imported: Number(row.cursor), total: Number(row.total),
          detail: "Already imported.",
        });
      }

      const start = Number(row.cursor);
      const batch = [];
      let line = 0;
      let exhausted = true;

      const reader = createInterface({
        input: createReadStream(file).pipe(createGunzip()),
        crlfDelay: Infinity,
      });

      for await (const text of reader) {
        if (!text.trim()) continue;
        if (line++ < start) continue;
        if (batch.length >= chunk) { exhausted = false; break; }
        try {
          const doc = JSON.parse(text);
          // The first line of the export is a header describing it, not an item.
          if (doc._vault) { continue; }
          if (!doc.uid || !doc.kind) continue;
          batch.push(doc);
        } catch {
          // A line that will not parse is skipped rather than failing the
          // whole import; the count at the end says how many arrived.
        }
      }
      reader.close();

      if (batch.length) {
        // One JSON document rather than parallel arrays.
        //
        // The obvious bulk form, unnest($1::text[], $2::text[], ...), cannot
        // carry `tags`: a text[][] is a single multidimensional array and
        // unnest flattens it completely, so every row would get every tag.
        // Postgres rejects it outright -- "column tags is of type text[] but
        // expression is of type text" -- which is how this was found.
        //
        // jsonb_to_recordset expands one document into rows and lets a column
        // be an array of its own, which is what this data actually is.
        const payload = batch.map((d) => ({
          uid: String(d.uid).toLowerCase(),
          kind: sanitizeKind(d.kind),
          title: String(d.title ?? "").slice(0, 500),
          body: String(d.body ?? ""),
          props: flatten(d.props),
          tags: Array.isArray(d.tags) ? d.tags.map(String) : [],
          pinned: Boolean(d.pinned),
          created_at: d.created_at || new Date().toISOString(),
          updated_at: d.updated_at || new Date().toISOString(),
        }));

        await client.query("BEGIN");
        await client.query(
          `INSERT INTO item (uid, kind, title, body, props, tags, pinned,
                             created_at, updated_at)
           SELECT r.uid, r.kind, r.title, r.body, r.props,
                  ARRAY(SELECT jsonb_array_elements_text(r.tags)),
                  r.pinned, r.created_at, r.updated_at
             FROM jsonb_to_recordset($1::jsonb) AS r(
                    uid text, kind text, title text, body text, props jsonb,
                    tags jsonb, pinned boolean,
                    created_at timestamptz, updated_at timestamptz)
           ON CONFLICT (uid) DO UPDATE SET
             kind = EXCLUDED.kind, title = EXCLUDED.title, body = EXCLUDED.body,
             props = EXCLUDED.props, tags = EXCLUDED.tags,
             updated_at = EXCLUDED.updated_at`,
          [JSON.stringify(payload)]);
        await client.query(
          "UPDATE import_progress SET cursor = $2, total = total + $3 WHERE name = $1",
          [NAME, start + batch.length, batch.length]);
        await client.query("COMMIT");
      }

      if (exhausted) {
        await client.query(
          "UPDATE import_progress SET finished_at = now() WHERE name = $1", [NAME]);
      }

      const after = await client.query(
        "SELECT cursor, total FROM import_progress WHERE name = $1", [NAME]);

      return send(res, 200, {
        done: exhausted,
        imported_this_call: batch.length,
        imported: Number(after.rows[0].total),
        cursor: Number(after.rows[0].cursor),
        detail: exhausted ? "Finished." : "More to go — call this again.",
      });
    } catch (error) {
      await client.query("ROLLBACK").catch(() => {});
      throw error;
    } finally {
      client.release();
    }
  },
  { methods: ["POST"] },
);

/** The export can carry kinds the CHECK constraint would reject. */
function sanitizeKind(kind) {
  const clean = String(kind).toLowerCase().replace(/[^a-z0-9_]/g, "_").slice(0, 40);
  return /^[a-z]/.test(clean) ? clean : `k_${clean}`.slice(0, 40);
}

/** props must be a JSON object; anything else becomes one. */
function flatten(props) {
  if (props && typeof props === "object" && !Array.isArray(props)) return props;
  if (props === undefined || props === null) return {};
  return { value: props };
}
