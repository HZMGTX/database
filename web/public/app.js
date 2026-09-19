/**
 * The interface.
 *
 * Talks to /api over fetch. The token is held in localStorage on this device
 * only -- it is never sent anywhere but this origin's own API, and "Lock"
 * removes it.
 *
 * Every string that comes back from the database reaches the page through
 * textContent. Nothing builds markup from stored text, so an item titled
 * `<img onerror=...>` is a title, not a tag.
 */
(() => {
  "use strict";

  const KEY = "db.token";

  /** What the server wraps a matched run in: STX and ETX. */
  const MARK_START = "\u0002";
  const MARK_END = "\u0003";
  const el = (id) => document.getElementById(id);

  const ui = {
    gate: el("gate"), gateForm: el("gate-form"), token: el("token"),
    gateError: el("gate-error"), gateHint: el("gate-hint"),
    app: el("app"), omni: el("omni"), q: el("q"), explain: el("explain"),
    count: el("count"), results: el("results"), more: el("more"),
    empty: el("empty"), emptyTitle: el("empty-title"), emptyBody: el("empty-body"),
    totals: el("totals"), kinds: el("kinds"), tags: el("tags"),
    kindList: el("kind-list"), projectList: el("project-list"),
    projects: el("projects"), fProject: el("f-project"),
    sheet: el("sheet"), scrim: el("scrim"), editor: el("editor"),
    heading: el("sheet-heading"), close: el("close"),
    fTitle: el("f-title"), fKind: el("f-kind"), fTags: el("f-tags"),
    fBody: el("f-body"), fProps: el("f-props"), fPinned: el("f-pinned"),
    formError: el("form-error"), metaLine: el("meta-line"),
    save: el("save"), del: el("delete"), newItem: el("new-item"), lock: el("lock"),
    toast: el("toast"),
  };

  let token = "";
  let editing = null;      // the item open in the sheet, or null for a new one
  let offset = 0;
  let lastQuery = "";
  let projects = [];       // [{slug, label, colour, n}]
  // The project filter is held apart from the search box. Narrowing to one
  // project is a place you are, not a term you typed -- it should survive
  // clearing the search, and clearing the search should not dump you back
  // into all 54,000 rows.
  let project = null;

  // ── Transport ───────────────────────────────────────────────────────────

  async function api(path, { method = "GET", body, signal } = {}) {
    const response = await fetch(`/api${path}`, {
      method,
      signal,
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });

    const text = await response.text();
    let payload = null;
    if (text) {
      try { payload = JSON.parse(text); }
      catch { throw new Error(`The server answered with something that is not JSON (${response.status}).`); }
    }
    if (!response.ok) {
      const error = new Error(payload?.detail || `HTTP ${response.status}`);
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function toast(message, tone = "") {
    ui.toast.textContent = message;
    ui.toast.dataset.tone = tone;
    ui.toast.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { ui.toast.hidden = true; }, 3200);
  }

  // ── Gate ────────────────────────────────────────────────────────────────

  async function openWith(candidate) {
    token = candidate;
    await api("/stats");            // the cheapest call that requires the token
    localStorage.setItem(KEY, candidate);
    ui.gate.hidden = true;
    ui.app.hidden = false;
    await Promise.all([loadFacets(), run("")]);
    ui.q.focus();
  }

  ui.gateForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    ui.gateError.hidden = true;
    const candidate = ui.token.value.trim();
    if (!candidate) return;
    try {
      await openWith(candidate);
    } catch (error) {
      token = "";
      ui.gateError.textContent =
        error.status === 503 ? error.message : "That token was not accepted.";
      ui.gateError.hidden = false;
    }
  });

  ui.lock.addEventListener("click", () => {
    localStorage.removeItem(KEY);
    location.reload();
  });

  // ── Facets ──────────────────────────────────────────────────────────────

  function colourOf(slug) {
    return projects.find((p) => p.slug === slug)?.colour || "var(--ink-3)";
  }

  function labelOf(slug) {
    if (slug === "") return "Unfiled";
    return projects.find((p) => p.slug === slug)?.label || slug;
  }

  function renderProjects() {
    ui.projects.textContent = "";

    const total = projects.reduce((sum, p) => sum + p.n, 0);
    const entries = [{ slug: null, label: "Everything", colour: null, n: total },
                     ...projects];

    for (const entry of entries) {
      const button = document.createElement("button");
      button.type = "button";
      button.setAttribute("aria-pressed", String(entry.slug === project));
      if (entry.colour) button.style.color = entry.colour;

      const swatch = document.createElement("span");
      swatch.className = "swatch";
      if (entry.colour) swatch.style.background = entry.colour;
      const label = document.createElement("span");
      label.textContent = entry.label;
      const count = document.createElement("span");
      count.className = "n";
      count.textContent = entry.n.toLocaleString();

      button.append(swatch, label, count);
      button.addEventListener("click", () => {
        project = entry.slug === project ? null : entry.slug;
        renderProjects();
        run(ui.q.value);
      });
      ui.projects.append(button);
    }
  }

  async function loadFacets() {
    const stats = await api("/stats");
    projects = stats.projects || [];
    renderProjects();

    ui.projectList.textContent = "";
    for (const p of projects) {
      if (!p.slug) continue;
      const option = document.createElement("option");
      option.value = p.slug;
      option.label = p.label;
      ui.projectList.append(option);
    }

    ui.totals.textContent = "";
    const n = document.createElement("b");
    n.textContent = stats.items.toLocaleString();
    ui.totals.append(n, document.createTextNode(
      `item${stats.items === 1 ? "" : "s"}` +
      (stats.trashed ? ` · ${stats.trashed} in the trash` : "")));

    fill(ui.kinds, stats.kinds.map((k) => ({
      label: k.kind, n: k.n, query: `kind:${k.kind}`,
    })));
    fill(ui.tags, stats.tags.map((t) => ({
      label: t.tag, n: t.n, query: `tag:${t.tag}`,
    })));

    ui.kindList.textContent = "";
    for (const k of stats.kinds) {
      const option = document.createElement("option");
      option.value = k.kind;
      ui.kindList.append(option);
    }
  }

  function fill(list, entries) {
    list.textContent = "";
    for (const entry of entries) {
      const li = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.query = entry.query;
      button.setAttribute("aria-pressed", "false");
      const label = document.createElement("span");
      label.textContent = entry.label;
      const count = document.createElement("span");
      count.className = "n";
      count.textContent = entry.n.toLocaleString();
      button.append(label, count);
      button.addEventListener("click", () => {
        const next = ui.q.value.trim() === entry.query ? "" : entry.query;
        ui.q.value = next;
        run(next);
      });
      li.append(button);
      list.append(li);
    }
  }

  // ── Searching ───────────────────────────────────────────────────────────

  let inFlight = null;

  async function run(text, { append = false } = {}) {
    lastQuery = text;
    if (!append) offset = 0;

    inFlight?.abort();
    inFlight = new AbortController();

    // The project filter is prepended rather than typed into the box, so the
    // box keeps showing what the person actually wrote.
    const scoped = project === null
      ? text
      : `project:${project === "" ? "none" : project} ${text}`.trim();

    try {
      const page = await api(
        `/items?q=${encodeURIComponent(scoped)}&limit=50&offset=${offset}`,
        { signal: inFlight.signal });
      render(page, append);
    } catch (error) {
      if (error.name === "AbortError") return;
      ui.results.textContent = "";
      ui.more.hidden = true;
      ui.count.textContent = "";
      ui.empty.hidden = false;
      ui.emptyTitle.textContent = "That query did not work.";
      ui.emptyBody.textContent = error.message;
    }
  }

  function render(page, append) {
    if (!append) ui.results.textContent = "";

    const fragment = document.createDocumentFragment();
    for (const hit of page.hits) fragment.append(row(hit));
    ui.results.append(fragment);

    const shown = ui.results.children.length;
    ui.more.hidden = !page.truncated;
    offset = shown;

    ui.count.textContent = "";
    const strong = document.createElement("b");
    strong.textContent = page.total.toLocaleString() + (page.total_capped ? "+" : "");
    ui.count.append(strong, document.createTextNode(
      ` item${page.total === 1 ? "" : "s"}` +
      (shown < page.total ? `, showing ${shown}` : "") +
      ` · ${page.took_ms} ms`));

    ui.explain.textContent = "";
    if (page.understood?.length) {
      ui.explain.append(document.createTextNode("Read as: "));
      page.understood.forEach((clause, i) => {
        if (i) ui.explain.append(document.createTextNode(", "));
        const b = document.createElement("b");
        b.textContent = clause;
        ui.explain.append(b);
      });
    }

    ui.empty.hidden = shown > 0;
    if (!shown) {
      const searching = Boolean(lastQuery.trim());
      ui.emptyTitle.textContent = searching ? "Nothing matched." : "Nothing in here yet.";
      const where = project === null ? "" : ` in ${labelOf(project)}`;
      ui.emptyTitle.textContent = searching
        ? `Nothing matched${where}.`
        : `Nothing${where} yet.`;
      ui.emptyBody.textContent = searching
        ? (project === null
            ? "Words are combined with AND, so every one has to be present. Try fewer."
            : "Nothing here matches. Pick Everything above to search all projects.")
        : "Press + Add to put the first thing in.";
    }

    for (const button of document.querySelectorAll(".facets button")) {
      button.setAttribute("aria-pressed",
        String(button.dataset.query === lastQuery.trim()));
    }
  }

  function row(hit) {
    const li = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "hit";

    const head = document.createElement("div");
    head.className = "hit-head";
    const kind = document.createElement("span");
    kind.className = "kind";
    kind.textContent = hit.kind;
    head.append(kind);

    // Only worth showing when the list is mixed; inside one project it is
    // the same word on every row and reads as noise.
    if (project === null && hit.project) {
      const badge = document.createElement("span");
      badge.className = "in-project";
      badge.style.color = colourOf(hit.project);
      const swatch = document.createElement("span");
      swatch.className = "swatch";
      badge.append(swatch, document.createTextNode(labelOf(hit.project)));
      head.append(badge);
    }

    const title = document.createElement("span");
    title.className = "hit-title";
    title.textContent = hit.title || "(untitled)";
    head.append(title);
    if (hit.pinned) {
      const pin = document.createElement("span");
      pin.className = "pin";
      pin.textContent = "★";
      head.append(pin);
    }
    button.append(head);

    if (hit.snippet) {
      const preview = document.createElement("div");
      preview.className = "hit-preview";
      // The matched runs arrive wrapped in two control characters so that
      // each caller picks its own rendering. Here they become <mark>, built
      // from text nodes so that stored text is never parsed as markup.
      let cursor = 0;
      for (const part of hit.snippet.split(MARK_START)) {
        const [inside, ...rest] = part.split(MARK_END);
        if (cursor === 0 && !rest.length) {
          preview.append(document.createTextNode(inside));
        } else if (rest.length) {
          const mark = document.createElement("mark");
          mark.textContent = inside;
          preview.append(mark, document.createTextNode(rest.join(MARK_END)));
        } else {
          preview.append(document.createTextNode(inside));
        }
        cursor++;
      }
      button.append(preview);
    }

    const meta = document.createElement("div");
    meta.className = "meta";
    for (const tag of hit.tags || []) {
      const span = document.createElement("span");
      span.className = "tag";
      span.textContent = tag;
      meta.append(span);
    }
    // The first few fields, because for most of this data the fields ARE
    // the content: a fish is its value and its rarity. A field that just
    // repeats the title is dropped, since it crowds out the ones that say
    // something -- catalogue rows carry both `name` and a title built from it.
    const shown = Object.entries(hit.props || {}).filter(([key, value]) =>
      value !== null && typeof value !== "object" &&
      String(value) !== hit.title);
    for (const [key, value] of shown.slice(0, 5)) {
      const span = document.createElement("span");
      span.className = "prop";
      const k = document.createElement("b");
      k.textContent = key + " ";
      span.append(k, document.createTextNode(String(value)));
      meta.append(span);
    }
    if (meta.childNodes.length) button.append(meta);

    button.addEventListener("click", () => open(hit.uid));
    li.append(button);
    return li;
  }

  ui.omni.addEventListener("submit", (event) => { event.preventDefault(); run(ui.q.value); });

  let typing = null;
  ui.q.addEventListener("input", () => {
    clearTimeout(typing);
    typing = setTimeout(() => run(ui.q.value), 180);
  });

  ui.more.addEventListener("click", () => run(lastQuery, { append: true }));

  // ── Editing ─────────────────────────────────────────────────────────────

  async function open(uid) {
    try {
      editing = await api(`/items/${encodeURIComponent(uid)}`);
    } catch (error) {
      return toast(error.message, "bad");
    }
    ui.heading.textContent = "Edit";
    ui.fTitle.value = editing.title;
    ui.fProject.value = editing.project || "";
    ui.fKind.value = editing.kind;
    ui.fTags.value = (editing.tags || []).join(" ");
    ui.fBody.value = editing.body;
    ui.fProps.value = Object.keys(editing.props || {}).length
      ? JSON.stringify(editing.props, null, 2) : "";
    ui.fPinned.checked = Boolean(editing.pinned);
    ui.del.hidden = false;
    ui.metaLine.textContent =
      `uid ${editing.uid} · short ${editing.uid.slice(-8)} · revision ${editing.rev} · ` +
      `created ${new Date(editing.created_at).toLocaleString()}`;
    show();
  }

  function blank() {
    editing = null;
    ui.heading.textContent = "Add";
    ui.fTitle.value = "";
    // A new row lands in whichever project you are looking at, because that
    // is almost always the one you mean.
    ui.fProject.value = project || "";
    ui.fKind.value = "note";
    ui.fTags.value = "";
    ui.fBody.value = "";
    ui.fProps.value = "";
    ui.fPinned.checked = false;
    ui.del.hidden = true;
    ui.metaLine.textContent = "";
    show();
    ui.fTitle.focus();
  }

  function show() {
    ui.formError.hidden = true;
    ui.sheet.hidden = false;
  }

  function hide() {
    ui.sheet.hidden = true;
    editing = null;
  }

  ui.newItem.addEventListener("click", blank);
  ui.close.addEventListener("click", hide);
  ui.scrim.addEventListener("click", hide);

  ui.editor.addEventListener("submit", async (event) => {
    event.preventDefault();
    ui.formError.hidden = true;

    let props = {};
    const raw = ui.fProps.value.trim();
    if (raw) {
      try {
        props = JSON.parse(raw);
        if (typeof props !== "object" || props === null || Array.isArray(props)) {
          throw new Error("Fields must be a JSON object, like {\"value\": 10}.");
        }
      } catch (error) {
        ui.formError.textContent = `Fields: ${error.message}`;
        ui.formError.hidden = false;
        return;
      }
    }

    const payload = {
      title: ui.fTitle.value,
      project: ui.fProject.value.trim().toLowerCase(),
      kind: (ui.fKind.value || "note").trim().toLowerCase(),
      body: ui.fBody.value,
      tags: ui.fTags.value.split(/[\s,]+/).filter(Boolean),
      props,
      pinned: ui.fPinned.checked,
    };

    ui.save.disabled = true;
    try {
      if (editing) {
        // Sending the revision we loaded is what makes a concurrent edit a
        // refusal rather than a silent overwrite.
        await api(`/items/${editing.uid}`, {
          method: "PATCH",
          body: { ...payload, expected_rev: editing.rev },
        });
        toast("Saved.", "good");
      } else {
        await api("/items", { method: "POST", body: payload });
        toast("Added.", "good");
      }
      hide();
      await Promise.all([loadFacets(), run(lastQuery)]);
    } catch (error) {
      ui.formError.textContent = error.message;
      ui.formError.hidden = false;
    } finally {
      ui.save.disabled = false;
    }
  });

  ui.del.addEventListener("click", async () => {
    if (!editing) return;
    const title = editing.title || "this item";
    if (!confirm(`Move "${title}" to the trash?\n\nIt stays searchable with is:trashed and can be restored.`)) {
      return;
    }
    try {
      await api(`/items/${editing.uid}`, { method: "DELETE" });
      toast("Moved to the trash.", "good");
      hide();
      await Promise.all([loadFacets(), run(lastQuery)]);
    } catch (error) {
      toast(error.message, "bad");
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !ui.sheet.hidden) return hide();
    if (ui.app.hidden) return;
    const typingNow = /^(INPUT|TEXTAREA)$/.test(document.activeElement?.tagName || "");
    if (event.key === "/" && !typingNow) { event.preventDefault(); ui.q.focus(); ui.q.select(); }
    if (event.key.toLowerCase() === "n" && !typingNow && ui.sheet.hidden) {
      event.preventDefault();
      blank();
    }
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter" && !ui.sheet.hidden) {
      event.preventDefault();
      ui.editor.requestSubmit();
    }
  });

  // ── Start ───────────────────────────────────────────────────────────────

  (async () => {
    // Say plainly when the server is not set up, rather than showing a token
    // box for a database that is not there.
    try {
      const health = await (await fetch("/api/health")).json();
      if (!health.ok) {
        ui.gate.hidden = false;
        ui.gateError.textContent = health.detail;
        ui.gateError.hidden = false;
        ui.token.disabled = true;
        return;
      }
      ui.gateHint.textContent =
        `${health.items.toLocaleString()} items stored · ${health.latency_ms} ms to reach the database`;
    } catch {
      /* Health is best-effort; the token box still works without it. */
    }

    const saved = localStorage.getItem(KEY);
    if (saved) {
      try { return await openWith(saved); }
      catch { localStorage.removeItem(KEY); }
    }
    ui.gate.hidden = false;
    ui.token.focus();
  })();
})();
