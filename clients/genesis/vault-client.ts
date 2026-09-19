/**
 * Vault client for genesis-ai-dev.
 *
 * Talks to a local Vault server over its REST API. Additive: nothing in the
 * monorepo imports this until you choose to.
 *
 * Install:
 *   copy to  lib/vault-client/src/index.ts   (or anywhere convenient)
 *
 * Use:
 *   import { search, capture } from '@genesis/vault-client';
 *   const hits = await search('kind:task status:todo');
 *
 * Calls fail soft by default: a sidecar must not be able to fail a request
 * path. Pass `{ strict: true }` where you would rather see the error.
 */

export interface VaultHit {
  uid: string;
  kind: string;
  title: string;
  snippet: string;
  tags: string[];
  score: number;
  updated_at: string;
  pinned: boolean;
  trashed: boolean;
}

export interface VaultItem {
  uid: string;
  kind: string;
  title: string;
  body: string;
  props: Record<string, unknown>;
  tags: string[];
  rev: number;
  created_at: string;
  updated_at: string;
}

export interface VaultProblem {
  type: string;
  title: string;
  status: number;
  detail: string;
}

export interface ClientOptions {
  baseUrl?: string;
  token?: string;
  timeoutMs?: number;
  strict?: boolean;
}

const BASE = process.env.VAULT_URL ?? 'http://127.0.0.1:8787';
const TOKEN = process.env.VAULT_TOKEN ?? '';
const TIMEOUT = Number(process.env.VAULT_TIMEOUT_MS ?? 2000);

export class VaultError extends Error {
  readonly status: number;
  readonly problem?: VaultProblem;

  constructor(message: string, status: number, problem?: VaultProblem) {
    super(message);
    this.name = 'VaultError';
    this.status = status;
    this.problem = problem;
  }
}

async function request<T>(path: string, init: RequestInit & ClientOptions = {}): Promise<T> {
  const { baseUrl = BASE, token = TOKEN, timeoutMs = TIMEOUT, ...rest } = init;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetch(`${baseUrl}${path}`, {
      ...rest,
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(rest.headers ?? {}),
      },
      signal: controller.signal,
    });

    const text = await response.text();
    const payload = text ? JSON.parse(text) : null;

    if (!response.ok) {
      const problem = payload as VaultProblem | null;
      throw new VaultError(
        problem?.detail || problem?.title || `vault: HTTP ${response.status}`,
        response.status,
        problem ?? undefined,
      );
    }
    return payload as T;
  } finally {
    clearTimeout(timer);
  }
}

export async function search(
  query: string,
  options: ClientOptions & { limit?: number } = {},
): Promise<VaultHit[]> {
  const { limit = 20, strict = false, ...rest } = options;
  try {
    const params = new URLSearchParams({ q: query, limit: String(limit) });
    const payload = await request<{ hits: VaultHit[] }>(`/api/v1/search?${params}`, rest);
    return payload.hits ?? [];
  } catch (error) {
    if (strict) throw error;
    return [];
  }
}

export async function capture(
  item: { title: string; body?: string; kind?: string; tags?: string[];
          props?: Record<string, unknown> },
  options: ClientOptions = {},
): Promise<VaultItem | null> {
  const { strict = false, ...rest } = options;
  try {
    return await request<VaultItem>('/api/v1/items', {
      ...rest,
      method: 'POST',
      body: JSON.stringify({ kind: 'note', body: '', tags: [], props: {}, ...item }),
      // Makes a retry after a timeout safe: a replayed key returns the
      // original result rather than creating a second item.
      headers: { 'Idempotency-Key': `genesis-${Date.now()}-${Math.random().toString(36).slice(2)}` },
    });
  } catch (error) {
    if (strict) throw error;
    return null;
  }
}

export async function get(ref: string, options: ClientOptions = {}): Promise<VaultItem | null> {
  try {
    return await request<VaultItem>(`/api/v1/items/${encodeURIComponent(ref)}`, options);
  } catch (error) {
    if (options.strict) throw error;
    return null;
  }
}

export async function available(options: ClientOptions = {}): Promise<boolean> {
  try {
    const health = await request<{ ok: boolean }>('/api/v1/health',
      { ...options, timeoutMs: options.timeoutMs ?? 500 });
    return Boolean(health.ok);
  } catch {
    return false;
  }
}
