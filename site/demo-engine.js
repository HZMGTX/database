/* Vault's query language, reimplemented for the public demo.
 *
 * The demo runs with no server, so this file does in the browser what
 * vault/search/parse.py and vault/search/run.py do in SQLite: it parses the
 * same grammar, applies the same predicates, and ranks with the same BM25
 * weights over the same four columns.
 *
 * A reimplementation is a chance to drift, so it is checked rather than
 * trusted. site/build.py runs every query the demo advertises through the
 * real Python engine and records the answers; tests/test_site.py replays
 * them through this file and fails if the two differ by one item.
 *
 * The parts deliberately left out, because they need the database itself:
 *   - the trigram index (substring and typo matching)
 *   - synonym expansion
 *   - snippet() — highlights here are computed from the tokens instead
 *   - has:file / is:withheld — the demo corpus has no attachments
 *
 * Everything else is meant to behave exactly like the real thing.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.VaultQuery = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // ── Tokenizer ────────────────────────────────────────────────────────────
  // Mirrors  tokenize = "unicode61 remove_diacritics 2 tokenchars '_@'"
  // A token is a run of letters, digits, underscore or at-sign, folded to
  // lower case with combining marks stripped.
  var TOKEN_RE = /[\p{L}\p{N}_@]+/gu;

  function fold(text) {
    return String(text == null ? "" : text)
      .normalize("NFD")
      .replace(/[̀-ͯ]/g, "")
      .toLowerCase();
  }

  function tokenize(text) {
    var out = fold(text).match(TOKEN_RE);
    return out || [];
  }

  // ── Grammar ──────────────────────────────────────────────────────────────
  // The same shape as _TOKEN_RE in parse.py: an optional leading '-', then
  // either field<op>value, or a "quoted phrase", or a bare word.
  var CLAUSE_RE = new RegExp(
    "(-)?(?:([A-Za-z_][A-Za-z0-9_]*)(:|>=|<=|>|<|=)(\"[^\"]*\"|\\S+)" +
      "|\"([^\"]*)\"" +
      "|(\\S+))",
    "g"
  );

  var SORTS = ["rank", "recent", "created", "title", "due", "oldest"];
  var FLAGS = ["pinned", "untagged", "trashed", "any", "done", "open",
               "overdue", "orphan", "withheld"];
  var STRUCTURES = ["file", "link", "body", "tag", "due"];
  var CJK_RE = /[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯]/;

  function QueryError(message) {
    var e = new Error(message);
    e.name = "QueryError";
    return e;
  }

  function stripQuotes(value) {
    if (value.length >= 2 && value[0] === '"' && value[value.length - 1] === '"') {
      return value.slice(1, -1);
    }
    return value;
  }

  /* Parse query text into phrases, predicates and output options. */
  function compile(text, opts) {
    opts = opts || {};
    var now = opts.now == null ? Date.now() / 1000 : opts.now;
    var positives = [];     // phrases that must be present
    var negatives = [];     // phrases that must be absent
    var predicates = [];    // functions of (item, index) -> boolean
    var explain = [];
    var sort = "rank";
    var limit = opts.defaultLimit || 50;
    var includeTrashed = false;
    var trashedOnly = false;
    var useFallback = false;

    CLAUSE_RE.lastIndex = 0;
    var m;
    while ((m = CLAUSE_RE.exec(text || "")) !== null) {
      var negate = Boolean(m[1]);
      var field = m[2];
      var op = m[3];
      var rawValue = m[4];
      var phrase = m[5];
      var word = m[6];

      if (field && op) {
        var value = stripQuotes(rawValue || "");
        var handled = applyField(field.toLowerCase(), op, value, negate,
                                 predicates, explain, now);
        if (handled === "sort") {
          if (SORTS.indexOf(value.toLowerCase()) < 0) {
            throw QueryError("unknown sort '" + value + "'. Try: " +
                             SORTS.slice().sort().join(", "));
          }
          sort = value.toLowerCase();
        } else if (handled === "limit") {
          var n = parseInt(value, 10);
          if (isNaN(n)) throw QueryError("limit must be a number, got '" + value + "'");
          limit = Math.max(1, Math.min(1000, n));
        } else if (handled === "trashed") {
          trashedOnly = true;
          includeTrashed = true;
        } else if (handled === "any_state") {
          includeTrashed = true;
        }
        continue;
      }

      var term = phrase !== undefined ? phrase : word;
      if (!term) continue;

      var terms = tokenize(term);
      var entry = { raw: term, terms: terms, isPhrase: phrase !== undefined };
      if (phrase === undefined && (term.length < 3 || CJK_RE.test(term))) {
        useFallback = true;
        entry.fallback = fold(term);
      }
      (negate ? negatives : positives).push(entry);
      explain.push((negate ? "without " : "containing ") +
                   (entry.isPhrase ? "the phrase " : "") + "'" + term + "'");
    }

    if (!trashedOnly && !includeTrashed) {
      predicates.push(function (it) { return !it.deleted_at; });
    } else if (trashedOnly) {
      predicates.push(function (it) { return Boolean(it.deleted_at); });
    }

    return { positives: positives, negatives: negatives, predicates: predicates,
             sort: sort, limit: limit, explain: explain, useFallback: useFallback,
             fallback: useFallback ? shortWords(text) : [],
             hasText: positives.length > 0 };
  }

  /* The words FTS5 cannot index, taken from the raw query text exactly as
     _like_fallback does: split on whitespace and the comparison characters,
     drop exclusions, keep anything under three characters or unsegmented. */
  function shortWords(text) {
    var words = (String(text || "").match(/[^\s:<>=]+/g) || [])
      .filter(function (w) { return w && w[0] !== "-"; });
    return words.filter(function (w) { return w.length < 3 || CJK_RE.test(w); });
  }

  /* One field:value clause. Mirrors _apply_field in parse.py. */
  function applyField(field, op, value, negate, predicates, explain, now) {
    var not = negate ? "not " : "";
    function push(fn) {
      predicates.push(negate ? function (it) { return !fn(it); } : fn);
    }

    if (field === "sort") return "sort";
    if (field === "limit") return "limit";

    if (field === "kind") {
      var kinds = value.split(",").map(function (v) { return v.trim(); })
                       .filter(Boolean);
      push(function (it) { return kinds.indexOf(it.kind) >= 0; });
      explain.push(not + "of kind " + kinds.join(" or "));
      return null;
    }

    if (field === "tag") {
      if (value.slice(-2) === "/*" || value === "*") {
        var prefix = value.slice(-2) === "/*" ? value.slice(0, -2) : "";
        push(function (it) {
          return (it.tags || []).some(function (t) {
            return t === prefix || t.indexOf(prefix + "/") === 0;
          });
        });
        explain.push(not + "tagged " + prefix + " or below");
      } else {
        var slug = value.replace(/^\/+|\/+$/g, "").toLowerCase();
        push(function (it) { return (it.tags || []).indexOf(slug) >= 0; });
        explain.push(not + "tagged " + value);
      }
      return null;
    }

    if (field === "status") {
      var wanted = value.split(",").map(function (v) { return v.trim(); })
                        .filter(Boolean);
      push(function (it) {
        return it.facet ? wanted.indexOf(it.facet.status) >= 0 : false;
      });
      explain.push("status " + wanted.join(" or "));
      return null;
    }

    if (field === "is") {
      var flag = value.toLowerCase();
      if (flag === "pinned") {
        predicates.push(function (it) { return Boolean(it.pinned) !== negate; });
        explain.push(not + "pinned");
      } else if (flag === "untagged") {
        predicates.push(function (it) {
          return negate ? it._tags_cache !== "" : it._tags_cache === "";
        });
        explain.push(not + "untagged");
      } else if (flag === "trashed") {
        explain.push("in the trash");
        return "trashed";
      } else if (flag === "any") {
        explain.push("including trashed");
        return "any_state";
      } else if (flag === "done" || flag === "open") {
        var set = flag === "done" ? ["done", "cancelled"]
                                  : ["todo", "doing", "blocked"];
        push(function (it) {
          return it.facet ? set.indexOf(it.facet.status) >= 0 : false;
        });
        explain.push(flag);
      } else if (flag === "overdue") {
        predicates.push(function (it) {
          if (!it.facet || it.facet.due_epoch == null) return false;
          if (["done", "cancelled"].indexOf(it.facet.status) >= 0) return false;
          return it.facet.due_epoch < now;
        });
        explain.push("overdue");
      } else if (flag === "orphan") {
        predicates.push(function (it) { return edgeCount(it) === 0; });
        explain.push("with no links");
      } else if (flag === "withheld") {
        // Attachments are not part of the demo corpus, so nothing qualifies.
        predicates.push(function () { return false; });
        explain.push("content withheld as a possible secret");
      } else {
        throw QueryError("unknown flag is:" + value + ". Try: " +
                         FLAGS.slice().sort().join(", "));
      }
      return null;
    }

    if (field === "has") {
      var what = value.toLowerCase();
      if (what === "file") push(function (it) { return it.kind === "file"; });
      else if (what === "link") push(function (it) { return edgeCount(it) > 0; });
      else if (what === "body") {
        predicates.push(function (it) {
          return negate ? it.body === "" : it.body !== "";
        });
      } else if (what === "tag") {
        predicates.push(function (it) {
          return negate ? it._tags_cache === "" : it._tags_cache !== "";
        });
      } else if (what === "due") {
        push(function (it) {
          return Boolean(it.facet) && it.facet.due_epoch != null;
        });
      } else {
        throw QueryError("unknown has:" + value + ". Try: " +
                         STRUCTURES.slice().sort().join(", "));
      }
      explain.push((negate ? "without " : "with ") + what);
      return null;
    }

    if (["due", "starts", "created", "updated"].indexOf(field) >= 0) {
      applyDate(field, op, value, predicates, explain, now);
      return null;
    }

    if (field === "uid") {
      var want = value.toLowerCase();
      predicates.push(function (it) {
        return it.uid === want || it.uid.slice(-8) === want;
      });
      return null;
    }

    applyProperty(field, op, value, negate, predicates, explain);
    return null;
  }

  function edgeCount(it) {
    var links = it.links || { in: [], out: [] };
    return (links.in || []).length + (links.out || []).length;
  }

  /* Date comparisons. The demo accepts ISO dates and today/tomorrow/
   * yesterday, which is the subset the example queries use; the real engine
   * also understands "friday", "next week" and a dozen other phrasings. */
  function applyDate(field, op, value, predicates, explain, now) {
    var bare = value.replace(/^[<>=]+/, "");
    var operator = value.slice(0, value.length - bare.length) ||
                   (op !== ":" ? op : "");
    var moment = parseMoment(bare, now);
    if (moment == null) throw QueryError("could not read the date '" + bare + "'");

    if (field === "created" || field === "updated") {
      var cmp = { "<": "<", ">": ">", "<=": "<=", ">=": ">=" }[operator] || ">=";
      predicates.push(function (it) {
        var at = isoToEpoch(field === "created" ? it.created_at : it.updated_at);
        return compare(at, cmp, moment.epoch);
      });
      explain.push(field + " " + cmp + " " + bare);
      return;
    }

    var column = field === "due" ? "due_epoch" : "starts_epoch";
    if ((operator === "" || operator === ":") &&
        ["today", "tomorrow", "yesterday"].indexOf(bare.toLowerCase()) >= 0) {
      var low = Math.floor(moment.epoch / 86400) * 86400;
      var high = low + 86399;
      predicates.push(function (it) {
        var v = it.facet ? it.facet[column] : null;
        return v != null && v >= low && v <= high;
      });
      explain.push(field + " on " + bare);
      return;
    }

    var sqlOp = { "<": "<", ">": ">", "<=": "<=", ">=": ">=" }[operator] || "<=";
    predicates.push(function (it) {
      var v = it.facet ? it.facet[column] : null;
      return v != null && compare(v, sqlOp, moment.epoch);
    });
    explain.push(field + " " + sqlOp + " " + bare);
  }

  function compare(left, op, right) {
    if (left == null) return false;
    if (op === "<") return left < right;
    if (op === ">") return left > right;
    if (op === "<=") return left <= right;
    return left >= right;
  }

  function isoToEpoch(iso) {
    if (!iso) return null;
    var t = Date.parse(iso);
    return isNaN(t) ? null : Math.floor(t / 1000);
  }

  /* A date with no time of day means the *end* of that day, because "due
     2026-10-01" means due by the first, not at midnight starting it. This
     matches dates.end_of_day, down to the 23:59:59. */
  function parseMoment(text, now) {
    var word = text.toLowerCase();
    var day = Math.floor(now / 86400) * 86400;
    if (word === "today") return { epoch: day + 86399 };
    if (word === "tomorrow") return { epoch: day + 86400 + 86399 };
    if (word === "yesterday") return { epoch: day - 86400 + 86399 };
    if (/^\d{4}-\d{2}-\d{2}$/.test(text)) {
      return { epoch: Math.floor(Date.parse(text + "T23:59:59Z") / 1000) };
    }
    var stamp = Date.parse(/Z$|[+-]\d{2}:?\d{2}$/.test(text) ? text : text + "Z");
    if (!isNaN(stamp)) return { epoch: Math.floor(stamp / 1000) };
    return null;
  }

  /* Property comparisons, through the same typed projection the database
   * builds: a string lands in vtext, a number in vnum, and nothing else is
   * projected at all. */
  function applyProperty(field, op, value, negate, predicates, explain) {
    var numeric = value.trim() !== "" && !isNaN(Number(value)) ? Number(value) : null;

    function values(it) {
      var out = [];
      var props = it.props || {};
      if (!Object.prototype.hasOwnProperty.call(props, field)) return out;
      var v = props[field];
      var list = Array.isArray(v) ? v : [v];
      for (var i = 0; i < list.length; i++) {
        var x = list[i];
        if (typeof x === "number") out.push({ num: x, text: null });
        else if (typeof x === "string") out.push({ num: null, text: x });
      }
      return out;
    }

    function wrap(fn) {
      predicates.push(negate ? function (it) { return !fn(it); } : fn);
    }

    if ([">", "<", ">=", "<="].indexOf(op) >= 0) {
      if (numeric === null) {
        throw QueryError(field + op + value + " needs a number on the right");
      }
      wrap(function (it) {
        return values(it).some(function (v) {
          return v.num !== null && compare(v.num, op, numeric);
        });
      });
      explain.push(field + " " + op + " " + value);
      return;
    }

    if (numeric !== null) {
      wrap(function (it) {
        return values(it).some(function (v) {
          return v.num === numeric || v.text === value;
        });
      });
    } else {
      var lowered = value.toLowerCase();
      wrap(function (it) {
        return values(it).some(function (v) {
          return v.text !== null && v.text.toLowerCase() === lowered;
        });
      });
    }
    explain.push(field + " is " + value);
  }

  // ── Index ────────────────────────────────────────────────────────────────
  // Four columns, in the order the FTS5 table declares them, because the
  // BM25 weights are positional.
  var COLUMNS = ["title", "body", "_tags_cache", "_search_extra"];
  var WEIGHTS = [12.0, 4.0, 6.0, 1.0];
  var K1 = 1.2;
  var B = 0.75;

  function buildIndex(items) {
    var docs = items.map(function (it, id) {
      var columns = COLUMNS.map(function (name) { return tokenize(it[name] || ""); });
      var length = columns.reduce(function (n, c) { return n + c.length; }, 0);
      return { id: id, item: it, columns: columns, length: length };
    });
    var total = docs.reduce(function (n, d) { return n + d.length; }, 0);
    return {
      items: items,
      docs: docs,
      rows: docs.length,
      avgdl: docs.length ? total / docs.length : 0,
    };
  }

  /* How many times a phrase occurs in one column. A single word is the
   * one-token case of the same routine. */
  function phraseFreq(tokens, phrase) {
    if (!phrase.length) return 0;
    var hits = 0;
    for (var i = 0; i + phrase.length <= tokens.length; i++) {
      var ok = true;
      for (var j = 0; j < phrase.length; j++) {
        if (tokens[i + j] !== phrase[j]) { ok = false; break; }
      }
      if (ok) hits++;
    }
    return hits;
  }

  function docFreq(index, phrase) {
    var n = 0;
    for (var i = 0; i < index.docs.length; i++) {
      var doc = index.docs[i];
      for (var c = 0; c < doc.columns.length; c++) {
        if (phraseFreq(doc.columns[c], phrase)) { n++; break; }
      }
    }
    return n;
  }

  /* BM25 exactly as fts5_aux.c computes it: per-column frequencies scaled by
   * the column weights and summed *before* the saturation term, an IDF
   * floored at 1e-6, and the whole thing negated so that better matches sort
   * first ascending. */
  function score(index, doc, phrases, idfs) {
    var total = 0;
    var norm = K1 * (1 - B + B * (index.avgdl ? doc.length / index.avgdl : 0));
    for (var p = 0; p < phrases.length; p++) {
      var freq = 0;
      for (var c = 0; c < doc.columns.length; c++) {
        freq += WEIGHTS[c] * phraseFreq(doc.columns[c], phrases[p]);
      }
      total += idfs[p] * ((freq * (K1 + 1)) / (freq + norm));
    }
    return -total;
  }

  function run(index, text, opts) {
    opts = opts || {};
    var started = (typeof performance !== "undefined" && performance.now)
      ? performance.now() : 0;
    var q = compile(text, opts);
    var limit = opts.limit || q.limit || 50;
    var note = "";

    // Phrase order matters for scoring parity: positives first, then the
    // excluded ones, which is how parse.py assembles the MATCH expression.
    var phrases = q.positives.concat(q.negatives).map(function (e) { return e.terms; });
    var idfs = phrases.map(function (phrase) {
      var n = docFreq(index, phrase);
      var idf = Math.log((index.rows - n + 0.5) / (n + 0.5));
      return idf <= 0 ? 1e-6 : idf;
    });

    var results = [];
    for (var i = 0; i < index.docs.length; i++) {
      var doc = index.docs[i];
      var item = doc.item;

      var keep = true;
      for (var p = 0; p < q.predicates.length; p++) {
        if (!q.predicates[p](item, i)) { keep = false; break; }
      }
      if (!keep) continue;

      var matched = true;
      if (q.hasText) {
        for (var a = 0; a < q.positives.length && matched; a++) {
          matched = hasPhrase(doc, q.positives[a]);
        }
      }
      if (matched) {
        for (var b = 0; b < q.negatives.length && matched; b++) {
          if (hasPhrase(doc, q.negatives[b])) matched = false;
        }
      }
      // The SQL is `id IN (MATCH ...) OR (title LIKE ... AND body LIKE ...)`
      // -- one OR at the top, not a widening of each term. A row the word
      // index misses entirely still qualifies on the substring scan.
      if (!matched && q.fallback.length) matched = likeMatches(doc.item, q.fallback);
      if (!matched) continue;

      results.push({
        item: item,
        id: i,
        // The fallback path scores everything 0.0 in SQL too, so ordering
        // falls back to id DESC there as it does here.
        score: q.hasText && !q.fallback.length ? score(index, doc, phrases, idfs) : 0,
        doc: doc,
      });
    }

    if (q.fallback.length) {
      var short = q.fallback;
      var narrowed = q.predicates.length > 1;
      if (!narrowed && short.length) {
        note = short.join(", ") + " is too short for the word index, so this " +
               "scanned text directly. Add a filter such as kind: or tag: to narrow it.";
      }
    }

    sortResults(results, q.sort, q.hasText);
    var truncated = results.length > limit;
    var total = results.length;
    results = results.slice(0, limit);

    return {
      hits: results.map(function (r) {
        return { uid: r.item.uid, item: r.item, score: r.score,
                 highlights: q.positives.concat(q.negatives.length ? [] : []) };
      }),
      total: total,
      truncated: truncated,
      explain: q.explain,
      note: note,
      sort: q.sort,
      positives: q.positives,
      tookMs: (typeof performance !== "undefined" && performance.now)
        ? performance.now() - started : 0,
    };
  }

  function hasPhrase(doc, entry) {
    for (var c = 0; c < doc.columns.length; c++) {
      if (phraseFreq(doc.columns[c], entry.terms)) return true;
    }
    return false;
  }

  /* Every short word must appear as a substring of the title or the body,
     matched case-insensitively the way SQLite's LIKE does for ASCII. */
  function likeMatches(item, words) {
    for (var i = 0; i < words.length; i++) {
      var needle = fold(words[i]);
      if (fold(item.title).indexOf(needle) < 0 &&
          fold(item.body).indexOf(needle) < 0) return false;
    }
    return true;
  }

  function sortResults(results, sort, hasText) {
    // Each comparator mirrors one entry in SORT_SQL, tie-breaking on the
    // row id exactly as the SQL does. The fields are compared one at a time
    // rather than packed into a single number: a millisecond timestamp
    // multiplied up far enough to leave room for an id overflows the range
    // where JavaScript can still tell two integers apart, and the tie-break
    // silently disappears.
    function byTime(field, descending) {
      return function (a, b) {
        var x = Date.parse(a.item[field]);
        var y = Date.parse(b.item[field]);
        if (x !== y) return descending ? y - x : x - y;
        return descending ? b.id - a.id : a.id - b.id;
      };
    }

    var cmp;
    if (sort === "recent") cmp = byTime("updated_at", true);
    else if (sort === "oldest") cmp = byTime("updated_at", false);
    else if (sort === "created") cmp = byTime("created_at", true);
    else if (sort === "title") {
      cmp = function (a, b) {
        var x = a.item.title.toLowerCase(), y = b.item.title.toLowerCase();
        if (x < y) return -1;
        if (x > y) return 1;
        return a.id - b.id;
      };
    } else if (sort === "due") {
      cmp = function (a, b) {
        var x = a.item.facet ? a.item.facet.due_epoch : null;
        var y = b.item.facet ? b.item.facet.due_epoch : null;
        if ((x == null) !== (y == null)) return x == null ? 1 : -1;
        if (x != null && x !== y) return x - y;
        return a.id - b.id;
      };
    } else if (hasText) {
      cmp = function (a, b) {
        if (a.score !== b.score) return a.score - b.score;
        return b.id - a.id;
      };
    } else {
      cmp = byTime("updated_at", true);
    }
    results.sort(cmp);
  }

  return { tokenize: tokenize, fold: fold, compile: compile, buildIndex: buildIndex,
           run: run, COLUMNS: COLUMNS, WEIGHTS: WEIGHTS };
});
