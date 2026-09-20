/**
 * A typed client for the shared database.
 *
 * https://github.com/HZMGTX/database keeps notes, tasks, events, links,
 * people and files in one place, with full-text search, tags, relationships
 * and versioned history over all of it. This package speaks to its REST API.
 *
 * Three properties are deliberate:
 *
 *   1. **Additive.** Nothing in the monorepo imports this until you add an
 *      import. Adding the package changes no behaviour.
 *   2. **Fail-soft by default.** An unreachable or slow database returns an
 *      empty result rather than throwing, because a sidecar should not be
 *      able to fail a request that was not about it. Pass `strict` where you
 *      would rather handle the error.
 *   3. **Separate from `lib/db`.** The database holds its own store in its own
 *      process. No Postgres connection, Drizzle schema or existing table is
 *      involved.
 *
 * Configuration is read from the environment on every call, so changing it
 * takes effect without a restart:
 *
 *   DB_TOKEN       required; the bearer token. The only one that has to
 *                  be set, because it is the only one that is secret.
 *   DB_URL         override only to point somewhere else
 *   DB_TIMEOUT_MS  default 8000; a serverless cold start can take a second
 */

// The deployed database. A localhost default was right when the only
// database was one you started by hand on the same machine; it is wrong now
// that there is a real one, because it makes every call fail by default and
// read as the client being broken rather than unconfigured.
const DEFAULT_BASE = "https://database-null-s-projects4.vercel.app";
// Two seconds suits a server on the same machine. This one is a serverless
// function across the internet, where a cold start alone can exceed that --
// measured at just over a second on a first call and well under on the
// next. A timeout that fires on the first request of the day is worse than
// a slow one.
const DEFAULT_TIMEOUT_MS = 8000;
const API = "/api/v1";

/** The kinds that ship with it. Any other string is a kind you defined. */
export type DbKind = "note" | "task" | "event" | "link" | "file" | "person";

/** Where a task stands. */
export type TaskStatus = "todo" | "doing" | "blocked" | "done" | "cancelled";

/**
 * The characters a matched run is wrapped in: STX and ETX.
 *
 * They are control characters rather than HTML or ANSI on purpose, so that
 * each caller decides how to render a match without having to unpick someone
 * else's markup. Pass a snippet through {@link renderSnippet} — printing one
 * unprocessed puts two invisible control characters into your output.
 */
export const MARK_START = "\u0002";
export const MARK_END = "\u0003";

/** One ranked search result. */
export interface DbHit {
  uid: string;
  kind: string;
  title: string;
  /**
   * The matched passage, with each match wrapped in {@link MARK_START} and
   * {@link MARK_END}. Empty when the match was in the title rather than the
   * body, since there is no passage to quote. Use {@link renderSnippet}.
   */
  snippet: string;
  tags: string[];
  /** BM25, negated: lower sorts first. */
  score: number;
  updated_at: string;
  pinned: boolean;
  trashed: boolean;
}

/** A page of results. */
export interface DbSearchPage {
  hits: DbHit[];
  /** The query as sent. */
  query?: string;
  /** Exact below a thousand; a floor above it, which `total_capped` marks. */
  total: number | null;
  total_capped?: boolean;
  /**
   * What each clause was understood to mean, in plain English. Worth showing
   * beside the results: it is how someone finds out that `due:friday` was
   * read as a date and `duw:friday` was read as a property nobody has.
   */
  understood?: string[];
  /** Anything the caller should know about how the query ran. */
  note?: string;
  /** How long the search took, server-side. */
  took_ms?: number;
}

/**
 * Turns a snippet's control characters into something renderable.
 *
 * ```ts
 * renderSnippet(hit.snippet)                          // plain text
 * renderSnippet(hit.snippet, { start: "**", end: "**" })   // Markdown
 * ```
 *
 * @param snippet - A {@link DbHit.snippet}
 * @param marks - What to replace the markers with; both default to nothing
 */
export function renderSnippet(
  snippet: string,
  marks: { start?: string; end?: string } = {},
): string {
  return (snippet ?? "")
    .split(MARK_START).join(marks.start ?? "")
    .split(MARK_END).join(marks.end ?? "");
}

/** A whole item. */
export interface DbItem {
  uid: string;
  kind: string;
  title: string;
  body: string;
  tags: string[];
  props: Record<string, unknown>;
  /** Kind-specific fields: due and status for a task, starts for an event. */
  facet?: Record<string, unknown>;
  links?: { in: DbEdge[]; out: DbEdge[] };
  pinned: boolean;
  rev: number;
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
}

