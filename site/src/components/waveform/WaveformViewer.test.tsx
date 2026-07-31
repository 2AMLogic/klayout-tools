// @vitest-environment jsdom
/**
 * Interaction tests for the waveform viewer against a fixture `signals`
 * payload — covers zoom, cursor placement/readout, per-node visibility
 * toggles, and PVT corner overlay/selection (issue #100's test plan).
 *
 * Uses `@testing-library/react` (added by this issue; the rest of the site
 * suite is pure-logic vitest tests with no DOM dependency, so this file
 * opts into the `jsdom` environment per-file rather than switching the
 * whole suite's default).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { WaveformViewer } from "./WaveformViewer";
import type { LayoutSignals } from "@/data/types";
import type { WaveformData } from "./types";

const ttWaveform: WaveformData = {
  plotname: "Transient Analysis",
  variables: [
    { index: 0, name: "time", type: "time" },
    { index: 1, name: "v(in)", type: "voltage" },
    { index: 2, name: "v(out)", type: "voltage" },
  ],
  points: [
    [0, 0, 1.8],
    [1e-9, 0, 1.8],
    [1.5e-9, 1.8, 0],
    [2e-9, 1.8, 0],
  ],
};

const ssWaveform: WaveformData = {
  ...ttWaveform,
  points: [
    [0, 0, 1.62],
    [1e-9, 0, 1.62],
    [1.8e-9, 1.62, 0],
    [2e-9, 1.62, 0],
  ],
};

// Shaped like a real `blocks/<slug>/output/layout.json` `signals` section —
// i.e. `klt sim`'s own response fields (issue #99's pipeline) plus the
// viewer-facing `default_corner_id` / per-corner `waveform` path.
const signals: LayoutSignals = {
  schema_version: 1,
  engine: "ngspice",
  engine_version: "46",
  status: "pass",
  corner_count: 2,
  default_corner_id: "tt/1.800V/27C",
  passed: 2,
  failed: 0,
  errored: 0,
  measurements: [
    {
      name: "tphl",
      unit: "s",
      limits: null,
      status: "pass",
      worst_case: { corner_id: "ss/1.620V/125C", value: 1.9e-10, margin: null },
    },
  ],
  corners: [
    {
      corner_id: "tt/1.800V/27C",
      process: "tt",
      supply_v: { vdd: 1.8 },
      temperature_c: 27,
      status: "pass",
      runtime_s: 2.6,
      measurements: [{ name: "tphl", value: 8.2e-11, unit: "s", status: "pass", margin: 1e-10 }],
      diagnostics: [],
      waveform: "signals/tt_1.800V_27C.json",
    },
    {
      corner_id: "ss/1.620V/125C",
      process: "ss",
      supply_v: { vdd: 1.62 },
      temperature_c: 125,
      status: "pass",
      runtime_s: 2.7,
      measurements: [{ name: "tphl", value: 1.9e-10, unit: "s", status: "pass", margin: 1e-11 }],
      diagnostics: [],
      waveform: "signals/ss_1.620V_125C.json",
    },
  ],
};

function mockFetch() {
  return vi.fn((input: string | URL | Request) => {
    const url = String(input);
    const body = url.includes("ss_1.620V_125C") ? ssWaveform : ttWaveform;
    return Promise.resolve({
      ok: true,
      json: () => Promise.resolve(body),
    } as Response);
  });
}

beforeEach(() => {
  vi.stubGlobal("fetch", mockFetch());
  // jsdom returns a zero-size rect by default; the click handler bails out
  // when width is 0, so give the plot a stable, non-zero geometry.
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

describe("WaveformViewer", () => {
  it("loads the default corner's waveform and renders a plot", async () => {
    render(<WaveformViewer slug="sky130_fd_sc_hd__inv_1" signals={signals} />);

    expect(screen.getByLabelText("Toggle corner tt/1.800V/27C")).toBeChecked();
    expect(screen.getByLabelText("Toggle corner ss/1.620V/125C")).not.toBeChecked();

    await waitFor(() => expect(screen.getByTestId("waveform-plot")).toBeInTheDocument());
    expect(screen.getByTestId("waveform-plot").querySelectorAll("path").length).toBeGreaterThan(0);
  });

  it("supports PVT corner overlay/selection", async () => {
    render(<WaveformViewer slug="sky130_fd_sc_hd__inv_1" signals={signals} />);
    await waitFor(() =>
      expect(screen.getByTestId("waveform-plot").querySelectorAll("path").length).toBeGreaterThan(0),
    );
    const initialPaths = screen.getByTestId("waveform-plot").querySelectorAll("path").length;

    fireEvent.click(screen.getByLabelText("Toggle corner ss/1.620V/125C"));

    await waitFor(() => {
      const paths = screen.getByTestId("waveform-plot").querySelectorAll("path").length;
      expect(paths).toBeGreaterThan(initialPaths);
    });
    expect(screen.getByLabelText("Toggle corner ss/1.620V/125C")).toBeChecked();
  });

  it("supports per-node visibility toggles", async () => {
    render(<WaveformViewer slug="sky130_fd_sc_hd__inv_1" signals={signals} />);
    await waitFor(() => expect(screen.getByLabelText("Toggle signal v(out)")).toBeInTheDocument());

    const beforeCount = screen.getByTestId("waveform-plot").querySelectorAll("path").length;
    fireEvent.click(screen.getByLabelText("Toggle signal v(out)"));

    await waitFor(() => {
      const afterCount = screen.getByTestId("waveform-plot").querySelectorAll("path").length;
      expect(afterCount).toBeLessThan(beforeCount);
    });
    expect(screen.getByLabelText("Toggle signal v(out)")).not.toBeChecked();
  });

  it("places a cursor on click and shows a readout", async () => {
    render(<WaveformViewer slug="sky130_fd_sc_hd__inv_1" signals={signals} />);
    await waitFor(() => expect(screen.getByTestId("waveform-plot")).toBeInTheDocument());

    expect(screen.queryByTestId("cursor-readout")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("waveform-plot"), { clientX: 320, clientY: 100 });

    await waitFor(() => expect(screen.getByTestId("cursor-readout")).toBeInTheDocument());
    const readout = within(screen.getByTestId("cursor-readout"));
    expect(readout.getByText(/tt\/1\.800V\/27C · v\(out\)/)).toBeInTheDocument();
  });

  it("supports zoom in/out/reset", async () => {
    render(<WaveformViewer slug="sky130_fd_sc_hd__inv_1" signals={signals} />);
    await waitFor(() => expect(screen.getByTestId("waveform-plot")).toBeInTheDocument());

    const domainLabel = () => screen.getByText(/–/).textContent ?? "";
    const fullRangeText = domainLabel();

    fireEvent.click(screen.getByRole("button", { name: "Zoom in" }));
    await waitFor(() => expect(domainLabel()).not.toBe(fullRangeText));

    fireEvent.click(screen.getByRole("button", { name: "Reset zoom" }));
    await waitFor(() => expect(domainLabel()).toBe(fullRangeText));
  });

  it("renders a 'no signals data' message for an empty corners list", () => {
    render(
      <WaveformViewer
        slug="sky130_fd_sc_hd__inv_1"
        signals={{ ...signals, corner_count: 0, passed: 0, corners: [] }}
      />,
    );
    expect(screen.getByText("No signals data.")).toBeInTheDocument();
  });
});
