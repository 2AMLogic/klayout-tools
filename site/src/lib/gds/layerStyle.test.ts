/**
 * Tests for per-PDK layer styling and layer naming (issue #943 / #1284).
 *
 * The expected colors below are the values the PDKs themselves ship in their
 * KLayout layer-property files (`sky130A.lyp`, `gf180mcu.lyp`), which
 * `scripts/gen-gds-layer-styles.mjs` extracts — so this suite fails loudly
 * if a regeneration ever drops or reshuffles the two families' tables, which
 * is exactly the "works for both sky130 and gf180 blocks with correct layer
 * styling" acceptance criterion.
 */
import { describe, expect, it } from "vitest";
import { fallbackLayerColor, resolveLayerStyle } from "./layerStyle";
import { layerNamesFromRenders } from "./layerNames";

describe("resolveLayerStyle — sky130", () => {
  it("styles met1 (68/20) from the PDK's own layer properties", () => {
    const style = resolveLayerStyle(68, 20, "sky130");
    expect(style).toEqual({
      fill: "#0000ff",
      frame: "#0000ff",
      name: "met1.drawing",
      fromPdk: true,
    });
  });

  it("styles diff (65/20) and li1 (67/20) distinctly", () => {
    expect(resolveLayerStyle(65, 20, "sky130").name).toBe("diff.drawing");
    expect(resolveLayerStyle(67, 20, "sky130").name).toBe("li1.drawing");
    expect(resolveLayerStyle(65, 20, "sky130").fill).not.toBe(
      resolveLayerStyle(67, 20, "sky130").fill,
    );
  });
});

describe("resolveLayerStyle — gf180mcu", () => {
  it("styles Metal1 (34/0) and Nwell (21/0) from the PDK's own layer properties", () => {
    expect(resolveLayerStyle(34, 0, "gf180mcu")).toEqual({
      fill: "#eddd07",
      frame: "#eddd07",
      name: "Metal1",
      fromPdk: true,
    });
    expect(resolveLayerStyle(21, 0, "gf180mcu").name).toBe("Nwell");
  });

  it("does not style a gf180mcu layer with the sky130 table's color", () => {
    // 34/0 is Metal1 in gf180mcu but a mask layer in sky130 — the whole point
    // of keying styling on the block's PDK family (issues #655/#1060).
    expect(resolveLayerStyle(34, 0, "gf180mcu").fill).not.toBe(
      resolveLayerStyle(34, 0, "sky130").fill,
    );
  });
});

describe("resolveLayerStyle — fallbacks", () => {
  it("falls back to a deterministic hue when the PDK family is unknown", () => {
    const style = resolveLayerStyle(68, 20);
    expect(style.fromPdk).toBe(false);
    expect(style.name).toBe("Layer 68/20");
    expect(style.fill).toBe(fallbackLayerColor(68, 20));
    expect(style.fill).toMatch(/^#[0-9a-f]{6}$/);
  });

  it("falls back for a layer the PDK table doesn't cover", () => {
    const style = resolveLayerStyle(9999, 7, "sky130");
    expect(style.fromPdk).toBe(false);
    expect(style.name).toBe("Layer 9999/7");
  });

  it("gives adjacent layers visibly different fallback colors", () => {
    expect(fallbackLayerColor(10, 0)).not.toBe(fallbackLayerColor(11, 0));
    expect(fallbackLayerColor(10, 0)).toBe(fallbackLayerColor(10, 0));
  });

  it("prefers a name override over the PDK table's name", () => {
    const style = resolveLayerStyle(68, 20, "sky130", { "68/20": "met1 (from renders)" });
    expect(style.name).toBe("met1 (from renders)");
    expect(style.fill).toBe("#0000ff");
  });
});

describe("layerNamesFromRenders", () => {
  it("maps layer/datatype to the pipeline's own layer names", () => {
    expect(
      layerNamesFromRenders({
        overview: "renders/overview.png",
        center_crop: "renders/center_crop/overview.png",
        Nwell: "renders/21_0.png",
        Metal1: "renders/34_0.png",
        layer_49_0: "renders/49_0.png",
      }),
    ).toEqual({ "21/0": "Nwell", "34/0": "Metal1" });
  });

  it("handles sky130's dotted names and returns {} for a block with no renders", () => {
    expect(layerNamesFromRenders({ "li1.drawing": "renders/67_20.png" })).toEqual({
      "67/20": "li1.drawing",
    });
    expect(layerNamesFromRenders(undefined)).toEqual({});
    expect(layerNamesFromRenders({})).toEqual({});
  });
});
