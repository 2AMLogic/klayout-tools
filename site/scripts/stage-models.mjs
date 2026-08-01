/**
 * Prebuild step: stage self-contained, checksummed sky130 primitive-device
 * SPICE model decks (per process corner) as static site assets, so the
 * in-browser SPICE playground (Epic #90, phase B/C) can `fetch()` them
 * client-side instead of trusting a JS engine's baked-in, un-checksummed
 * model blobs.
 *
 * This is **phase A** of the in-browser SPICE playground (issue #149, parent
 * #148): it only stages the model text, independent of the engine/UI work.
 *
 * What gets staged
 * -----------------
 * For each of the four sky130 gallery standard cells (the ones that
 * instantiate exactly the two sky130 primitives `nfet_01v8` /
 * `pfet_01v8_hvt`), and for each of the `tt`/`ss`/`ff` process corners, a
 * single self-contained deck is written to:
 *
 *     site/public/blocks/<slug>/models/<corner>.spice
 *
 * reachable at the URL `/blocks/<slug>/models/<corner>.spice`. Each deck is
 * the same device-corner text `scripts/gallery_signals.py`'s
 * `_sky130_models_lib` assembles for build-time `klt sim` runs — the
 * concatenation, per device, of its `*__mismatch.corner.spice` and
 * `*__<corner>.corner.spice` files — with one adaptation for client-side use:
 * the corner files' relative `.include` of their `*.pm3.spice` binned-model
 * cards is resolved **inline**, so each staged deck is one portable file (a
 * browser engine cannot follow a build-machine filesystem `.include`, and
 * `_sky130_models_lib`'s absolute-path includes are meaningless off the build
 * host). The model content is otherwise byte-for-byte upstream.
 *
 * A `models/checksums.json` is written alongside, publishing for every deck
 * its own SHA-256 plus the SHA-256 of each `pdks/cell-netlists/` source file
 * that went into it — the same SHA-256-of-file-bytes convention
 * `docs/cli/sim.md`'s `environment` reproducibility block uses — so any staged
 * deck is verifiable against its sources. A `models/NOTICE` (Apache-2.0
 * attribution for the SkyWater PDK model text) is staged next to them.
 *
 * gf180mcu is intentionally out of scope for this phase — sky130 only.
 *
 * Resilience: like `copy-renders.mjs`, this script NEVER fails the build. A
 * fresh checkout with no fetched netlists (`pdks/cell-netlists/` absent — it
 * is gitignored, populated by `scripts/fetch-cell-netlists.sh`) is a normal
 * no-op, not an error. It runs AFTER `copy-renders.mjs` (which wipes and
 * rebuilds `site/public/blocks/`), adding only the per-block `models/` subtree.
 */

import {
  readdirSync,
  existsSync,
  statSync,
  mkdirSync,
  writeFileSync,
  readFileSync,
  copyFileSync,
} from "node:fs";
import { createHash } from "node:crypto";
import { dirname, join, resolve, relative } from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const SITE_DIR = resolve(SCRIPT_DIR, "..");
const NOTICE_SRC = join(SCRIPT_DIR, "sky130-models.NOTICE.txt");

/** The sky130 model set the four sky130 gallery cells need. Mirrors the
 * sky130 entries of `CELL_CONFIGS` / `_sky130_models_lib` in
 * `scripts/gallery_signals.py` — kept in sync with that module deliberately. */
export const SKY130 = {
  corners: ["tt", "ss", "ff"],
  devices: ["nfet_01v8", "pfet_01v8_hvt"],
  slugs: [
    "sky130_fd_sc_hd__inv_1",
    "sky130_fd_sc_hd__nand2_2",
    "sky130_fd_sc_hd__buf_4",
    "sky130_fd_sc_hd__dfxtp_2",
  ],
};

/** SHA-256 hex digest of a Buffer or string — the `docs/cli/sim.md`
 * `environment` (and `sim.py`'s `_sha256_file`) reproducibility convention. */
export function sha256Hex(data) {
  return createHash("sha256").update(data).digest("hex");
}

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
 * Resolve the repository's `pdks/cell-netlists/` directory.
 *   1. `KLT_CELL_NETLISTS_DIR` env override (tests / CI).
 *   2. `<site>/../pdks/cell-netlists` — repo root is one level above `site/`.
 */
export function cellNetlistsDir() {
  const override = process.env.KLT_CELL_NETLISTS_DIR;
  if (override) return resolve(override);
  return resolve(SITE_DIR, "..", "pdks", "cell-netlists");
}

/**
 * Resolve the repository's `blocks/` directory. Mirrors `copy-renders.mjs`.
 *   1. `KLT_BLOCKS_DIR` env override (tests / CI).
 *   2. `<site>/../blocks`.
 */
export function blocksDir() {
  const override = process.env.KLT_BLOCKS_DIR;
  if (override) return resolve(override);
  return resolve(SITE_DIR, "..", "blocks");
}

/**
 * Replace every `.include "<relative>"` directive in `text` with the referenced
 * file's content (resolved relative to `modelsDir`), recording each inlined
 * file in `sources`. sky130 corner files include exactly their `*.pm3.spice`
 * binned-model cards this way; leaving that relative include unresolved would
 * make the staged deck unusable off the build host. Unresolvable includes are
 * left verbatim (best-effort, like every optional artifact in this pipeline).
 */
