#!/usr/bin/env node
/**
 * Regenerates `src/lib/gds/layerStyles.generated.ts` from the open PDKs' own
 * KLayout layer-property (`.lyp`) files (issue #943 / #1284).
 *
 * The embedded GDS viewer (`src/components/layout/GdsCanvas.tsx`) renders raw
 * GDSII client-side, so it needs a `(layer, datatype) -> color + name` table
 * to style shapes the way KLayout would. Rather than hand-picking colors, we
 * derive them from the PDK's shipped `.lyp` — the same file KLayout itself
 * reads — so the browser view matches the per-layer PNG renders the Python
 * pipeline produces with KLayout.
 *
 * The generated table is committed because a `site/` build must not depend on
 * a local PDK install (Cloudflare Pages builds have none). Regenerate after a
 * PDK version bump:
 *
 *   node scripts/gen-gds-layer-styles.mjs                 # auto-discovers installed PDKs
 *   node scripts/gen-gds-layer-styles.mjs --sky130 <path> --gf180mcu <path>
 *
 * Source PDKs are Apache-2.0 licensed open PDKs (sky130 via volare, gf180mcu
 * via volare/ciel) — only layer numbers, display colors, and layer names are
 * extracted, all of which are public PDK metadata.
 */
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { homedir } from "node:os";

const HERE = dirname(fileURLToPath(import.meta.url));
const OUT_PATH = resolve(HERE, "..", "src", "lib", "gds", "layerStyles.generated.ts");

/** PDK families we generate a palette for, with where to find their `.lyp`. */
const FAMILIES = [
  {
    family: "sky130",
    flag: "--sky130",
    // ~/.volare/volare/sky130/versions/<hash>/sky130A/libs.tech/klayout/tech/sky130A.lyp
    discover: () => discoverVolare("sky130", "sky130A", "sky130A.lyp"),
  },
  {
    family: "gf180mcu",
    flag: "--gf180mcu",
    discover: () => discoverVolare("gf180mcu", "gf180mcuD", "gf180mcu.lyp"),
  },
];

function discoverVolare(pdk, variant, lypName) {
  for (const root of [join(homedir(), ".volare", "volare"), join(homedir(), ".ciel", "ciel")]) {
    const versionsDir = join(root, pdk, "versions");
    if (!existsSync(versionsDir)) continue;
    for (const version of readdirSync(versionsDir).sort()) {
      const candidate = join(versionsDir, version, variant, "libs.tech", "klayout", "tech", lypName);
      if (existsSync(candidate)) return candidate;
    }
  }
  return undefined;
}

/**
 * Extracts `{ layer, datatype, fill, frame, name }` records from a `.lyp`.
 * These files are flat (no `<group-members>` nesting) for both PDKs, so a
 * per-`<properties>` block regex scan is sufficient and avoids an XML dep.
 */
function parseLyp(text) {
  const entries = [];
  const blocks = text.split("<properties>").slice(1);
  for (const block of blocks) {
    const source = /<source>([^<]*)<\/source>/.exec(block)?.[1];
    if (!source) continue;
    // sky130: "235/4@1"; gf180mcu: "pass_mk 2/222@1" (name inline in source,
    // with an empty `<name/>` element). "*/*@1" wildcards are skipped.
    const match = /^(?:(\S+)\s+)?(-?\d+)\/(-?\d+)/.exec(source.trim());
    if (!match) continue;
    const fill = /<fill-color>#([0-9a-fA-F]{6})<\/fill-color>/.exec(block)?.[1];
    const frame = /<frame-color>#([0-9a-fA-F]{6})<\/frame-color>/.exec(block)?.[1];
    if (!fill && !frame) continue;
    // "<name>li1.drawing - 67/20</name>" -> "li1.drawing"
    const rawName = /<name>([^<]*)<\/name>/.exec(block)?.[1] ?? "";
    const name = rawName.split(" - ")[0].trim() || (match[1] ?? "");
    entries.push({
      layer: Number(match[2]),
      datatype: Number(match[3]),
      fill: `#${(fill ?? frame).toLowerCase()}`,
      frame: `#${(frame ?? fill).toLowerCase()}`,
      name,
    });
  }
  return entries;
}

function main() {
  const argv = process.argv.slice(2);
  const tables = [];
  for (const spec of FAMILIES) {
    const flagIndex = argv.indexOf(spec.flag);
    const path = flagIndex >= 0 ? argv[flagIndex + 1] : spec.discover();
    if (!path || !existsSync(path)) {
      console.error(
        `error: no .lyp found for ${spec.family}. Install the PDK (scripts/fetch-pdks.sh) or pass ${spec.flag} <path>.`,
      );
      process.exit(1);
    }
    const entries = parseLyp(readFileSync(path, "utf8"));
    if (entries.length === 0) {
      console.error(`error: parsed 0 layer entries from ${path}`);
      process.exit(1);
    }
    tables.push({ family: spec.family, path, entries });
    console.log(`${spec.family}: ${entries.length} layers from ${path}`);
  }

  const lines = [];
  lines.push("/**");
  lines.push(" * GENERATED FILE — do not edit by hand.");
  lines.push(" *");
  lines.push(" * Per-PDK `(layer, datatype) -> display style` tables for the embedded GDS");
  lines.push(" * viewer (issue #943 / #1284), extracted from each open PDK's own KLayout");
  lines.push(" * layer-property file so the browser view matches the per-layer PNG renders");
  lines.push(" * the Python pipeline produces with KLayout.");
  lines.push(" *");
  lines.push(" * Regenerate with `node scripts/gen-gds-layer-styles.mjs` (see that script for");
  lines.push(" * provenance and PDK licensing).");
  lines.push(" *");
  lines.push(" * Sources:");
  for (const table of tables) {
    lines.push(` *   ${table.family}: ${table.path.replace(homedir(), "~")}`);
  }
  lines.push(" */");
  lines.push("");
  lines.push("/** `[layer, datatype, fillColor, frameColor, name]`. */");
  lines.push("export type GeneratedLayerStyle = [number, number, string, string, string];");
  lines.push("");
  for (const table of tables) {
    const constName = `${table.family.toUpperCase().replace(/[^A-Z0-9]/g, "_")}_LAYER_STYLES`;
    lines.push(`export const ${constName}: readonly GeneratedLayerStyle[] = [`);
    for (const entry of table.entries) {
      lines.push(
        `  [${entry.layer}, ${entry.datatype}, ${JSON.stringify(entry.fill)}, ${JSON.stringify(entry.frame)}, ${JSON.stringify(entry.name)}],`,
      );
    }
    lines.push("];");
    lines.push("");
  }
  lines.push("export const GENERATED_LAYER_STYLES: Record<string, readonly GeneratedLayerStyle[]> = {");
  for (const table of tables) {
    const constName = `${table.family.toUpperCase().replace(/[^A-Z0-9]/g, "_")}_LAYER_STYLES`;
    lines.push(`  ${JSON.stringify(table.family)}: ${constName},`);
  }
  lines.push("};");
  lines.push("");

  mkdirSync(dirname(OUT_PATH), { recursive: true });
  writeFileSync(OUT_PATH, lines.join("\n"), "utf8");
  console.log(`wrote ${OUT_PATH}`);
}

main();
