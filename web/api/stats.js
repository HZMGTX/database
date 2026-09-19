/** What is in here, by kind. Reads the counts table, so it never scans. */
import { handler, send } from "./_lib/http.js";
import { query } from "./_lib/db.js";

export default handler(async (req, res) => {
  const [kinds, totals, tags, projects] = await Promise.all([
    query("SELECT kind, n FROM kind_count WHERE n > 0 ORDER BY n DESC, kind"),
    query(`SELECT
             count(*) FILTER (WHERE deleted_at IS NULL)::bigint AS live,
             count(*) FILTER (WHERE deleted_at IS NOT NULL)::bigint AS trashed,
             max(updated_at) AS last_write
           FROM item`),
    query(`SELECT t AS tag, count(*)::bigint AS n
             FROM item i, unnest(i.tags) t
            WHERE i.deleted_at IS NULL
            GROUP BY t ORDER BY n DESC, t LIMIT 60`),
    query(`SELECT c.project AS slug, COALESCE(p.label, '') AS label,
                  p.colour, c.n::bigint AS n
             FROM project_count c
             LEFT JOIN project p ON p.slug = c.project
            WHERE c.n > 0
            ORDER BY COALESCE(p.sort_order, 500), c.n DESC`),
  ]);

  return send(res, 200, {
    items: Number(totals.rows[0].live),
    trashed: Number(totals.rows[0].trashed),
    last_write: totals.rows[0].last_write,
    projects: projects.rows.map((r) => ({
      slug: r.slug,
      label: r.label || (r.slug === "" ? "Unfiled" : r.slug),
      colour: r.colour,
      n: Number(r.n),
    })),
    kinds: kinds.rows.map((r) => ({ kind: r.kind, n: Number(r.n) })),
    tags: tags.rows.map((r) => ({ tag: r.tag, n: Number(r.n) })),
  });
});
