'use strict';

const { truncateString } = require('../../utils/helpers');
const { createEmbed, errorEmbed, warnEmbed } = require('../../utils/embed');
const { resolveMessagePrefix } = require('../../utils/prefixResolver');
const remoteDb = require('../../services/remoteDbService');

const MAX_RESULTS = 6;
const SNIPPET_LENGTH = 140;

/** Kind labels, so a result says what it is without a second lookup. */
const GLYPHS = {
  note: '📝', task: '☑️', event: '📅', link: '🔗', person: '👤', file: '📄',
};

/**
 * One result as a line of the embed.
 *
 * The snippet arrives with each matched run wrapped in two control
 * characters, which is how the database avoids picking a rendering for its
 * callers. Here they become Discord bold. Printing one unprocessed puts two
 * invisible characters in the channel.
 */
function formatHit(hit) {
  const glyph = GLYPHS[hit.kind] || '•';
  const title = truncateString(hit.title || '(untitled)', 90);
  const where = hit.project ? ` · \`${hit.project}\`` : '';

  const snippet = remoteDb
    .renderSnippet(hit.snippet, { start: '**', end: '**' })
    .replace(/\s+/g, ' ')
    .trim();

  const body = snippet ? `\n${truncateString(snippet, SNIPPET_LENGTH)}` : '';
  return `${glyph} **${title}**${where}${body}`;
}

module.exports = [
  {
    name: 'db',
    aliases: ['database', 'kb', 'lookup'],
    description: 'Search the shared database.',
    category: 'Utility',
    cooldown: 3,
    handler: async (msg, args) => {
      const prefix = resolveMessagePrefix(msg);
      const query = args.join(' ').trim();

      if (!query) {
        return msg.reply({
          embeds: [createEmbed('🔎 Search the database', [
            `\`${prefix}db <anything>\``,
            '',
            '**Narrowing it down**',
            '`kind:fish legendary` — a kind and a word',
            '`project:vyrex value>5000` — one project, a number compared',
            '`rarity:Legendary` — any field, matched',
            '`tag:work/*` — a tag and everything under it',
            '`"an exact phrase"` · `-excluded`',
          ].join('\n'), '#6366F1')],
        });
      }

      // Told apart on purpose: an unset token is something to fix in the
      // environment, a silent server is the database being down.
      if (!remoteDb.configured()) {
        return msg.reply({
          embeds: [warnEmbed('Not connected',
            'No `DB_TOKEN` is set, so I cannot reach the database. ' +
            'Add it to the bot\'s environment and restart.')],
        });
      }

      let page;
      try {
        page = await remoteDb.search(query, { limit: MAX_RESULTS, strict: true });
      } catch (error) {
        return msg.reply({
          embeds: [errorEmbed(
            error.status === 400
              ? `That query did not parse: ${error.message}`
              : `The database did not answer: ${error.message}`)],
        });
      }

      if (!page.length) {
        return msg.reply({
          embeds: [createEmbed('Nothing matched', [
            `No results for \`${truncateString(query, 120)}\`.`,
            '',
            'Words are combined with AND, so every one has to be present. Try fewer.',
          ].join('\n'), '#6b7280')],
        });
      }

      return msg.reply({
        embeds: [createEmbed(
          `🔎 ${page.length} result${page.length === 1 ? '' : 's'}`,
          page.map(formatHit).join('\n\n'),
          '#00B4D8',
        )],
      });
    },
  },
];
