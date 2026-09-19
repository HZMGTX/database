"""A single HTML file that still works when Vault is gone.

This is the format for leaving. It carries every item as JSON embedded in the
page, with a search box and a client-side index, and it opens from a USB
stick on a machine with no Python, no server and no network. Substring
search over a few thousand items in a browser is instant, so the index is
deliberately naive rather than clever.
"""

import html
import json
from typing import Any, Optional, TextIO

from vault import __version__, dates
from vault.db import Database
from vault.exporters.jsonl import iter_records

__all__ = ["export"]

_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root {
    --bg: #fbfbfa; --panel: #fff; --ink: #1b1b1a; --muted: #6a6a67;
    --line: #e4e3df; --accent: #2f5d50; --mark: #fde68a;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #17181a; --panel: #1f2023; --ink: #e9e8e5; --muted: #9a9a96;
      --line: #2e3034; --accent: #7fc4ad; --mark: #6b5a1f;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 16px/1.6 ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif;
  }
  header {
    position: sticky; top: 0; background: var(--bg);
    border-bottom: 1px solid var(--line); padding: 16px; z-index: 5;
  }
  .wrap { max-width: 860px; margin: 0 auto; padding: 0 16px; }
  h1 { font-size: 1.05rem; margin: 0 0 10px; letter-spacing: .01em; }
  h1 span { color: var(--muted); font-weight: 400; }
  input[type=search] {
    width: 100%; padding: 11px 13px; font-size: 1rem; color: var(--ink);
    background: var(--panel); border: 1px solid var(--line); border-radius: 9px;
  }
  input[type=search]:focus { outline: 2px solid var(--accent); outline-offset: 1px; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
  .chip {
    font-size: .8rem; padding: 3px 10px; border-radius: 999px; cursor: pointer;
    background: var(--panel); border: 1px solid var(--line); color: var(--muted);
  }
  .chip[aria-pressed="true"] { background: var(--accent); border-color: var(--accent); color: #fff; }
  #count { color: var(--muted); font-size: .85rem; margin: 14px 0 8px; }
  article {
    background: var(--panel); border: 1px solid var(--line); border-radius: 11px;
    padding: 14px 16px; margin-bottom: 10px;
  }
  article h2 { font-size: 1rem; margin: 0 0 4px; }
  .meta { color: var(--muted); font-size: .78rem; display: flex; gap: 10px; flex-wrap: wrap; }
  .body { margin-top: 8px; white-space: pre-wrap; overflow-wrap: anywhere; }
  mark { background: var(--mark); color: inherit; border-radius: 3px; }
  .tag { color: var(--accent); }
  footer { color: var(--muted); font-size: .8rem; padding: 28px 16px 40px; text-align: center; }
  @media (max-width: 520px) { .wrap { padding: 0 16px; } }
</style>
</head>
<body>
<header><div class="wrap">
  <h1>__TITLE__ <span>· __COUNT__ items · exported __WHEN__</span></h1>
  <input type="search" id="q" placeholder="Search everything…" autofocus autocomplete="off">
  <div class="chips" id="kinds"></div>
</div></header>
<main class="wrap">
  <p id="count"></p>
  <div id="results"></div>
</main>
<footer>Exported from Vault __VERSION__. This file needs nothing but a browser.</footer>
<script id="data" type="application/json">__DATA__</script>
<script>
const ITEMS = JSON.parse(document.getElementById('data').textContent);
const results = document.getElementById('results');
const countEl = document.getElementById('count');
const q = document.getElementById('q');
let kindFilter = null;

// Pre-lowercased haystack per item: the whole search is substring matching,
// and doing the lowercasing once is the only optimisation it needs.
for (const item of ITEMS) {
  item._hay = [item.title, item.body, (item.tags || []).join(' '), item.kind]
    .join(' ').toLowerCase();
}

const kinds = [...new Set(ITEMS.map(i => i.kind))].sort();
document.getElementById('kinds').innerHTML = kinds
  .map(k => `<button class="chip" data-kind="${k}" aria-pressed="false">${k}</button>`).join('');
document.getElementById('kinds').addEventListener('click', e => {
  const button = e.target.closest('.chip');
  if (!button) return;
  const kind = button.dataset.kind;
  kindFilter = kindFilter === kind ? null : kind;
  for (const chip of document.querySelectorAll('.chip'))
    chip.setAttribute('aria-pressed', String(chip.dataset.kind === kindFilter));
  render();
});

function escapeHtml(text) {
  return (text || '').replace(/[&<>"]/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);
}

function highlight(text, terms) {
  let out = escapeHtml(text);
  for (const term of terms) {
    if (term.length < 2) continue;
    const safe = term.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
    out = out.replace(new RegExp(safe, 'gi'), m => `<mark>${m}</mark>`);
  }
  return out;
}

function render() {
  const query = q.value.trim().toLowerCase();
  const terms = query.split(/\\s+/).filter(Boolean);
  let matches = ITEMS;
  if (kindFilter) matches = matches.filter(i => i.kind === kindFilter);
  if (terms.length) matches = matches.filter(i => terms.every(t => i._hay.includes(t)));

  countEl.textContent = `${matches.length} of ${ITEMS.length} item${ITEMS.length === 1 ? '' : 's'}`;
  results.innerHTML = matches.slice(0, 300).map(item => {
    const body = (item.body || '').slice(0, 600);
    const tags = (item.tags || []).map(t => `<span class="tag">#${escapeHtml(t)}</span>`).join(' ');
    return `<article>
      <h2>${highlight(item.title || '(untitled)', terms)}</h2>
      <div class="meta"><span>${escapeHtml(item.kind)}</span><span>${escapeHtml(item.updated_at || '')}</span>${tags}</div>
      ${body ? `<div class="body">${highlight(body, terms)}</div>` : ''}
    </article>`;
  }).join('');
  if (matches.length > 300)
    results.innerHTML += `<p style="color:var(--muted)">…and ${matches.length - 300} more. Narrow the search.</p>`;
}

q.addEventListener('input', render);
render();
</script>
</body>
</html>
"""


def export(db: Database, out: TextIO, *, query: Optional[str] = None,
           include_trashed: bool = False, title: str = "Vault", **_: Any) -> int:
    records = []
    for doc in iter_records(db, query=query, include_trashed=include_trashed):
        records.append({
            "uid": doc["uid"], "kind": doc["kind"], "title": doc.get("title", ""),
            "body": doc.get("body", ""), "tags": doc.get("tags") or [],
            "updated_at": doc.get("updated_at", ""),
            "props": doc.get("props") or {},
        })

    # </script> inside the data would end the block early; escaping the slash
    # is the standard way to embed JSON in a page safely.
    payload = json.dumps(records, ensure_ascii=False).replace("</", "<\\/")

    page = (_PAGE
            .replace("__TITLE__", html.escape(title))
            .replace("__COUNT__", f"{len(records):,}")
            .replace("__WHEN__", dates.utcnow()[:10])
            .replace("__VERSION__", html.escape(__version__))
            .replace("__DATA__", payload))
    out.write(page)
    return len(records)
