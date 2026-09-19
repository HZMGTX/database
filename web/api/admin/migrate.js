/**
 * Creates the tables. Safe to call again at any time.
 *
 * The schema lives in db/schema.sql rather than in this file so that the
 * same text can be applied by hand with psql -- which is what you want at
 * three in the morning when the function is the thing that is broken.
 */
import { readFile } from "node:fs/promises";
import path from "node:path";
import { handler, send } from "../_lib/http.js";
import { pool } from "../_lib/db.js";

export default handler(
  async (req, res) => {
    const file = path.join(process.cwd(), "db", "schema.sql");
    const sql = await readFile(file, "utf8");

    const client = await pool().connect();
    try {
      // One statement stream, so a half-applied schema is not a possible
      // outcome: either every table exists afterwards or none of the new
      // ones do.
      await client.query("BEGIN");
      await client.query(sql);
      await client.query("COMMIT");
    } catch (error) {
      await client.query("ROLLBACK").catch(() => {});
      throw error;
    } finally {
      client.release();
    }

    const { rows } = await pool().query(
      "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename");
    return send(res, 200, { ok: true, tables: rows.map((r) => r.tablename) });
  },
  { methods: ["POST"] },
);