function inlineIncludes(text, modelsDir, netlistsRoot, sources) {
  return text.replace(
    /^[ \t]*\.include[ \t]+"([^"]+)"[ \t]*$/gim,
    (match, rel) => {
      const incPath = join(modelsDir, rel);
      if (!isFile(incPath)) return match;
      const raw = readFileSync(incPath);
      sources.push({
        path: relative(netlistsRoot, incPath),
        sha256: sha256Hex(raw),
        bytes: raw.length,
      });
      return `* [stage-models] inlined ${rel}\n${raw.toString("utf8").replace(/\s+$/, "")}\n`;
    },
  );
}

/**
 * Assemble one self-contained sky130 model deck for `corner`.
 * Returns `{ text, sources }` where `sources` lists every
 * `pdks/cell-netlists/` file (relpath + sha256 + bytes) that went into it, or
 * `null` when any required source file is missing.
 */
export function assembleCornerDeck(modelsDir, corner, netlistsRoot) {
  const parts = [
    `* sky130 primitive-device models — corner "${corner}"`,
    `* devices: ${SKY130.devices.join(", ")}`,
    "* Assembled from the SkyWater Open Source PDK (Apache-2.0); see NOTICE.",
    "* Generated by site/scripts/stage-models.mjs (issue #149) — do not edit.",
    "*",
  ];
  const sources = [];

  for (const dev of SKY130.devices) {
    for (const kind of [`${dev}__mismatch.corner`, `${dev}__${corner}.corner`]) {
      const file = `sky130_fd_pr__${kind}.spice`;
      const filePath = join(modelsDir, file);
      if (!isFile(filePath)) return null;
      const raw = readFileSync(filePath);
      sources.push({
        path: relative(netlistsRoot, filePath),
        sha256: sha256Hex(raw),
        bytes: raw.length,
      });
      parts.push(`* ---- ${file} ----`);
      parts.push(
        inlineIncludes(raw.toString("utf8"), modelsDir, netlistsRoot, sources).replace(/\s+$/, ""),
      );
    }
  }

  return { text: `${parts.join("\n")}\n`, sources };
}

/**
 * Stage the sky130 model decks (+ checksums + NOTICE) for one block into
 * `<destRoot>/<slug>/models/`. Returns the number of decks written (0 when
 * source model files are absent — a normal no-op).
 */
export function stageModelsForBlock(slug, netlistsRoot, destRoot) {
  const modelsDir = join(netlistsRoot, "sky130", "models");
  if (!isDir(modelsDir)) return 0;

  const decks = {};
  const built = [];
  for (const corner of SKY130.corners) {
    const deck = assembleCornerDeck(modelsDir, corner, netlistsRoot);
    if (!deck) return 0; // require the full set or stage nothing
    const bytes = Buffer.from(deck.text, "utf8");
    decks[corner] = {
      path: `models/${corner}.spice`,
      sha256: sha256Hex(bytes),
      bytes: bytes.length,
      sources: deck.sources,
    };
    built.push({ corner, bytes });
  }

  const outDir = join(destRoot, slug, "models");
  mkdirSync(outDir, { recursive: true });
  for (const { corner, bytes } of built) {
    writeFileSync(join(outDir, `${corner}.spice`), bytes);
  }

  const manifest = {
    schema_version: 1,
    pdk: "sky130",
    generator: "site/scripts/stage-models.mjs",
    devices: SKY130.devices,
    source_repo: "https://github.com/google/skywater-pdk-libs-sky130_fd_pr",
    license: "Apache-2.0",
    notice: "models/NOTICE",
    decks,
  };
  writeFileSync(join(outDir, "checksums.json"), `${JSON.stringify(manifest, null, 2)}\n`);

  if (isFile(NOTICE_SRC)) {
    copyFileSync(NOTICE_SRC, join(outDir, "NOTICE"));
  }

  return built.length;
}

function main() {
  const netlistsRoot = cellNetlistsDir();
  const blocksRoot = blocksDir();
  const destRoot = join(SITE_DIR, "public", "blocks");

  const modelsDir = join(netlistsRoot, "sky130", "models");
  if (!isDir(modelsDir)) {
    console.log(
      `[stage-models] sky130 models not found at ${modelsDir}; nothing to stage ` +
        "(run scripts/fetch-cell-netlists.sh to populate)",
    );
    return;
  }

  let blockCount = 0;
  let deckCount = 0;
  for (const slug of SKY130.slugs) {
    // Only stage for gallery blocks that actually exist in this checkout.
    if (!isDir(join(blocksRoot, slug))) continue;
    const staged = stageModelsForBlock(slug, netlistsRoot, destRoot);
    if (staged > 0) {
      blockCount += 1;
      deckCount += staged;
    }
  }

  console.log(
    `[stage-models] staged ${deckCount} model deck(s) across ${blockCount} sky130 ` +
      `block(s) into ${destRoot}`,
  );
}

// Only run when invoked directly (not when imported by tests).
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main();
}
