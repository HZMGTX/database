/**
 * Finding the data files that ship beside the code.
 *
 * `process.cwd()` is the project root when this runs locally and /var/task
 * when it runs as a function, and the layout under each is not the same --
 * which is how `db/schema.sql` came to be missing in production while every
 * local check passed. Both roots are tried, module-relative first, because
 * that one is true wherever the file actually sits.
 *
 * A missing file here means the bundle did not include it: these are read at
 * runtime, so nothing in the import graph points at them and Vercel has to be
 * told to ship them (vercel.json -> functions -> includeFiles). The error says
 * so rather than leaving an ENOENT to be interpreted.
 */
import { readFile, stat } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));

function candidates(relative) {
  return [
    path.join(HERE, "..", "..", relative),   // api/_lib -> project root
    path.join(process.cwd(), relative),
    path.join("/var/task", relative),
  ];
}

/** The first candidate that exists. */
export async function locate(relative) {
  const tried = candidates(relative);
  for (const candidate of tried) {
    try {
      if ((await stat(candidate)).isFile()) return candidate;
    } catch {
      /* try the next one */
    }
  }
  const error = new Error(
    `${relative} was not shipped with this function. It is read at runtime, ` +
    `so it has to be listed in vercel.json under functions -> includeFiles. ` +
    `Looked in: ${tried.join(", ")}`);
  error.status = 500;
  throw error;
}

export async function readAsset(relative, encoding = "utf8") {
  return readFile(await locate(relative), encoding);
}
