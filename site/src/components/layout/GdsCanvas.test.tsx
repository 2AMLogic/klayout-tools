// @vitest-environment jsdom
/**
 * Tests for the same-origin GDS renderer (issue #943 / #1284).
 *
 * Two halves, mirroring how the component is built: `drawLayout()` is a free
 * function over a 2D context, so it is exercised directly against a
 * recording fake context (jsdom has no real canvas); the component itself is
 * exercised through the DOM — fetch → parse → layer list → visibility
 * toggles → zoom — using the same byte-level GDS fixture the parser suite
 * uses, so what the test loads is a real GDS stream, not a hand-built object
 * graph that could never come off the wire.
 */
import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import GdsCanvas, { drawLayout } from "./GdsCanvas";
import { buildTwoLevelFixture } from "@/lib/gds/gdsFixture";
import { fitView, flattenGds, parseGds, resolveLayerStyle } from "@/lib/gds";

interface RecordingContext {
  ctx: CanvasRenderingContext2D;
  calls: { op: string; args: unknown[] }[];
}

function recordingContext(): RecordingContext {
  const calls: { op: string; args: unknown[] }[] = [];
  const record =
    (op: string) =>
    (...args: unknown[]) => {
      calls.push({ op, args });
    };
  const ctx = {
    setTransform: record("setTransform"),
    clearRect: record("clearRect"),
    fillRect: record("fillRect"),
    beginPath: record("beginPath"),
    moveTo: record("moveTo"),
    lineTo: record("lineTo"),
    closePath: record("closePath"),
    fill: record("fill"),
    stroke: record("stroke"),
    fillText: record("fillText"),
    measureText: () => ({ width: 10 }),
    globalAlpha: 1,
    fillStyle: "",
    strokeStyle: "",
    lineWidth: 1,
    lineCap: "butt",
    lineJoin: "round",
    font: "",
    textBaseline: "middle",
  } as unknown as CanvasRenderingContext2D;
  return { ctx, calls };
}

const fixtureLayout = () => flattenGds(parseGds(buildTwoLevelFixture()));

describe("drawLayout", () => {
  it("flips the y axis so GDS's up is the canvas's up", () => {
    const { ctx, calls } = recordingContext();
    const layout = fixtureLayout();
    const visible = new Set(layout.layers.map((layer) => layer.key));
    drawLayout(ctx, layout, { scale: 0.05, tx: 10, ty: 400 }, visible, new Map(), 800, 600, 1);

    const worldTransform = calls.find(
      (call) => call.op === "setTransform" && call.args[3] === -0.05,
    );
    expect(worldTransform?.args).toEqual([0.05, 0, 0, -0.05, 10, 400]);
  });

  it("draws only the layers marked visible", () => {
    const layout = fixtureLayout();
    const styles = new Map(
      layout.layers.map((layer) => [
        layer.key,
        { fill: "#ff0000", frame: "#ff0000", name: layer.key, fromPdk: false },
      ]),
    );

    const all = recordingContext();
    drawLayout(
      all.ctx,
      layout,
      { scale: 0.05, tx: 0, ty: 0 },
      new Set(layout.layers.map((layer) => layer.key)),
      styles,
      800,
      600,
      1,
    );

    const none = recordingContext();
    drawLayout(none.ctx, layout, { scale: 0.05, tx: 0, ty: 0 }, new Set(), styles, 800, 600, 1);

    const fills = (source: RecordingContext) =>
      source.calls.filter((call) => call.op === "fill").length;
    expect(fills(all)).toBeGreaterThan(0);
    expect(fills(none)).toBe(0);
  });

  it("emits a subpath per polygon and strokes paths with their own width", () => {
    const { ctx, calls } = recordingContext();
    const layout = fixtureLayout();
    const styles = new Map(
      layout.layers.map((layer) => [
        layer.key,
        { fill: "#00ff00", frame: "#00ff00", name: layer.key, fromPdk: false },
      ]),
    );
    drawLayout(
      ctx,
      layout,
      { scale: 0.05, tx: 0, ty: 0 },
      new Set(["68/20", "67/20"]),
      styles,
      800,
      600,
      1,
    );

    // 8 flattened squares -> 8 moveTo for the polygon layer, plus one per path.
    expect(calls.filter((call) => call.op === "closePath")).toHaveLength(8);
    expect(calls.filter((call) => call.op === "stroke").length).toBeGreaterThanOrEqual(8);
  });
});