/** A typed relationship between two items. */
export interface DbEdge {
  rel: string;
  uid: string;
  title: string;
}

/** What to write. */
export interface CaptureInput {
  title: string;
  body?: string;
  kind?: DbKind | (string & {});
  /** Hierarchical, slash-separated: `projects/genesis`. */
  tags?: string[];
  /** Arbitrary fields. Numbers become range-queryable with no migration. */
  props?: Record<string, unknown>;
  /** Kind-specific fields, e.g. `{ status: "todo", due: "2026-10-01" }`. */
  facet?: Record<string, unknown>;
}

/** Options `capture` accepts, beyond the ones every call takes. */
export interface CaptureOptions extends CallOptions {
  /**
   * Reuse one key across your own retries of a single write, so they cannot
   * write a duplicate. A key is generated per call otherwise.
   */
  idempotencyKey?: string;
}

/** Options every call accepts. */
export interface CallOptions {
  /** Throw a {@link DbError} instead of returning an empty result. */
  strict?: boolean;
  /** Overrides DB_TIMEOUT_MS for this call. */
  timeoutMs?: number;
  /** Aborts this call from the outside; composes with the timeout. */
  signal?: AbortSignal;
}

/** A call that did not succeed. */
export class DbError extends Error {
  /** HTTP status, when the server answered at all. */
  readonly status?: number;
  /** The RFC 9457 problem document, when the server sent one. */
  readonly problem?: Record<string, unknown>;

  constructor(message: string, status?: number,
              problem?: Record<string, unknown>) {
    super(message);
    this.name = "DbError";
    this.status = status;
    this.problem = problem;
  }
}

/**
 * The one thing here that is not a web standard.
 *
 * Declared locally rather than pulled in from @types/node, so this package
 * has no dependencies at all and nothing has to be added to the lockfile to
 * build it. Everything else it uses -- fetch, AbortController, AbortSignal,
 * URLSearchParams, setTimeout -- comes from the DOM lib, the same way
 * lib/api-client-react gets its globals.
 *
 * The optional chaining is not decoration: this file is written to run in a
 * browser too, where `process` does not exist at all.
 */
declare const process: { env?: Record<string, string | undefined> } | undefined;

function env(name: string): string | undefined {
  try {
    return typeof process !== "undefined" ? process?.env?.[name] : undefined;
  } catch {
    return undefined;
  }
}

/** The configured base URL, without a trailing slash. */
export function baseUrl(): string {
  return (env("DB_URL") || DEFAULT_BASE).replace(/\/+$/, "");
}

function configuredTimeout(): number {
  return Number(env("DB_TIMEOUT_MS")) || DEFAULT_TIMEOUT_MS;
}

function headersFor(extra?: Record<string, string>): Record<string, string> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...extra,
  };
  const token = env("DB_TOKEN");
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

/**
 * One request against the database API.
 *
 * Exported because the API is larger than the four helpers below: history,
 * revisions, tags, links, export and the rest are all reachable this way
 * without this package having to grow a wrapper for each of them.
 *
 * @throws {DbError} on transport failure, timeout, or a non-2xx response
 */
export async function request<T = unknown>(
  path: string,
  options: {
    method?: string;
    body?: unknown;
    headers?: Record<string, string>;
    timeoutMs?: number;
    signal?: AbortSignal;
  } = {},
): Promise<T> {
  const { method = "GET", body, headers } = options;
  const timeoutMs = options.timeoutMs ?? configuredTimeout();

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  // An external signal has to abort this call too, so the caller's own
  // cancellation is not swallowed by the timeout layered on top of it.
  //
  // The `aborted` check is not redundant. A signal that has already aborted
  // dispatched its event before we subscribed, so the listener never fires
  // and the request runs to completion — which is the opposite of what the
  // caller asked for, and silent. Passing an already-aborted signal is the
  // ordinary case when cancellation and the call race, such as a component
  // that unmounts while it is deciding whether to fetch.
  const onAbort = () => controller.abort();
  if (options.signal?.aborted) {
    controller.abort();
  } else {
    options.signal?.addEventListener("abort", onAbort, { once: true });
  }

  try {
    const response = await fetch(`${baseUrl()}${API}${path}`, {
      method,
      headers: headersFor(headers),
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });

    const text = await response.text();
    let payload: unknown = null;
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch {
        throw new DbError(
          `db: ${response.status} with a body that is not JSON`,
          response.status,
        );
      }
    }

    if (!response.ok) {
      const problem = (payload ?? {}) as Record<string, unknown>;
      const detail = (problem.detail ?? problem.title) as string | undefined;
      throw new DbError(detail || `db: HTTP ${response.status}`,
                           response.status, problem);
    }
    return payload as T;
  } catch (error) {
    if (error instanceof DbError) throw error;
    if ((error as Error)?.name === "AbortError") {
      throw new DbError(
        options.signal?.aborted
          ? "db: the call was cancelled"
          : `db: no answer within ${timeoutMs}ms`,
      );
    }
    throw new DbError(`db: ${(error as Error).message}`);
  } finally {
    clearTimeout(timer);
    options.signal?.removeEventListener("abort", onAbort);
  }
}

