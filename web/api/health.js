/**
 * Is it up, and is it actually attached to a database?
 *
 * The only endpoint that answers without a token, because "is it up" has to
 * be answerable by a monitor that holds no credential. It deliberately says
 * nothing about what is *in* the database beyond a count.
 */
import { handler, send } from "./_lib/http.js";
import { connectionString, query } from "./_lib/db.js";

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
      return send(res, 200, {
        ok: true,
        storage: "postgres",
        secured,
        items: Number(rows[0].items),
        latency_ms: Date.now() - started,
      });
    } catch (error) {
      // A database that is attached but has no tables yet is a normal state
      // on a first deploy, not a failure worth a 500.
      const needsMigration = /relation "item" does not exist/i.test(error.message);
      return send(res, needsMigration ? 503 : 500, {
        ok: false,
        storage: "postgres",
        secured,
        detail: needsMigration
          ? "Attached, but the tables are not created yet. POST /api/admin/migrate"
          : error.message,
      });
    }
  },
  { open: true, methods: ["GET"] },
);
