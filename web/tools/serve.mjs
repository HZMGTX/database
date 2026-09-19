/**
 * Runs the API locally, the way Vercel runs it.
 *
 * Vercel maps api/foo.js to /api/foo and api/items/[ref].js to /api/items/:ref.
 * This does the same routing against a plain Node server, so the handlers can
 * be exercised end-to-end against a real Postgres before anything is deployed.
 * It is a development tool; nothing in the deployed app imports it.
 */
import { createServer } from "node:http";
import { watch } from "node:fs";
import { readdir, readFile, stat } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const PORT = Number(process.env.PORT) || 3210;

/** Walks api/ and builds the same routes Vercel would. */
async function routes(dir = path.join(ROOT, "api"), prefix = "/api") {
  const found = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    if (entry.name.startsWith("_")) continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      found.push(...await routes(full, `${prefix}/${entry.name}`));
      continue;
    }
    if (!entry.name.endsWith(".js")) continue;

    const base = entry.name.replace(/\.js$/, "");
    const dynamic = /^\[(.+)\]$/.exec(base);
    const route = dynamic ? `${prefix}/:${dynamic[1]}`
                : base === "index" ? prefix
                : `${prefix}/${base}`;
    found.push({ route, file: full, param: dynamic ? dynamic[1] : null });
  }
  // Static segments win over a dynamic one at the same depth, which is what
  // Vercel does; without this /api/items/index could shadow /api/items/:ref.
  return found.sort((a, b) => (a.param ? 1 : 0) - (b.param ? 1 : 0));
}

const table = await routes();
console.log("routes:");
for (const r of table) console.log("  " + r.route);

function match(pathname) {
  for (const entry of table) {
    if (!entry.param) {
      if (entry.route === pathname) return { entry, params: {} };
      continue;
    }
    const prefix = entry.route.replace(/\/:[^/]+$/, "");
    if (pathname.startsWith(prefix + "/")) {
      const rest = pathname.slice(prefix.length + 1);
      if (rest && !rest.includes("/")) {
        return { entry, params: { [entry.param]: decodeURIComponent(rest) } };
      }
    }
  }
  return null;
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);

  if (!url.pathname.startsWith("/api/")) {
    // Static files, with cleanUrls.
    let file = path.join(ROOT, "public", url.pathname === "/" ? "index.html" : url.pathname);
    try {
      if (!(await stat(file)).isFile()) throw new Error("not a file");
    } catch {
      try { await stat(file + ".html"); file += ".html"; }
      catch { res.writeHead(404); return res.end("not found"); }
    }
    const types = { ".html": "text/html", ".css": "text/css", ".js": "text/javascript",
                    ".svg": "image/svg+xml", ".json": "application/json" };
    res.writeHead(200, { "Content-Type": types[path.extname(file)] || "text/plain" });
    return res.end(await readFile(file));
  }

  // vercel.json rewrites /api/v1/* to /api/*, so a client written against
  // the versioned path reaches the same handler. Mirrored here, or this
  // server would answer 404 for a path production serves.
  const pathname = url.pathname.startsWith("/api/v1/")
    ? "/api/" + url.pathname.slice("/api/v1/".length)
    : url.pathname;

  const hit = match(pathname);
  if (!hit) { res.writeHead(404); return res.end('{"error":"no such route"}'); }

  // Vercel gives handlers req.query and res.status()/res.json().
  req.query = { ...Object.fromEntries(url.searchParams), ...hit.params };
  res.status = (code) => { res.statusCode = code; return res; };

  const module = await import(pathToFileURL(hit.entry.file).href + `?t=${Date.now()}`);
  try {
    await module.default(req, res);
  } catch (error) {
    console.error(error);
    if (!res.headersSent) res.writeHead(500, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: true, detail: error.message }));
  }
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`\nlistening on http://127.0.0.1:${PORT}`);
});

// Exit when anything under api/ changes.
//
// The cache-buster on the handler import only reloads the handler itself;
// its own imports -- _lib/items.js, _lib/query.js -- stay cached for the
// life of the process. Editing one of those and re-testing silently
// exercises the OLD code, which looks exactly like the edit not working.
// Rather than pretend to hot-reload, this stops and says so.
let stopping = false;
watch(path.join(ROOT, "api"), { recursive: true }, (_event, name) => {
  if (stopping || !name || !name.endsWith(".js")) return;
  stopping = true;
  console.log(`\n${name} changed — exiting so the next start picks it up.`);
  console.log("(imports below the handler stay cached; a restart is the only honest reload)");
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 500).unref();
});