/**
 * Searches the database with its query language.
 *
 * Bare words are ANDed and ranked; `"quoted"` is a phrase; `-word` excludes;
 * `kind:`, `tag:`, `status:`, `due:`, `is:`, `has:` and `field>value` narrow.
 *
 * Returns an empty page rather than throwing when the database is unreachable,
 * unless `strict` is set.
 */
export async function search(
  query: string,
  options: CallOptions & { limit?: number; offset?: number } = {},
): Promise<DbSearchPage> {
  const { limit = 20, offset, strict, ...rest } = options;
  try {
    const params = new URLSearchParams({ q: query, limit: String(limit) });
    if (offset) params.set("offset", String(offset));
    const page = await request<DbSearchPage>(`/search?${params}`, rest);
    return {
      hits: page?.hits ?? [],
      query: page?.query,
      total: page?.total ?? 0,
      total_capped: page?.total_capped,
      understood: page?.understood,
      note: page?.note,
      took_ms: page?.took_ms,
    };
  } catch (error) {
    if (strict) throw error;
    return { hits: [], total: 0 };
  }
}

/**
 * Writes an item into the database.
 *
 * A request that never gets an answer -- a timeout, a dropped connection --
 * is sent once more, because that is the one failure where the caller cannot
 * tell whether the write landed. Both attempts carry the same
 * `Idempotency-Key`, and the database returns what the first one created
 * rather than writing a second copy, so the retry cannot duplicate anything.
 *
 * A failure the database *answered* with is not retried: it would be refused
 * again, and the key is already spent on it. Neither is a call the caller
 * cancelled, which is not a failure at all.
 *
 * The cost of the retry is time: a write that is never answered takes up to
 * twice the timeout before giving up. Pass `timeoutMs` where that matters.
 *
 * Returns `null` rather than throwing when the database is unreachable, unless
 * `strict` is set.
 */
export async function capture(
  item: CaptureInput,
  options: CaptureOptions = {},
): Promise<DbItem | null> {
  const { strict, idempotencyKey, ...rest } = options;
  try {
    if (!item?.title) throw new DbError("db: capture needs a title");

    // One key across both attempts. Generating a second one for the retry
    // would defeat the point and write the item twice.
    const key = idempotencyKey ?? newIdempotencyKey();
    const send = () => request<DbItem>("/items", {
      ...rest,
      method: "POST",
      body: {
        kind: item.kind ?? "note",
        title: item.title,
        body: item.body ?? "",
        tags: item.tags ?? [],
        props: item.props ?? {},
        ...(item.facet ? { facet: item.facet } : {}),
      },
      headers: { "Idempotency-Key": key },
    });

    try {
      return await send();
    } catch (error) {
      // `status` is set only when the database answered. Without it the
      // request never arrived, or its answer never came back, and nobody
      // knows which.
      if (error instanceof DbError && error.status !== undefined) throw error;
      // A cancelled call looks the same from here, and retrying it would
      // ignore what the caller asked for.
      if (rest.signal?.aborted) throw error;
      return await send();
    }
  } catch (error) {
    if (strict) throw error;
    return null;
  }
}

/** Reads one item by full uid or eight-character short handle. */
export async function get(
  ref: string,
  options: CallOptions = {},
): Promise<DbItem | null> {
  const { strict, ...rest } = options;
  try {
    return await request<DbItem>(`/items/${encodeURIComponent(ref)}`, rest);
  } catch (error) {
    if (strict) throw error;
    return null;
  }
}

/**
 * Whether the database is answering.
 *
 * Worth calling before a feature that depends on the database, so the interface can
 * say so instead of showing an empty list.
 */
export async function available(options: CallOptions = {}): Promise<boolean> {
  try {
    const health = await request<{ ok?: boolean }>("/health", {
      ...options,
      timeoutMs: options.timeoutMs ?? 500,
    });
    return Boolean(health?.ok);
  } catch {
    return false;
  }
}

/**
 * A key unique to one write, so every attempt at it is recognised as the
 * same write rather than as several.
 */
function newIdempotencyKey(): string {
  return `genesis-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}
