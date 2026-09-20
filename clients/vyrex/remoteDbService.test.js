'use strict';

/**
 * The database bridge has one job beyond forwarding: it must never be the
 * reason a command handler throws. A database that does not answer is an
 * ordinary case, not an exceptional one — it is a serverless function
 * across the internet, so a cold start, a dropped connection or a slow
 * reply is a Tuesday.
 *
 * So these tests spend most of their effort on the unhappy paths: server
 * absent, server slow, connection dropped mid-answer, server returning an
 * error, server returning something that is not JSON. Every one of them has
 * to come back with an empty result and no exception, unless the caller
 * explicitly asked for strict mode.
 *
 * A stub HTTP server stands in for the database, which keeps the test
 * hermetic and lets it produce responses a real server would not.
 */

const { test, before, after, beforeEach } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');

let server;
let db;
let handler;          // set per test; receives (req, res)
const received = [];  // every request the stub saw

before(async () => {
  server = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', (chunk) => chunks.push(chunk));
    req.on('end', () => {
      received.push({
        method: req.method,
        url: req.url,
        headers: req.headers,
        body: chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) : null,
      });
      handler(req, res);
    });
  });

  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  process.env.DB_URL = `http://127.0.0.1:${server.address().port}`;
  delete process.env.DB_TOKEN;
  delete process.env.DB_DEBUG;
  db = require('../src/services/remoteDbService');
});

after(async () => {
  if (server) await new Promise((resolve) => server.close(resolve));
});

beforeEach(() => {
  received.length = 0;
  handler = (req, res) => {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, hits: [] }));
  };
});

function json(status, payload) {
  return (req, res) => {
    res.writeHead(status, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(payload));
  };
}

test('search sends the query and returns the hits', async () => {
  handler = json(200, { hits: [{ uid: 'abc', title: 'A ticket' }], total: 1 });

  const hits = await db.search('kind:task status:todo', { limit: 5 });

  assert.equal(hits.length, 1);
  assert.equal(hits[0].title, 'A ticket');
  assert.equal(received[0].method, 'GET');
  assert.match(received[0].url, /^\/api\/v1\/search\?/);

  const query = new URLSearchParams(received[0].url.split('?')[1]);
  assert.equal(query.get('q'), 'kind:task status:todo');
  assert.equal(query.get('limit'), '5');
});

test('search returns an empty array when the database answers with an error', async () => {
  handler = json(400, { title: 'Bad query', detail: 'unknown flag is:nonsense' });

  assert.deepEqual(await db.search('is:nonsense'), []);
});

test('search rethrows the problem detail in strict mode', async () => {
  handler = json(400, { title: 'Bad query', detail: 'unknown flag is:nonsense' });

  await assert.rejects(
    () => db.search('is:nonsense', { strict: true }),
    (error) => {
      assert.equal(error.message, 'unknown flag is:nonsense');
      assert.equal(error.status, 400);
      return true;
    },
  );
});

test('search survives a body that is not JSON at all', async () => {
  handler = (req, res) => {
    res.writeHead(200, { 'Content-Type': 'text/html' });
    res.end('<html>a proxy error page</html>');
  };

  assert.deepEqual(await db.search('anything'), []);
});

test('capture posts the item and carries an idempotency key', async () => {
  handler = json(201, { uid: 'new-uid', title: 'Ticket #412 escalated' });

  const created = await db.capture({
    title: 'Ticket #412 escalated',
    body: 'Customer waited two days.',
    tags: ['support', 'guild/9'],
    props: { ticket_id: 412 },
  });

  assert.equal(created.uid, 'new-uid');
  assert.equal(received[0].method, 'POST');
  assert.equal(received[0].url, '/api/v1/items');
  assert.equal(received[0].body.title, 'Ticket #412 escalated');
  assert.deepEqual(received[0].body.tags, ['support', 'guild/9']);
  assert.equal(received[0].body.props.ticket_id, 412);
  assert.match(received[0].headers['idempotency-key'], /^vyrex-\d+-[a-z0-9]+$/);
});

test('two captures use different idempotency keys', async () => {
  handler = json(201, { uid: 'x' });

  await db.capture({ title: 'one' });
  await db.capture({ title: 'two' });

  assert.notEqual(received[0].headers['idempotency-key'],
                  received[1].headers['idempotency-key']);
});

test('a write that gets no answer is retried once, with the same key', async () => {
  // A dropped connection is the case the whole idempotency mechanism exists
  // for: the write may have landed, and the caller cannot tell.
  let calls = 0;
  handler = (req, res) => {
    calls += 1;
    if (calls === 1) return res.destroy();
    res.writeHead(201, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ uid: 'written-once' }));
  };

  const created = await db.capture({ title: 'survives a dropped connection' });

  assert.equal(created.uid, 'written-once');
  assert.equal(received.length, 2);
  // The same key on both, which is what stops the retry writing a second
  // copy. A fresh key per attempt would make the retry the bug.
  assert.equal(received[0].headers['idempotency-key'],
               received[1].headers['idempotency-key']);
});

