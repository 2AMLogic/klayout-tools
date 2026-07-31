/**
 * Build-time layout data loader for the klayout-tools.org gallery.
 *
 * Discovers every block directory under the repo's `blocks/` tree, reads
 * each block's `blocks/<slug>/output/layout.json` (the schema-v1 contract
 * emitted by `klt layout-metrics`, issue #61 — see
 * `src/klayout_tools/layout_metrics.py` and `docs/cli/layout-metrics.md`),
 * and returns a typed, slug-sorted `Layout[]`.
 *
 * Design notes (see `blocks/README.md` and issue #59):
 *   - Runs at build time only (Node `fs`). Not bundled into client output.
 *   - Resilient to missing files: a block directory with no `layout.json`
 *     (or an unparsable one) yields a stub record with
 *     `status: "no_artifacts"` rather than crashing the build.
 *   - Forward-compat guard: a `layout.json` whose `schema_version` is not
 *     the version we understand is logged and SKIPPED (a stub is emitted
 *     instead), so the build still completes with the remaining blocks.
 *   - Block discovery: immediate subdirectories of `blocks/`, skipping
 *     hidden / `_`-prefixed entries.
 */

import { readdirSync, existsSync, readFileSync, statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { SCHEMA_VERSION } from "./types.ts";
import type { Layout, LayoutStatus } from "./types.ts";

const VALID_STATUSES: ReadonlySet<string> = new Set<LayoutStatus>([
  "ok",
  "partial",
  "no_artifacts",
]);

/** Directory of this module when executed unbundled (Node ESM, vitest). */
const MODULE_DIR = dirname(fileURLToPath(import.meta.url));

/**
 * Absolute path to the repository's `blocks/` directory.
 *
 * Resolution order:
 *   1. `KLT_BLOCKS_DIR` env override (used by tests and CI).
 *   2. `<cwd>/../blocks` — `astro build`/`dev` run with cwd = `site/`, so
 *      the repo root is one level up. This path is stable even after Vite
 *      bundles this module (bundling rewrites `import.meta.url`, so we
 *      cannot rely on the module's own location during a build).
 *   3. `<module>/../../../blocks` — fallback for direct unbundled execution
 *      (e.g. `node`/vitest importing `src/data/loadLayouts.ts`).
 */
export function blocksDir(): string {
  const override = process.env.KLT_BLOCKS_DIR;
  if (override) return resolve(override);

  const fromCwd = resolve(process.cwd(), "..", "blocks");
  if (isDir(fromCwd)) return fromCwd;

  return resolve(MODULE_DIR, "..", "..", "..", "blocks");
}

/** True if `path` exists and is a directory. */
function isDir(path: string): boolean {
  try {
    return statSync(path).isDirectory();
  } catch {
    return false;
  }
}

/**
 * Enumerate block directories under `root`.
 *
 * Returns absolute paths to each block directory, in discovery order
 * (alphabetical by directory name).
 */
export function discoverBlockDirs(root: string): string[] {
  if (!isDir(root)) return [];

  const dirs: string[] = [];
  for (const entry of readdirSync(root).sort()) {
    if (entry.startsWith(".") || entry.startsWith("_")) continue;
    const full = join(root, entry);
    if (!isDir(full)) continue;
    dirs.push(full);
  }
  return dirs;
}

/**
 * Title-cased fallback name derived from a slug (e.g. `a-b` -> `A B`).
 *
 * Mirrors `_default_name` in `klt layout-metrics`
 * (`src/klayout_tools/layout_metrics.py`) so a loader-synthesized stub uses
 * the same `name` the real extractor would emit for the same block.
 */
function titleCaseSlug(slug: string): string {
  return slug
    .replace(/[-_]/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

/** Construct a `no_artifacts` stub for a block with no parsable `layout.json`. */
function makeStub(slug: string): Layout {
  return {
    schema_version: SCHEMA_VERSION,
    generated_at: new Date(0).toISOString(),
    slug,
    name: titleCaseSlug(slug),
    status: "no_artifacts",
  };
}

/**
 * Validate a parsed JSON value against the required-field shape of schema
 * v1. Returns the value typed as `Layout` when valid, or `null` when it is
 * missing required fields / has an unknown `schema_version`.
 *
 * Note: only required-field presence and the `status` enum are enforced.
 * Optional fields are passed through as-is (the producer guarantees their
 * shapes per the contract).
 */
function validateLayout(data: unknown, slug: string): Layout | null {
  if (typeof data !== "object" || data === null) {
    console.warn(`[loadLayouts] ${slug}: layout.json is not an object; using stub`);
    return null;
  }
  const obj = data as Record<string, unknown>;

  if (obj.schema_version !== SCHEMA_VERSION) {
    console.warn(
      `[loadLayouts] ${slug}: unknown schema_version ` +
        `${JSON.stringify(obj.schema_version)} (expected ${SCHEMA_VERSION}); skipping`,
    );
    return null;
  }

  const required = ["generated_at", "slug", "name", "status"] as const;
  for (const key of required) {
    if (!(key in obj)) {
      console.warn(`[loadLayouts] ${slug}: layout.json missing required field "${key}"; using stub`);
      return null;
    }
  }

  if (typeof obj.status !== "string" || !VALID_STATUSES.has(obj.status)) {
    console.warn(`[loadLayouts] ${slug}: invalid status ${JSON.stringify(obj.status)}; using stub`);
    return null;
  }

  return obj as unknown as Layout;
}

/**
 * Read and validate a single block directory's `layout.json`.
 * Always returns a `Layout`: a parsed record when valid, otherwise a stub.
 */
export function loadLayout(blockPath: string): Layout {
  const slug = blockPath.split(/[\\/]/).filter(Boolean).pop() ?? blockPath;
  const jsonPath = join(blockPath, "output", "layout.json");

  if (!existsSync(jsonPath)) {
    return makeStub(slug);
  }

  let raw: string;
  try {
    raw = readFileSync(jsonPath, "utf8");
  } catch (err) {
    console.warn(`[loadLayouts] ${slug}: failed to read layout.json (${String(err)}); using stub`);
    return makeStub(slug);
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (err) {
    console.warn(`[loadLayouts] ${slug}: layout.json is not valid JSON (${String(err)}); using stub`);
    return makeStub(slug);
  }

  const layout = validateLayout(parsed, slug);
  return layout ?? makeStub(slug);
}

/**
 * Load every block, slug-sorted.
 *
 * Never throws for missing/invalid `layout.json` — those become stubs so
 * the static build always succeeds, even with zero `layout.json` files
 * present.
 */
export function loadLayouts(root: string = blocksDir()): Layout[] {
  const layouts = discoverBlockDirs(root).map(loadLayout);
  layouts.sort((a, b) => a.slug.localeCompare(b.slug));
  return layouts;
}
