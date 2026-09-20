/**
 * GET  /api/items?q=...   search, or list when q is empty
 * POST /api/items         write a new item
 */
import { handler, send, readJson } from "../_lib/http.js";
import { search, create } from "../_lib/items.js";

export default handler(
  async (req, res) => {
    if (req.method === "GET") {
      const url = new URL(req.url, "http://localhost");
      const page = await search(url.searchParams.get("q") || "", {
        limit: Number(url.searchParams.get("limit")) || 50,
        offset: Number(url.searchParams.get("offset")) || 0,
      });
      return send(res, 200, page);
    }

    const body = await readJson(req);
    // A repeat of a request that already succeeded returns what it created,
    // rather than a second copy of it.
    const item = await create(body, "api", {
      idempotencyKey: req.headers["idempotency-key"] || "",
    });
    res.setHeader("Location", `/api/items/${item.uid}`);
    if (item.replayed) res.setHeader("Idempotent-Replayed", "true");
    return send(res, 201, item);
  },
  { methods: ["GET", "POST"] },
);
