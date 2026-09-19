/* Vault — the web UI.
 *
 * Plain ES modules, no framework, no build step, no network requests. The
 * page is served from the same origin as the API, so there is nothing to
 * configure and nothing to install.
 *
 * State lives in the URL hash, so the back button works, a search is a link
 * you can keep, and a reload lands you where you were.
 */

import { render, renderSnippet, escapeHtml } from './md.js';
import { startField, driftNebula } from './field.js';

const API = '/api/v1';

const el = (id) => document.getElementById(id);
const dom = {
  q: el('q'), form: el('searchForm'), clear: el('clearSearch'),
  hits: el('hits'), results: el('results'), empty: el('empty'), more: el('more'),
  detail: el('detail'), pane: document.querySelector('.pane'),
  understood: el('understood'),
  savedSearches: el('savedSearches'), kindList: el('kindList'), tagList: el('tagList'),
  itemCount: el('itemCount'), toast: el('toast'),
  rail: el('rail'), railOpen: el('railOpen'), railClose: el('railClose'),
  editor: el('editor'), editorForm: el('editorForm'), editorTitle: el('editorTitle'),
  editorNote: el('editorNote'),
  fKind: el('fKind'), fTitle: el('fTitle'), fBody: el('fBody'), fTags: el('fTags'),
  fFacet: el('fFacet'),
  help: el('help'), newItem: el('newItem'),
};

const state = {
  query: '', hits: [], offset: 0, total: 0, selected: -1,
  open: null, schema: null, editing: null, busy: false,
};

const PAGE = 30;

/* ── plumbing ─────────────────────────────────────────────────────────── */

