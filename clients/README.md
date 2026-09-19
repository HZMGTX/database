# Vault clients

Two small modules that let your projects talk to a running Vault, so they can
search what Vault knows and capture into it.

They live here rather than in the projects themselves because this repository
is the only one set up for writing — copy the file you want into place.

| For | Copy | To |
| --- | --- | --- |
| VYREX | `vyrex/vaultClient.js` | `src/services/vaultClient.js` |
| genesis-ai-dev | `genesis/vault-client.ts` | `lib/vault-client/src/index.ts` |

Both are **additive**. Nothing in either project imports them until you add an
import yourself, so dropping the file in changes no behaviour.

## Running Vault

```
cd /path/to/database
./vault serve
```

That binds to `127.0.0.1:8787`. Set `VAULT_URL` if you move it, and
`VAULT_TOKEN` if you started it with `--token` or `--lan`.

## VYREX

```js
const vault = require('./services/vaultClient');

// in a command handler
const hits = await vault.search('kind:task status:todo tag:support');
await vault.capture({
  title: `Ticket #${ticket.id} escalated`,
  body: ticket.summary,
  tags: ['support', `guild/${guild.id}`],
  props: { ticket_id: ticket.id, escalated_by: user.id },
});
```

Every call **fails soft**: if Vault is not running, `search` returns `[]` and
`capture` returns `null` rather than throwing. A note-taking sidecar must
never be able to take the bot down. Set `VAULT_DEBUG=1` to log why a call
failed.

## genesis-ai-dev

```ts
import { search, capture, available } from '@genesis/vault-client';

if (await available()) {
  const hits = await search('kind:note tag:design', { limit: 10 });
}
```

Same fail-soft default; pass `{ strict: true }` where you would rather see
the error.

## Reading VYREX's own database

Separate from these clients, Vault can read the bot's SQLite store directly:

```
./vault connect vyrex --db-path /path/to/vyrex.db --describe
./vault connect vyrex --db-path /path/to/vyrex.db
```

That connection is opened `mode=ro` with `query_only`, which SQLite enforces
itself — this cannot write to the bot's database even if asked to. WAL means
it does not block the bot either, so it is safe to run while the bot is live.
Re-running reads only rows added since last time.
