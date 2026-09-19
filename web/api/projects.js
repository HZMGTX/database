/**
 * GET  /api/projects   every project, with how much is in it
 * POST /api/projects   create one, or rename / recolour an existing one
 *
 * A project is what a row is FOR. It exists so that 53,442 rows of game data
 * and 665 rows of something else do not sit in one undifferentiated list.
 */
import { handler, send, fail, readJson } from "./_lib/http.js";
import { query } from "./_lib/db.js";

const SLUG = /^[a-z0-9][a-z0-9_-]{0,48}$/;

export default handler(
  async (req, res) => {
    if (req.method === "GET") {
      const { rows } = await query(
        `SELECT p.slug, p.label, p.colour, p.note, p.sort_order,
                COALESCE(c.n, 0)::bigint AS items
           FROM project p
           LEFT JOIN project_count c ON c.project = p.slug
          ORDER BY p.sort_order, p.slug`);

      // The unfiled bucket is not a row in `project` -- nothing created it --
      // but it is a real place things live, so it is reported alongside.
      const unfiled = await query(
        "SELECT COALESCE(n, 0)::bigint AS n FROM project_count WHERE project = ''");

      return send(res, 200, {
        projects: rows.map((r) => ({
          slug: r.slug, label: r.label, colour: r.colour,
          note: r.note, items: Number(r.items),
        })),
        unfiled: Number(unfiled.rows[0]?.n ?? 0),
      });
    }

    const body = await readJson(req);
    const slug = String(body.slug ?? "").trim().toLowerCase();
    if (!SLUG.test(slug)) {
      return fail(res, 400,
        "A project name is lowercase letters, digits, hyphens and underscores, " +
        "starting with a letter or digit, up to 49 characters.");
    }
    const colour = body.colour ? String(body.colour).trim() : null;
    if (colour && !/^#[0-9a-fA-F]{6}$/.test(colour)) {
      return fail(res, 400, "A colour is a hex value like #00f8ff.");
    }

    const { rows } = await query(
      `INSERT INTO project (slug, label, colour, note)
            VALUES ($1, $2, $3, $4)
       ON CONFLICT (slug) DO UPDATE
              SET label  = EXCLUDED.label,
                  colour = EXCLUDED.colour,
                  note   = EXCLUDED.note
        RETURNING slug, label, colour, note`,
      [slug, String(body.label ?? slug).trim().slice(0, 120) || slug,
       colour, String(body.note ?? "").slice(0, 500)]);

    return send(res, 200, rows[0]);
  },
  { methods: ["GET", "POST"] },
);
