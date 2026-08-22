/**
 * Unit tests for hierarchy flattening (issue #943 / #1284).
 *
 * The two-level fixture used here (`buildTwoLevelFixture()`: one cell placed
 * plainly, once rotated 90°, and once as a 2×3 array) was written to disk and
 * read back with KLayout to confirm both the expected instance count (8
 * placements of `CELL`) and the expected top-cell bounding box
 * `(-500,-500; 9500,10000)` asserted below — so these numbers describe GDSII
 * semantics, not just this implementation's behavior.
 */
import { describe, expect, it } from "vitest";
import { parseGds } from "./parseGds";
import {
  affineScale,
  composeAffine,
  flattenGds,
  refTransform,
  IDENTITY,
} from "./flattenGds";
import { GdsWriter, buildTwoLevelFixture, squareXY } from "./gdsFixture";

function flattenFixture() {
  return flattenGds(parseGds(buildTwoLevelFixture()));
}

describe("refTransform / composeAffine", () => {
  it("rotates counter-clockwise about the origin", () => {
    const t = refTransform(false, 1, 90, 0, 0);
    expect(t.a).toBeCloseTo(0, 12);
    expect(t.b).toBeCloseTo(1, 12);
    expect(t.c).toBeCloseTo(-1, 12);
    expect(t.d).toBeCloseTo(0, 12);
  });

  it("reflects about the x-axis before rotating", () => {
    const t = refTransform(true, 1, 0, 0, 0);
    // (1, 1) -> (1, -1)
    expect(t.a * 1 + t.c * 1 + t.e).toBeCloseTo(1, 12);
    expect(t.b * 1 + t.d * 1 + t.f).toBeCloseTo(-1, 12);
  });

  it("composes parent-after-child", () => {
    const parent = refTransform(false, 1, 90, 0, 0);
    const child = refTransform(false, 1, 0, 10, 0);
    const composed = composeAffine(parent, child);
    // The child's (0,0) sits at (10,0) inside the parent, which the parent's
    // 90° rotation carries to (0,10).
    expect(composed.e).toBeCloseTo(0, 9);
    expect(composed.f).toBeCloseTo(10, 9);
  });

  it("reports the uniform scale factor used for path widths", () => {
    expect(affineScale(IDENTITY)).toBeCloseTo(1, 12);
    expect(affineScale(refTransform(false, 3, 45, 0, 0))).toBeCloseTo(3, 12);
    expect(affineScale(refTransform(true, 2, 30, 0, 0))).toBeCloseTo(2, 12);
  });
});

