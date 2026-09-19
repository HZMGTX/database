/* ═══════════════════════════════════════════════════════════════════════════
   THEME CUSTOMIZER

   A control for every token in the registry, generated from the registry
   itself. Adding a token to theme.js makes a control appear here; there is
   no second list to keep in step, because a second list is how the two
   drift apart.

   How it stays instant:

     * A control writes one token. theme.js writes one CSS custom property.
       Nothing in the interface re-renders, so there is no flicker and no
       layout thrash — the browser recomputes only the rules that actually
       reference that property.

     * Drags coalesce. `input` fires far faster than the display refreshes,
       so theme.js batches writes into one per animation frame. Dragging a
       slider therefore costs one style write per frame no matter how fast
       the pointer moves.

     * Persistence is debounced separately. Serialising to localStorage on
       every pixel of a drag would block the main thread on a synchronous
       write; committing shortly after the drag settles does not.
   ═══════════════════════════════════════════════════════════════════════ */

import { GROUPS, PRESETS, TOKENS, theme } from './theme.js';

const COMMIT_DELAY_MS = 260;

const escapeAttr = (value) => String(value ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);

/** Percentage a range token's value sits at, for the slider fill. */
function fillPercent(token, value) {
  const span = token.max - token.min;
  if (span <= 0) return 0;
  return ((Number(value) - token.min) / span) * 100;
}

/** Trailing-edge debounce. One timer per instance, no allocation per call. */
function debounce(fn, wait) {
  let timer = 0;
  return function scheduled() {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => { timer = 0; fn(); }, wait);
  };
}

export class Customizer {
  /**
   * @param {HTMLElement} node   Panel root.
   * @param {object} options
   * @param {Function} [options.onToast]  Optional status reporter.
   */
  constructor(node, { onToast = null } = {}) {
    this.node = node;
    this.onToast = onToast;
    this.controls = new Map();     // token key -> { input, hex, value }
    this.auditNode = null;
    this.open = false;

    this.commit = debounce(() => theme.save(), COMMIT_DELAY_MS);
    this.refreshAudit = this.refreshAudit.bind(this);

    this.build();
    this.bind();
    this.syncAll();

    // Any change from anywhere — a preset, a reset, an import — refreshes
    // the controls, so the panel can never show a value the page is not using.
    theme.subscribe((key) => {
      if (key === '*') this.syncAll();
      else this.syncToken(key);
      this.scheduleAudit();
    });
  }

  /* ── construction ───────────────────────────────────────────────────── */

  build() {
    const groups = new Map(GROUPS.map((g) => [g.id, []]));
    for (const token of TOKENS) {
      const bucket = groups.get(token.group);
      if (bucket) bucket.push(token);
    }

    const presetButtons = Object.entries(PRESETS).map(([name, preset]) =>
      `<button type="button" class="cz-preset" data-preset="${escapeAttr(name)}">`
      + `${escapeAttr(preset.label)}</button>`).join('');

    const sections = GROUPS.map((group, index) => {
      const tokens = groups.get(group.id) || [];
      if (!tokens.length) return '';
      return `
        <details class="cz-group" ${index < 2 ? 'open' : ''} data-group="${group.id}">
          <summary>${escapeAttr(group.label)}</summary>
          ${group.hint ? `<p class="cz-hint">${escapeAttr(group.hint)}</p>` : ''}
          ${tokens.map((token) => this.rowHtml(token)).join('')}
        </details>`;
    }).join('');

    this.node.innerHTML = `
      <header class="cz-head">
        <h2>Theme</h2>
        <button type="button" class="icon-button" data-cz="close" aria-label="Close">&times;</button>
      </header>

      <div class="cz-presets">${presetButtons}</div>

      <div class="cz-body">${sections}</div>

      <div class="cz-audit">
        <h3>Contrast</h3>
        <div data-cz="audit"></div>
      </div>

      <footer class="cz-foot">
        <button type="button" class="ghost" data-cz="reset">Reset</button>
        <button type="button" class="ghost" data-cz="export">Copy</button>
        <button type="button" class="ghost" data-cz="import">Paste</button>
      </footer>`;

    this.auditNode = this.node.querySelector('[data-cz="audit"]');

    for (const token of TOKENS) {
      const row = this.node.querySelector(`[data-token="${token.key}"]`);
      if (!row) continue;
      this.controls.set(token.key, {
        token,
        input: row.querySelector('[data-role="input"]'),
        hex: row.querySelector('[data-role="hex"]'),
        value: row.querySelector('[data-role="value"]'),
      });
    }
  }

  rowHtml(token) {
    const value = theme.get(token.key);
    const note = token.hint
      ? `<p class="cz-note">${escapeAttr(token.hint)}</p>` : '';

    if (token.type === 'color') {
      return `
        <div class="cz-row" data-token="${token.key}">
          <label for="cz-${token.key}">${escapeAttr(token.label)}</label>
          <span class="cz-value" data-role="value"></span>
          ${note}
          <div class="cz-control">
            <input class="cz-swatch" type="color" id="cz-${token.key}"
                   data-role="input" value="${escapeAttr(value)}">
            <input class="cz-hex" type="text" data-role="hex" spellcheck="false"
                   value="${escapeAttr(value)}" aria-label="${escapeAttr(token.label)} hex">
          </div>
        </div>`;
    }

    if (token.type === 'select') {
      const options = token.options.map((option) =>
        `<option value="${escapeAttr(option.value)}"`
        + `${option.value === value ? ' selected' : ''}>`
        + `${escapeAttr(option.label)}</option>`).join('');
      return `
        <div class="cz-row" data-token="${token.key}">
          <label for="cz-${token.key}">${escapeAttr(token.label)}</label>
          <span class="cz-value" data-role="value"></span>
          ${note}
          <div class="cz-control">
            <select class="cz-select" id="cz-${token.key}" data-role="input">${options}</select>
          </div>
        </div>`;
    }

    return `
      <div class="cz-row" data-token="${token.key}">
        <label for="cz-${token.key}">${escapeAttr(token.label)}</label>
        <span class="cz-value" data-role="value"></span>
        ${note}
        <div class="cz-control">
          <input type="range" id="cz-${token.key}" data-role="input"
                 min="${token.min}" max="${token.max}" step="${token.step}"
                 value="${escapeAttr(value)}"
                 style="--fill:${fillPercent(token, value)}%">
        </div>
      </div>`;
  }

