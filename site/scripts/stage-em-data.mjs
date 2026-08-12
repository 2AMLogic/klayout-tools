/**
 * Prebuild step: stage geode-fem site export JSON (Epic #840 Phase 1a,
 * issue #849) from the repo's `examples/em/` tree into `site/public/em/` so
 * the EM gallery's `PatchAntennaResult` component (issue #850) can
 * `fetch()` the committed solver output client-side.
 *
 * Why this exists
 * ----------------
 * Like `copy-renders.mjs` stages `blocks/<slug>/output/` renders (and
 * `stage-models.mjs` stages sky130 SPICE decks), the export JSON lives
 * **outside** `site/` (`examples/em/<benchmark>/<benchmark>.em-export.json`)
 * and Vite cannot reference files outside its project root. A copy at
 * `site/public/em/<benchmark>/<benchmark>.em-export.json` is reachable at
 * `/em/<benchmark>/<benchmark>.em-export.json` -- the default `dataUrl`
 * `PatchAntennaResult.tsx` fetches.
 *
 * What gets staged
 * -----------------
 * Every immediate subdirectory of `examples/em/` (the benchmark name, e.g.
 * `patch_antenna`) whose own `<benchmark>.em-export.json` file exists is
 * copied verbatim -- no transformation, so the staged file is byte-for-byte
 * the committed solver-run export (issue #850's "no illustrative/faked
 * fields" requirement traces to this file being an untouched copy).
 *
 * Resilience: like the other staging scripts, this NEVER fails the build --
 * a checkout without `examples/em/` (or without a given benchmark's export
 * yet) is a normal no-op, not an error. The staged output
 * (`site/public/em/`) is git-ignored and regenerated on every build.
 */

import { readdirSync, existsSync, statSync, mkdirSync, copyFileSync, rmSync } from "node:fs";
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
 * Resolve the repository's `examples/em/` directory.
 *   1. `KLT_EM_EXAMPLES_DIR` env override (tests / CI) -- mirrors
 *      `copy-renders.mjs`'s `KLT_BLOCKS_DIR`.
 *   2. `<site>/../examples/em` -- the repo root is one level above `site/`.
 */
export function emExamplesDir() {
  const override = process.env.KLT_EM_EXAMPLES_DIR;
  if (override) return resolve(override);
  return resolve(SITE_DIR, "..", "examples", "em");
}

/**
 * Stage every `<benchmark>/<benchmark>.em-export.json` found under
 * `examplesRoot` into `<destRoot>/<benchmark>/<benchmark>.em-export.json`.
 * Returns the number of files staged.
 */
export function stageEmData(examplesRoot, destRoot) {
  if (!isDir(examplesRoot)) return 0;

  let staged = 0;
  for (const entry of readdirSync(examplesRoot).sort()) {
    if (entry.startsWith(".") || entry.startsWith("_")) continue;
    const benchmarkDir = join(examplesRoot, entry);
    if (!isDir(benchmarkDir)) continue;

    const src = join(benchmarkDir, `${entry}.em-export.json`);
    if (!isFile(src)) continue;

    const destDir = join(destRoot, entry);
    mkdirSync(destDir, { recursive: true });
    copyFileSync(src, join(destDir, `${entry}.em-export.json`));
    staged += 1;
  }
  return staged;
}

function main() {
  const examplesRoot = emExamplesDir();
  const destRoot = join(SITE_DIR, "public", "em");

  // Start from a clean staging tree, like copy-renders.mjs.
  if (existsSync(destRoot)) {
    rmSync(destRoot, { recursive: true, force: true });
  }

  if (!isDir(examplesRoot)) {
    console.log(`[stage-em-data] ${examplesRoot} not found; nothing to stage`);
    return;
  }

  const count = stageEmData(examplesRoot, destRoot);
  console.log(`[stage-em-data] staged ${count} em export(s) into ${destRoot}`);
}

// Only run when invoked directly (not when imported by tests).
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main();
}
