# `@workspace/vault-client`

Talks to a local [Vault](https://github.com/HZMGTX/database) — a personal
database that keeps notes, tasks, events, links, people and files in one
SQLite file with full-text search across all of it.

```ts
import { search, capture, available } from "@workspace/vault-client";

if (await available()) {
  const { hits } = await search("kind:note tag:design due:today");
}

await capture({
  title: `Snapshot ${snapshot.id} published`,
  tags: ["projects", `project/${project.id}`],
  props: { project_id: project.id, size_bytes: snapshot.size },
});
```

Search results carry their matched passage in `hit.snippet`, with each match
wrapped in two control characters (STX and ETX) rather than in HTML or ANSI,
so each caller chooses how to render it. Print one unprocessed and you put
two invisible control characters into your output, so pass it through
`renderSnippet`:

```ts
import { renderSnippet } from "@workspace/vault-client";

renderSnippet(hit.snippet);                             // plain text
renderSnippet(hit.snippet, { start: "<mark>", end: "</mark>" });
```

`page.understood` says, in plain English, what each clause of the query was
taken to mean — worth showing beside the results, because it is how someone
discovers that `duw:friday` was read as a property nobody has rather than as
a date.

## What this is not

It is **not** a replacement for anything in `lib/db`. Vault is a separate
process holding a separate store; this package speaks HTTP to it and touches
no Postgres, no Drizzle schema and no existing table. Nothing in the monorepo
imports it until you add an import, so adding the package changes no
behaviour.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `VAULT_URL` | `http://127.0.0.1:8787` | Where the server is |
| `VAULT_TOKEN` | — | Bearer token, if started with `--token` |
| `VAULT_TIMEOUT_MS` | `2000` | Per-request timeout |

Start the server on the same machine:

```
cd /path/to/database && ./vault serve
```

## Failure

Every function is fail-soft by default: an unreachable or slow Vault gives
back an empty result rather than throwing, because a sidecar should not be
able to fail a request that was not about it. Pass `{ strict: true }` where
you would rather handle the error, and you get a `VaultError` carrying the
HTTP status and the RFC 9457 problem document Vault returned.

## The query language

The same grammar the CLI and web UI use:

| | |
| --- | --- |
| `budget review` | both words, ranked by BM25 |
| `"exact phrase"` | a phrase |
| `-draft` | excluding a word |
| `kind:task,note` | either kind |
| `tag:work/*` | a tag and everything beneath it |
| `status:todo,doing` | task state |
| `due:today`, `starts>2026-10-01` | dates, in your zone |
| `size_bytes>5000` | any property, compared numerically |
| `is:open`, `is:orphan`, `has:link` | flags and structure |
| `sort:recent`, `limit:20` | output |
