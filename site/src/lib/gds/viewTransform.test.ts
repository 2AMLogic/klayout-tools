/**
 * Tests for the viewer's pan/zoom math (issue #943 / #1284), including
 * against the gallery's real blocks: "clicking a render opens an
 * interactive, user-navigable view" is only true if a freshly fitted view
 * actually places that block's geometry inside the visible surface, and if
 * zooming keeps the point under the cursor put.
 */
import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { flattenGds } from "./flattenGds";
import { parseGds } from "./parseGds";
import { fitView, panView, screenToWorld, worldToScreen, zoomView } from "./viewTransform";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..", "..", "..");
const WIDTH = 900;
const HEIGHT = 600;

const REAL_BLOCKS: [slug: string, file: string][] = [
  ["gf180-bandgap", "bandgap_top.gds"],
  ["sky130-bandgap", "bandgap_core_routed.gds"],
];

function loadBlock(slug: string, file: string) {
  const path = resolve(REPO_ROOT, "blocks", slug, "output", file);
  const buffer = readFileSync(path);
  return flattenGds(
    parseGds(buffer.buffer.slice(buffer.byteOffset, buffer.byteOffset + buffer.byteLength)),
  );
}

describe("fitView", () => {
  it("centres a bounding box on the surface", () => {
    const view = fitView({ minX: 0, minY: 0, maxX: 1000, maxY: 1000 }, 800, 400);
    const [cx, cy] = worldToScreen(view, 500, 500);
    expect(cx).toBeCloseTo(400, 9);
    expect(cy).toBeCloseTo(200, 9);
  });

  it("fits the constraining axis and leaves a margin", () => {
    const view = fitView({ minX: 0, minY: 0, maxX: 1000, maxY: 100 }, 800, 400);
    expect(view.scale).toBeCloseTo((800 / 1000) * 0.92, 9);
  });

  it("degrades to an identity-scale centred view when there is nothing to fit", () => {
    expect(fitView(null, 800, 400)).toEqual({ scale: 1, tx: 400, ty: 200 });
    expect(fitView({ minX: 0, minY: 0, maxX: 10, maxY: 10 }, 0, 0)).toEqual({
      scale: 1,
      tx: 0,
      ty: 0,
    });
  });
});

describe("zoomView / panView", () => {
  it("keeps the world point under the anchor fixed while zooming", () => {
    const view = fitView({ minX: 0, minY: 0, maxX: 1000, maxY: 1000 }, WIDTH, HEIGHT);
    const anchor: [number, number] = [123, 456];
    const before = screenToWorld(view, ...anchor);

    const zoomed = zoomView(view, 2.5, ...anchor, 1e-9, 1e3);
    const after = screenToWorld(zoomed, ...anchor);

    expect(zoomed.scale).toBeCloseTo(view.scale * 2.5, 12);
    expect(after[0]).toBeCloseTo(before[0], 6);
    expect(after[1]).toBeCloseTo(before[1], 6);
  });

  it("clamps the scale to the supplied bounds", () => {
    const view = { scale: 1, tx: 0, ty: 0 };
    expect(zoomView(view, 1e9, 0, 0, 0.5, 4).scale).toBe(4);
    expect(zoomView(view, 1e-9, 0, 0, 0.5, 4).scale).toBe(0.5);
  });

  it("translates by the drag delta without changing scale", () => {
    const panned = panView({ scale: 0.5, tx: 10, ty: 20 }, 35, -15);
    expect(panned).toEqual({ scale: 0.5, tx: 45, ty: 5 });
  });

  it("round-trips screen <-> world coordinates", () => {
    const view = { scale: 0.037, tx: 21, ty: 512 };
    const [wx, wy] = screenToWorld(view, 640, 480);
    const [sx, sy] = worldToScreen(view, wx, wy);
    expect(sx).toBeCloseTo(640, 9);
    expect(sy).toBeCloseTo(480, 9);
  });
});

const hasBlocks = existsSync(resolve(REPO_ROOT, "blocks"));

describe.skipIf(!hasBlocks)("fitting the gallery's real blocks", () => {
  it.each(REAL_BLOCKS)("puts every %s shape inside the visible surface", (slug, file) => {
    const layout = loadBlock(slug, file);
    const view = fitView(layout.bbox, WIDTH, HEIGHT);
    expect(view.scale).toBeGreaterThan(0);

    let checked = 0;
    for (const layer of layout.layers) {
      for (const polygon of layer.polygons) {
        for (let i = 0; i + 1 < polygon.length; i += 2) {
          const [x, y] = worldToScreen(view, polygon[i], polygon[i + 1]);
          expect(x).toBeGreaterThanOrEqual(-1);
          expect(x).toBeLessThanOrEqual(WIDTH + 1);
          expect(y).toBeGreaterThanOrEqual(-1);
          expect(y).toBeLessThanOrEqual(HEIGHT + 1);
          checked += 1;
        }
      }
    }
    expect(checked).toBeGreaterThan(1000);
  });

  it.each(REAL_BLOCKS)("keeps %s's fitted layout large enough to actually see", (slug, file) => {
    const layout = loadBlock(slug, file);
    const view = fitView(layout.bbox, WIDTH, HEIGHT);
    const bbox = layout.bbox!;
    const [left, top] = worldToScreen(view, bbox.minX, bbox.maxY);
    const [right, bottom] = worldToScreen(view, bbox.maxX, bbox.minY);
    // The layout should span at least half of the constraining axis — a
    // regression that fits against a zero-sized surface (scale 1) or an
    // unflattened bbox would fail this.
    expect(Math.max(right - left, bottom - top)).toBeGreaterThan(
      Math.min(WIDTH, HEIGHT) * 0.5,
    );
  });
});
