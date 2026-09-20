/**
 * Is it up, and is it actually attached to a database?
 *
 * The only endpoint that answers without a token, because "is it up" has to
 * be answerable by a monitor that holds no credential. It deliberately says
 * nothing about what is *in* the database beyond a count.
 */
import { handler, send } from "./_lib/http.js";
import { connectionString, query } from "./_lib/db.js";
import { ensureSchema } from "./_lib/schema.js";

export default handler(
  async (req, res) => {
    const configured = Boolean(connectionString());
    const secured = Boolean(process.env.API_TOKEN);

    if (!configured) {
      return send(res, 503, {
        ok: false,
        storage: "missing",
        secured,
        detail:
          "No Postgres is attached. Vercel dashboard → Storage → Create → " +
          "Postgres → connect it to this project, then redeploy.",
      });
    }

    try {
      const started = Date.now();
      const { rows } = await query(
        "SELECT count(*)::bigint AS items FROM item WHERE deleted_at IS NULL");
      // Which version of the schema this database is actually on. Already
      // worked out before this handler ran, so it costs nothing here, and
      // it is the one way to tell from outside whether a deploy and its
      // database are in step -- without needing a credential to ask.
      const schema = await ensureSchema().catch(() => null);

      return send(res, 200, {
        ok: true,
        storage: "postgres",
        secured,
        items: Number(rows[0].items),
        schema,
        latency_ms: Date.now() - started,
      });
    } catch (error) {
      // The tables are created by the first request that reaches a database
      // without them, so getting here means that failed -- which is worth
      // saying, rather than repeating advice that has already been taken.
      const noTables = /relation "item" does not exist/i.test(error.message);
      return send(res, noTables ? 503 : 500, {
        ok: false,
        storage: "postgres",
        secured,
        detail: noTables
          ? "Attached, but the tables are not there and creating them did " +
            "not work. Check the function logs for [schema], or POST " +
            "/api/admin/migrate to see the error directly."
          : error.message,
      });
    }
  },
  { open: true, methods: ["GET"] },
);
