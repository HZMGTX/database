/**
 * GET /api/search?q=…&limit=&offset=
 *
 * The same page `GET /api/items?q=…` returns. It exists under its own name
 * because that is the path the clients in the projects were written against,
 * and a client that is already merged cannot be asked to change. Searching is
 * also the thing this database is mostly for, so a route of its own is honest
 * rather than a shim.
 */
import { handler, send } from "./_lib/http.js";
import { search } from "./_lib/items.js";

export default handler(async (req, res) => {
  const url = new URL(req.url, "http://localhost");
  return send(res, 200, await search(url.searchParams.get("q") || "", {
    limit: Number(url.searchParams.get("limit")) || 50,
    offset: Number(url.searchParams.get("offset")) || 0,
  }));
});
