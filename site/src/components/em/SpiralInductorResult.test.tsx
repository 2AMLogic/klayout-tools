// @vitest-environment jsdom
/**
 * Render/interaction tests for `SpiralInductorResult` (issue #877) against a
 * trimmed, real-shaped export fixture (mirroring the committed
 * `examples/em/spiral_inductor/spiral_inductor.em-export.json`'s structure --
 * 1 field frame, a multi-point S-parameter sweep on a *different* frequency
 * grid than the frame, plus the `l_nh`/`r_ohm`/`q` extras this benchmark
 * family carries -- see `PatchAntennaResult.test.tsx` for the shared
 * index-misalignment gotcha this mirrors).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { SpiralInductorResult } from "./SpiralInductorResult";
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
    points: [
      { frequency_hz: 100_000_000, s11_db: -0.08, l_nh: 1.86, r_ohm: 0.23, q: 5.09 },
      { frequency_hz: 1_000_000_000, s11_db: -0.5, l_nh: 1.9, r_ohm: 0.45, q: 26.5 },
      { frequency_hz: 5_000_000_000, s11_db: -2.1, l_nh: 2.1, r_ohm: 1.9, q: 34.8 },
      { frequency_hz: 20_000_000_000, s11_db: -6.4, l_nh: 3.4, r_ohm: 8.2, q: 12.1 },
      { frequency_hz: 40_000_000_000, s11_db: -1.2, l_nh: -0.5, r_ohm: 15.6, q: -0.8 },
    ],
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
      fixture: "tests/fixtures/spiral_3p5.msh",
      fixture_sha256: "c9707fb9bd5f3f484b845e96b90cf53f0b196794c5793f7e4f682bf97e101589",
      description: "Port-driven 3.5-turn generic square spiral inductor.",
    },
    solve_parameters: { port_resistance_ohm: 50, conductor_model: "leontovich_good_conductor" },
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

describe("SpiralInductorResult", () => {
  it("shows a loading state before the export fetch resolves", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    render(<SpiralInductorResult />);
    expect(screen.getByTestId("spiral-inductor-loading")).toBeInTheDocument();
  });

  it("renders the field viewer, a single-frame note, and the S11 plot from the fetched export", async () => {
    vi.stubGlobal("fetch", mockFetchOk(fixture));
    render(<SpiralInductorResult />);

    await waitFor(() => expect(screen.getByTestId("spiral-inductor-result")).toBeInTheDocument());
    expect(screen.getByTestId("field-viewer-canvas")).toBeInTheDocument();
    expect(screen.getByTestId("field-frame-note")).toHaveTextContent("1.000 GHz");

    await waitFor(() => expect(screen.getByTestId("waveform-plot")).toBeInTheDocument());
    expect(screen.getByTestId("waveform-plot").querySelectorAll("path").length).toBeGreaterThan(0);
  });

  it("fetches the default staged export URL", async () => {
    const fetchMock = mockFetchOk(fixture);
    vi.stubGlobal("fetch", fetchMock);
    render(<SpiralInductorResult />);
    await waitFor(() => expect(screen.getByTestId("spiral-inductor-result")).toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledWith("/em/spiral_inductor/spiral_inductor.em-export.json");
  });

  it("defaults the frequency slider to the first S-parameter sweep point and shows L/Q", async () => {
    vi.stubGlobal("fetch", mockFetchOk(fixture));
    render(<SpiralInductorResult />);
    await waitFor(() => expect(screen.getByTestId("spiral-inductor-result")).toBeInTheDocument());

    expect(screen.getByLabelText("S-parameter frequency")).toHaveValue("0");
    const label = screen.getByTestId("spiral-inductor-frequency-label");
    expect(label).toHaveTextContent("-0.08 dB");
    expect(label).toHaveTextContent("L 1.86 nH");
    expect(label).toHaveTextContent("Q 5.1");
  });

  it("moving the frequency slider updates the L/Q readout label and the plot cursor together", async () => {
    vi.stubGlobal("fetch", mockFetchOk(fixture));
    render(<SpiralInductorResult />);
    await waitFor(() => expect(screen.getByTestId("spiral-inductor-result")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByTestId("waveform-plot")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText("S-parameter frequency"), { target: { value: "2" } });

    await waitFor(() =>
      expect(screen.getByTestId("spiral-inductor-frequency-label")).toHaveTextContent("Q 34.8"),
    );
    expect(screen.getByTestId("field-frame-note")).toHaveTextContent("1.000 GHz");
    await waitFor(() => expect(screen.getByTestId("cursor-readout")).toBeInTheDocument());
  });

  it("renders an error state when the export fetch fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve({ ok: false, status: 404 } as Response)),
    );
    render(<SpiralInductorResult />);
    await waitFor(() => expect(screen.getByTestId("spiral-inductor-error")).toBeInTheDocument());
  });

  it("calls onLoad with the full parsed export once the fetch resolves", async () => {
    vi.stubGlobal("fetch", mockFetchOk(fixture));
    const onLoad = vi.fn();
    render(<SpiralInductorResult onLoad={onLoad} />);
    await waitFor(() => expect(screen.getByTestId("spiral-inductor-result")).toBeInTheDocument());
    expect(onLoad).toHaveBeenCalledTimes(1);
    expect(onLoad).toHaveBeenCalledWith(fixture);
  });

  it("does not call onLoad when the export fetch fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve({ ok: false, status: 404 } as Response)),
    );
    const onLoad = vi.fn();
    render(<SpiralInductorResult onLoad={onLoad} />);
    await waitFor(() => expect(screen.getByTestId("spiral-inductor-error")).toBeInTheDocument());
    expect(onLoad).not.toHaveBeenCalled();
  });
});