test('a write the database refused is not retried', async () => {
  // An answer means the database saw it and said no. Sending it again would
  // be refused again, and the key is already spent on that answer.
  handler = json(400, { detail: 'An item needs at least a title or a body.' });

  assert.equal(await db.capture({ title: 'refused' }), null);
  assert.equal(received.length, 1);
});

test('a caller can bring their own key and retry safely themselves', async () => {
  handler = json(201, { uid: 'written-once' });

  await db.capture({ title: 'one thing', idempotencyKey: 'ticket-412' });
  await db.capture({ title: 'one thing', idempotencyKey: 'ticket-412' });

  assert.equal(received[0].headers['idempotency-key'], 'ticket-412');
  assert.equal(received[1].headers['idempotency-key'], 'ticket-412');
});

test('capture refuses an item with no title, without calling out', async () => {
  assert.equal(await db.capture({ body: 'orphaned text' }), null);
  assert.equal(received.length, 0);
});

test('a slow server times out rather than hanging a handler', async () => {
  handler = () => { /* never responds */ };

  // The timeout is set explicitly rather than leaning on the default. This
  // test is about the mechanism giving up at all, and pinning it to
  // whatever the default happens to be makes it fail the moment that
  // number changes for an unrelated reason -- which is exactly what
  // happened when the default moved from 2s to 8s for cold starts.
  const good = process.env.DB_TIMEOUT_MS;
  process.env.DB_TIMEOUT_MS = '600';
  try {
    const started = Date.now();
    const hits = await db.search('slow', { limit: 1 });
    const elapsed = Date.now() - started;

    assert.deepEqual(hits, []);
    assert.ok(elapsed >= 500, `gave up too early, after ${elapsed}ms`);
    assert.ok(elapsed < 3000, `gave up after ${elapsed}ms`);
  } finally {
    if (good === undefined) delete process.env.DB_TIMEOUT_MS;
    else process.env.DB_TIMEOUT_MS = good;
  }
});

test('a server that is not running yields empty results, not a throw', async () => {
  // Nothing listening at all: a wrong DB_URL, or no network out of the
  // host. Every call has to answer for itself rather than throw.
  const good = process.env.DB_URL;
  process.env.DB_URL = 'http://127.0.0.1:9';
  try {
    assert.deepEqual(await db.search('nobody home'), []);
    assert.equal(await db.capture({ title: 'nobody home' }), null);
    assert.equal(await db.get('abcdef12'), null);
    assert.equal(await db.available(), false);
  } finally {
    process.env.DB_URL = good;
  }

  // ...and it recovers the moment the server is back.
  assert.deepEqual(await db.search('still fine'), []);
  assert.equal(received.length, 1);
});

test('DB_URL is honoured when it changes, and trailing slashes are trimmed', async () => {
  const good = process.env.DB_URL;
  process.env.DB_URL = `${good}///`;
  try {
    await db.search('trimmed');
    assert.equal(received[0].url.startsWith('/api/v1/search?'), true);
  } finally {
    process.env.DB_URL = good;
  }
});

test('available reports true only when the server says ok', async () => {
  handler = json(200, { ok: true, version: '1.0.0' });
  assert.equal(await db.available(), true);

  handler = json(503, { title: 'Not ready' });
  assert.equal(await db.available(), false);
});

test('the bearer token is sent only when one is configured', async () => {
  handler = json(200, { hits: [] });

  await db.search('no token');
  assert.equal(received[0].headers.authorization, undefined);

  process.env.DB_TOKEN = 'secret-token';
  try {
    await db.search('with token');
    assert.equal(received[1].headers.authorization, 'Bearer secret-token');
  } finally {
    delete process.env.DB_TOKEN;
  }
});

test('get looks an item up by its short handle', async () => {
  handler = json(200, { uid: '01a0bb3f6522764c86e8335019b3cd5e', title: 'Found' });

  const item = await db.get('19b3cd5e');

  assert.equal(item.title, 'Found');
  assert.equal(received[0].url, '/api/v1/items/19b3cd5e');
});

test('renderSnippet replaces the highlight markers', () => {
  const snippet = `Please review the ${db.MARK_START}budget${db.MARK_END} before Friday.`;

  assert.equal(db.renderSnippet(snippet),
               'Please review the budget before Friday.');
  assert.equal(db.renderSnippet(snippet, { start: '**', end: '**' }),
               'Please review the **budget** before Friday.');
  // A snippet is empty when the match was in the title, so there is no
  // passage to quote. That must not become the string "undefined".
  assert.equal(db.renderSnippet(''), '');
  assert.equal(db.renderSnippet(undefined), '');
});
