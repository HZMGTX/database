# The database

A Postgres database with a web interface and an HTTP API, deployed on Vercel.
It stores things, searches them, and keeps them.

## Setting it up

Two steps, once.

**1. Attach a Postgres.** In the Vercel dashboard, open this project →
**Storage** → **Create** → **Postgres** → connect it to the project. Vercel
sets `DATABASE_URL` for you.

**2. Set a token.** Project → **Settings** → **Environment Variables** → add
`API_TOKEN` with a long random value, for every environment. Then redeploy.

Without `API_TOKEN` the API refuses every request rather than defaulting to
open. A database on a public URL with no token is readable and writable by
anyone who finds it, so failing shut is the only safe default.

Then, once:

```
curl -X POST -H "Authorization: Bearer $API_TOKEN" https://<your-url>/api/admin/migrate
```

and, if you want the existing data loaded, call this until it answers
`"done": true` (it does about 4,000 rows per call):

```
curl -X POST -H "Authorization: Bearer $API_TOKEN" https://<your-url>/api/admin/import
```

## The API

Everything except `/api/health` needs `Authorization: Bearer <API_TOKEN>`.

Every route is also served under `/api/v1/…`, because the clients in the
projects were written against that prefix and a merged client cannot be asked
to change.

| | |
| --- | --- |
| `GET /api/health` | up? attached? how many items? — no token needed |
| `GET /api/stats` | counts by kind and tag |
| `GET /api/search?q=…&limit=&offset=` | search |
| `GET /api/items?q=…&limit=&offset=` | the same thing, under the collection |
| `POST /api/items` | write one |
| `GET /api/items/:ref` | read one, with its links |
| `PATCH /api/items/:ref` | change only the fields you send |
| `DELETE /api/items/:ref` | to the trash |
| `DELETE /api/items/:ref?purge=1` | gone, with its history |

`:ref` is the full uid or its **last** eight characters. The tail, not the
head: these ids start with a millisecond timestamp, so a run written in the
same instant shares a prefix and differs only at the end.

`PATCH` accepts `expected_rev` (or an `If-Match` header). If the item changed
since you read it you get **409** instead of a silent overwrite.

`POST /api/items` accepts an `Idempotency-Key` header. Send the same key
again and you get back the item the first request created, with
`Idempotent-Replayed: true`, rather than a second copy of it. That is what
makes it safe to retry a write that timed out — the one failure where you
cannot tell whether it landed. Keys are remembered for a day. If what a key
created has since been purged, reusing it is a **409** rather than a
resurrection.

The schema applies itself: the first request to reach a database that is
behind the deployed code brings it up to date, and `GET /api/health` reports
which version is in force. `POST /api/admin/migrate` does the same thing
deliberately and shows you the tables.

## The query language

| | |
| --- | --- |
| `sword shield` | both words, ranked |
| `"exact phrase"` | that phrase |
| `-broken` | without that word |
| `kind:fish,ores` | either kind |
| `tag:work/*` | that tag and everything under it |
| `value>5000` | any field, compared as a number |
| `rarity:Legendary` | any field, matched as text |
| `is:pinned` | also `untagged`, `orphan`, `trashed`, `any` |
| `has:body` | also `tag`, `props`, `link` |
| `sort:recent limit:50` | output |

Ranking is Postgres full-text with weights: title first, then tags, then the
body, then the values inside `props`. That last one matters more than it
sounds — most of what is in here is catalogue data whose real content is
`{"rarity": "Legendary", "value": 8800}`.

## Running it locally

```
cd web
npm install
DATABASE_URL=postgresql://localhost/yourdb API_TOKEN=dev node tools/serve.mjs
```

`tools/serve.mjs` routes `api/` the same way Vercel does, so the handlers can
be exercised against a real Postgres before anything is deployed. It is a
development tool; nothing deployed imports it.

## What this is not

It is **not** a replacement for VYREX's own database. That one is
`better-sqlite3`, synchronous because the file is on the same disk, with 899
call sites depending on that. No network-backed database can be synchronous
in Node. VYREX talks to this one over HTTP, asynchronously, at whatever few
places are worth it — see `clients/`.
