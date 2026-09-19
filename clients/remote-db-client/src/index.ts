/**
 * A typed client for a local the database server.
 *
 * the database (https://github.com/HZMGTX/database) is a self-contained personal
 * database: notes, tasks, events, links, people and files in one SQLite file,
 * with full-text search, tags, relationships and versioned history over all
 * of it. This package speaks to its REST API over HTTP.
 *
 * Three properties are deliberate:
 *
 *   1. **Additive.** Nothing in the monorepo imports this until you add an
 *      import. Adding the package changes no behaviour.
 *   2. **Fail-soft by default.** An unreachable or slow the database returns an
 *      empty result rather than throwing, because a sidecar should not be
 *      able to fail a request that was not about it. Pass `strict` where you
 *      would rather handle the error.
 *   3. **Separate from `lib/db`.** the database holds its own store in its own
 *      process. No Postgres connection, Drizzle schema or existing table is
 *      involved.
 *
 * Configuration is read from the environment on every call, so changing it
 * takes effect without a restart:
 *
 *   DB_URL         default http://127.0.0.1:8787
 *   DB_TOKEN       bearer token, when the server was started with --token
 *   DB_TIMEOUT_MS  default 2000
 */

const DEFAULT_BASE = "http://127.0.0.1:8787";
const DEFAULT_TIMEOUT_MS = 2000;
const API = "/api/v1";

/** The kinds the database ships with. Any other string is a kind you defined. */
export type DbKind = "note" | "task" | "event" | "link" | "file" | "person";

/** Where a task stands. */
export type TaskStatus = "todo" | "doing" | "blocked" | "done" | "cancelled";

/**
 * The characters the database wraps a matched run in: STX and ETX.
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
  /** How long the database took, server-side. */
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

/** A whole item, as the database composes it. */
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

/** Options every call accepts. */
export interface CallOptions {
  /** Throw a {@link DbError} instead of returning an empty result. */
  strict?: boolean;
  /** Overrides DB_TIMEOUT_MS for this call. */
  timeoutMs?: number;
  /** Aborts this call from the outside; composes with the timeout. */
  signal?: AbortSignal;
}

/** A the database call that did not succeed. */
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

function env(name: string): string | undefined {
  return typeof process !== "undefined" ? process.env?.[name] : undefined;
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
 * One request against the the database API.
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
 * The request carries an `Idempotency-Key`, so a retry after a timeout
 * returns the first call's result rather than creating a duplicate.
 *
 * Returns `null` rather than throwing when the database is unreachable, unless
 * `strict` is set.
 */
export async function capture(
  item: CaptureInput,
  options: CallOptions = {},
): Promise<DbItem | null> {
  const { strict, ...rest } = options;
  try {
    if (!item?.title) throw new DbError("db: capture needs a title");
    return await request<DbItem>("/items", {
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
      headers: { "Idempotency-Key": idempotencyKey() },
    });
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
 * Whether a the database server is answering.
 *
 * Worth calling before a feature that depends on the database, so the interface can
 * say "the database is not running" instead of showing an empty list.
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

/** A key unique to one attempt, so a retry is recognised as the same write. */
function idempotencyKey(): string {
  return `genesis-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}
