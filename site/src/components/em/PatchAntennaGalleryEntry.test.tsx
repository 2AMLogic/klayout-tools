// @vitest-environment jsdom
/**
 * Render tests for `PatchAntennaGalleryEntry` (issue #851) — verifies the
 * title/description, the wrapped `PatchAntennaResult`, and that the
 * `ProvenancePanel` appears once the export fetch resolves (sourced from
 * `PatchAntennaResult`'s `onLoad` callback, not a second fetch).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { PatchAntennaGalleryEntry } from "./PatchAntennaGalleryEntry";
import type { EmSiteExport } from "./types";

const fixture: EmSiteExport = {
  schema_version: 1,
  benchmark: "patch_antenna",
  mesh: {
    vertices: [
      [-1, -1, 0],
      [1, -1, 0],
      [1, 1, 0],
      [-1, 1, 0],
    ],
    cells: [[0, 1, 2, 3]],
  },
  frames: [{ label: "2.275 GHz", frequency_hz: 2_274_530_000, scalar: [0, 0.5, 1, 0.75] }],
  s_parameters: {
    ports: ["p1"],
    reference_impedance_ohm: 50,
    points: [{ frequency_hz: 2_000_000_000, s11_db: -0.39 }],
  },
  provenance: {
    generator: {
      repo: "https://github.com/rjwalters/geode-fem",
      commit: "90759f103fdbdc42e47b1941ccd8d0e0b031c4e6",
      version: "0.3.0",
    },
    geometry: { fixture: "tests/fixtures/patch_2g4.msh" },
    generated_at: "2026-08-12T04:15:40Z",
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
  vi.spyOn(SVGElement.prototype, "getBoundingClientRect").mockReturnValue({
    x: 0,
    y: 0,
    width: 640,
    height: 260,
    top: 0,
    left: 0,
    right: 640,
    bottom: 260,
    toJSON: () => ({}),
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("PatchAntennaGalleryEntry", () => {
  it("renders the title, description, and the wrapped result before provenance loads", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    render(<PatchAntennaGalleryEntry />);
    expect(screen.getByText("Patch antenna — S11 + near field")).toBeInTheDocument();
    expect(screen.getByTestId("patch-antenna-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("em-provenance-panel")).not.toBeInTheDocument();
  });

  it("shows the provenance panel once the export fetch resolves", async () => {
    vi.stubGlobal("fetch", mockFetchOk(fixture));
    render(<PatchAntennaGalleryEntry />);

    await waitFor(() => expect(screen.getByTestId("patch-antenna-result")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByTestId("em-provenance-panel")).toBeInTheDocument());
    expect(screen.getByTestId("em-provenance-commit")).toHaveTextContent("90759f103fdb");
  });

  it("does not show a provenance panel if the export fetch fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve({ ok: false, status: 404 } as Response)),
    );
    render(<PatchAntennaGalleryEntry />);
    await waitFor(() => expect(screen.getByTestId("patch-antenna-error")).toBeInTheDocument());
    expect(screen.queryByTestId("em-provenance-panel")).not.toBeInTheDocument();
  });
});
