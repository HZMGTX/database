# the database clients

Two modules that let the projects talk to a running the database: search what the database
knows, and write into it.

**Both are already installed.** These are the canonical copies, kept here so
the code lives beside the server it talks to; each is also present in its own
project, where it is the file that actually runs.

| Project | Installed as | Pull request |
| --- | --- | --- |
| VYREX | `src/services/remoteDbService.js` + `tests/remoteDbService.test.js` | [VYREX#27](https://github.com/HZMGTX/VYREX/pull/27) |
| genesis-ai-dev | `lib/remote-db-client/` — the package `@workspace/remote-db-client` | [genesis-ai-dev#49](https://github.com/HZMGTX/genesis-ai-dev/pull/49) |

Neither project imports its client until you add an import, so both are inert
until you use them.

If you change one here, copy it back across; there is no build step that does
it for you, deliberately — a generated file in someone else's repository is
worse than a copied one.

## Running the server

```
cd /path/to/database
./db serve
```

That binds `127.0.0.1:8787`. Set `DB_URL` if you move it and `DB_TOKEN`
if you started it with `--token` or `--lan`.

## VYREX

```js
const database = require('./services/remoteDbService');

if (!await database.available()) return interaction.reply('the database is not running.');

const hits = await database.search('kind:task status:todo tag:support');

await database.capture({
  title: `Ticket #${ticket.id} escalated`,
  body: ticket.summary,
  kind: 'task',
  tags: ['support', `guild/${guild.id}`],
  props: { ticket_id: ticket.id, waited_hours: 49 },
  facet: { status: 'todo', priority: 4 },
});
```

## genesis-ai-dev

```ts
import { search, capture, available, renderSnippet } from "@workspace/remote-db-client";

if (await available()) {
  const page = await search("kind:note tag:design", { limit: 10 });
}
```

## Both

Every call **fails soft**: an unreachable or slow the database gives back an empty
result rather than throwing, because the database is a separate process somebody has
to start and a sidecar must not be able to fail a request that was not about
it. Pass `strict` where you would rather handle the error — you get the HTTP
status and the RFC 9457 problem document the database returned.

A hit's `snippet` wraps each match in **STX and ETX**, not in markup, so that
each caller picks its own rendering. Printing one unprocessed puts two
invisible control characters into your output, so both clients export
`renderSnippet`:

```js
renderSnippet(hit.snippet)                             // plain
renderSnippet(hit.snippet, { start: '**', end: '**' }) // Discord, Markdown
```

A search page also carries `understood`: what each clause of the query was
taken to mean, in plain English. Show it beside the results and someone can
see that `duw:friday` was read as a property nobody has rather than as a date.

## Reading VYREX's own database

Separate from the clients, the database can read the bot's SQLite store directly:

```
./db connect vyrex --db-path /path/to/vyrex.db --describe
./db connect vyrex --db-path /path/to/vyrex.db
```

Opened `mode=ro` with `query_only`, which SQLite enforces itself — this cannot
write to the bot's database even if asked. WAL means it does not block the bot
either, so it is safe while the bot is live. Re-running reads only rows added
since last time.
