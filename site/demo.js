/* The demo page: wiring only.
 *
 * Everything that decides what matches lives in demo-engine.js, which is the
 * part held to parity with the Python engine. This file turns results into
 * elements and keeps the URL in step with the query box.
 *
 * All text from the corpus reaches the page through textContent or a
 * document.createTextNode. The one place markup is assembled -- the search
 * highlight -- builds elements around text nodes rather than concatenating a
 * string, so a corpus containing a stray angle bracket cannot become markup.
 */
(function () {
  "use strict";

  var corpus = window.CORPUS;
  var engine = window.VaultQuery;
  var index = engine.buildIndex(corpus.items);
  var byUid = {};
  corpus.items.forEach(function (item) { byUid[item.uid] = item; });

  var EXAMPLES = [
    "budget",
    "typography",
    "kind:task status:todo",
    "kind:task sort:due",
    "tag:craft/*",
    "tag:clients/meridian",
    "kind:book rating>4 sort:title",
    "pages>400",
    "shelf:craft",
    'author:"Don Norman"',
    '"dark background"',
    "design -book",
    "is:pinned",
    "is:untagged",
    "is:orphan",
    "has:link",
    "has:due",
    "due<2026-10-01",
    "created<2026-03-01",
    "sort:recent limit:8",
    "UI",
  ];

  var el = {
    form: document.getElementById("omni"),
    input: document.getElementById("omnibox"),
    clear: document.getElementById("clear"),
    explain: document.getElementById("explain"),
    count: document.getElementById("count"),
    note: document.getElementById("engine-note"),
    results: document.getElementById("results"),
    empty: document.getElementById("empty"),
    examples: document.getElementById("examples-list"),
    kinds: document.getElementById("kinds-list"),
    tags: document.getElementById("tags-list"),
    sheet: document.getElementById("sheet"),
    sheetBody: document.getElementById("sheet-body"),
    sheetScrim: document.getElementById("sheet-scrim"),
    sheetClose: document.getElementById("sheet-close"),
  };

  // ── Helpers ────────────────────────────────────────────────────────────

  function node(tag, className, text) {
    var n = document.createElement(tag);
    if (className) n.className = className;
    if (text != null) n.textContent = text;
    return n;
  }

  function titleCase(text) {
    return text.charAt(0).toUpperCase() + text.slice(1);
  }

  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function formatDay(value) {
    var d = typeof value === "number" ? new Date(value * 1000) : new Date(value);
    if (isNaN(d.getTime())) return "";
    return d.getUTCDate() + " " + MONTHS[d.getUTCMonth()] + " " + d.getUTCFullYear();
  }

  /* Highlight the query's own terms in a passage, by walking the text and
     wrapping matched runs in <mark>. Built from nodes, never from a string,
     so corpus text is never parsed as markup. */
  function highlight(text, terms) {
    var fragment = document.createDocumentFragment();
    if (!terms.length) {
      fragment.appendChild(document.createTextNode(text));
      return fragment;
    }
    var folded = engine.fold(text);
    var marks = [];
    terms.forEach(function (term) {
      var needle = engine.fold(term);
      if (needle.length < 2) return;
      var from = 0;
      while (true) {
        var at = folded.indexOf(needle, from);
        if (at < 0) break;
        marks.push([at, at + needle.length]);
        from = at + needle.length;
      }
    });
    if (!marks.length) {
      fragment.appendChild(document.createTextNode(text));
      return fragment;
    }
    marks.sort(function (a, b) { return a[0] - b[0]; });
    var merged = [marks[0]];
    for (var i = 1; i < marks.length; i++) {
      var last = merged[merged.length - 1];
      if (marks[i][0] <= last[1]) last[1] = Math.max(last[1], marks[i][1]);
      else merged.push(marks[i]);
    }
    var cursor = 0;
    merged.forEach(function (range) {
      if (range[0] > cursor) {
        fragment.appendChild(document.createTextNode(text.slice(cursor, range[0])));
      }
      var mark = node("mark", null, text.slice(range[0], range[1]));
      fragment.appendChild(mark);
      cursor = range[1];
    });
    if (cursor < text.length) {
      fragment.appendChild(document.createTextNode(text.slice(cursor)));
    }
    return fragment;
  }

  /* A passage of the body centred on the first matched term, which is what
     FTS5's snippet() does in the real thing. */
  function snippetFor(item, terms) {
    var text = (item.body || "").replace(/\s+/g, " ").trim();
    if (!text) return "";
    var folded = engine.fold(text);
    var at = -1;
    for (var i = 0; i < terms.length && at < 0; i++) {
      var needle = engine.fold(terms[i]);
      if (needle.length >= 2) at = folded.indexOf(needle);
    }
    if (at < 0) return text.slice(0, 190) + (text.length > 190 ? "…" : "");
    var from = Math.max(0, at - 70);
    var to = Math.min(text.length, at + 130);
    return (from > 0 ? "…" : "") + text.slice(from, to) + (to < text.length ? "…" : "");
  }

  function termsOf(result) {
    var out = [];
    (result.positives || []).forEach(function (entry) {
      out.push(entry.raw);
      entry.terms.forEach(function (t) { out.push(t); });
    });
    return out;
  }

  // ── Rendering ──────────────────────────────────────────────────────────

  function renderHit(item, terms) {
    var li = document.createElement("li");
    var button = node("button", "hit");
    button.type = "button";
    button.setAttribute("data-uid", item.uid);

    var head = node("div", "hit-head");
    var kind = node("span", "kind", item.kind);
    kind.setAttribute("data-kind", item.kind);
    head.appendChild(kind);
    var title = node("span", "hit-title");
    title.appendChild(highlight(item.title, terms));
    head.appendChild(title);
    if (item.pinned) {
      var pin = node("span", "pin", "★");
      pin.title = "Pinned";
      head.appendChild(pin);
    }
    button.appendChild(head);

    var snippet = snippetFor(item, terms);
    if (snippet) {
      var snip = node("div", "hit-snip");
      snip.appendChild(highlight(snippet, terms));
      button.appendChild(snip);
    }

    var meta = node("div", "meta");
    if (item.facet && item.facet.status) {
      var state = node("span", "state", item.facet.status);
      state.setAttribute("data-status", item.facet.status);
      meta.appendChild(state);
    }
    if (item.facet && item.facet.due_epoch != null) {
      meta.appendChild(node("span", "when", "due " + formatDay(item.facet.due_epoch)));
    }
    if (item.facet && item.facet.starts_local) {
      meta.appendChild(node("span", "when", formatDay(item.facet.starts_local.slice(0, 10))));
    }
    (item.tags || []).forEach(function (tag) {
      meta.appendChild(node("span", "tag", tag));
    });
    if (meta.childNodes.length) button.appendChild(meta);

    li.appendChild(button);
    return li;
  }

  function render(query) {
    var result;
    try {
      result = engine.run(index, query, {});
    } catch (error) {
      el.results.textContent = "";
      el.empty.hidden = true;
      el.count.textContent = "";
      el.note.hidden = false;
      el.note.textContent = error.message;
      el.explain.textContent = "";
      return;
    }

    var terms = termsOf(result);
    el.results.textContent = "";
    var fragment = document.createDocumentFragment();
    result.hits.forEach(function (hit) {
      fragment.appendChild(renderHit(hit.item, terms));
    });
    el.results.appendChild(fragment);

    el.empty.hidden = result.hits.length > 0;
    el.note.hidden = !result.note;
    if (result.note) el.note.textContent = result.note;

    el.count.textContent = "";
    var shown = result.hits.length;
    el.count.appendChild(node("b", null, String(result.total)));
    el.count.appendChild(document.createTextNode(
      (result.total === 1 ? " item" : " items") +
      (query.trim() ? " matched" : " in the corpus") +
      (shown < result.total ? ", showing the first " + shown : "") +
      " · " + result.tookMs.toFixed(1) + " ms"));

    el.explain.textContent = "";
    if (result.explain.length) {
      el.explain.appendChild(document.createTextNode("Read as: "));
      result.explain.forEach(function (clause, i) {
        if (i) el.explain.appendChild(document.createTextNode(", "));
        el.explain.appendChild(node("b", null, clause));
      });
    }

    markPressed(el.examples, query);
    markPressed(el.kinds, query);
    markPressed(el.tags, query);
  }

  function markPressed(list, query) {
    if (!list) return;
    Array.prototype.forEach.call(list.querySelectorAll("button"), function (button) {
      button.setAttribute("aria-pressed",
        button.getAttribute("data-query") === query ? "true" : "false");
    });
  }

  // ── Detail sheet ───────────────────────────────────────────────────────

  var lastFocused = null;

  function openItem(uid) {
    var item = byUid[uid];
    if (!item) return;
    lastFocused = document.activeElement;
    el.sheetBody.textContent = "";

    var kind = node("span", "kind", item.kind);
    kind.setAttribute("data-kind", item.kind);
    el.sheetBody.appendChild(kind);

    var heading = node("h2", null, item.title);
    heading.id = "sheet-title";
    el.sheetBody.appendChild(heading);

    var meta = node("div", "meta");
    if (item.facet && item.facet.status) {
      var state = node("span", "state", item.facet.status);
      state.setAttribute("data-status", item.facet.status);
      meta.appendChild(state);
    }
    (item.tags || []).forEach(function (tag) {
      var button = node("button", "tag", tag);
      button.type = "button";
      button.addEventListener("click", function () {
        closeSheet();
        setQuery("tag:" + tag);
      });
      meta.appendChild(button);
    });
    if (meta.childNodes.length) el.sheetBody.appendChild(meta);

    if (item.body) el.sheetBody.appendChild(node("div", "body", item.body));

    var facts = [];
    if (item.facet) {
      if (item.facet.due_local) facts.push(["due", item.facet.due_local.replace("T", " ")]);
      if (item.facet.starts_local) facts.push(["starts", item.facet.starts_local.replace("T", " ")]);
      if (item.facet.ends_local) facts.push(["ends", item.facet.ends_local.replace("T", " ")]);
      if (item.facet.location) facts.push(["location", item.facet.location]);
      if (item.facet.url) facts.push(["url", item.facet.url]);
      if (item.facet.org) facts.push(["org", item.facet.org]);
      if (item.facet.role) facts.push(["role", item.facet.role]);
      if (item.facet.priority) facts.push(["priority", String(item.facet.priority)]);
      if (item.facet.estimate_minutes) {
        facts.push(["estimate", item.facet.estimate_minutes + " min"]);
      }
    }
    Object.keys(item.props || {}).forEach(function (key) {
      facts.push([key, String(item.props[key])]);
    });
    facts.push(["created", formatDay(item.created_at)]);

    if (facts.length) {
      var dl = node("dl", "props");
      facts.forEach(function (pair) {
        dl.appendChild(node("dt", null, pair[0]));
        var dd = node("dd");
        if (/^https?:\/\//.test(pair[1])) {
          var a = node("a", null, pair[1]);
          a.href = pair[1];
          a.rel = "noopener noreferrer nofollow";
          dd.appendChild(a);
        } else {
          dd.textContent = pair[1];
        }
        dl.appendChild(dd);
      });
      el.sheetBody.appendChild(dl);
    }

    var links = (item.links || {});
    var edges = (links.out || []).map(function (e) { return [e.rel, e]; })
      .concat((links.in || []).map(function (e) { return [e.rel + " ←", e]; }));
    if (edges.length) {
      var section = node("div", "links");
      section.appendChild(node("h3", null, "Linked"));
      var ul = document.createElement("ul");
      edges.forEach(function (pair) {
        var li = document.createElement("li");
        li.appendChild(node("span", "rel", pair[0] + " "));
        var button = node("button", null, pair[1].title);
        button.type = "button";
        button.addEventListener("click", function () { openItem(pair[1].uid); });
        li.appendChild(button);
        ul.appendChild(li);
      });
      section.appendChild(ul);
      el.sheetBody.appendChild(section);
    }

    el.sheetBody.appendChild(node("p", "uid", "uid " + item.uid +
      "   ·   short " + item.uid.slice(-8)));

    el.sheet.hidden = false;
    el.sheetClose.focus();
  }

  function closeSheet() {
    el.sheet.hidden = true;
    if (lastFocused && lastFocused.focus) lastFocused.focus();
  }

  // ── Wiring ─────────────────────────────────────────────────────────────

  function setQuery(query, options) {
    el.input.value = query;
    el.clear.hidden = !query;
    render(query);
    if (!options || !options.silent) {
      var hash = query ? "#q=" + encodeURIComponent(query) : "#";
      if (window.location.hash !== hash) {
        window.history.replaceState(null, "", hash);
      }
    }
  }

  function chip(list, label, query, className) {
    var li = document.createElement("li");
    var button = node("button", className, label);
    button.type = "button";
    button.setAttribute("data-query", query);
    button.setAttribute("aria-pressed", "false");
    button.addEventListener("click", function () {
      setQuery(el.input.value === query ? "" : query);
      el.input.focus();
    });
    li.appendChild(button);
    list.appendChild(li);
    return button;
  }

  EXAMPLES.forEach(function (query) { chip(el.examples, query, query); });

  corpus.kinds.forEach(function (kind) {
    var n = corpus.items.filter(function (i) { return i.kind === kind.name; }).length;
    var button = chip(el.kinds, "", "kind:" + kind.name);
    button.appendChild(document.createTextNode(kind.plural));
    button.appendChild(node("span", "n", String(n)));
  });

  var roots = {};
  corpus.tags.forEach(function (slug) {
    var root = slug.split("/")[0];
    roots[root] = (roots[root] || 0) + corpus.items.filter(function (i) {
      return (i.tags || []).indexOf(slug) >= 0;
    }).length;
  });
  Object.keys(roots).sort().forEach(function (root) {
    var button = chip(el.tags, "", "tag:" + root + "/*");
    button.appendChild(document.createTextNode(root));
    button.appendChild(node("span", "n", String(roots[root])));
  });

  var pending = null;
  el.input.addEventListener("input", function () {
    el.clear.hidden = !el.input.value;
    if (pending) clearTimeout(pending);
    // Fast enough to run on every keystroke at this size, debounced anyway so
    // a held key does not queue a render per repeat.
    pending = setTimeout(function () { setQuery(el.input.value); }, 60);
  });

  el.form.addEventListener("submit", function (event) {
    event.preventDefault();
    if (pending) clearTimeout(pending);
    setQuery(el.input.value);
  });

  el.clear.addEventListener("click", function () {
    setQuery("");
    el.input.focus();
  });

  el.results.addEventListener("click", function (event) {
    var button = event.target.closest ? event.target.closest(".hit") : null;
    if (button) openItem(button.getAttribute("data-uid"));
  });

  el.sheetScrim.addEventListener("click", closeSheet);
  el.sheetClose.addEventListener("click", closeSheet);

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !el.sheet.hidden) {
      closeSheet();
      return;
    }
    if (event.key === "/" && document.activeElement !== el.input) {
      event.preventDefault();
      el.input.focus();
      el.input.select();
    }
  });

  window.addEventListener("hashchange", function () { fromHash(); });

  function fromHash() {
    var hash = window.location.hash || "";
    var match = /^#q=(.*)$/.exec(hash);
    var query = "";
    if (match) {
      try { query = decodeURIComponent(match[1]); } catch (e) { query = ""; }
    }
    if (query !== el.input.value) setQuery(query, { silent: true });
  }

  fromHash();
  if (!el.input.value) render("");
})();
