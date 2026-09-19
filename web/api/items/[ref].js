/**
 * GET    /api/items/:ref            one item, with its links
 * PATCH  /api/items/:ref            change the fields you pass, only those
 * DELETE /api/items/:ref            to the trash
 * DELETE /api/items/:ref?purge=1    gone, with its history
 *
 * `:ref` is the full uid or its last eight characters. The tail, not the
 * head: these ids start with a millisecond timestamp, so a run written in
 * the same instant shares a prefix and differs only at the end.
 */
import { handler, send, fail, readJson } from "../_lib/http.js";
import { get, update, remove } from "../_lib/items.js";

export default handler(
  async (req, res) => {
    const url = new URL(req.url, "http://localhost");
    const ref = req.query?.ref || url.pathname.split("/").pop();

    if (req.method === "GET") {
      const item = await get(ref);
      if (!item) return fail(res, 404, `No item matches '${ref}'.`);
      res.setHeader("ETag", `"${item.uid}.${item.rev}"`);
      return send(res, 200, item);
    }

    if (req.method === "PATCH") {
      const patch = await readJson(req);

      // If-Match is the HTTP way of saying the same thing as expected_rev,
      // so accept either and let the one write path enforce it.
      const ifMatch = req.headers["if-match"];
      if (ifMatch && patch.expected_rev == null) {
        const parsed = /^"?[0-9a-f]{32}\.(\d+)"?$/.exec(ifMatch.trim());
        if (parsed) patch.expected_rev = Number(parsed[1]);
      }

      const item = await update(ref, patch);
      if (!item) return fail(res, 404, `No item matches '${ref}'.`);
      res.setHeader("ETag", `"${item.uid}.${item.rev}"`);
      return send(res, 200, item);
    }

    const purge = url.searchParams.get("purge") === "1";
    const item = await remove(ref, { purge });
    if (!item) return fail(res, 404, `No item matches '${ref}'.`);
    return send(res, 200, item);
  },
  { methods: ["GET", "PATCH", "DELETE"] },
);
