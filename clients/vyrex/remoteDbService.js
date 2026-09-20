'use strict';

/**
 * A client for the shared database.
 *
 * https://github.com/HZMGTX/database keeps notes, tasks, events, links,
 * people and files in one place, with full-text search over all of it. This service lets VYREX search what is in there and write things
 * into it from a command handler.
 *
 * Three things are deliberately true of this file:
 *
 *   1. **Additive.** The `db` command is the only thing that uses it.
 *   2. **Every call fails soft.** A note-taking sidecar must never be able to
 *      take the bot down, so a database that is not answering, or is slow, gives
 *      back an empty result instead of throwing into a handler. Pass
 *      `strict: true` on any call where you would rather see the error.
 *   3. **It touches no VYREX data.** It speaks HTTP to a separate process.
 *      The bot's own database, `engine.js` and `applySchema.js` are not
 *      involved and are not modified.
 *
 * Configuration, all optional:
 *
 *   DB_TOKEN       required; the bearer token. It is the only one that
 *                  has to be set, because it is the only one that is secret.
 *   DB_URL         override only to point somewhere else
 *   DB_TIMEOUT_MS  per-request timeout (default 8000; a serverless cold
 *                  start can take a second)
 *   DB_DEBUG       set to 1 to log why a call failed
 *
 * @module services/remoteDbService
 */

// The deployed database. A localhost default was right when the only
// database was one you started by hand on the same machine; it is wrong now
// that there is a real one, because it makes every call fail by default and
// look like the bot is broken rather than unconfigured.
const DEFAULT_BASE = 'https://database-null-s-projects4.vercel.app';
// Two seconds was right for a server on the same machine. The database is
// now a serverless function across the internet, and a cold start alone can
// take longer than that -- verified: `db legendary` timed out at 2000ms on
// the first call and answered in well under a second on the next. A timeout
// that fires on the first request of the day is worse than a slow one.
const DEFAULT_TIMEOUT_MS = 8000;
const API = '/api/v1';

/**
 * Whether a token is configured at all.
 *
 * Worth separating from "the server did not answer": one is something to
 * fix in the environment, the other is the database being down, and telling
 * them apart is the difference between a useful message and a shrug.
 */
function configured() {
  return Boolean(process.env.DB_TOKEN);
}

/**
 * Where the database is.
 *
 * Read on every call rather than captured at load time, so that all four
 * settings behave the same way. Reading the URL once while reading the token
 * per request is the kind of asymmetry nobody notices until an operator
 * changes DB_URL, sees no effect, and has no way to tell why.
 *
 * @returns {string} The base URL, without a trailing slash
 */
function baseUrl() {
  return (process.env.DB_URL || DEFAULT_BASE).replace(/\/+$/, '');
}

/**
 * The configured per-request timeout.
 * @returns {number} Milliseconds
 */
function timeout() {
  return Number(process.env.DB_TIMEOUT_MS) || DEFAULT_TIMEOUT_MS;
}

/**
 * Builds request headers, adding the bearer token when one is configured.
 * @param {object} [extra] - Additional headers to merge in
 * @returns {object} The header map to send
 */
