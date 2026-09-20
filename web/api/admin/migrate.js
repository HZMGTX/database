/**
 * Applies the schema on demand.
 *
 * Every request already brings the schema up to date if it is behind, so
 * this exists for the times you want it done deliberately and want to see
 * what came out: after editing db/schema.sql by hand, or to confirm what
 * the database actually has.
 *
 * The schema itself lives in db/schema.sql rather than in this file so the
 * same text can be applied with psql -- which is what you want at three in
 * the morning when the function is the thing that is broken.
 */
import { handler, send } from "../_lib/http.js";
import { query } from "../_lib/db.js";
import { forceSchema } from "../_lib/schema.js";

export default handler(
  async (req, res) => {
    const sha = await forceSchema();
    const { rows } = await query(
      "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename");
    return send(res, 200, { ok: true, schema: sha, tables: rows.map((r) => r.tablename) });
  },
  { methods: ["POST"] },
);
