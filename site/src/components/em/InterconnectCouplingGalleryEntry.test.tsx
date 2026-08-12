// @vitest-environment jsdom
/**
 * Render tests for `InterconnectCouplingGalleryEntry` (issue #842) --
 * verifies the title/description (including the field-solver-epic
 * cross-references this issue's acceptance criteria require), the wrapped
 * `InterconnectCouplingResult`, and that the `ProvenancePanel` appears with
 * the klayout-tools source-repo/commit row once the export fetch resolves.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { InterconnectCouplingGalleryEntry } from "./InterconnectCouplingGalleryEntry";
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
  frames: [{ label: "VPWR driven 1V, VGND 0V", frequency_hz: null, scalar: [0, 0.3, 1, 0.6] }],
  capacitance: {
    conductors: ["vgnd", "vpwr"],
    matrix_farad: [
      [2.6074097610214034e-16, -6.620723588287389e-18],
      [-6.620723588287389e-18, 2.607409761021375e-16],
    ],
    max_rel_asymmetry: 0,
  },
  provenance: {
    generator: {
      repo: "https://github.com/rjwalters/geode-fem",
      commit: "90759f103fdbdc42e47b1941ccd8d0e0b031c4e6",
      version: "0.3.0",
    },
    geometry: {
      fixture: "blocks/sky130_fd_sc_hd__nand2_2/output/sky130_fd_sc_hd__nand2_2.gds",
      source_repo: "https://github.com/2AMLogic/klayout-tools",
      source_commit: "c4e43f742bda57a29cdecfe7a4a71aacd86cc6f4",
      source_block: "sky130_fd_sc_hd__nand2_2",
    },
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

describe("InterconnectCouplingGalleryEntry", () => {
  it("renders the title, epic cross-references, and the wrapped result before provenance loads", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    render(<InterconnectCouplingGalleryEntry />);
    expect(screen.getByText("On-chip power/ground coupling — a real fleet block")).toBeInTheDocument();
    expect(screen.getByText("#701")).toBeInTheDocument();
    expect(screen.getByText("#708")).toBeInTheDocument();
    expect(screen.getByText("#709")).toBeInTheDocument();
    expect(screen.getByTestId("interconnect-coupling-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("em-provenance-panel")).not.toBeInTheDocument();
  });

  it("shows the provenance panel with the klayout-tools source commit once the export fetch resolves", async () => {
    vi.stubGlobal("fetch", mockFetchOk(fixture));
    render(<InterconnectCouplingGalleryEntry />);

    await waitFor(() => expect(screen.getByTestId("interconnect-coupling-result")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByTestId("em-provenance-panel")).toBeInTheDocument());
    expect(screen.getByTestId("em-provenance-commit")).toHaveTextContent("90759f103fdb");
    expect(screen.getByTestId("em-provenance-source-commit")).toHaveTextContent("c4e43f742bda");
  });

  it("does not show a provenance panel if the export fetch fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve({ ok: false, status: 404 } as Response)),
    );
    render(<InterconnectCouplingGalleryEntry />);
    await waitFor(() => expect(screen.getByTestId("interconnect-coupling-error")).toBeInTheDocument());
    expect(screen.queryByTestId("em-provenance-panel")).not.toBeInTheDocument();
  });
});
