// @vitest-environment jsdom
/**
 * Render tests for `InterconnectCouplingResult` (issue #842) against a
 * trimmed, real-shaped `capacitance` export fixture (mirroring the
 * committed `examples/em/interconnect_coupling/interconnect_coupling.em-export.json`'s
 * structure -- a single driven field frame, no `s_parameters` at all).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { InterconnectCouplingResult } from "./InterconnectCouplingResult";
import type { EmSiteExport } from "./types";

const fixture: EmSiteExport = {
  schema_version: 1,
  benchmark: "interconnect_coupling",
  mesh: {
    vertices: [
      [-1, -1, 0],
      [1, -1, 0],
      [1, 1, 0],
      [-1, 1, 0],
    ],
    cells: [[0, 1, 2, 3]],
  },
  frames: [
    {
      label: "VPWR driven 1V, VGND 0V (mid-length cross-section)",
      frequency_hz: null,
      scalar: [0, 0.3, 1, 0.6],
    },
  ],
  capacitance: {
    conductors: ["vgnd", "vpwr"],
    matrix_farad: [
      [2.6074097610214034e-16, -6.620723588287389e-18],
      [-6.620723588287389e-18, 2.607409761021375e-16],
    ],
    matrix_flux_diag_farad: [null, null],
    max_rel_asymmetry: 0,
    oracle: {
      description: "sky130 open-PDK tech-LEF area+edge parasitic-capacitance model.",
      source: "sky130_fd_sc_hd__nom.tlef LAYER met1 (public, Apache-2.0 sky130A open PDK)",
      conductor: "vgnd",
      predicted_self_capacitance_farad: 2.5401187360000004e-16,
      fem_self_capacitance_farad: 2.6074097610214034e-16,
      rel_err: 0.026491291161990404,
    },
  },
  provenance: {
    generator: {
      repo: "https://github.com/rjwalters/geode-fem",
      license: "MIT",
      commit: "90759f103fdbdc42e47b1941ccd8d0e0b031c4e6",
      version: "0.3.0",
      backend: "burn::backend::NdArray<f64, i32> (CPU)",
      build_profile: "release",
    },
    geometry: {
      fixture: "blocks/sky130_fd_sc_hd__nand2_2/output/sky130_fd_sc_hd__nand2_2.gds",
      fixture_sha256: "ffd92048dda55ee84e4f9e401e1c4ca539233587d74a2ce665e4e9d86c550682",
      description: "Real met1 VGND/VPWR power-rail pair of sky130_fd_sc_hd__nand2_2.",
      source_repo: "https://github.com/2AMLogic/klayout-tools",
      source_commit: "c4e43f742bda57a29cdecfe7a4a71aacd86cc6f4",
      source_block: "sky130_fd_sc_hd__nand2_2",
      source_layer: "met1 (68/20)",
    },
    solve_parameters: { rail_width_um: 0.48, rail_length_um: 2.3, gap_um: 2.24, eps_r: 4.0 },
    generated_at: "2026-08-12T07:04:00Z",
  },
};

function mockFetchOk(payload: EmSiteExport) {
  return vi.fn(() =>
    Promise.resolve({
      ok: true,
      json: () => Promise.resolve(payload),
    } as Response),
  );
}

function makeFakeGL(): WebGL2RenderingContext {
  const overrides: Record<string, (...args: unknown[]) => unknown> = {
    createShader: () => ({}),
    createProgram: () => ({}),
    createBuffer: () => ({}),
    getShaderParameter: () => true,
    getProgramParameter: () => true,
    getAttribLocation: () => 0,
    getUniformLocation: () => ({}),
    getExtension: () => ({}),
  };
  const handler: ProxyHandler<Record<string, unknown>> = {
    get(target, prop) {
      if (typeof prop === "string" && prop in overrides) return overrides[prop];
      if (typeof prop === "string" && /^[A-Z][A-Z0-9_]*$/.test(prop)) return 1;
      return target[prop as string] ?? (() => undefined);
    },
  };
  return new Proxy({}, handler) as unknown as WebGL2RenderingContext;
}

beforeEach(() => {
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ((..._args: unknown[]) => makeFakeGL()) as any,
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("InterconnectCouplingResult", () => {
  it("shows a loading state before the export fetch resolves", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    render(<InterconnectCouplingResult />);
    expect(screen.getByTestId("interconnect-coupling-loading")).toBeInTheDocument();
  });

  it("fetches the default staged export URL", async () => {
    const fetchMock = mockFetchOk(fixture);
    vi.stubGlobal("fetch", fetchMock);
    render(<InterconnectCouplingResult />);
    await waitFor(() => expect(screen.getByTestId("interconnect-coupling-result")).toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledWith(
      "/em/interconnect_coupling/interconnect_coupling.em-export.json",
    );
  });

  it("renders the field viewer and the capacitance matrix from the fetched export", async () => {
    vi.stubGlobal("fetch", mockFetchOk(fixture));
    render(<InterconnectCouplingResult />);

    await waitFor(() => expect(screen.getByTestId("interconnect-coupling-result")).toBeInTheDocument());
    expect(screen.getByTestId("field-viewer-canvas")).toBeInTheDocument();
    // Both real values happen to land in the same "aF" engineering-notation
    // bucket (260.741 aF self, -6.621 aF mutual) -- standard SI bucketing
    // (`[1, 1000)` of the chosen power-of-1000 unit), not a display bug.
    expect(screen.getByTestId("interconnect-coupling-self-cap-vgnd")).toHaveTextContent("260.741 aF");
    expect(screen.getByTestId("interconnect-coupling-self-cap-vpwr")).toHaveTextContent("260.741 aF");
    expect(screen.getByTestId("interconnect-coupling-mutual-cap")).toHaveTextContent("-6.621 aF");
  });

  it("renders the PDK oracle cross-check with its relative error", async () => {
    vi.stubGlobal("fetch", mockFetchOk(fixture));
    render(<InterconnectCouplingResult />);
    await waitFor(() => expect(screen.getByTestId("interconnect-coupling-result")).toBeInTheDocument());

    expect(screen.getByTestId("interconnect-coupling-oracle")).toHaveTextContent("2.6% rel. err.");
  });

  it("renders an error state when the export fetch fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve({ ok: false, status: 404 } as Response)),
    );
    render(<InterconnectCouplingResult />);
    await waitFor(() => expect(screen.getByTestId("interconnect-coupling-error")).toBeInTheDocument());
  });

  it("calls onLoad with the full parsed export once the fetch resolves", async () => {
    vi.stubGlobal("fetch", mockFetchOk(fixture));
    const onLoad = vi.fn();
    render(<InterconnectCouplingResult onLoad={onLoad} />);
    await waitFor(() => expect(screen.getByTestId("interconnect-coupling-result")).toBeInTheDocument());
    expect(onLoad).toHaveBeenCalledTimes(1);
    expect(onLoad).toHaveBeenCalledWith(fixture);
  });

  it("does not call onLoad when the export fetch fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve({ ok: false, status: 404 } as Response)),
    );
    const onLoad = vi.fn();
    render(<InterconnectCouplingResult onLoad={onLoad} />);
    await waitFor(() => expect(screen.getByTestId("interconnect-coupling-error")).toBeInTheDocument());
    expect(onLoad).not.toHaveBeenCalled();
  });
});