describe("drawLayout — the gallery's real blocks", () => {
  const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..", "..", "..");
  const hasBlocks = existsSync(resolve(REPO_ROOT, "blocks"));

  function loadBlock(slug: string, file: string) {
    const buffer = readFileSync(resolve(REPO_ROOT, "blocks", slug, "output", file));
    return flattenGds(
      parseGds(buffer.buffer.slice(buffer.byteOffset, buffer.byteOffset + buffer.byteLength)),
    );
  }

  /** Runs the full real pipeline: bytes -> flatten -> style -> canvas ops. */
  function renderBlock(slug: string, file: string, pdkFamily: "sky130" | "gf180mcu") {
    const layout = loadBlock(slug, file);
    const styles = new Map(
      layout.layers.map((layer) => [
        layer.key,
        resolveLayerStyle(layer.layer, layer.datatype, pdkFamily),
      ]),
    );
    const view = fitView(layout.bbox, 900, 600);
    const recorded = recordingContext();
    drawLayout(
      recorded.ctx,
      layout,
      view,
      new Set(layout.layers.map((layer) => layer.key)),
      styles,
      900,
      600,
      1,
    );
    return { layout, styles, ...recorded };
  }

  it.skipIf(!hasBlocks)("draws gf180-bandgap with gf180mcu's own layer colors", () => {
    const { layout, calls } = renderBlock("gf180-bandgap", "bandgap_top.gds", "gf180mcu");

    // One fill per layer that has polygons; 8624 subpaths for its geometry.
    const layersWithPolygons = layout.layers.filter((layer) => layer.polygons.length > 0).length;
    expect(calls.filter((call) => call.op === "fill")).toHaveLength(layersWithPolygons);
    expect(calls.filter((call) => call.op === "closePath")).toHaveLength(8624);

    // Metal1 (34/0) must be gf180mcu's Metal1 color, not sky130's 34/0.
    expect(resolveLayerStyle(34, 0, "gf180mcu")).toMatchObject({
      name: "Metal1",
      fill: "#eddd07",
      fromPdk: true,
    });
    expect(
      layout.layers.filter((layer) => !resolveLayerStyle(layer.layer, layer.datatype, "gf180mcu").fromPdk),
    ).toHaveLength(0);
  });

  it.skipIf(!hasBlocks)("draws sky130-bandgap with sky130's own layer colors", () => {
    const { layout, calls } = renderBlock(
      "sky130-bandgap",
      "bandgap_core_routed.gds",
      "sky130",
    );

    expect(calls.filter((call) => call.op === "closePath").length).toBeGreaterThan(3000);
    expect(calls.some((call) => call.op === "fillText")).toBe(true); // 11 pin labels
    expect(resolveLayerStyle(68, 20, "sky130")).toMatchObject({
      name: "met1.drawing",
      fill: "#0000ff",
      fromPdk: true,
    });
    // Every layer this block draws is covered by sky130A.lyp except 83/20,
    // which neither the PDK's layer properties nor the render pipeline names
    // (`layout.json` labels it `layer_83_20`) — so it exercises the
    // deterministic fallback on real data rather than being mis-styled.
    const uncovered = layout.layers.filter(
      (layer) => !resolveLayerStyle(layer.layer, layer.datatype, "sky130").fromPdk,
    );
    expect(uncovered.map((layer) => layer.key)).toEqual(["83/20"]);
    expect(resolveLayerStyle(83, 20, "sky130")).toMatchObject({
      name: "Layer 83/20",
      fromPdk: false,
    });
  });
});

