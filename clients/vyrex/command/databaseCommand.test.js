'use strict';

/**
 * The `db` command reaches a database that lives in another process, over
 * the network, behind a token. Each of those is a way for it to be
 * unavailable, and a command that answers "no results" to all three is
 * useless: an unset token, a server that is down and a query with a typo
 * need three different answers.
 *
 * These check that it distinguishes them, and that a result is rendered
 * without leaking the control characters the snippet arrives wrapped in.
 */

const { test, before, beforeEach, after } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');

let server;
let command;
let handler = null;

/** The smallest thing the command treats as a Discord message. */
function fakeMessage(content) {
  const replies = [];
  return {
    content,
    author: { id: '1' },
    guild: { id: '2' },
    reply: (payload) => { replies.push(payload); return Promise.resolve(payload); },
    replies,
  };
}

function firstEmbed(message) {
  const data = message.replies[0]?.embeds?.[0]?.data;
  assert.ok(data, 'the command replied with no embed');
  return data;
}

before(async () => {
  server = http.createServer((req, res) => handler(req, res));
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  process.env.DB_URL = `http://127.0.0.1:${server.address().port}`;
  [command] = require('../src/commands/utility/database.js');
});

after(async () => { if (server) await new Promise((r) => server.close(r)); });

beforeEach(() => {
  process.env.DB_TOKEN = 'test-token';
  handler = (req, res) => {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ hits: [], total: 0 }));
  };
});

test('with no query it explains the syntax rather than searching', async () => {
  let called = false;
  handler = (req, res) => { called = true; res.writeHead(200); res.end('{}'); };

  const msg = fakeMessage('');
  await command.handler(msg, []);

  assert.match(firstEmbed(msg).title, /Search the database/);
  assert.equal(called, false, 'it searched for nothing');
});

test('an unset token is reported as that, not as no results', async () => {
  delete process.env.DB_TOKEN;

  const msg = fakeMessage('db anything');
  await command.handler(msg, ['anything']);

  const embed = firstEmbed(msg);
  assert.match(embed.title, /Not connected/);
  assert.match(embed.description, /DB_TOKEN/);
});

test('a server that is down is reported as that, not as no results', async () => {
  const good = process.env.DB_URL;
  process.env.DB_URL = 'http://127.0.0.1:9';
  try {
    const msg = fakeMessage('db anything');
    await command.handler(msg, ['anything']);
    assert.match(firstEmbed(msg).title, /Error/);
  } finally {
    process.env.DB_URL = good;
  }
});

test('a query the database rejects shows why', async () => {
  handler = (req, res) => {
    res.writeHead(400, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ detail: 'Unknown flag is:nonsense. Try: pinned' }));
  };

  const msg = fakeMessage('db is:nonsense');
  await command.handler(msg, ['is:nonsense']);

  assert.match(firstEmbed(msg).description, /did not parse/);
  assert.match(firstEmbed(msg).description, /Unknown flag/);
});

test('no matches says so, and says why a search can come back empty', async () => {
  const msg = fakeMessage('db nothing here');
  await command.handler(msg, ['nothing', 'here']);

  const embed = firstEmbed(msg);
  assert.match(embed.title, /Nothing matched/);
  assert.match(embed.description, /combined with AND/);
});

test('results render, with the snippet markers turned into bold', async () => {
  handler = (req, res) => {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      total: 2,
      hits: [
        { uid: 'a'.repeat(32), kind: 'fish', title: 'Primal Whale Shark',
          project: 'vyrex', tags: [], score: -2,
          snippet: 'a \u0002legendary\u0003 catch' },
        { uid: 'b'.repeat(32), kind: 'file', title: 'jailService.js',
          project: 'vyrex', tags: [], score: -1, snippet: '' },
      ],
    }));
  };

  const msg = fakeMessage('db legendary');
  await command.handler(msg, ['legendary']);

  const embed = firstEmbed(msg);
  assert.match(embed.title, /2 results/);
  assert.match(embed.description, /Primal Whale Shark/);
  assert.match(embed.description, /a \*\*legendary\*\* catch/);
  assert.match(embed.description, /`vyrex`/);

  // The control characters must never reach the channel.
  assert.ok(!embed.description.includes('\u0002'), 'STX leaked into the reply');
  assert.ok(!embed.description.includes('\u0003'), 'ETX leaked into the reply');
});

test('one result is not called "1 results"', async () => {
  handler = (req, res) => {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      total: 1,
      hits: [{ uid: 'c'.repeat(32), kind: 'note', title: 'Only one',
               project: '', tags: [], score: -1, snippet: '' }],
    }));
  };

  const msg = fakeMessage('db only');
  await command.handler(msg, ['only']);
  assert.match(firstEmbed(msg).title, /1 result$/);
});