async function api(path, options = {}) {
  const response = await fetch(API + path, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  const text = await response.text();
  const payload = text ? JSON.parse(text) : null;
  if (!response.ok) {
    // The server answers with problem+json, so there is always a sentence.
    const error = new Error(payload?.detail || payload?.title || `HTTP ${response.status}`);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

let toastTimer;
function toast(message, bad = false) {
  dom.toast.textContent = message;
  dom.toast.classList.toggle('bad', bad);
  dom.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { dom.toast.hidden = true; }, bad ? 5200 : 2600);
}

function relative(iso) {
  if (!iso) return '';
  const then = new Date(iso.replace(' ', 'T'));
  const seconds = (Date.now() - then.getTime()) / 1000;
  if (seconds < 60) return 'just now';
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  if (seconds < 86400 * 30) return `${Math.floor(seconds / 86400)}d ago`;
  return then.toISOString().slice(0, 10);
}

const handle = (uid) => (uid || '').slice(-8);

/* ── search ───────────────────────────────────────────────────────────── */

async function runSearch({ append = false } = {}) {
  if (state.busy) return;
  state.busy = true;
  dom.results.setAttribute('aria-busy', 'true');
  if (!append) { state.offset = 0; state.selected = -1; }

  try {
    const params = new URLSearchParams({
      q: state.query, limit: String(PAGE), offset: String(state.offset),
    });
    const payload = await api(`/search?${params}`);

    state.hits = append ? state.hits.concat(payload.hits) : payload.hits;
    state.total = payload.total ?? state.hits.length;
    renderHits(payload);
  } catch (error) {
    dom.hits.innerHTML = '';
    dom.empty.hidden = false;
    dom.empty.innerHTML =
      `<h3>That query did not work</h3><p>${escapeHtml(error.message)}</p>`;
    dom.more.hidden = true;
    dom.understood.hidden = true;
  } finally {
    state.busy = false;
    dom.results.setAttribute('aria-busy', 'false');
  }
}

function renderHits(payload) {
  dom.clear.hidden = !state.query;

  if (payload.understood?.length && state.query.trim()) {
    dom.understood.hidden = false;
    dom.understood.innerHTML =
      `Read as: <b>${payload.understood.map(escapeHtml).join('</b> · <b>')}</b>`
      + (payload.note ? ` — ${escapeHtml(payload.note)}` : '');
  } else {
    dom.understood.hidden = true;
  }

  if (!state.hits.length) {
    dom.hits.innerHTML = '';
    dom.empty.hidden = false;
    dom.empty.innerHTML = state.query
      ? `<h3>Nothing matched</h3><p>Try fewer words, or <code>is:any</code> to include the trash.</p>`
      : `<h3>Nothing here yet</h3><p>Press <code>n</code> to add something.</p>`;
    dom.more.hidden = true;
    dom.itemCount.textContent = state.query ? '0 results' : '';
    return;
  }

  dom.empty.hidden = true;
  dom.hits.innerHTML = state.hits.map((hit, index) => `
    <li>
      <button class="hit" data-index="${index}" data-uid="${escapeHtml(hit.uid)}"
              aria-selected="${index === state.selected}">
        <span class="hit-top">
          <span class="hit-kind">${escapeHtml(hit.kind)}</span>
          <span class="hit-title">${escapeHtml(hit.title || '(untitled)')}</span>
          ${hit.pinned ? '<span class="pin" title="Pinned">★</span>' : ''}
          <span class="hit-when">${escapeHtml(relative(hit.updated_at))}</span>
        </span>
        ${hit.snippet ? `<span class="hit-snippet">${renderSnippet(hit.snippet)}</span>` : ''}
        ${hit.tags?.length
          ? `<span class="hit-tags">${hit.tags.map(
              (t) => `<span class="tag">${escapeHtml(t)}</span>`).join('')}</span>`
          : ''}
      </button>
    </li>`).join('');

  dom.more.hidden = !state.hits.length
    || (state.hits.length >= state.total && !payload.total_capped);
  const shown = `${state.hits.length} of ${state.total}${payload.total_capped ? '+' : ''}`;
  dom.itemCount.textContent = shown;
}

/* ── detail ───────────────────────────────────────────────────────────── */

async function openItem(uid) {
  try {
    const doc = await api(`/items/${uid}`);
    state.open = doc;
    dom.pane.classList.add('split');
    dom.detail.hidden = false;
    renderDetail(doc);
    if (location.hash !== `#/item/${uid}`) {
      history.pushState(null, '', `#/item/${uid}`);
    }
  } catch (error) {
    toast(error.message, true);
  }
}

function closeDetail() {
  state.open = null;
  dom.detail.hidden = true;
  dom.pane.classList.remove('split');
  if (location.hash.startsWith('#/item/')) history.back();
}

function facetRows(doc) {
  const facet = doc.facet || {};
  const rows = [];
  const add = (label, value) => { if (value !== undefined && value !== null && value !== '')
    rows.push([label, value]); };

  if (doc.kind === 'task') {
    add('Status', facet.status);
    if (facet.priority) add('Priority', '★'.repeat(facet.priority));
    if (facet.due_local) add('Due', `${facet.due_local.replace('T', ' ')} ${facet.due_tzid || ''}`);
    add('Completed', facet.completed_at);
  } else if (doc.kind === 'event') {
    const when = (facet.starts_local || '').replace('T', ' ')
      + (facet.ends_local ? ` — ${facet.ends_local.replace('T', ' ')}` : '')
      + (facet.all_day ? '' : ` ${facet.tzid || ''}`);
    add(facet.all_day ? 'All day' : 'When', when);
    add('Location', facet.location);
    add('Repeats', facet.rrule);
  } else if (doc.kind === 'link') {
    add('URL', facet.url);
  } else if (doc.kind === 'file') {
    add('File', facet.filename);
    if (facet.content_withheld) add('Withheld', facet.withheld_reason);
  } else if (doc.kind === 'person') {
    add('Organisation', facet.org); add('Role', facet.role);
    add('Birthday', facet.birthday);
  }
  for (const ident of doc.identities || []) add(ident.channel, ident.value);
  for (const [key, value] of Object.entries(doc.props || {}).sort()) {
    if (typeof value !== 'object') add(key, value);
  }
  return rows;
}

function renderDetail(doc) {
  const rows = facetRows(doc);
  const out = [];

  out.push(`<h1>${escapeHtml(doc.title || '(untitled)')}</h1>`);
  out.push(`<div class="detail-meta">
      <span>${escapeHtml(doc.kind)}</span>
      <span>${escapeHtml(handle(doc.uid))}</span>
      <span>rev ${doc.rev}</span>
      <span>updated ${escapeHtml(relative(doc.updated_at))}</span>
      ${doc.deleted_at ? '<span style="color:var(--danger)">in the trash</span>' : ''}
    </div>`);

  out.push(`<div class="detail-actions">
      <button class="ghost" data-act="edit">Edit</button>
      <button class="ghost" data-act="pin">${doc.pinned ? 'Unpin' : 'Pin'}</button>
      ${doc.deleted_at
        ? '<button class="ghost" data-act="restore">Restore</button>'
        : '<button class="ghost" data-act="trash">Trash</button>'}
      <button class="ghost" data-act="close">Close</button>
    </div>`);

  if (doc.tags?.length) {
    out.push(`<div class="hit-tags">${doc.tags.map(
      (t) => `<button class="tag" data-tag="${escapeHtml(t)}">${escapeHtml(t)}</button>`
    ).join('')}</div>`);
  }

  if (rows.length) {
    out.push('<dl class="props">' + rows.map(
      ([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(String(value))}</dd>`
    ).join('') + '</dl>');
  }

  if (doc.body) out.push(`<div class="body-text">${render(doc.body)}</div>`);

  const out_ = doc.links?.out || [];
  const in_ = doc.links?.in || [];
  if (out_.length || in_.length) {
    out.push('<div class="section-title">Links</div><ul class="link-rows">');
    for (const link of out_) {
      out.push(`<li><span class="link-rel">${escapeHtml(link.rel)}</span>
        <button data-open="${escapeHtml(link.uid)}">${escapeHtml(link.title || handle(link.uid))}</button></li>`);
    }
    for (const link of in_) {
      out.push(`<li><span class="link-rel">← ${escapeHtml(link.rel)}</span>
        <button data-open="${escapeHtml(link.uid)}">${escapeHtml(link.title || handle(link.uid))}</button></li>`);
    }
    out.push('</ul>');
  }

  dom.detail.innerHTML = out.join('\n');
  dom.detail.scrollTop = 0;
}

/* ── editor ───────────────────────────────────────────────────────────── */

function facetInputsFor(kind, doc) {
  const facet = doc?.facet || {};
  const field = (id, label, type, value, wide = false, attrs = '') => `
    <label class="field${wide ? ' wide' : ''}">
      <span>${label}</span>
      <input id="${id}" type="${type}" value="${escapeHtml(value ?? '')}" ${attrs}>
    </label>`;

  if (kind === 'task') {
    const options = ['todo', 'doing', 'blocked', 'done', 'cancelled'].map(
      (s) => `<option ${facet.status === s ? 'selected' : ''}>${s}</option>`).join('');
    return `<label class="field"><span>Status</span><select id="xStatus">${options}</select></label>
      ${field('xPriority', 'Priority (0-5)', 'number', facet.priority ?? 0, false, 'min="0" max="5"')}
      ${field('xDue', 'Due', 'text', facet.due_local || '', true,
              'placeholder="friday, tomorrow 3pm, +7d, 2026-05-01"')}`;
  }
  if (kind === 'event') {
    return `${field('xStarts', 'Starts', 'text', facet.starts_local || '', false,
                    'placeholder="monday 9:30am"')}
      ${field('xEnds', 'Ends', 'text', facet.ends_local || '')}
      <label class="field"><span>All day</span>
        <select id="xAllDay"><option value="">no</option>
        <option value="1" ${facet.all_day ? 'selected' : ''}>yes</option></select></label>
      ${field('xLocation', 'Location', 'text', facet.location || '')}`;
  }
  if (kind === 'link') return field('xUrl', 'URL', 'url', facet.url || '', true);
  if (kind === 'person') {
    return `${field('xGiven', 'First name', 'text', facet.given_name || '')}
      ${field('xFamily', 'Last name', 'text', facet.family_name || '')}
      ${field('xOrg', 'Organisation', 'text', facet.org || '')}
      ${field('xRole', 'Role', 'text', facet.role || '')}
      ${field('xEmail', 'Email', 'email',
              (doc?.identities || []).find((i) => i.channel === 'email')?.value || '', true)}`;
  }
  return '';
}

function collectFacet(kind) {
  const value = (id) => el(id)?.value?.trim() || '';
  if (kind === 'task') {
    const facet = { status: value('xStatus') || 'todo',
                    priority: Number(value('xPriority') || 0) };
    if (value('xDue')) facet.due = value('xDue');
    return facet;
  }
  if (kind === 'event') {
    const facet = { starts: value('xStarts'), all_day: Boolean(value('xAllDay')),
                    location: value('xLocation') };
    if (value('xEnds')) facet.ends = value('xEnds');
    return facet;
  }
  if (kind === 'link') return { url: value('xUrl') };
  if (kind === 'person') {
    return { given_name: value('xGiven'), family_name: value('xFamily'),
             org: value('xOrg'), role: value('xRole'),
             emails: value('xEmail') ? [value('xEmail')] : [] };
  }
  return null;
}

function openEditor(doc = null) {
  state.editing = doc;
  dom.editorTitle.textContent = doc ? 'Edit item' : 'New item';
  dom.editorNote.textContent = doc ? `${handle(doc.uid)} · rev ${doc.rev}` : '';

  const kinds = (state.schema?.kinds || []).filter((k) => k.has_facet || k.name === 'note');
  dom.fKind.innerHTML = kinds.map(
    (k) => `<option value="${k.name}" ${doc?.kind === k.name ? 'selected' : ''}>${k.label}</option>`
  ).join('');
  dom.fKind.disabled = Boolean(doc);   // changing kind would orphan the facet

  dom.fTitle.value = doc?.title || '';
  dom.fBody.value = doc?.body || '';
  dom.fTags.value = (doc?.tags || []).join(' ');
  dom.fFacet.innerHTML = facetInputsFor(dom.fKind.value, doc);

  dom.editor.showModal();
  setTimeout(() => dom.fTitle.focus(), 30);
}

async function saveEditor() {
  const kind = dom.fKind.value;
  const payload = {
    kind,
    title: dom.fTitle.value.trim(),
    body: dom.fBody.value,
    tags: dom.fTags.value.split(/[\s,]+/).filter(Boolean),
  };
  const facet = collectFacet(kind);
  if (facet) payload.facet = facet;

  try {
    if (state.editing) {
      // If-Match carries the revision we read, so a concurrent edit is a
      // 409 rather than a silent overwrite.
      const updated = await api(`/items/${state.editing.uid}`, {
        method: 'PATCH',
        headers: { 'If-Match': `"${state.editing.uid}.${state.editing.rev}"` },
        body: JSON.stringify(payload),
      });
      toast('Saved');
      await openItem(updated.uid);
    } else {
      const created = await api('/items', { method: 'POST', body: JSON.stringify(payload) });
      toast('Added');
      await openItem(created.uid);
    }
    await Promise.all([runSearch(), loadSidebar()]);
  } catch (error) {
    if (error.status === 409) {
      toast('Someone else changed this while you were editing — reopening', true);
      await openItem(state.editing.uid);
    } else {
      toast(error.message, true);
    }
  }
}

/* ── sidebar ──────────────────────────────────────────────────────────── */

async function loadSidebar() {
  try {
    const [searches, tags, stats] = await Promise.all([
      api('/saved-searches'), api('/tags'), api('/stats'),
    ]);

    dom.savedSearches.innerHTML = searches.searches.map((s) => `
      <li><button data-query="${escapeHtml(s.query)}">
        <span class="label">${escapeHtml(s.name)}</span></button></li>`).join('');

    dom.kindList.innerHTML = Object.entries(stats.by_kind)
      .sort((a, b) => b[1] - a[1]).slice(0, 12).map(([kind, count]) => `
      <li><button data-query="kind:${escapeHtml(kind)}">
        <span class="label">${escapeHtml(kind)}</span>
        <span class="count">${count.toLocaleString()}</span></button></li>`).join('');

    dom.tagList.innerHTML = tags.tags.filter((t) => t.count)
      .sort((a, b) => b.count - a.count).slice(0, 20).map((t) => `
      <li><button data-query="tag:${escapeHtml(t.slug)}">
        <span class="label">${escapeHtml(t.slug)}</span>
        <span class="count">${t.count.toLocaleString()}</span></button></li>`).join('');

    dom.itemCount.textContent = `${stats.items.toLocaleString()} items`;
  } catch (error) {
    dom.itemCount.textContent = 'offline';
  }
}

function markCurrent(query) {
  for (const button of document.querySelectorAll('.rail-list button')) {
    button.setAttribute('aria-current', String(button.dataset.query === query));
  }
}

/* ── navigation ───────────────────────────────────────────────────────── */

function select(index) {
  if (!state.hits.length) return;
  state.selected = Math.max(0, Math.min(state.hits.length - 1, index));
  for (const node of dom.hits.querySelectorAll('.hit')) {
    const isSelected = Number(node.dataset.index) === state.selected;
    node.setAttribute('aria-selected', String(isSelected));
    if (isSelected) node.scrollIntoView({ block: 'nearest' });
  }
}

function setQuery(query, { push = true } = {}) {
  state.query = query;
  dom.q.value = query;
  markCurrent(query);
  if (push) {
    const target = query ? `#/q/${encodeURIComponent(query)}` : '#/';
    if (location.hash !== target) history.pushState(null, '', target);
  }
  runSearch();
}

function applyHash() {
  const hash = decodeURIComponent(location.hash || '');
  if (hash.startsWith('#/item/')) {
    // Also populate the list behind it: arriving on a deep link should not
    // leave an empty column where the results belong.
    if (!state.hits.length) runSearch();
    openItem(hash.slice(7));
  } else if (hash.startsWith('#/q/')) {
    closeDetailQuietly();
    setQuery(hash.slice(4), { push: false });
  } else {
    closeDetailQuietly();
    setQuery('', { push: false });
  }
}

function closeDetailQuietly() {
  state.open = null;
  dom.detail.hidden = true;
  dom.pane.classList.remove('split');
}

/* ── events ───────────────────────────────────────────────────────────── */

let debounce;
dom.q.addEventListener('input', () => {
  clearTimeout(debounce);
  debounce = setTimeout(() => setQuery(dom.q.value), 180);
});
dom.form.addEventListener('submit', (event) => {
  event.preventDefault();
  clearTimeout(debounce);
  setQuery(dom.q.value);
});
dom.clear.addEventListener('click', () => { setQuery(''); dom.q.focus(); });
dom.more.addEventListener('click', () => {
  state.offset += PAGE;
  runSearch({ append: true });
});

dom.hits.addEventListener('click', (event) => {
  const button = event.target.closest('.hit');
  if (!button) return;
  select(Number(button.dataset.index));
  openItem(button.dataset.uid);
});

document.querySelector('.rail').addEventListener('click', (event) => {
  const button = event.target.closest('button[data-query]');
  if (!button) return;
  setQuery(button.dataset.query);
  dom.rail.dataset.open = 'false';
});

dom.detail.addEventListener('click', async (event) => {
  const openTarget = event.target.closest('[data-open]');
  if (openTarget) return openItem(openTarget.dataset.open);

  const tag = event.target.closest('[data-tag]');
  if (tag) return setQuery(`tag:${tag.dataset.tag}`);

  const wikilink = event.target.closest('[data-wikilink]');
  if (wikilink) return setQuery(`"${wikilink.dataset.wikilink}"`);

  const action = event.target.closest('[data-act]')?.dataset.act;
  if (!action || !state.open) return;
  const doc = state.open;

  try {
    if (action === 'close') return closeDetail();
    if (action === 'edit') return openEditor(doc);
    if (action === 'pin') {
      await api(`/items/${doc.uid}`, {
        method: 'PATCH',
        headers: { 'If-Match': `"${doc.uid}.${doc.rev}"` },
        body: JSON.stringify({ pinned: !doc.pinned }),
      });
      toast(doc.pinned ? 'Unpinned' : 'Pinned');
    } else if (action === 'trash') {
      await api(`/items/${doc.uid}`, { method: 'DELETE' });
      toast('Moved to the trash');
    } else if (action === 'restore') {
      await api(`/items/${doc.uid}/restore`, { method: 'POST' });
      toast('Restored');
    }
    await openItem(doc.uid);
    await Promise.all([runSearch(), loadSidebar()]);
  } catch (error) {
    toast(error.message, true);
  }
});

dom.fKind.addEventListener('change', () => {
  dom.fFacet.innerHTML = facetInputsFor(dom.fKind.value, null);
});
dom.editorForm.addEventListener('submit', (event) => {
  if (event.submitter?.value === 'save') {
    event.preventDefault();
    dom.editor.close();
    saveEditor();
  }
});
dom.newItem.addEventListener('click', () => openEditor(null));

dom.railOpen.addEventListener('click', () => { dom.rail.dataset.open = 'true'; });
dom.railClose.addEventListener('click', () => { dom.rail.dataset.open = 'false'; });
el('showHelp').addEventListener('click', () => dom.help.showModal());
el('helpClose').addEventListener('click', () => dom.help.close());

window.addEventListener('popstate', applyHash);

document.addEventListener('keydown', (event) => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName);

  if (event.key === 'Escape') {
    if (dom.editor.open || dom.help.open) return;
    if (state.open) closeDetail();
    else if (dom.q.value) { setQuery(''); }
    return;
  }
  if (typing || event.metaKey || event.ctrlKey || event.altKey) return;

  if (event.key === '/') { event.preventDefault(); dom.q.focus(); dom.q.select(); }
  else if (event.key === 'n') { event.preventDefault(); openEditor(null); }
  else if (event.key === 'j') { event.preventDefault(); select(state.selected + 1); }
  else if (event.key === 'k') { event.preventDefault(); select(state.selected - 1); }
  else if (event.key === 'Enter' && state.selected >= 0) {
    openItem(state.hits[state.selected].uid);
  } else if (event.key === 'e' && state.open) {
    event.preventDefault(); openEditor(state.open);
  } else if (event.key === '?') { dom.help.showModal(); }
});

/* ── start ────────────────────────────────────────────────────────────── */

(async function start() {
  // Decorative depth planes, started before data lands so the interface has
  // dimension from the first paint.
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  startField(el('field'), { reducedMotion });
  driftNebula(el('nebula'), { reducedMotion });

  try {
    state.schema = await api('/schema');
  } catch (error) {
    dom.empty.hidden = false;
    dom.empty.innerHTML =
      `<h3>Cannot reach the server</h3><p>Is <code>vault serve</code> still running?</p>`;
    return;
  }
  await loadSidebar();
  applyHash();
})();
