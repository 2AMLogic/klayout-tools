// @vitest-environment jsdom
/**
 * Render/interaction tests for `PatchAntennaResult` (issue #850) against a
 * trimmed, real-shaped export fixture (mirroring the committed
 * `examples/em/patch_antenna/patch_antenna.em-export.json`'s structure --
 * 1 field frame, a multi-point S-parameter sweep on a *different* frequency
 * grid than the frame -- the exact index-misalignment gotcha this issue's
 * implementation has to handle correctly).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { PatchAntennaResult } from "./PatchAntennaResult";
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
    points: [
      { frequency_hz: 2_000_000_000, s11_db: -0.39 },
      { frequency_hz: 2_100_000_000, s11_db: -0.7 },
      { frequency_hz: 2_200_000_000, s11_db: -2.71 },
      { frequency_hz: 2_300_000_000, s11_db: -6.0 },
      { frequency_hz: 2_400_000_000, s11_db: -3.2 },
    ],
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

describe("PatchAntennaResult", () => {
  it("shows a loading state before the export fetch resolves", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    render(<PatchAntennaResult />);
    expect(screen.getByTestId("patch-antenna-loading")).toBeInTheDocument();
  });

  it("renders the field viewer, a single-frame note, and the S11 plot from the fetched export", async () => {
    vi.stubGlobal("fetch", mockFetchOk(fixture));
    render(<PatchAntennaResult />);

    await waitFor(() => expect(screen.getByTestId("patch-antenna-result")).toBeInTheDocument());
    expect(screen.getByTestId("field-viewer-canvas")).toBeInTheDocument();
    expect(screen.getByTestId("field-frame-note")).toHaveTextContent("2.275 GHz");

    await waitFor(() => expect(screen.getByTestId("waveform-plot")).toBeInTheDocument());
    expect(screen.getByTestId("waveform-plot").querySelectorAll("path").length).toBeGreaterThan(0);
  });

  it("fetches the default staged export URL", async () => {
    const fetchMock = mockFetchOk(fixture);
    vi.stubGlobal("fetch", fetchMock);
    render(<PatchAntennaResult />);
    await waitFor(() => expect(screen.getByTestId("patch-antenna-result")).toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledWith("/em/patch_antenna/patch_antenna.em-export.json");
  });

  it("defaults the frequency slider to the first S-parameter sweep point", async () => {
    vi.stubGlobal("fetch", mockFetchOk(fixture));
    render(<PatchAntennaResult />);
    await waitFor(() => expect(screen.getByTestId("patch-antenna-result")).toBeInTheDocument());

    expect(screen.getByLabelText("S-parameter frequency")).toHaveValue("0");
    expect(screen.getByTestId("patch-antenna-frequency-label")).toHaveTextContent("-0.39 dB");
  });

  it("moving the frequency slider updates the S11 readout label and the plot cursor together", async () => {
    vi.stubGlobal("fetch", mockFetchOk(fixture));
    render(<PatchAntennaResult />);
    await waitFor(() => expect(screen.getByTestId("patch-antenna-result")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByTestId("waveform-plot")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText("S-parameter frequency"), { target: { value: "3" } });

    await waitFor(() =>
      expect(screen.getByTestId("patch-antenna-frequency-label")).toHaveTextContent("-6.00 dB"),
    );
    // The field overlay stays pinned to the export's single available frame
    // -- the slider does not throw or index past `frames.length`.
    expect(screen.getByTestId("field-frame-note")).toHaveTextContent("2.275 GHz");
    await waitFor(() => expect(screen.getByTestId("cursor-readout")).toBeInTheDocument());
  });

  it("clicking the S11 plot moves the frequency slider back in sync (nearest sweep point)", async () => {
    vi.stubGlobal("fetch", mockFetchOk(fixture));
    render(<PatchAntennaResult />);
    await waitFor(() => expect(screen.getByTestId("waveform-plot")).toBeInTheDocument());

    // Click near the right edge of the plot -- closest to the last sweep point.
    fireEvent.click(screen.getByTestId("waveform-plot"), { clientX: 630, clientY: 100 });

    await waitFor(() => expect(screen.getByLabelText("S-parameter frequency")).toHaveValue("4"));
  });

  it("renders an error state when the export fetch fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve({ ok: false, status: 404 } as Response)),
    );
    render(<PatchAntennaResult />);
    await waitFor(() => expect(screen.getByTestId("patch-antenna-error")).toBeInTheDocument());
  });
});
