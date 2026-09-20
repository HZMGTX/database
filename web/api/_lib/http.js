/**
 * Request plumbing shared by every endpoint: authentication, JSON bodies,
 * consistent errors, and CORS for the clients that call this from elsewhere.
 */
import { NotConfigured } from "./db.js";
import { ensureSchema } from "./schema.js";

/**
 * Checks the bearer token.
 *
 * `API_TOKEN` is required in production. If it is unset the API refuses
 * everything rather than defaulting to open: a database on a public URL with
 * no token is readable and writable by anyone who finds it, and failing shut
 * is the only safe default.
 */
export function authorize(req) {
  const expected = process.env.API_TOKEN;
  if (!expected) {
    return {
      ok: false,
      status: 503,
      detail:
        "API_TOKEN is not set, so this database refuses every request. Set " +
        "it in the Vercel project's environment variables and redeploy. " +
        "An open database on a public URL is readable and writable by anyone.",
    };
  }

  const header = req.headers.authorization || "";
  const token = header.startsWith("Bearer ") ? header.slice(7)
              : (req.headers["x-api-token"] || "");

  if (!token) {
    return { ok: false, status: 401, detail: "Missing bearer token." };
  }
  if (!timingSafeEqual(token, expected)) {
    return { ok: false, status: 403, detail: "That token is not right." };
  }
  return { ok: true };
}

/** Compares without leaking the answer through how long it took. */
function timingSafeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string") return false;
  if (a.length !== b.length) return false;
  let difference = 0;
  for (let i = 0; i < a.length; i++) difference |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return difference === 0;
}

export function send(res, status, payload) {
  res.status(status);
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "no-store");
  res.end(JSON.stringify(payload));
}

export function fail(res, status, detail, extra = {}) {
  send(res, status, { error: true, status, detail, ...extra });
}

/** Reads a JSON body, whether or not the platform parsed it already. */
export async function readJson(req) {
  if (req.body && typeof req.body === "object") return req.body;
  if (typeof req.body === "string" && req.body) return JSON.parse(req.body);

  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  if (!chunks.length) return {};
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

/**
 * Wraps a handler with the things every endpoint needs.
 *
 * `open: true` skips authentication — used only by /api/health, so that
 * "is it up" can be answered without a credential.
 */
export function handler(run, { open = false, methods = ["GET"] } = {}) {
  return async (req, res) => {
    res.setHeader("Access-Control-Allow-Origin", "*");
    res.setHeader("Access-Control-Allow-Headers",
                  "Authorization, Content-Type, X-Api-Token, Idempotency-Key");
    res.setHeader("Access-Control-Allow-Methods", methods.concat("OPTIONS").join(", "));
    // Without this a browser can see the status but not where the new item
    // went, nor whether the write was a replay.
    res.setHeader("Access-Control-Expose-Headers", "Location, Idempotent-Replayed");

    if (req.method === "OPTIONS") {
      res.status(204);
      return res.end();
    }
    if (!methods.includes(req.method)) {
      return fail(res, 405, `${req.method} is not allowed here. Try: ${methods.join(", ")}`);
    }
    if (!open) {
      const auth = authorize(req);
      if (!auth.ok) return fail(res, auth.status, auth.detail);
    }

    // A deploy that ships code depending on a new table should not wait for
    // somebody to remember to migrate. After the first request on a warm
    // instance this costs nothing; a failure here is logged rather than
    // fatal, because a read that would have worked anyway should still work.
    try {
      await ensureSchema();
    } catch (error) {
      if (!(error instanceof NotConfigured)) {
        console.error("[schema]", error.message);
      }
    }

    try {
      return await run(req, res);
    } catch (error) {
      if (error instanceof NotConfigured) {
        return fail(res, 503, error.message);
      }
      // Postgres tells you exactly what was wrong; pass that through rather
      // than replacing it with "internal error" and making it undebuggable.
      const detail = error?.detail ? `${error.message} (${error.detail})`
                                   : error?.message || String(error);
      console.error("[api]", detail);
      return fail(res, error?.status || 500, detail);
    }
  };
}
