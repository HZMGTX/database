/* ═══════════════════════════════════════════════════════════════════════════
   THEME ENGINE

   Every visual property in this interface is a token. Nothing is hardcoded
   in a component; the stylesheet composes everything from the primitives
   declared here, so changing one value updates every place it is used --
   including the canvas, which reads the live values each frame rather than
   caching them at start-up.

   Three design rules this follows:

     1. **Primitives, not results.** Colours are stored as RGB triples and
        alphas as separate scalars, so a glow can be rebuilt at any
        intensity from the same hue. Storing a finished `box-shadow` string
        would make "make the glow subtler" impossible without re-parsing it.

     2. **One write per frame, at most.** Applying a token writes a single
        CSS custom property on the document element. The browser then
        recomputes only what actually references it. No component is told
        about the change, nothing re-renders, and there is no flicker,
        because no DOM is replaced.

     3. **The default profile is a real design, not a placeholder.** It ships
        as the signature cosmic palette, and every colour in it has been
        checked for contrast against the surface it sits on.
   ═══════════════════════════════════════════════════════════════════════ */

const STORAGE_KEY = 'vault.theme.v1';

/* ── token registry ────────────────────────────────────────────────────────
   Each token declares how it is edited and how it reaches CSS. `css` is the
   custom property written to :root. Colour tokens additionally write a
   `-rgb` companion so the stylesheet can compose rgba() at any alpha.      */

export const GROUPS = [
  { id: 'base', label: 'Base', hint: 'The ground everything sits on.' },
  { id: 'planes', label: 'Panels', hint: 'Floating surfaces and their edges.' },
  { id: 'ink', label: 'Text', hint: 'Every level of emphasis.' },
  { id: 'accent', label: 'Accents', hint: 'Signals: focus, selection, state.' },
  { id: 'glass', label: 'Glass', hint: 'Backdrop blur and saturation.' },
  { id: 'glow', label: 'Glow', hint: 'Bloom radii and intensity.' },
  { id: 'layout', label: 'Layout', hint: 'The modular grid and its metrics.' },
  { id: 'type', label: 'Type', hint: 'Family and scale.' },
  { id: 'depth', label: 'Depth', hint: 'Z-index stratification.' },
  { id: 'motion', label: 'Motion', hint: 'Field physics and timing.' },
];

