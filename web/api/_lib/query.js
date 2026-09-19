/**
 * The query language: what someone types, turned into parameterised SQL.
 *
 * Rule one: no value a caller types is ever concatenated into SQL. Every one
 * becomes a bound parameter. The only strings built here are fixed fragments
 * chosen by this file, never by input.
 *
 * What it accepts:
 *
 *   sword shield          both words, ranked
 *   "exact phrase"        that phrase
 *   -broken               without that word
 *   kind:fish,weapons     either kind
 *   tag:work/clients      a tag
 *   tag:work/*            that tag and everything under it
 *   value>5000            any property, compared as a number
 *   rarity:Legendary      any property, compared as text
 *   is:pinned             flags
 *   has:body              structure
 *   sort:recent limit:50  output
 */

const CLAUSE = new RegExp(
  "(-)?(?:([A-Za-z_][A-Za-z0-9_]*)(:|>=|<=|>|<|=)(\"[^\"]*\"|\\S+)" +
    "|\"([^\"]*)\"" +
    "|(\\S+))",
  "g",
);

const SORTS = {
  rank: null, // handled separately: needs the text query to rank against
  project: "i.project ASC, i.kind ASC, i.title ASC, i.id ASC",
  recent: "i.updated_at DESC, i.id DESC",
  oldest: "i.updated_at ASC, i.id ASC",
  created: "i.created_at DESC, i.id DESC",
  title: "i.title ASC, i.id ASC",
  kind: "i.kind ASC, i.title ASC, i.id ASC",
};

const FLAGS = ["pinned", "untagged", "trashed", "any", "orphan"];
const STRUCTURES = ["body", "tag", "props", "link"];

export class QueryError extends Error {
  constructor(message) {
    super(message);
    this.name = "QueryError";
    this.status = 400;
  }
}

function unquote(value) {
  return value.length >= 2 && value.startsWith('"') && value.endsWith('"')
    ? value.slice(1, -1)
    : value;
}

/**
 * Compiles query text.
 *
 * @returns {{where: string, params: any[], order: string, limit: number,
 *            offset: number, understood: string[], textQuery: string|null}}
 */
