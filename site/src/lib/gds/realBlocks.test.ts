/**
 * End-to-end reader tests against the gallery's own committed GDS files
 * (issue #943 / #1284) — the ones the site actually stages and the viewer
 * actually opens.
 *
 * Every expected number below was produced independently with KLayout (the
 * same engine the Python pipeline renders these blocks' PNGs with):
 *
 *   ly = klayout.db.Layout(); ly.read(path)
 *   top = ly.top_cell(); top.bbox()            # -> bbox, in database units
 *   for li in ly.layer_indexes():              # -> per-layer shape counts
 *       sum(1 for _ in top.begin_shapes_rec(li))
 *
 * so a regression in the parser or the hierarchy flattener shows up as a
 * disagreement with KLayout rather than with an earlier run of this same
 * code. Both PDK families and both hierarchy shapes are covered: gf180mcu's
 * flat single-cell canary, sky130's 27-cell hierarchical canary (which
 * exercises SREF flattening), and a sky130 standard cell (which exercises
 * PATH elements and TEXT labels).
 */
import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { parseGds } from "./parseGds";
import { flattenGds } from "./flattenGds";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..", "..", "..");

function blockGds(slug: string, file: string): ArrayBuffer | undefined {
  const path = resolve(REPO_ROOT, "blocks", slug, "output", file);
  if (!existsSync(path)) return undefined;
  const buffer = readFileSync(path);
  return buffer.buffer.slice(buffer.byteOffset, buffer.byteOffset + buffer.byteLength);
}

function load(slug: string, file: string) {
  const bytes = blockGds(slug, file);
  if (!bytes) throw new Error(`missing fixture blocks/${slug}/output/${file}`);
  return flattenGds(parseGds(bytes));
}

/** `{ "<layer>/<datatype>": geometryCount }` for a flattened layout. */
function shapeCounts(layout: ReturnType<typeof load>): Record<string, number> {
  const out: Record<string, number> = {};
  for (const layer of layout.layers) {
    if (layer.shapeCount > 0) out[layer.key] = layer.shapeCount;
  }
  return out;
}

const hasBlocks = existsSync(resolve(REPO_ROOT, "blocks"));

describe.skipIf(!hasBlocks)("gf180-bandgap (gf180mcu canary, flat)", () => {
  it("matches KLayout's top cell, units, and bounding box", () => {
    const layout = load("gf180-bandgap", "bandgap_top.gds");
    expect(layout.topName).toBe("bandgap_top");
    expect(layout.dbuMicrons).toBeCloseTo(0.001, 9);
    expect(layout.bbox).toEqual({ minX: -3400, minY: -3800, maxX: 218700, maxY: 288520 });
  });

  it("matches KLayout's per-layer shape counts", () => {
    const layout = load("gf180-bandgap", "bandgap_top.gds");
    expect(shapeCounts(layout)).toEqual({
      "21/0": 9,
      "22/0": 173,
      "30/0": 422,
      "31/0": 207,
      "32/0": 117,
      "33/0": 9442,
      "34/0": 395,
      "35/0": 301,
      "36/0": 326,
      "38/0": 372,
      "40/0": 2,
      "41/0": 2,
      "42/0": 74,
      "46/0": 2,
      "49/0": 66,
      "62/0": 1,
      "75/0": 1,
      "81/0": 3,
      "110/5": 66,
      "117/5": 1,
      "117/10": 1,
      "127/5": 8,
    });
    expect(layout.shapeCount).toBe(11991);
    expect(layout.truncated).toBe(false);
  });
});

describe.skipIf(!hasBlocks)("sky130-bandgap (sky130 canary, 27-cell hierarchy)", () => {
  it("flattens the hierarchy to KLayout's shape counts and bounding box", () => {
    const layout = load("sky130-bandgap", "bandgap_core_routed.gds");
    expect(layout.topName).toBe("bandgap_core_routed");
    expect(layout.bbox).toEqual({ minX: -10000, minY: -10000, maxX: 302100, maxY: 215440 });
    expect(shapeCounts(layout)).toEqual({
      "64/20": 13,
      "65/20": 148,
      "65/44": 44,
      "66/13": 157,
      "66/20": 303,
      "66/44": 739,
      "67/20": 616,
      "67/44": 564,
      "68/20": 1044,
      "68/44": 26,
      "69/20": 56,
      "82/44": 36,
      "83/20": 24,
      "86/20": 157,
      "94/20": 157,
    });
  });

  it("keeps the 13 met1 pin labels KLayout reports on 68/5", () => {
    const layout = load("sky130-bandgap", "bandgap_core_routed.gds");
    const labels = layout.layers.find((layer) => layer.key === "68/5");
    expect(labels?.texts).toHaveLength(13);
    expect(labels?.shapeCount).toBe(0);
  });
});

describe.skipIf(!hasBlocks)("sky130_fd_sc_hd__inv_1 (standard cell, paths + labels)", () => {
  it("matches KLayout's geometry and label counts, including PATH elements", () => {
    const layout = load("sky130_fd_sc_hd__inv_1", "sky130_fd_sc_hd__inv_1.gds");
    expect(layout.bbox).toEqual({ minX: -190, minY: -240, maxX: 1570, maxY: 2960 });
    expect(shapeCounts(layout)).toEqual({
      "64/16": 2,
      "64/20": 1,
      "65/20": 2,
      "66/20": 1,
      "66/44": 11,
      "67/16": 3,
      "67/20": 6,
      "67/44": 6,
      "68/16": 4,
      "68/20": 2,
      "78/44": 1,
      "81/4": 1,
      "93/44": 1,
      "94/20": 1,
      "95/20": 1,
      "122/16": 2,
      "236/0": 1,
    });

    const textCount = layout.layers.reduce((total, layer) => total + layer.texts.length, 0);
    expect(textCount).toBe(8);

    // KLayout reports two PATH shapes in this cell; they must survive as
    // paths (with a real width), not be silently dropped or coerced.
    const paths = layout.layers.flatMap((layer) => layer.paths);
    expect(paths).toHaveLength(2);
    for (const path of paths) {
      expect(path.width).toBeGreaterThan(0);
    }
  });
});

describe.skipIf(!hasBlocks)("gf180mcu_fd_sc_mcu9t5v0__and2_1 (gf180mcu standard cell)", () => {
  it("parses the second PDK family's standard cell", () => {
    const layout = load(
      "gf180mcu_fd_sc_mcu9t5v0__and2_1",
      "gf180mcu_fd_sc_mcu9t5v0__and2_1.gds",
    );
    expect(layout.bbox).toEqual({ minX: -430, minY: -450, maxX: 4910, maxY: 5490 });
    const geometry = layout.layers.reduce((total, layer) => total + layer.shapeCount, 0);
    const labels = layout.layers.reduce((total, layer) => total + layer.texts.length, 0);
    expect(geometry).toBe(34); // KLayout: 24 boxes + 10 polygons
    expect(labels).toBe(9);
  });
});