export const TOKENS = [
  /* ── base ─────────────────────────────────────────────────────────── */
  { key: 'space',        group: 'base',   type: 'color', css: '--c-space',
    label: 'Void',            def: '#05030f',
    hint: 'The page behind every plane.' },
  { key: 'spaceDeep',    group: 'base',   type: 'color', css: '--c-space-deep',
    label: 'Deep void',       def: '#010006',
    hint: 'Vignette edge and code wells.' },
  { key: 'nebulaA',      group: 'base',   type: 'color', css: '--c-nebula-a',
    label: 'Nebula A',        def: '#7800c8' },
  { key: 'nebulaB',      group: 'base',   type: 'color', css: '--c-nebula-b',
    label: 'Nebula B',        def: '#0096c8' },
  { key: 'nebulaC',      group: 'base',   type: 'color', css: '--c-nebula-c',
    label: 'Nebula C',        def: '#46008c' },
  { key: 'nebulaAlpha',  group: 'base',   type: 'range', css: '--nebula-alpha',
    label: 'Nebula strength', def: 0.22, min: 0, max: 0.6, step: 0.01, unit: '' },
  { key: 'vignette',     group: 'base',   type: 'range', css: '--vignette-alpha',
    label: 'Vignette',        def: 0.72, min: 0, max: 1, step: 0.02, unit: '' },

  /* ── planes ───────────────────────────────────────────────────────── */
  { key: 'plane',        group: 'planes', type: 'color', css: '--c-plane',
    label: 'Panel tint',      def: '#0f061c' },
  { key: 'planeRaised',  group: 'planes', type: 'color', css: '--c-plane-raised',
    label: 'Raised tint',     def: '#1e0c32' },
  { key: 'planeAlpha',   group: 'planes', type: 'range', css: '--plane-alpha',
    label: 'Panel opacity',   def: 0.72, min: 0.2, max: 1, step: 0.02, unit: '' },
  { key: 'planeAlpha2',  group: 'planes', type: 'range', css: '--plane-alpha-2',
    label: 'Tile opacity',    def: 0.66, min: 0.1, max: 1, step: 0.02, unit: '' },
  { key: 'edge',         group: 'planes', type: 'color', css: '--c-edge',
    label: 'Hairline',        def: '#a078ff' },
  { key: 'edgeAlpha',    group: 'planes', type: 'range', css: '--edge-alpha',
    label: 'Hairline opacity', def: 0.14, min: 0, max: 0.8, step: 0.01, unit: '' },
  { key: 'edgeAlphaHi',  group: 'planes', type: 'range', css: '--edge-alpha-hi',
    label: 'Hairline hover',  def: 0.28, min: 0, max: 1, step: 0.01, unit: '' },

  /* ── ink ──────────────────────────────────────────────────────────── */
  { key: 'ink',          group: 'ink',    type: 'color', css: '--c-ink',
    label: 'Body',            def: '#ece9fb' },
  { key: 'ink2',         group: 'ink',    type: 'color', css: '--c-ink-2',
    label: 'Secondary',       def: '#b4abd6' },
  { key: 'ink3',         group: 'ink',    type: 'color', css: '--c-ink-3',
    label: 'Muted',           def: '#7d739f' },
  { key: 'inkDim',       group: 'ink',    type: 'color', css: '--c-ink-dim',
    label: 'Dim',             def: '#8c84ab',
    hint: 'Timestamps and counts. Kept above 4.5:1 by default.' },

  /* ── accents ──────────────────────────────────────────────────────── */
  { key: 'accent',       group: 'accent', type: 'color', css: '--c-accent',
    label: 'Accent 1',        def: '#00f8ff',
    hint: 'Focus rings, links, selection.' },
  { key: 'accent2',      group: 'accent', type: 'color', css: '--c-accent-2',
    label: 'Accent 2',        def: '#ba00ff',
    hint: 'Kind labels, tags, markers.' },
  { key: 'accentDeep',   group: 'accent', type: 'color', css: '--c-accent-deep',
    label: 'Accent 1 deep',   def: '#00b6c4' },
  { key: 'accent2Deep',  group: 'accent', type: 'color', css: '--c-accent-2-deep',
    label: 'Accent 2 deep',   def: '#7d00ad' },
  { key: 'warn',         group: 'accent', type: 'color', css: '--c-warn',
    label: 'Pinned',          def: '#ffc24d' },
  { key: 'danger',       group: 'accent', type: 'color', css: '--c-danger',
    label: 'Danger',          def: '#ff4d7e' },

  /* ── glass ────────────────────────────────────────────────────────── */
  { key: 'blur',         group: 'glass',  type: 'range', css: '--glass-blur',
    label: 'Backdrop blur',   def: 28, min: 0, max: 40, step: 1, unit: 'px' },
  { key: 'saturate',     group: 'glass',  type: 'range', css: '--glass-sat',
    label: 'Saturation',      def: 240, min: 100, max: 300, step: 5, unit: '%' },

  /* ── glow ─────────────────────────────────────────────────────────── */
  { key: 'glowIntensity', group: 'glow',  type: 'range', css: '--glow-intensity',
    label: 'Glow intensity',  def: 1, min: 0, max: 2.5, step: 0.05, unit: '' },
  { key: 'glowNear',     group: 'glow',   type: 'range', css: '--glow-near',
    label: 'Inner radius',    def: 12, min: 0, max: 48, step: 1, unit: 'px' },
  { key: 'glowFar',      group: 'glow',   type: 'range', css: '--glow-far',
    label: 'Outer radius',    def: 36, min: 0, max: 120, step: 2, unit: 'px' },
  { key: 'glowRing',     group: 'glow',   type: 'range', css: '--glow-ring',
    label: 'Ring opacity',    def: 0.34, min: 0, max: 1, step: 0.02, unit: '' },
  { key: 'shadowAlpha',  group: 'glow',   type: 'range', css: '--shadow-alpha',
    label: 'Drop shadow',     def: 0.55, min: 0, max: 1, step: 0.02, unit: '' },
  { key: 'insetAlpha',   group: 'glow',   type: 'range', css: '--inset-alpha',
    label: 'Inner highlight', def: 0.08, min: 0, max: 1, step: 0.01, unit: '' },

  /* ── layout ───────────────────────────────────────────────────────── */
  { key: 'radius',       group: 'layout', type: 'range', css: '--r-base',
    label: 'Corner radius',   def: 12, min: 0, max: 28, step: 1, unit: 'px',
    hint: 'Every other radius derives from this.' },
  { key: 'gap',          group: 'layout', type: 'range', css: '--gap',
    label: 'Module gap',      def: 8, min: 0, max: 24, step: 1, unit: 'px' },
  { key: 'pad',          group: 'layout', type: 'range', css: '--pad',
    label: 'Module padding',  def: 16, min: 8, max: 32, step: 1, unit: 'px' },
  { key: 'railWidth',    group: 'layout', type: 'range', css: '--rail-w',
    label: 'Rail width',      def: 244, min: 180, max: 360, step: 4, unit: 'px' },
  { key: 'tap',          group: 'layout', type: 'range', css: '--tap',
    label: 'Control height',  def: 42, min: 32, max: 56, step: 1, unit: 'px' },
  { key: 'tileGap',      group: 'layout', type: 'range', css: '--tile-gap',
    label: 'Row spacing',     def: 8, min: 0, max: 20, step: 1, unit: 'px' },

  /* ── type ─────────────────────────────────────────────────────────── */
  { key: 'fontBody',     group: 'type',   type: 'select', css: '--font',
    label: 'Interface font',  def: 'system',
    options: [
      { value: 'system', label: 'System',
        css: 'ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif' },
      { value: 'grotesk', label: 'Grotesque',
        css: '"Helvetica Neue", Helvetica, "Segoe UI", Arial, sans-serif' },
      { value: 'humanist', label: 'Humanist',
        css: 'Optima, Candara, "Gill Sans", "Segoe UI", sans-serif' },
      { value: 'serif', label: 'Serif',
        css: 'Charter, "Iowan Old Style", Georgia, "Times New Roman", serif' },
      { value: 'mono', label: 'Monospace',
        css: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace' },
    ] },
  { key: 'fontMono',     group: 'type',   type: 'select', css: '--mono',
    label: 'Code font',       def: 'ui',
    options: [
      { value: 'ui', label: 'UI monospace',
        css: 'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace' },
      { value: 'courier', label: 'Courier',
        css: '"Courier New", Courier, monospace' },
    ] },
  { key: 'fontSize',     group: 'type',   type: 'range', css: '--font-size',
    label: 'Base size',       def: 15, min: 12, max: 19, step: 1, unit: 'px' },
  { key: 'lineHeight',   group: 'type',   type: 'range', css: '--line',
    label: 'Line height',     def: 1.58, min: 1.2, max: 2, step: 0.02, unit: '' },
  { key: 'tracking',     group: 'type',   type: 'range', css: '--tracking',
    label: 'Letter spacing',  def: 0, min: -0.02, max: 0.08, step: 0.005, unit: 'em' },

  /* ── depth ────────────────────────────────────────────────────────── */
  { key: 'zField',       group: 'depth',  type: 'range', css: '--z-field',
    label: 'Field plane',     def: 0, min: 0, max: 9, step: 1, unit: '' },
  { key: 'zChrome',      group: 'depth',  type: 'range', css: '--z-chrome',
    label: 'Interface plane', def: 10, min: 10, max: 49, step: 1, unit: '' },
  { key: 'zHud',         group: 'depth',  type: 'range', css: '--z-hud',
    label: 'HUD plane',       def: 60, min: 50, max: 99, step: 1, unit: '' },
  { key: 'parallax',     group: 'depth',  type: 'range', css: '--parallax',
    label: 'Parallax throw',  def: 26, min: 0, max: 80, step: 2, unit: '' },

  /* ── motion ───────────────────────────────────────────────────────── */
  { key: 'speed',        group: 'motion', type: 'range', css: '--t-scale',
    label: 'Transition speed', def: 1, min: 0.2, max: 2.5, step: 0.05, unit: '' },
  { key: 'springX',      group: 'motion', type: 'range', css: '--spring-x',
    label: 'Spring tension',  def: 0.1, min: 0.01, max: 0.9, step: 0.01, unit: '' },
  { key: 'springY',      group: 'motion', type: 'range', css: '--spring-y',
    label: 'Spring overshoot', def: 1, min: 0.6, max: 1.6, step: 0.02, unit: '' },
  { key: 'fieldCount',   group: 'motion', type: 'range', css: '--field-count',
    label: 'Star count',      def: 92, min: 0, max: 320, step: 4, unit: '' },
  { key: 'fieldSpeed',   group: 'motion', type: 'range', css: '--field-speed',
    label: 'Drift speed',     def: 1, min: 0, max: 4, step: 0.05, unit: '' },
  { key: 'fieldBloom',   group: 'motion', type: 'range', css: '--field-bloom',
    label: 'Star bloom',      def: 1, min: 0, max: 3, step: 0.05, unit: '' },
];

const BY_KEY = new Map(TOKENS.map((t) => [t.key, t]));

/* ── presets ──────────────────────────────────────────────────────────────
   Whole-palette starting points. Each one only overrides what it needs;
   everything else falls back to the default profile.                      */

export const PRESETS = {
  cosmic: {
    label: 'Cosmic (default)',
    values: {},
  },
  abyss: {
    label: 'Abyss',
    values: {
      space: '#00060a', spaceDeep: '#000306', plane: '#04141c',
      planeRaised: '#062430', accent: '#2bf5c0', accent2: '#0aa6ff',
      accentDeep: '#149e80', accent2Deep: '#0064a8',
      nebulaA: '#006e8c', nebulaB: '#00a884', nebulaC: '#003a52',
      edge: '#3fd8c0', edgeAlpha: 0.14,
    },
  },
  ember: {
    label: 'Ember',
    values: {
      space: '#0b0503', spaceDeep: '#050201', plane: '#1c0a06',
      planeRaised: '#2e1109', accent: '#ffb545', accent2: '#ff4d2e',
      accentDeep: '#c07a18', accent2Deep: '#a32612',
      nebulaA: '#a83208', nebulaB: '#c47a10', nebulaC: '#5c1604',
      edge: '#ff9a5c', edgeAlpha: 0.15, ink: '#fbeee6', ink2: '#d9bfae',
      ink3: '#a3866f', inkDim: '#b09880',
    },
  },
  graphite: {
    label: 'Graphite',
    values: {
      space: '#0d0e10', spaceDeep: '#08090a', plane: '#17191d',
      planeRaised: '#22252b', accent: '#7fd4ff', accent2: '#a6b4c8',
      accentDeep: '#3f88ad', accent2Deep: '#5c6a7d',
      nebulaA: '#2b3a4a', nebulaB: '#233444', nebulaC: '#1a2028',
      nebulaAlpha: 0.10, edge: '#8fa3bd', edgeAlpha: 0.16,
      blur: 16, saturate: 130, glowIntensity: 0.45, glowFar: 20,
      ink: '#e8eaee', ink2: '#b0b6c0', ink3: '#7e8590', inkDim: '#8b929d',
    },
  },
  paper: {
    label: 'Paper (light)',
    values: {
      space: '#f7f6f2', spaceDeep: '#ffffff', plane: '#ffffff',
      planeRaised: '#f0efe9', planeAlpha: 0.94, planeAlpha2: 0.92,
      accent: '#0f6f5c', accent2: '#7126a8',
      accentDeep: '#0b5347', accent2Deep: '#4e1a75',
      nebulaA: '#d8cff0', nebulaB: '#cfe6e0', nebulaC: '#e6dff5',
      nebulaAlpha: 0.30, vignette: 0.06,
      edge: '#2a2417', edgeAlpha: 0.16, edgeAlphaHi: 0.32,
      ink: '#15140f', ink2: '#45423a', ink3: '#6b675c', inkDim: '#5e5a50',
      blur: 12, saturate: 140,
      glowIntensity: 0.30, glowNear: 6, glowFar: 14, glowRing: 0.22,
      shadowAlpha: 0.12, insetAlpha: 0.5,
      warn: '#9a6b00', danger: '#b02a37',
      fieldCount: 0,
    },
  },
};

/* ── colour helpers ────────────────────────────────────────────────────── */

const HEX3 = /^#([0-9a-f])([0-9a-f])([0-9a-f])$/i;
const HEX6 = /^#([0-9a-f]{6})$/i;

export function hexToRgb(hex) {
  let value = String(hex || '').trim();
  const short = value.match(HEX3);
  if (short) value = `#${short[1]}${short[1]}${short[2]}${short[2]}${short[3]}${short[3]}`;
  const full = value.match(HEX6);
  if (!full) return [0, 0, 0];
  const n = parseInt(full[1], 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function channelLuminance(channel) {
  const c = channel / 255;
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

export function relativeLuminance(hex) {
  const [r, g, b] = hexToRgb(hex);
  return 0.2126 * channelLuminance(r)
       + 0.7152 * channelLuminance(g)
       + 0.0722 * channelLuminance(b);
}

/** WCAG contrast ratio between two hex colours. */
export function contrast(a, b) {
  const la = relativeLuminance(a);
  const lb = relativeLuminance(b);
  const hi = la > lb ? la : lb;
  const lo = la > lb ? lb : la;
  return (hi + 0.05) / (lo + 0.05);
}

/** Flatten a translucent plane over the page colour, for honest contrast maths. */
export function composite(hex, alpha, overHex) {
  const [r, g, b] = hexToRgb(hex);
  const [br, bg, bb] = hexToRgb(overHex);
  const mix = (f, back) => Math.round(f * alpha + back * (1 - alpha));
  const to2 = (n) => n.toString(16).padStart(2, '0');
  return `#${to2(mix(r, br))}${to2(mix(g, bg))}${to2(mix(b, bb))}`;
}

/* ── state ─────────────────────────────────────────────────────────────── */

function defaults() {
  const out = Object.create(null);
  for (const token of TOKENS) out[token.key] = token.def;
  return out;
}

function clampNumber(token, value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return token.def;
  if (n < token.min) return token.min;
  if (n > token.max) return token.max;
  return n;
}

/** Coerce anything loaded from storage into a value the token accepts. */
function sanitise(token, value) {
  if (value === undefined || value === null) return token.def;
  if (token.type === 'color') {
    const text = String(value).trim();
    return (HEX6.test(text) || HEX3.test(text)) ? text.toLowerCase() : token.def;
  }
  if (token.type === 'range') return clampNumber(token, value);
  if (token.type === 'select') {
    return token.options.some((o) => o.value === value) ? value : token.def;
  }
  return token.def;
}

export class Theme {
  /**
   * @param {HTMLElement|null} root Element whose inline style carries the
   *   custom properties. Defaults to the document element, and tolerates
   *   there being no document at all so the engine and its colour maths can
   *   be exercised outside a browser.
   */
  constructor(root = (typeof document !== 'undefined' ? document.documentElement : null)) {
    this.root = root;
    this.values = defaults();
    this.listeners = new Set();
    // A flat numeric mirror of the tokens the canvas reads every frame.
    // Keeping it here means the render loop never touches getComputedStyle,
    // which would force a style recalculation 60 times a second.
    this.live = Object.create(null);
    this._raf = 0;
    this._pending = new Set();
  }

  /* -- reading -------------------------------------------------------- */

  get(key) { return this.values[key]; }

  token(key) { return BY_KEY.get(key); }

  /** Numeric value of a range token, for the canvas. */
  number(key) {
    const value = this.values[key];
    return typeof value === 'number' ? value : Number(value) || 0;
  }

  /** RGB triple of a colour token, for the canvas. */
  rgb(key) {
    const cached = this.live[key];
    if (cached) return cached;
    const value = hexToRgb(this.values[key]);
    this.live[key] = value;
    return value;
  }

  /* -- writing -------------------------------------------------------- */

  /**
   * Set one token. Writes a single custom property; nothing re-renders.
   *
   * Writes are coalesced into one animation frame, so dragging a slider
   * produces at most one style write per frame however fast the input
   * fires -- which is what keeps a drag smooth instead of thrashing style
   * recalculation.
   */
  set(key, value, { commit = true, immediate = false } = {}) {
    const token = BY_KEY.get(key);
    if (!token) return;

    const clean = sanitise(token, value);
    if (this.values[key] === clean) return;
    this.values[key] = clean;
    delete this.live[key];

    const canSchedule = typeof requestAnimationFrame === 'function';
    if (immediate || !canSchedule) {
      this.applyToken(token);
      this.notify(key);
    } else {
      this._pending.add(key);
      if (!this._raf) {
        this._raf = requestAnimationFrame(() => {
          this._raf = 0;
          const keys = Array.from(this._pending);
          this._pending.clear();
          for (const k of keys) this.applyToken(BY_KEY.get(k));
          for (const k of keys) this.notify(k);
        });
      }
    }
    if (commit) this.save();
  }

  /** Apply one token to CSS. */
  applyToken(token) {
    if (!token || !this.root) return;
    const value = this.values[token.key];
    const style = this.root.style;

    if (token.type === 'color') {
      const [r, g, b] = hexToRgb(value);
      style.setProperty(token.css, value);
      style.setProperty(`${token.css}-rgb`, `${r}, ${g}, ${b}`);
      return;
    }
    if (token.type === 'select') {
      const option = token.options.find((o) => o.value === value) || token.options[0];
      style.setProperty(token.css, option.css);
      return;
    }
    style.setProperty(token.css, `${value}${token.unit || ''}`);
  }

  /** Apply every token. Used on boot and after a preset or reset. */
  applyAll() {
    for (const token of TOKENS) this.applyToken(token);
    this.live = Object.create(null);
    this.notify('*');
  }

  /** Replace the whole profile. */
  setAll(values, { commit = true } = {}) {
    for (const token of TOKENS) {
      this.values[token.key] = sanitise(token, values[token.key]);
    }
    this.applyAll();
    if (commit) this.save();
  }

  applyPreset(name, { commit = true } = {}) {
    const preset = PRESETS[name];
    if (!preset) return;
    this.setAll({ ...defaults(), ...preset.values }, { commit });
  }

  reset({ commit = true } = {}) {
    this.setAll(defaults(), { commit });
  }

  /* -- persistence ---------------------------------------------------- */

  /**
   * Persist. Only values that differ from the defaults are stored, so a
   * later release that changes a default is picked up rather than being
   * masked by a saved copy of the old one.
   */
  save() {
    if (typeof localStorage === 'undefined') return;
    try {
      const diff = Object.create(null);
      for (const token of TOKENS) {
        if (this.values[token.key] !== token.def) diff[token.key] = this.values[token.key];
      }
      if (Object.keys(diff).length === 0) {
        localStorage.removeItem(STORAGE_KEY);
      } else {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(diff));
      }
    } catch (error) {
      // Private browsing, a full quota, or storage disabled entirely. The
      // theme still works for this session; it simply will not persist.
    }
  }

  load() {
    let stored = null;
    try {
      const raw = typeof localStorage === 'undefined'
        ? null : localStorage.getItem(STORAGE_KEY);
      if (raw) stored = JSON.parse(raw);
    } catch (error) {
      stored = null;
    }
    if (stored && typeof stored === 'object') {
      for (const token of TOKENS) {
        if (token.key in stored) {
          this.values[token.key] = sanitise(token, stored[token.key]);
        }
      }
    }
    this.applyAll();
    return this;
  }

  /* -- export / import ------------------------------------------------ */

  toJSON() {
    const out = Object.create(null);
    for (const token of TOKENS) out[token.key] = this.values[token.key];
    return out;
  }

  fromJSON(text, { commit = true } = {}) {
    const parsed = typeof text === 'string' ? JSON.parse(text) : text;
    if (!parsed || typeof parsed !== 'object') throw new Error('not a theme');
    this.setAll(parsed, { commit });
  }

  /* -- change notification -------------------------------------------- */

  subscribe(listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  notify(key) {
    for (const listener of this.listeners) {
      try {
        listener(key, this);
      } catch (error) {
        // A misbehaving listener must not stop the others, or one broken
        // panel would freeze the whole theme.
      }
    }
  }

  /* -- accessibility --------------------------------------------------- */

  /**
   * Contrast of every text token against the surface it actually sits on,
   * with translucency flattened first. The customizer shows these live, so
   * a palette that has become unreadable says so rather than waiting to be
   * discovered.
   */
  audit() {
    const surface = composite(this.values.plane, this.values.planeAlpha2,
                              this.values.space);
    const raised = composite(this.values.planeRaised, this.values.planeAlpha2,
                             this.values.space);
    const rows = [
      { label: 'Body text', fg: this.values.ink, bg: surface, need: 4.5 },
      { label: 'Secondary', fg: this.values.ink2, bg: surface, need: 4.5 },
      { label: 'Muted', fg: this.values.ink3, bg: surface, need: 4.5 },
      { label: 'Dim', fg: this.values.inkDim, bg: surface, need: 4.5 },
      { label: 'Accent 1', fg: this.values.accent, bg: surface, need: 4.5 },
      { label: 'Accent 2', fg: this.values.accent2, bg: surface, need: 3.0 },
      { label: 'On raised', fg: this.values.ink, bg: raised, need: 4.5 },
    ];
    for (const row of rows) {
      row.ratio = contrast(row.fg, row.bg);
      row.pass = row.ratio >= row.need;
    }
    return rows;
  }
}

export const theme = new Theme();
