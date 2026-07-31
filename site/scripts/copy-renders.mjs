/**
 * Prebuild step: stage per-block render PNGs (and, when downloadable, the
 * source layout file) from the repo's `blocks/` tree into `site/public/` so
 * the static Vite build can serve them.
 *
 * Why this exists
 * ----------------
 * Vite serves anything under `public/` verbatim at the site root, but block
 * artifacts live **outside** `site/` (under the repo's `blocks/<slug>/output/`)
 * and Vite cannot reference files outside its project root. So we copy the
 * files into `site/public/blocks/<slug>/...` at build time; a copied file at
 * `site/public/blocks/<slug>/renders/1_0.png` is then reachable at the URL
 * `/blocks/<slug>/renders/1_0.png` — exactly what `DetailPage.tsx` (the block
 * detail page, originally #64) references. (Ported unchanged from the
 * pre-Astro-migration script in #92 — the staging contract with the block
 * pipeline, #62, did not change.)
 *
 * What gets staged
 * -----------------
 *   - Renders: every path in `layout.json`'s `renders` map (relative to the
 *     block's `output/` directory, e.g. `renders/1_0.png` — see #60/#61) is
 *     copied to the matching relative path under
 *     `site/public/blocks/<slug>/...`, served at `/blocks/<slug>/...`.
 *   - Downloadable layout file: when `layout.json` sets `downloadable: true`
 *     (the content-pipeline / public-repo gate, #62 — not yet wired up, so
 *     this never fires today), `layout_file` is staged the same way so the
 *     detail page's download link resolves.
 *
 * Discovery rules mirror `src/data/loadLayouts.ts`: immediate subdirectories
 * of `blocks/`, skipping hidden / `_`-prefixed entries. The block directory
 * name is its `slug` — the same key the loader emits — so staged paths line
 * up with the `Layout.slug` used by the page components.
 *
 * Resilience: this script NEVER fails the build. A fresh checkout (or any
 * block without renders/no `layout.json`) is a normal no-op, not an error.
 * The staged output directory (`site/public/blocks/`) is git-ignored and
 * regenerated on every build.
 */

import {
  readdirSync,
  existsSync,
  statSync,
  mkdirSync,
  copyFileSync,
  rmSync,
  readFileSync,
} from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const SITE_DIR = resolve(SCRIPT_DIR, "..");

/** True if `path` exists and is a directory. */
function isDir(path) {
  try {
    return statSync(path).isDirectory();
  } catch {
    return false;
  }
}

/** True if `path` exists and is a regular file. */
function isFile(path) {
  try {
    return statSync(path).isFile();
  } catch {
    return false;
  }
}

/**
 * Resolve the repository's `blocks/` directory.
 *   1. `KLT_BLOCKS_DIR` env override (tests / CI) — mirrors `loadLayouts.ts`.
 *   2. `<site>/../blocks` — the repo root is one level above `site/`.
 */
function blocksDir() {
  const override = process.env.KLT_BLOCKS_DIR;
  if (override) return resolve(override);
  return resolve(SITE_DIR, "..", "blocks");
}

/** Enumerate `{ slug, dir }` for every block directory under `root`. */
function discoverBlocks(root) {
  if (!isDir(root)) return [];

  const blocks = [];
  for (const entry of readdirSync(root).sort()) {
    if (entry.startsWith(".") || entry.startsWith("_")) continue;
    const full = join(root, entry);
    if (!isDir(full)) continue;
    blocks.push({ slug: entry, dir: full });
  }
  return blocks;
}

/** Parse `<dir>/output/layout.json`, or `null` when absent/unparsable. */
function readLayoutJson(dir) {
  const jsonPath = join(dir, "output", "layout.json");
  if (!isFile(jsonPath)) return null;
  try {
    return JSON.parse(readFileSync(jsonPath, "utf8"));
  } catch {
    return null;
  }
}

/** Copy `<dir>/output/<relPath>` to `<destRoot>/<slug>/<relPath>` if it exists. */
function stageFile(dir, destRoot, slug, relPath) {
  const src = join(dir, "output", relPath);
  if (!isFile(src)) return false;
  const dest = join(destRoot, slug, relPath);
  mkdirSync(dirname(dest), { recursive: true });
  copyFileSync(src, dest);
  return true;
}

function main() {
  const root = blocksDir();
  const destRoot = join(SITE_DIR, "public", "blocks");

  // Start from a clean staging tree so removed/renamed renders don't linger.
  if (existsSync(destRoot)) {
    rmSync(destRoot, { recursive: true, force: true });
  }

  if (!isDir(root)) {
    console.log(`[copy-renders] blocks directory not found at ${root}; nothing to stage`);
    return;
  }

  let renderCount = 0;
  let blocksWithRenders = 0;
  let downloadCount = 0;

  for (const { slug, dir } of discoverBlocks(root)) {
    const layout = readLayoutJson(dir);
    if (!layout) continue;

    const renders = layout.renders && typeof layout.renders === "object" ? layout.renders : null;
    if (renders) {
      let staged = 0;
      for (const relPath of Object.values(renders)) {
        if (typeof relPath !== "string") continue;
        if (stageFile(dir, destRoot, slug, relPath)) staged += 1;
      }
      if (staged > 0) {
        renderCount += staged;
        blocksWithRenders += 1;
      }
    }

    if (layout.downloadable === true && typeof layout.layout_file === "string") {
      if (stageFile(dir, destRoot, slug, layout.layout_file)) downloadCount += 1;
    }
  }

  console.log(
    `[copy-renders] staged ${renderCount} render(s) from ${blocksWithRenders} block(s) ` +
      `and ${downloadCount} downloadable layout file(s) into ${destRoot}`,
  );
}

main();