describe("flattenGds", () => {
  it("expands every SREF and AREF placement (KLayout: 8 placements of CELL)", () => {
    const layout = flattenFixture();
    expect(layout.topName).toBe("TOP");

    const met1 = layout.layers.find((layer) => layer.key === "68/20");
    const li1 = layout.layers.find((layer) => layer.key === "67/20");
    const labels = layout.layers.find((layer) => layer.key === "68/5");
    const topOnly = layout.layers.find((layer) => layer.key === "64/20");

    expect(met1?.polygons).toHaveLength(8);
    expect(li1?.paths).toHaveLength(8);
    expect(labels?.texts).toHaveLength(8);
    expect(topOnly?.polygons).toHaveLength(1);
    expect(layout.shapeCount).toBe(8 + 8 + 1);
    expect(layout.truncated).toBe(false);
  });

  it("matches KLayout's flattened bounding box for the fixture", () => {
    expect(flattenFixture().bbox).toEqual({
      minX: -500,
      minY: -500,
      maxX: 9500,
      maxY: 10000,
    });
  });

  it("applies a reference's rotation to the referenced geometry", () => {
    const layout = flattenFixture();
    const met1 = layout.layers.find((layer) => layer.key === "68/20");
    // The 90°-rotated placement at (5000, 0) maps the cell's unit square to
    // x ∈ [4000, 5000], y ∈ [0, 1000].
    const rotated = met1?.polygons.find((polygon) => polygon.some((v) => Math.abs(v - 4000) < 1));
    expect(rotated).toBeDefined();
    const xs = rotated!.filter((_, index) => index % 2 === 0);
    const ys = rotated!.filter((_, index) => index % 2 === 1);
    expect(Math.min(...xs)).toBeCloseTo(4000, 6);
    expect(Math.max(...xs)).toBeCloseTo(5000, 6);
    expect(Math.min(...ys)).toBeCloseTo(0, 6);
    expect(Math.max(...ys)).toBeCloseTo(1000, 6);
  });

  it("places an AREF on its column/row lattice", () => {
    const layout = flattenFixture();
    const met1 = layout.layers.find((layer) => layer.key === "68/20");
    // Origins of the 2x3 array: (0,5000) stepping 2000 in x and 2000 in y.
    const origins = new Set(
      (met1?.polygons ?? []).map((polygon) => `${Math.round(polygon[0])},${Math.round(polygon[1])}`),
    );
    for (const expected of [
      "0,5000",
      "2000,5000",
      "0,7000",
      "2000,7000",
      "0,9000",
      "2000,9000",
    ]) {
      expect(origins).toContain(expected);
    }
  });

  it("scales a path's width by the reference's magnification", () => {
    const buffer = new GdsWriter()
      .header()
      .beginStructure("CELL")
      .path(67, 20, 200, 0, [0, 0, 1000, 0])
      .endStructure()
      .beginStructure("TOP")
      .sref("CELL", 0, 0, { mag: 2.5 })
      .endStructure()
      .end();

    const layer = flattenGds(parseGds(buffer)).layers[0];
    expect(layer.paths[0].width).toBeCloseTo(500, 6);
  });

  it("does not overstate a flush path's bounding box (KLayout parity)", () => {
    const buffer = new GdsWriter()
      .header()
      .beginStructure("TOP")
      .path(67, 20, 200, 0, [0, 0, 1000, 0])
      .endStructure()
      .end();

    // pathtype 0 has no end cap: the drawn extent is 0..1000 in x, ±100 in y.
    expect(flattenGds(parseGds(buffer)).bbox).toEqual({
      minX: 0,
      minY: -100,
      maxX: 1000,
      maxY: 100,
    });
  });

  it("extends a square-capped path by half a width at each end", () => {
    const buffer = new GdsWriter()
      .header()
      .beginStructure("TOP")
      .path(67, 20, 200, 2, [0, 0, 1000, 0])
      .endStructure()
      .end();

    expect(flattenGds(parseGds(buffer)).bbox).toEqual({
      minX: -100,
      minY: -100,
      maxX: 1100,
      maxY: 100,
    });
  });

  it("stops at the shape cap and reports the result as truncated", () => {
    const layout = flattenGds(parseGds(buildTwoLevelFixture()), { maxShapes: 4 });
    expect(layout.truncated).toBe(true);
    expect(layout.shapeCount).toBeLessThanOrEqual(4);
  });

  it("ignores a self-referencing structure instead of recursing forever", () => {
    const buffer = new GdsWriter()
      .header()
      .beginStructure("LOOP")
      .boundary(1, 0, squareXY(0, 0, 100))
      .sref("LOOP", 100, 100)
      .endStructure()
      .end();

    const layout = flattenGds(parseGds(buffer), { top: "LOOP" });
    expect(layout.shapeCount).toBe(1);
  });

  it("skips references to structures the stream never defines", () => {
    const buffer = new GdsWriter()
      .header()
      .beginStructure("TOP")
      .boundary(1, 0, squareXY(0, 0, 100))
      .sref("MISSING", 0, 0)
      .endStructure()
      .end();

    expect(flattenGds(parseGds(buffer)).shapeCount).toBe(1);
  });

  it("returns a null bbox for a structure with no geometry", () => {
    const buffer = new GdsWriter()
      .header()
      .beginStructure("EMPTY")
      .endStructure()
      .end();

    const layout = flattenGds(parseGds(buffer));
    expect(layout.bbox).toBeNull();
    expect(layout.layers).toEqual([]);
  });
});
