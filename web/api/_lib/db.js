/**
 * The one place a connection is opened.
 *
 * Serverless means many short-lived instances, each of which would otherwise
 * open its own pool and exhaust the server's connection limit under any real
 * traffic. The pool is cached on `globalThis` so that a warm instance reuses
 * it across invocations, and capped at a handful of connections because a
 * serverless function is never concurrent with itself.
 */
import pg from "pg";

const { Pool } = pg;

/** Every name Vercel's Postgres integrations use, in the order to try. */
const URL_KEYS = [
  "DATABASE_URL",
  "POSTGRES_URL",
  "POSTGRES_PRISMA_URL",
  "NEON_DATABASE_URL",
  "DATABASE_POSTGRES_URL",
];

export class NotConfigured extends Error {
  constructor() {
    super(
      "No database is attached yet. In the Vercel dashboard: Storage → " +
        "Create → Postgres → connect it to this project, then redeploy. " +
        "Looked for: " + URL_KEYS.join(", "),
    );
    this.name = "NotConfigured";
    this.status = 503;
  }
}

export function connectionString() {
  for (const key of URL_KEYS) {
    const value = process.env[key];
    if (value) return value;
  }
  return null;
}

/**
 * Whether to negotiate TLS for this connection string.
 *
 * Hardcoding TLS on breaks every Postgres that does not offer it -- a local
 * one, a container, a sidecar on a private network -- with "The server does
 * not support SSL connections", which says nothing about what to change.
 * Hardcoding it off would send credentials over the open internet.
 *
 * So it is derived: an explicit sslmode in the URL wins, a loopback or
 * private-network host defaults to off, and anything else defaults to on.
 */
export function sslFor(url) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return { rejectUnauthorized: false };
  }

  const mode = parsed.searchParams.get("sslmode");
  if (mode === "disable") return false;
  if (mode === "require" || mode === "prefer" || mode === "no-verify") {
    return { rejectUnauthorized: false };
  }
  if (mode === "verify-ca" || mode === "verify-full") {
    return { rejectUnauthorized: true };
  }

  const host = parsed.hostname;
  const local =
    host === "localhost" || host === "127.0.0.1" || host === "::1" ||
    host.endsWith(".local") ||
    /^10\./.test(host) || /^192\.168\./.test(host) ||
    /^172\.(1[6-9]|2\d|3[01])\./.test(host);
  if (local) return false;

  // A managed provider's certificate chain is usually not one Node ships a
  // root for, so the transport is encrypted without verifying the chain.
  return { rejectUnauthorized: false };
}

export function pool() {
  const url = connectionString();
  if (!url) throw new NotConfigured();

  if (!globalThis.__dbPool) {
    globalThis.__dbPool = new Pool({
      connectionString: url,
      ssl: sslFor(url),
      max: 3,
      idleTimeoutMillis: 10_000,
      connectionTimeoutMillis: 8_000,
      // A query that has not answered in 25s will not answer.
      statement_timeout: 25_000,
    });
    globalThis.__dbPool.on("error", (error) => {
      console.error("[db] idle client error:", error.message);
    });
  }
  return globalThis.__dbPool;
}

/**
 * Runs one parameterised statement.
 *
 * There is no overload that takes a finished SQL string: every value reaches
 * the server as a bound parameter, so nothing a caller types can change the
 * shape of a query.
 */
export async function query(text, params = []) {
  const started = Date.now();
  const result = await pool().query(text, params);
  const ms = Date.now() - started;
  if (ms > 1500) console.warn(`[db] slow (${ms}ms): ${text.slice(0, 90)}`);
  return result;
}

/** Runs several statements as one transaction, on one client. */
export async function transaction(run) {
  const client = await pool().connect();
  try {
    await client.query("BEGIN");
    const result = await run(client);
    await client.query("COMMIT");
    return result;
  } catch (error) {
    await client.query("ROLLBACK").catch(() => {});
    throw error;
  } finally {
    client.release();
  }
}
