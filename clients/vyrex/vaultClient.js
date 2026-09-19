'use strict';

/**
 * Vault client for VYREX.
 *
 * Talks to a local Vault server over its REST API. Nothing here imports
 * anything from VYREX and nothing in VYREX imports this until you choose to,
 * so dropping the file in changes no behaviour on its own.
 *
 * Install:
 *   copy this file to  src/services/vaultClient.js
 *
 * Use:
 *   const vault = require('./services/vaultClient');
 *   const hits = await vault.search('kind:task status:todo');
 *   await vault.capture({ title: 'Ticket #412 escalated', tags: ['support'] });
 *
 * Every call fails soft. A note-taking sidecar must never be able to take the
 * bot down, so a Vault that is not running, or is slow, resolves to an empty
 * result rather than throwing into a command handler.
 */

const DEFAULT_BASE = process.env.VAULT_URL || 'http://127.0.0.1:8787';
const DEFAULT_TOKEN = process.env.VAULT_TOKEN || '';
const DEFAULT_TIMEOUT_MS = Number(process.env.VAULT_TIMEOUT_MS || 2000);

function buildHeaders(extra) {
  const headers = { 'Content-Type': 'application/json', ...(extra || {}) };
  if (DEFAULT_TOKEN) headers.Authorization = `Bearer ${DEFAULT_TOKEN}`;
  return headers;
}

async function request(path, { method = 'GET', body, timeoutMs = DEFAULT_TIMEOUT_MS,
                               headers } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${DEFAULT_BASE}${path}`, {
      method,
      headers: buildHeaders(headers),
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });

    const text = await response.text();
    const payload = text ? JSON.parse(text) : null;

    if (!response.ok) {
      // Vault answers with RFC 9457 problem+json, so there is always a
      // sentence worth logging rather than just a status code.
      const detail = payload && (payload.detail || payload.title);
      const error = new Error(detail || `vault: HTTP ${response.status}`);
      error.status = response.status;
      error.problem = payload;
      throw error;
    }
    return payload;
  } finally {
    clearTimeout(timer);
  }
}

/** Search. Returns [] if Vault is unreachable. */
async function search(query, { limit = 20 } = {}) {
  try {
    const params = new URLSearchParams({ q: query, limit: String(limit) });
    const payload = await request(`/api/v1/search?${params}`);
    return payload && payload.hits ? payload.hits : [];
  } catch (error) {
    if (process.env.VAULT_DEBUG) console.warn('[vault] search failed:', error.message);
    return [];
  }
}

/** Store something. Returns the created item, or null if Vault is unreachable. */
async function capture({ title, body = '', kind = 'note', tags = [], props = {} } = {}) {
  try {
    return await request('/api/v1/items', {
      method: 'POST',
      body: { kind, title, body, tags, props },
      // An Idempotency-Key makes a retry after a timeout safe: the second
      // call returns the first call's result instead of creating a twin.
      headers: { 'Idempotency-Key': `vyrex-${Date.now()}-${Math.random().toString(36).slice(2)}` },
    });
  } catch (error) {
    if (process.env.VAULT_DEBUG) console.warn('[vault] capture failed:', error.message);
    return null;
  }
}

/** One item by uid or short handle. */
async function get(ref) {
  try {
    return await request(`/api/v1/items/${encodeURIComponent(ref)}`);
  } catch (error) {
    return null;
  }
}

/** Is a Vault server there? Useful for gating a command on availability. */
async function available({ timeoutMs = 500 } = {}) {
  try {
    const health = await request('/api/v1/health', { timeoutMs });
    return Boolean(health && health.ok);
  } catch (error) {
    return false;
  }
}

module.exports = { search, capture, get, available, request, DEFAULT_BASE };