function buildHeaders(extra) {
  const headers = { 'Content-Type': 'application/json', ...(extra || {}) };
  const token = process.env.DB_TOKEN;
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

/**
 * Logs a failure when DB_DEBUG is set, and stays silent otherwise.
 * @param {string} what - The operation that failed
 * @param {Error} error - The failure
 */
function note(what, error) {
  if (process.env.DB_DEBUG) {
    console.warn(`[db] ${what} failed: ${error.message}`);
  }
}

/**
 * Performs one request against the database API.
 *
 * The database answers errors with RFC 9457 problem+json, so a failure always
 * carries a sentence worth logging rather than only a status code.
 *
 * @param {string} path - Path below the API root, e.g. `/items`
 * @param {object} [options]
 * @param {string} [options.method='GET'] - HTTP method
 * @param {object} [options.body] - JSON body, serialised for you
 * @param {object} [options.headers] - Extra headers
 * @param {number} [options.timeoutMs] - Overrides DB_TIMEOUT_MS
 * @returns {Promise<object|null>} The parsed response body
 * @throws {Error} On transport failure, timeout, or a non-2xx response
 */
async function request(path, options = {}) {
  const { method = 'GET', body, headers } = options;
  const timeoutMs = options.timeoutMs || timeout();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetch(`${baseUrl()}${API}${path}`, {
      method,
      headers: buildHeaders(headers),
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });

    const text = await response.text();
    let payload = null;
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch (parseError) {
        throw new Error(`db: ${response.status} with an unreadable body`);
      }
    }

    if (!response.ok) {
      const detail = payload && (payload.detail || payload.title);
      const error = new Error(detail || `db: HTTP ${response.status}`);
      error.status = response.status;
      error.problem = payload;
      throw error;
    }
    return payload;
  } catch (error) {
    if (error.name === 'AbortError') {
      const timeout = new Error(`db: no answer within ${timeoutMs}ms`);
      timeout.code = 'DB_TIMEOUT';
      throw timeout;
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Searches the database with its query language.
 *
 * The grammar is the same one the CLI and web UI use: bare words are ANDed
 * and ranked, `"quoted"` is a phrase, `-word` excludes, and filters such as
 * `kind:task`, `status:todo`, `tag:support/*`, `due:today`, `is:open` and
 * `value>5000` narrow structurally.
 *
 * @param {string} query - e.g. `kind:task status:todo tag:support`
 * @param {object} [options]
 * @param {number} [options.limit=20] - Maximum hits to return
 * @param {boolean} [options.strict=false] - Throw instead of returning []
 * @returns {Promise<Array<object>>} Ranked hits, or [] when the database is unreachable
 */
async function search(query, options = {}) {
  const { limit = 20, strict = false } = options;
  try {
    const params = new URLSearchParams({ q: query, limit: String(limit) });
    const payload = await request(`/search?${params}`);
    return (payload && payload.hits) || [];
  } catch (error) {
    note('search', error);
    if (strict) throw error;
    return [];
  }
}

/**
 * Writes something into the database.
 *
 * A request that never gets an answer — a timeout, a reset connection — is
 * sent once more, because that is the one failure where the caller cannot
 * tell whether the write landed. Both attempts carry the same
 * `Idempotency-Key`, and the database returns what the first one created
 * rather than writing a second copy, so the retry cannot duplicate
 * anything.
 *
 * A failure the database *answered* with is not retried: it would be
 * refused again, and the key is already spent on it.
 *
 * The cost of the retry is time. A write that the database never answers
 * takes up to twice `DB_TIMEOUT_MS` before giving up, so defer the
 * interaction before calling this. Losing what somebody typed is worse than
 * making them wait for it.
 *
 * @param {object} item
 * @param {string} item.title - Required; what the thing is
 * @param {string} [item.body=''] - Longer text, indexed for search
 * @param {string} [item.kind='note'] - note | task | event | link | person
 * @param {Array<string>} [item.tags=[]] - Hierarchical, e.g. `support/tickets`
 * @param {object} [item.props={}] - Arbitrary fields, queryable once written
 * @param {object} [item.facet] - Kind-specific fields, e.g. `{ status, due }`
 * @param {string} [item.idempotencyKey] - Reuse a key to make your own retry
 *   safe; one is generated per call otherwise
 * @param {boolean} [item.strict=false] - Throw instead of returning null
 * @returns {Promise<object|null>} The created item, or null when the database is unreachable
 */
async function capture(item = {}) {
  const { title, body = '', kind = 'note', tags = [], props = {}, facet,
          idempotencyKey, strict = false } = item;
  try {
    if (!title) throw new Error('db: capture needs a title');
    const payload = { kind, title, body, tags, props };
    if (facet) payload.facet = facet;

    // One key across both attempts. Generating a second one for the retry
    // would defeat the whole point and write the item twice.
    const key = idempotencyKey || newIdempotencyKey();
    const send = () => request('/items', {
      method: 'POST',
      body: payload,
      headers: { 'Idempotency-Key': key },
    });

    try {
      return await send();
    } catch (error) {
      // `status` is set only when the database answered. Without it the
      // request never arrived, or its answer never came back, and neither
      // the caller nor this code knows which.
      if (error.status) throw error;
      note('capture retrying', error);
      return await send();
    }
  } catch (error) {
    note('capture', error);
    if (strict) throw error;
    return null;
  }
}

/**
 * Reads one item by its full uid or its eight-character short handle.
 * @param {string} ref - uid or short handle
 * @param {object} [options]
 * @param {boolean} [options.strict=false] - Throw instead of returning null
 * @returns {Promise<object|null>} The item, or null if absent or unreachable
 */
async function get(ref, options = {}) {
  try {
    return await request(`/items/${encodeURIComponent(ref)}`);
  } catch (error) {
    note('get', error);
    if (options.strict) throw error;
    return null;
  }
}

/**
 * Checks whether the database is answering.
 *
 * Use this to gate a command rather than letting it silently return nothing:
 * "the database is not answering" is a better reply than an empty list.
 *
 * @param {object} [options]
 * @param {number} [options.timeoutMs=500] - Short on purpose
 * @returns {Promise<boolean>} True when a healthy server answered
 */
async function available(options = {}) {
  try {
    const health = await request('/health', { timeoutMs: options.timeoutMs || 500 });
    return Boolean(health && health.ok);
  } catch (error) {
    note('health', error);
    return false;
  }
}

/**
 * The characters a matched run is wrapped in: STX and ETX.
 *
 * Control characters rather than markup, so each caller decides how to show a
 * match. A hit's `snippet` carries them, and sending one straight into a
 * Discord message puts two invisible control characters in the channel.
 */
const MARK_START = '\u0002';
const MARK_END = '\u0003';

/**
 * Turns a hit's snippet into something renderable.
 *
 * @param {string} snippet - A hit's `snippet` field
 * @param {object} [marks] - What to replace the markers with
 * @param {string} [marks.start=''] - Opens a match, e.g. `**` for Discord bold
 * @param {string} [marks.end=''] - Closes a match
 * @returns {string} The snippet with the markers replaced
 *
 * @example
 * renderSnippet(hit.snippet)                          // plain text
 * renderSnippet(hit.snippet, { start: '**', end: '**' })  // Discord bold
 */
function renderSnippet(snippet, marks = {}) {
  return String(snippet || '')
    .split(MARK_START).join(marks.start || '')
    .split(MARK_END).join(marks.end || '');
}

/**
 * A key unique to one write, so every attempt at it is recognised as the
 * same write rather than as several.
 * @returns {string} The idempotency key
 */
function newIdempotencyKey() {
  return `vyrex-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

module.exports = {
  configured,
  search,
  capture,
  get,
  available,
  request,
  renderSnippet,
  baseUrl,
  MARK_START,
  MARK_END,
};