describe("GdsCanvas", () => {
  let getContextSpy: ReturnType<typeof vi.spyOn>;
  let recording: RecordingContext;

  beforeEach(() => {
    recording = recordingContext();
    getContextSpy = vi.spyOn(HTMLCanvasElement.prototype, "getContext");
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    getContextSpy.mockImplementation(((..._args: unknown[]) => recording.ctx) as any);
    // jsdom reports a zero-sized layout for everything; give the viewer a
    // surface so its fit-to-view math has something to work with.
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
      width: 800,
      height: 600,
      top: 0,
      left: 0,
      right: 800,
      bottom: 600,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    } as DOMRect);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  function stubFetch(response: Partial<Response> & { arrayBuffer?: () => Promise<ArrayBuffer> }) {
    const fetchMock = vi.fn().mockResolvedValue(response as Response);
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  function stubFixtureFetch() {
    return stubFetch({
      ok: true,
      status: 200,
      statusText: "OK",
      arrayBuffer: async () => buildTwoLevelFixture(),
    });
  }

  it("fetches the block's GDS from the same origin and reports what it parsed", async () => {
    const fetchMock = stubFixtureFetch();
    render(<GdsCanvas fileUrl="/blocks/demo/demo.gds" displayName="Demo" pdkFamily="sky130" />);

    expect(screen.getByTestId("gds-canvas-status")).toHaveTextContent("Loading Demo layout");

    const summary = await screen.findByTestId("gds-canvas-summary");
    expect(fetchMock).toHaveBeenCalledWith("/blocks/demo/demo.gds", expect.anything());
    expect(fetchMock.mock.calls[0][0]).not.toMatch(/^https?:/);
    expect(summary).toHaveTextContent("TOP");
    expect(summary).toHaveTextContent("17 shapes");
    expect(summary).toHaveTextContent("4 layers");
  });

  it("names layers with the PDK's own layer names", async () => {
    stubFixtureFetch();
    render(<GdsCanvas fileUrl="/blocks/demo/demo.gds" displayName="Demo" pdkFamily="sky130" />);

    expect(await screen.findByRole("checkbox", { name: /met1\.drawing/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /li1\.drawing/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /nwell\.drawing/i })).toBeChecked();
  });

  it("prefers the block's own render-derived layer names when supplied", async () => {
    stubFixtureFetch();
    render(
      <GdsCanvas
        fileUrl="/blocks/demo/demo.gds"
        displayName="Demo"
        pdkFamily="sky130"
        layerNames={{ "68/20": "met1 (block)" }}
      />,
    );

    expect(await screen.findByRole("checkbox", { name: /met1 \(block\)/ })).toBeChecked();
  });

  it("falls back to numeric layer labels when the PDK family is unknown", async () => {
    stubFixtureFetch();
    render(<GdsCanvas fileUrl="/blocks/demo/demo.gds" displayName="Demo" />);

    expect(await screen.findByRole("checkbox", { name: /Layer 68\/20/ })).toBeChecked();
  });

  it("re-draws with a layer hidden when its checkbox is unchecked", async () => {
    stubFixtureFetch();
    render(<GdsCanvas fileUrl="/blocks/demo/demo.gds" displayName="Demo" pdkFamily="sky130" />);

    const met1 = await screen.findByRole("checkbox", { name: /met1\.drawing/ });
    await waitFor(() => expect(recording.calls.some((call) => call.op === "fill")).toBe(true));

    const before = recording.calls.filter((call) => call.op === "closePath").length;
    recording.calls.length = 0;
    fireEvent.click(met1);

    expect(met1).not.toBeChecked();
    await waitFor(() => expect(recording.calls.length).toBeGreaterThan(0));
    const after = recording.calls.filter((call) => call.op === "closePath").length;
    expect(after).toBeLessThan(before);
  });

  it("hides and restores every layer with the None / All buttons", async () => {
    stubFixtureFetch();
    render(<GdsCanvas fileUrl="/blocks/demo/demo.gds" displayName="Demo" pdkFamily="sky130" />);

    await screen.findByTestId("gds-canvas-layers");
    fireEvent.click(screen.getByRole("button", { name: "None" }));
    for (const box of screen.getAllByRole("checkbox")) expect(box).not.toBeChecked();

    fireEvent.click(screen.getByRole("button", { name: "All" }));
    for (const box of screen.getAllByRole("checkbox")) expect(box).toBeChecked();
  });

  it("zooms in and out about the view centre, and restores the fit", async () => {
    stubFixtureFetch();
    render(<GdsCanvas fileUrl="/blocks/demo/demo.gds" displayName="Demo" pdkFamily="sky130" />);
    await screen.findByTestId("gds-canvas-summary");

    const scaleOf = (source: RecordingContext) => {
      const call = [...source.calls]
        .reverse()
        .find((entry) => entry.op === "setTransform" && (entry.args[3] as number) < 0);
      return call ? (call.args[0] as number) : 0;
    };

    await waitFor(() => expect(scaleOf(recording)).toBeGreaterThan(0));
    const fitted = scaleOf(recording);

    fireEvent.click(screen.getByRole("button", { name: "Zoom in" }));
    await waitFor(() => expect(scaleOf(recording)).toBeGreaterThan(fitted));
    const zoomed = scaleOf(recording);

    fireEvent.click(screen.getByRole("button", { name: "Zoom out" }));
    await waitFor(() => expect(scaleOf(recording)).toBeLessThan(zoomed));

    fireEvent.click(screen.getByRole("button", { name: "Fit" }));
    await waitFor(() => expect(scaleOf(recording)).toBeCloseTo(fitted, 6));
  });

  it("fits the layout to the measured surface, not to a zero-sized one", async () => {
    // Regression guard: the surface is measured in an effect that runs in the
    // same commit as the one that fits the view, so a fit that doesn't wait
    // for a non-zero measurement silently locks in scale = 1 (the whole
    // layout drawn 20x too large, off-screen) with no later correction.
    // Fixture bbox is 10000 x 10500 dbu into an 800 x 600 surface:
    // min(800/10000, 600/10500) * 0.92.
    stubFixtureFetch();
    render(<GdsCanvas fileUrl="/blocks/demo/demo.gds" displayName="Demo" pdkFamily="sky130" />);
    await screen.findByTestId("gds-canvas-summary");

    await waitFor(() => {
      const worldTransform = [...recording.calls]
        .reverse()
        .find((call) => call.op === "setTransform" && (call.args[3] as number) < 0);
      expect(worldTransform?.args[0]).toBeCloseTo((600 / 10500) * 0.92, 9);
    });
  });

  it("pans on pointer drag", async () => {
    stubFixtureFetch();
    render(<GdsCanvas fileUrl="/blocks/demo/demo.gds" displayName="Demo" pdkFamily="sky130" />);
    const surface = await screen.findByTestId("gds-canvas-surface");

    const translationOf = (source: RecordingContext) => {
      const call = [...source.calls]
        .reverse()
        .find((entry) => entry.op === "setTransform" && (entry.args[3] as number) < 0);
      return call ? (call.args[4] as number) : 0;
    };

    await waitFor(() => expect(recording.calls.length).toBeGreaterThan(0));
    const before = translationOf(recording);

    fireEvent.pointerDown(surface, { clientX: 100, clientY: 100, pointerId: 1 });
    fireEvent.pointerMove(surface, { clientX: 160, clientY: 100, pointerId: 1 });
    fireEvent.pointerUp(surface, { clientX: 160, clientY: 100, pointerId: 1 });

    await waitFor(() => expect(translationOf(recording)).toBeCloseTo(before + 60, 6));
  });

  it("shows a readable error (and points at the download) when the file can't be fetched", async () => {
    stubFetch({ ok: false, status: 404, statusText: "Not Found" });
    render(<GdsCanvas fileUrl="/blocks/demo/missing.gds" displayName="Demo" />);

    const error = await screen.findByTestId("gds-canvas-error");
    expect(error).toHaveTextContent("404");
    expect(error).toHaveTextContent("Downloads section");
  });

  it("shows a readable error when the file isn't a GDS stream", async () => {
    stubFetch({
      ok: true,
      status: 200,
      statusText: "OK",
      arrayBuffer: async () => new TextEncoder().encode("<html>nope</html>").buffer,
    });
    render(<GdsCanvas fileUrl="/blocks/demo/demo.gds" displayName="Demo" />);

    expect(await screen.findByTestId("gds-canvas-error")).toHaveTextContent("not a GDSII stream");
  });
});