export function compile(text, { limit = 50, offset = 0 } = {}) {
  const params = [];
  const where = [];
  const understood = [];
  const positives = [];
  const negatives = [];
  let sort = "rank";
  let wantTrashed = false;
  let includeTrashed = false;

  /** Binds a value and returns its placeholder. */
  const bind = (value) => `$${params.push(value)}`;

  CLAUSE.lastIndex = 0;
  let match;
  while ((match = CLAUSE.exec(text || "")) !== null) {
    const negate = Boolean(match[1]);
    const field = match[2];
    const op = match[3];
    const rawValue = match[4];
    const phrase = match[5];
    const word = match[6];

    if (field && op) {
      const value = unquote(rawValue || "");
      const name = field.toLowerCase();

      if (name === "sort") {
        if (!(value.toLowerCase() in SORTS)) {
          throw new QueryError(
            `Unknown sort '${value}'. Try: ${Object.keys(SORTS).join(", ")}`);
        }
        sort = value.toLowerCase();
        continue;
      }

      if (name === "limit") {
        const n = Number.parseInt(value, 10);
        if (!Number.isFinite(n)) throw new QueryError(`limit needs a number, got '${value}'`);
        limit = Math.max(1, Math.min(500, n));
        continue;
      }

      if (name === "offset") {
        const n = Number.parseInt(value, 10);
        if (!Number.isFinite(n)) throw new QueryError(`offset needs a number, got '${value}'`);
        offset = Math.max(0, n);
        continue;
      }

      if (name === "project" || name === "p") {
        // "" is the unfiled bucket, and `project:none` is how you ask for it
        // -- an empty value cannot be typed, and nothing is a real answer.
        const wanted = value.split(",").map((v) => v.trim()).filter(Boolean)
          .map((v) => (["none", "unfiled", "-"].includes(v.toLowerCase())
            ? "" : v.toLowerCase()));
        if (!wanted.length) throw new QueryError("project: needs a value");
        where.push(`i.project ${negate ? "<> ALL" : "= ANY"}(${bind(wanted)})`);
        understood.push(
          `${negate ? "not " : ""}in ` +
          wanted.map((w) => (w === "" ? "no project" : w)).join(" or "));
        continue;
      }

      if (name === "kind") {
        const kinds = value.split(",").map((k) => k.trim()).filter(Boolean);
        if (!kinds.length) throw new QueryError("kind: needs a value");
        where.push(`i.kind ${negate ? "<> ALL" : "= ANY"}(${bind(kinds)})`);
        understood.push(`${negate ? "not " : ""}of kind ${kinds.join(" or ")}`);
        continue;
      }

      if (name === "tag") {
        if (value === "*" || value.endsWith("/*")) {
          const prefix = value === "*" ? "" : value.slice(0, -2);
          // The tag itself, or anything below it in the path.
          const clause =
            `(i.tags && ARRAY[${bind(prefix)}]::text[] ` +
            `OR EXISTS (SELECT 1 FROM unnest(i.tags) t WHERE t LIKE ${bind(prefix + "/%")}))`;
          where.push(negate ? `NOT ${clause}` : clause);
          understood.push(`${negate ? "not " : ""}tagged ${prefix} or below`);
        } else {
          const slug = value.replace(/^\/+|\/+$/g, "").toLowerCase();
          where.push(`${negate ? "NOT " : ""}(i.tags && ARRAY[${bind(slug)}]::text[])`);
          understood.push(`${negate ? "not " : ""}tagged ${slug}`);
        }
        continue;
      }

      if (name === "is") {
        const flag = value.toLowerCase();
        if (flag === "pinned") {
          where.push(`i.pinned = ${bind(!negate)}`);
          understood.push(`${negate ? "not " : ""}pinned`);
        } else if (flag === "untagged") {
          where.push(negate ? "cardinality(i.tags) > 0" : "cardinality(i.tags) = 0");
          understood.push(`${negate ? "not " : ""}untagged`);
        } else if (flag === "trashed") {
          wantTrashed = true;
          includeTrashed = true;
          understood.push("in the trash");
        } else if (flag === "any") {
          includeTrashed = true;
          understood.push("including trashed");
        } else if (flag === "orphan") {
          const clause =
            "NOT EXISTS (SELECT 1 FROM edge e WHERE e.src_id = i.id OR e.dst_id = i.id)";
          where.push(negate ? `NOT (${clause})` : clause);
          understood.push(`${negate ? "linked" : "with no links"}`);
        } else {
          throw new QueryError(`Unknown flag is:${value}. Try: ${FLAGS.join(", ")}`);
        }
        continue;
      }

      if (name === "has") {
        const what = value.toLowerCase();
        if (what === "body") {
          where.push(negate ? "i.body = ''" : "i.body <> ''");
        } else if (what === "tag") {
          where.push(negate ? "cardinality(i.tags) = 0" : "cardinality(i.tags) > 0");
        } else if (what === "props") {
          where.push(negate ? "i.props = '{}'::jsonb" : "i.props <> '{}'::jsonb");
        } else if (what === "link") {
          const clause =
            "EXISTS (SELECT 1 FROM edge e WHERE e.src_id = i.id OR e.dst_id = i.id)";
          where.push(negate ? `NOT (${clause})` : clause);
        } else {
          throw new QueryError(`Unknown has:${value}. Try: ${STRUCTURES.join(", ")}`);
        }
        understood.push(`${negate ? "without" : "with"} ${what}`);
        continue;
      }

      if (name === "uid") {
        const wanted = value.toLowerCase();
        where.push(`(i.uid = ${bind(wanted)} OR right(i.uid, 8) = ${bind(wanted)})`);
        understood.push(`uid ${wanted}`);
        continue;
      }

      // Anything else is a property inside the JSON bag.
      const numeric = value.trim() !== "" && Number.isFinite(Number(value))
        ? Number(value) : null;

      if ([">", "<", ">=", "<="].includes(op)) {
        if (numeric === null) {
          throw new QueryError(`${field}${op}${value} needs a number on the right`);
        }
        // The cast is only attempted on rows whose value looks numeric, so a
        // kind that stores this key as text cannot abort the whole query.
        const clause =
          `(jsonb_typeof(i.props -> ${bind(name)}) = 'number' ` +
          `AND (i.props ->> ${bind(name)})::numeric ${op} ${bind(numeric)})`;
        where.push(negate ? `NOT ${clause}` : clause);
        understood.push(`${name} ${op} ${value}`);
        continue;
      }

      const clause = numeric === null
        ? `lower(i.props ->> ${bind(name)}) = lower(${bind(value)})`
        : `((i.props ->> ${bind(name)}) = ${bind(String(value))} ` +
          `OR (jsonb_typeof(i.props -> ${bind(name)}) = 'number' ` +
          `AND (i.props ->> ${bind(name)})::numeric = ${bind(numeric)}))`;
      where.push(negate ? `NOT ${clause}` : clause);
      understood.push(`${name} is ${value}`);
      continue;
    }

    const term = phrase !== undefined ? phrase : word;
    if (!term) continue;
    (negate ? negatives : positives).push({ term, isPhrase: phrase !== undefined });
    understood.push(
      `${negate ? "without" : "containing"} ${phrase !== undefined ? "the phrase " : ""}'${term}'`);
  }

  // Postgres full-text. Phrases use <-> so the words must be adjacent;
  // everything else is ANDed, and exclusions are subtracted.
  let textQuery = null;
  let textParam = null;   // 1-based placeholder number, for SELECT to reuse
  const pieces = [];
  for (const { term, isPhrase } of positives) {
    pieces.push(isPhrase ? phraseToTsquery(term) : wordToTsquery(term));
  }
  for (const { term, isPhrase } of negatives) {
    const piece = isPhrase ? phraseToTsquery(term) : wordToTsquery(term);
    if (piece) pieces.push(`!(${piece})`);
  }
  const usable = pieces.filter(Boolean);
  if (usable.length) {
    textQuery = usable.join(" & ");
    const placeholder = bind(textQuery);
    textParam = Number(placeholder.slice(1));
    where.push(`i.search @@ to_tsquery('english', ${placeholder})`);
  }

  if (wantTrashed) where.push("i.deleted_at IS NOT NULL");
  else if (!includeTrashed) where.push("i.deleted_at IS NULL");

  // Everything bound so far belongs to WHERE. Ranking binds the text query a
  // second time, and the count statement -- which has no ORDER BY -- must not
  // be handed that extra parameter, or Postgres rejects the whole statement
  // with "bind message supplies 2 parameters, but prepared statement
  // requires 1". Recording the boundary here is what lets the caller pass
  // exactly the parameters each statement actually references.
  const whereParamCount = params.length;

  let order = SORTS[sort];
  if (sort === "rank") {
    // Reuses the WHERE binding rather than adding another, which keeps the
    // count statement -- which has no ORDER BY -- able to take exactly the
    // parameters it references.
    order = textQuery
      ? `ts_rank_cd(i.search, to_tsquery('english', $${textParam})) DESC, i.id DESC`
      : "i.updated_at DESC, i.id DESC";
  }

  return {
    where: where.length ? where.join(" AND ") : "TRUE",
    params,
    whereParamCount,
    order,
    limit,
    offset,
    understood,
    textQuery,
    textParam,
  };
}

/**
 * One word as a tsquery term.
 *
 * Every character that means something to the tsquery grammar is stripped
 * rather than escaped, because a term is only ever a literal here -- the
 * operators come from this file, never from what someone typed.
 */
function wordToTsquery(word) {
  const clean = String(word).replace(/[^\p{L}\p{N}_@.-]+/gu, " ").trim();
  if (!clean) return "";
  const parts = clean.split(/\s+/).filter(Boolean);
  if (!parts.length) return "";
  return parts.length === 1 ? `${parts[0]}:*` : `(${parts.join(" & ")})`;
}

/** A quoted phrase: the words, in that order, adjacent. */
function phraseToTsquery(phrase) {
  const clean = String(phrase).replace(/[^\p{L}\p{N}_@.-]+/gu, " ").trim();
  if (!clean) return "";
  const parts = clean.split(/\s+/).filter(Boolean);
  if (!parts.length) return "";
  return parts.length === 1 ? parts[0] : `(${parts.join(" <-> ")})`;
}

/** Wraps matched runs for display. Used for the snippet the API returns. */
export const SNIPPET_START = "\u0002";
export const SNIPPET_END = "\u0003";