  /* ── wiring ─────────────────────────────────────────────────────────── */

  bind() {
    // One delegated listener for every control, rather than one per token.
    this.node.addEventListener('input', (event) => {
      const row = event.target.closest('[data-token]');
      if (!row) return;
      const entry = this.controls.get(row.dataset.token);
      if (!entry) return;

      const role = event.target.dataset.role;
      let value = event.target.value;

      if (entry.token.type === 'color' && role === 'hex') {
        const text = value.trim();
        // Only act on a complete colour: writing on every keystroke would
        // flash through nonsense while somebody types out six digits.
        if (!/^#([0-9a-f]{3}|[0-9a-f]{6})$/i.test(text)) return;
        value = text;
      }

      // commit:false — the visual update is immediate, the write to storage
      // is debounced below.
      theme.set(entry.token.key, value, { commit: false });
      this.commit();
    });

    this.node.addEventListener('change', (event) => {
      const row = event.target.closest('[data-token]');
      if (row) { theme.save(); return; }
    });

    this.node.addEventListener('click', (event) => {
      const preset = event.target.closest('[data-preset]');
      if (preset) {
        theme.applyPreset(preset.dataset.preset);
        this.toast(`${PRESETS[preset.dataset.preset].label} applied`);
        return;
      }

      const action = event.target.closest('[data-cz]')?.dataset.cz;
      if (action === 'close') this.hide();
      else if (action === 'reset') {
        theme.reset();
        this.toast('Theme reset');
      } else if (action === 'export') this.exportTheme();
      else if (action === 'import') this.importTheme();
    });
  }

  /* ── syncing ────────────────────────────────────────────────────────── */

  syncToken(key) {
    const entry = this.controls.get(key);
    if (!entry) return;
    const { token, input, hex, value } = entry;
    const current = theme.get(key);

    if (token.type === 'color') {
      if (input && input.value !== current) input.value = current;
      if (hex && hex.value.toLowerCase() !== String(current).toLowerCase()) {
        hex.value = current;
      }
      if (value) value.textContent = '';
      return;
    }

    if (token.type === 'select') {
      if (input && input.value !== current) input.value = current;
      if (value) value.textContent = '';
      return;
    }

    if (input && Number(input.value) !== Number(current)) input.value = current;
    if (input) input.style.setProperty('--fill', `${fillPercent(token, current)}%`);
    if (value) {
      const unit = token.unit || '';
      const shown = Number.isInteger(current) ? current : Number(current).toFixed(2);
      value.textContent = `${shown}${unit}`;
    }
  }

  syncAll() {
    for (const key of this.controls.keys()) this.syncToken(key);
    this.refreshAudit();
  }

  scheduleAudit() {
    if (this._auditRaf) return;
    this._auditRaf = requestAnimationFrame(() => {
      this._auditRaf = 0;
      this.refreshAudit();
    });
  }

  /**
   * Live contrast readout. A palette that has become unreadable says so
   * here, rather than being discovered later by someone trying to read it.
   */
  refreshAudit() {
    if (!this.auditNode) return;
    const rows = theme.audit();
    this.auditNode.innerHTML = rows.map((row) => `
      <div class="cz-audit-row" data-pass="${row.pass}">
        <span>${escapeAttr(row.label)}</span>
        <b>${row.ratio.toFixed(1)}:1</b>
      </div>`).join('');
  }

  /* ── import / export ────────────────────────────────────────────────── */

  async exportTheme() {
    const text = JSON.stringify(theme.toJSON(), null, 2);
    try {
      await navigator.clipboard.writeText(text);
      this.toast('Theme copied to the clipboard');
    } catch (error) {
      // Clipboard access needs a secure context and a user gesture; when it
      // is refused, fall back to something the user can still act on.
      window.prompt('Copy your theme:', text);
    }
  }

  async importTheme() {
    let text = '';
    try {
      text = await navigator.clipboard.readText();
    } catch (error) {
      text = window.prompt('Paste a theme:') || '';
    }
    if (!text.trim()) return;
    try {
      theme.fromJSON(text);
      this.toast('Theme applied');
    } catch (error) {
      this.toast('That did not look like a theme', true);
    }
  }

  /* ── visibility ─────────────────────────────────────────────────────── */

  show() {
    this.open = true;
    this.node.dataset.open = 'true';
    this.node.removeAttribute('inert');
    this.refreshAudit();
  }

  hide() {
    this.open = false;
    this.node.dataset.open = 'false';
    // inert keeps the panel out of the tab order while it is off-screen,
    // so tabbing through the page does not disappear into it.
    this.node.setAttribute('inert', '');
  }

  toggle() {
    if (this.open) this.hide();
    else this.show();
  }

  toast(message, bad = false) {
    if (this.onToast) this.onToast(message, bad);
  }
}
