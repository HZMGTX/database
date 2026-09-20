/**
 * Keeping the deployed schema in step with the deployed code.
 *
 * A function that ships code depending on a new table, against a database
 * that has not got it yet, is broken from the moment it deploys until
 * somebody with the admin token remembers to call /api/admin/migrate. That
 * window is invisible -- the deploy is green, the health check is green,
 * and only the write path fails -- and it reopens on every schema change.
 *
 * So the schema applies itself. Every statement in db/schema.sql is written
 * to be safe to run again (CREATE ... IF NOT EXISTS, CREATE OR REPLACE), so
 * the only question is whether the database has already seen this exact
 * file. Its hash is recorded, and the answer is one cheap read.
 */
import { createHash } from "node:crypto";
import { query, transaction } from "./db.js";
import { readAsset } from "./assets.js";

/**
 * The advisory lock two instances contend for, so a deploy that starts
 * several at once applies the schema once rather than several times over.
 * Any constant does; this one is arbitrary and must simply not move.
 */
const LOCK = 8081720441;

/**
 * Per-instance, so a warm function pays nothing after the first request.
 *
 * On `globalThis` rather than in module scope for the same reason the pool
 * is: it survives a module being evaluated more than once inside one
 * instance, which module scope does not.
 */
function settled(value) {
  if (value !== undefined) globalThis.__dbSchema = value;
  return globalThis.__dbSchema ?? null;
}

/**
 * Applies db/schema.sql if the database has not already got this version.
 *
 * Resolves to the hash in force. Safe to call on every request.
 */
export function ensureSchema() {
  if (!settled()) {
    settled(apply(false).catch((error) => {
      // Forgotten, so the next request tries again rather than inheriting a
      // failure that may have been transient.
      settled(null);
      throw error;
    }));
  }
  return settled();
}

/**
 * Applies db/schema.sql whether or not the database already has it.
 *
 * What /api/admin/migrate calls. The hash check exists to make the common
 * path free, not to stop somebody deliberately reapplying the file.
 */
export async function forceSchema() {
  settled(apply(true));
  return settled();
}

async function apply(force) {
  const sql = await readAsset("db/schema.sql");
  const want = createHash("sha256").update(sql).digest("hex").slice(0, 32);

  // Creating the marker and reading it are one round trip, because this
  // runs on every cold start and a second trip to Neon is real latency.
  const results = await query(
    `CREATE TABLE IF NOT EXISTS schema_state (
       id         BOOLEAN PRIMARY KEY DEFAULT true CHECK (id),
       sha        TEXT NOT NULL,
       applied_at TIMESTAMPTZ NOT NULL DEFAULT now());
     SELECT sha FROM schema_state WHERE id;`);
  const read = Array.isArray(results) ? results[results.length - 1] : results;
  if (!force && read.rows[0]?.sha === want) return want;

  await transaction(async (client) => {
    // Held until this transaction ends, which is what stops two instances
    // from applying the same file at the same time.
    await client.query("SELECT pg_advisory_xact_lock($1)", [LOCK]);

    // Read again inside the lock: whoever was ahead has finished by now,
    // and if they applied this same version there is nothing left to do.
    const current = await client.query("SELECT sha FROM schema_state WHERE id");
    if (!force && current.rows[0]?.sha === want) return;

    console.warn(`[schema] applying ${want} (was ${current.rows[0]?.sha ?? "nothing"})`);
    await client.query(sql);
    await client.query(
      `INSERT INTO schema_state (id, sha) VALUES (true, $1)
       ON CONFLICT (id) DO UPDATE SET sha = EXCLUDED.sha, applied_at = now()`,
      [want]);
  });

  return want;
}
