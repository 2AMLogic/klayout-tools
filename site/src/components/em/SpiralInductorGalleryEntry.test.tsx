// @vitest-environment jsdom
/**
 * Render tests for `SpiralInductorGalleryEntry` (issue #877) — verifies the
 * title/description, the wrapped `SpiralInductorResult`, and that the
 * `ProvenancePanel` appears once the export fetch resolves (sourced from
 * `SpiralInductorResult`'s `onLoad` callback, not a second fetch). Mirrors
 * `PatchAntennaGalleryEntry.test.tsx`.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { SpiralInductorGalleryEntry } from "./SpiralInductorGalleryEntry";
import type { EmSiteExport } from "./types";

const fixture: EmSiteExport = {
  schema_version: 1,
  benchmark: "spiral_inductor",
  mesh: {
    vertices: [
      [-1, -1, 0],
      [1, -1, 0],
      [1, 1, 0],
      [-1, 1, 0],
    ],
    cells: [[0, 1, 2, 3]],
  },
  frames: [{ label: "1.000 GHz", frequency_hz: 1_000_000_000, scalar: [0, 0.5, 1, 0.75] }],
  s_parameters: {
    ports: ["p1"],
    reference_impedance_ohm: 50,
    points: [{ frequency_hz: 1_000_000_000, s11_db: -0.5, l_nh: 1.9, r_ohm: 0.45, q: 26.5 }],
  },
  provenance: {
    generator: {
      repo: "https://github.com/rjwalters/geode-fem",
      commit: "90759f103fdbdc42e47b1941ccd8d0e0b031c4e6",
      version: "0.3.0",
    },
    geometry: { fixture: "tests/fixtures/spiral_3p5.msh" },
    generated_at: "2026-08-12T06:08:00Z",
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

describe("SpiralInductorGalleryEntry", () => {
  it("renders the title, description, and the wrapped result before provenance loads", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    render(<SpiralInductorGalleryEntry />);
    expect(screen.getByText("Spiral inductor — L/Q")).toBeInTheDocument();
    expect(screen.getByTestId("spiral-inductor-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("em-provenance-panel")).not.toBeInTheDocument();
  });

  it("shows the provenance panel once the export fetch resolves", async () => {
    vi.stubGlobal("fetch", mockFetchOk(fixture));
    render(<SpiralInductorGalleryEntry />);

    await waitFor(() => expect(screen.getByTestId("spiral-inductor-result")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByTestId("em-provenance-panel")).toBeInTheDocument());
    expect(screen.getByTestId("em-provenance-commit")).toHaveTextContent("90759f103fdb");
  });

  it("does not show a provenance panel if the export fetch fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve({ ok: false, status: 404 } as Response)),
    );
    render(<SpiralInductorGalleryEntry />);
    await waitFor(() => expect(screen.getByTestId("spiral-inductor-error")).toBeInTheDocument());
    expect(screen.queryByTestId("em-provenance-panel")).not.toBeInTheDocument();
  });
});
