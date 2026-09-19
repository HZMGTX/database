'use strict';

/**
 * Vault — a bridge to a local personal database.
 *
 * Vault (https://github.com/HZMGTX/database) keeps notes, tasks, events,
 * links, people and files in one SQLite file with full-text search over all
 * of it. This service lets VYREX search what is in there and write things
 * into it from a command handler.
 *
 * Three things are deliberately true of this file:
 *
 *   1. **Nothing in VYREX imports it until you choose to.** Adding it changes
 *      no behaviour. Wire it into a command when you want it.
 *   2. **Every call fails soft.** A note-taking sidecar must never be able to
 *      take the bot down, so a Vault that is not running, or is slow, gives
 *      back an empty result instead of throwing into a handler. Pass
 *      `strict: true` on any call where you would rather see the error.
 *   3. **It touches no VYREX data.** It speaks HTTP to a separate process.
 *      The bot's own database, `engine.js` and `applySchema.js` are not
 *      involved and are not modified.
 *
 * Configuration, all optional:
 *
 *   VAULT_URL         where the server is      (default http://127.0.0.1:8787)
 *   VAULT_TOKEN       bearer token, if the server was started with --token
 *   VAULT_TIMEOUT_MS  per-request timeout      (default 2000)
 *   VAULT_DEBUG       set to 1 to log why a call failed
 *
 * Start the server on the machine running the bot:
 *
 *   cd /path/to/database && ./vault serve
 *
 * @module services/vaultService
 */

const DEFAULT_BASE = 'http://127.0.0.1:8787';
const DEFAULT_TIMEOUT_MS = 2000;
const API = '/api/v1';

/**
 * Where the Vault server is.
 *
 * Read on every call rather than captured at load time, so that all four
 * settings behave the same way. Reading the URL once while reading the token
 * per request is the kind of asymmetry nobody notices until an operator
 * changes VAULT_URL, sees no effect, and has no way to tell why.
 *
 * @returns {string} The base URL, without a trailing slash
 */
function baseUrl() {
  return (process.env.VAULT_URL || DEFAULT_BASE).replace(/\/+$/, '');
}

/**
 * The configured per-request timeout.
 * @returns {number} Milliseconds
 */
function timeout() {
  return Number(process.env.VAULT_TIMEOUT_MS) || DEFAULT_TIMEOUT_MS;
}

/**
 * Builds request headers, adding the bearer token when one is configured.
 * @param {object} [extra] - Additional headers to merge in
 * @returns {object} The header map to send
 */
function buildHeaders(extra) {
  const headers = { 'Content-Type': 'application/json', ...(extra || {}) };
  const token = process.env.VAULT_TOKEN;
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

/**
 * Logs a failure when VAULT_DEBUG is set, and stays silent otherwise.
 * @param {string} what - The operation that failed
 * @param {Error} error - The failure
 */
function note(what, error) {
  if (process.env.VAULT_DEBUG) {
    console.warn(`[vault] ${what} failed: ${error.message}`);
  }
}

/**
 * Performs one request against the Vault API.
 *
 * Vault answers errors with RFC 9457 problem+json, so a failure always
 * carries a sentence worth logging rather than only a status code.
 *
 * @param {string} path - Path below the API root, e.g. `/items`
 * @param {object} [options]
 * @param {string} [options.method='GET'] - HTTP method
 * @param {object} [options.body] - JSON body, serialised for you
 * @param {object} [options.headers] - Extra headers
 * @param {number} [options.timeoutMs] - Overrides VAULT_TIMEOUT_MS
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
        throw new Error(`vault: ${response.status} with an unreadable body`);
      }
    }

    if (!response.ok) {
      const detail = payload && (payload.detail || payload.title);
      const error = new Error(detail || `vault: HTTP ${response.status}`);
      error.status = response.status;
      error.problem = payload;
      throw error;
    }
    return payload;
  } catch (error) {
    if (error.name === 'AbortError') {
      const timeout = new Error(`vault: no answer within ${timeoutMs}ms`);
      timeout.code = 'VAULT_TIMEOUT';
      throw timeout;
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Searches Vault with its query language.
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
 * @returns {Promise<Array<object>>} Ranked hits, or [] when Vault is away
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
 * Writes something into Vault.
 *
 * The request carries an `Idempotency-Key`, so retrying after a timeout
 * returns the first call's result instead of creating a second copy — which
 * matters here, because a timeout is the failure a Discord handler is most
 * likely to hit and most likely to retry.
 *
 * @param {object} item
 * @param {string} item.title - Required; what the thing is
 * @param {string} [item.body=''] - Longer text, indexed for search
 * @param {string} [item.kind='note'] - note | task | event | link | person
 * @param {Array<string>} [item.tags=[]] - Hierarchical, e.g. `support/tickets`
 * @param {object} [item.props={}] - Arbitrary fields, queryable once written
 * @param {object} [item.facet] - Kind-specific fields, e.g. `{ status, due }`
 * @param {boolean} [item.strict=false] - Throw instead of returning null
 * @returns {Promise<object|null>} The created item, or null when Vault is away
 */
async function capture(item = {}) {
  const { title, body = '', kind = 'note', tags = [], props = {}, facet,
          strict = false } = item;
  try {
    if (!title) throw new Error('vault: capture needs a title');
    const payload = { kind, title, body, tags, props };
    if (facet) payload.facet = facet;
    return await request('/items', {
      method: 'POST',
      body: payload,
      headers: { 'Idempotency-Key': idempotencyKey() },
    });
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
 * Checks whether a Vault server is answering.
 *
 * Use this to gate a command rather than letting it silently return nothing:
 * "Vault is not running" is a better reply than an empty list.
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
 * The characters Vault wraps a matched run in: STX and ETX.
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
 * A key unique to this attempt, so a retry is recognised as the same write.
 * @returns {string} The idempotency key
 */
function idempotencyKey() {
  return `vyrex-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

module.exports = {
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
