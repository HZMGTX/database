/* A small Markdown renderer.
 *
 * Covers what people actually write in notes: headings, lists, task lists,
 * fenced and inline code, emphasis, links, blockquotes, rules and
 * [[wikilinks]]. It is not CommonMark and does not try to be -- a full
 * parser is a dependency this project does not take.
 *
 * Everything is escaped before any markup is inserted, and inline
 * formatting is applied only to already-escaped text, so a note containing
 * `<script>` renders as those characters rather than running.
 */

const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

export function escapeHtml(text) {
  return String(text ?? '').replace(/[&<>"']/g, (c) => ESCAPES[c]);
}

/** Inline formatting. Input must already be escaped. */
function inline(text, onWikilink) {
  let out = text;

  // Code spans first: their contents must not be touched by anything below.
  const spans = [];
  out = out.replace(/`([^`]+)`/g, (_, code) => {
    spans.push(code);
    return `\u0000${spans.length - 1}\u0000`;
  });

  out = out.replace(/\[\[([^\]|]+)(?:\|([^\]]*))?\]\]/g, (_, target, label) => {
    const text_ = escapeHtml(label || target);
    return onWikilink
      ? `<button type="button" data-wikilink="${escapeHtml(target)}">${text_}</button>`
      : text_;
  });

  // Links: the href is restricted to schemes that cannot execute script.
  out = out.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (whole, label, href) => {
    if (!/^(https?:|mailto:|#|\/)/i.test(href)) return whole;
    return `<a href="${href}" rel="noopener noreferrer" target="_blank">${label}</a>`;
  });
  out = out.replace(/(^|[\s(])(https?:\/\/[^\s<>)]+)/g,
    (_, lead, url) => `${lead}<a href="${url}" rel="noopener noreferrer" target="_blank">${url}</a>`);

  out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  out = out.replace(/(^|\W)_([^_]+)_(?=\W|$)/g, '$1<em>$2</em>');
  out = out.replace(/(^|[^*])\*([^*]+)\*(?!\*)/g, '$1<em>$2</em>');
  out = out.replace(/~~([^~]+)~~/g, '<del>$1</del>');

  return out.replace(/\u0000(\d+)\u0000/g, (_, index) => `<code>${spans[index]}</code>`);
}

export function render(markdown, { onWikilink = true } = {}) {
  const lines = String(markdown ?? '').replace(/\r\n?/g, '\n').split('\n');
  const html = [];
  let listType = null;
  let inCode = false;
  let codeLines = [];
  let paragraph = [];

  const closeList = () => { if (listType) { html.push(`</${listType}>`); listType = null; } };
  const closeParagraph = () => {
    if (paragraph.length) {
      html.push(`<p>${inline(escapeHtml(paragraph.join('\n')), onWikilink)}</p>`);
      paragraph = [];
    }
  };

  for (const line of lines) {
    const fence = line.match(/^```(\w*)\s*$/);
    if (fence) {
      if (inCode) {
        html.push(`<pre><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`);
        codeLines = []; inCode = false;
      } else {
        closeParagraph(); closeList(); inCode = true;
      }
      continue;
    }
    if (inCode) { codeLines.push(line); continue; }

    if (!line.trim()) { closeParagraph(); closeList(); continue; }

    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      closeParagraph(); closeList();
      const level = heading[1].length + 1;   // h1 in a note becomes h2 on the page
      html.push(`<h${level}>${inline(escapeHtml(heading[2]), onWikilink)}</h${level}>`);
      continue;
    }

    if (/^\s*([-*_])\1{2,}\s*$/.test(line)) {
      closeParagraph(); closeList(); html.push('<hr>'); continue;
    }

    const task = line.match(/^\s*[-*]\s+\[([ xX])\]\s+(.*)$/);
    if (task) {
      closeParagraph();
      if (listType !== 'ul') { closeList(); html.push('<ul>'); listType = 'ul'; }
      const checked = task[1].toLowerCase() === 'x' ? ' checked' : '';
      html.push(`<li><input type="checkbox" disabled${checked}> ` +
                `${inline(escapeHtml(task[2]), onWikilink)}</li>`);
      continue;
    }

    const bullet = line.match(/^\s*[-*+]\s+(.*)$/);
    if (bullet) {
      closeParagraph();
      if (listType !== 'ul') { closeList(); html.push('<ul>'); listType = 'ul'; }
      html.push(`<li>${inline(escapeHtml(bullet[1]), onWikilink)}</li>`);
      continue;
    }

    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (numbered) {
      closeParagraph();
      if (listType !== 'ol') { closeList(); html.push('<ol>'); listType = 'ol'; }
      html.push(`<li>${inline(escapeHtml(numbered[1]), onWikilink)}</li>`);
      continue;
    }

    const quote = line.match(/^>\s?(.*)$/);
    if (quote) {
      closeParagraph(); closeList();
      html.push(`<blockquote>${inline(escapeHtml(quote[1]), onWikilink)}</blockquote>`);
      continue;
    }

    paragraph.push(line);
  }

  if (inCode) html.push(`<pre><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`);
  closeParagraph();
  closeList();
  return html.join('\n');
}

/** Highlight FTS5's snippet markers (STX/ETX) after escaping. */
export function renderSnippet(text) {
  return escapeHtml(text)
    .replaceAll('\u0002', '<mark>')
    .replaceAll('\u0003', '</mark>');
}
