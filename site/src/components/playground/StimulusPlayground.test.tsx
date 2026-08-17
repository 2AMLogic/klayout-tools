// @vitest-environment jsdom
/**
 * Component tests for the stimulus-editing playground (Epic #90 phase C,
 * issue #151): the edit -> re-run -> waveform-update loop, and the
 * WASM-unavailable fallback to the plain (static-signals) `WaveformViewer`.
 *
 * Follows `WaveformViewer.test.tsx`'s house style (jsdom opt-in pragma,
 * `@testing-library/react`) and `engine.test.ts`'s style of mocking
 * `eecircuit-engine` directly (rather than mocking this repo's own
 * `@/lib/playground` wrapper) so these tests exercise the real
 * netlist-assembly / engine-wrapper code path end to end.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { resetPlaygroundEngine } from "@/lib/playground";
import { StimulusPlayground } from "./StimulusPlayground";
import type { LayoutSignals } from "@/data/types";
import type { WaveformData } from "@/components/waveform/types";

const FAKE_HEADER =
  "Title: * playground\nDate: now\nPlotname: Transient Analysis\nCommand: ngspice-45.2+, Build Tue Mar 24 02:02:56 UTC 2026\n";

let failNextStart = false;
const setNetListArgs: string[] = [];

function fakeResult() {
  return {
    header: FAKE_HEADER,
    numVariables: 3,
    variableNames: ["time", "v(a)", "v(y)"],
    numPoints: 2,
    dataType: "real" as const,
    data: [
      { name: "time", type: "time" as const, values: [0, 1e-9] },
      { name: "v(a)", type: "voltage" as const, values: [0, 1.8] },
      { name: "v(y)", type: "voltage" as const, values: [1.8, 0] },
    ],
  };
}

class FakeSimulation {
  start = vi.fn(async () => {
    if (failNextStart) throw new Error("boom — WASM unavailable");
  });
  setNetList = vi.fn((input: string) => {
    setNetListArgs.push(input);
  });
  runSim = vi.fn(async () => fakeResult());
}

vi.mock("eecircuit-engine", () => ({ Simulation: FakeSimulation }));

const ttWaveform: WaveformData = {
  plotname: "Transient Analysis",
  variables: [
    { index: 0, name: "time", type: "time" },
    { index: 1, name: "v(a)", type: "voltage" },
    { index: 2, name: "v(y)", type: "voltage" },
  ],
  points: [
    [0, 0, 1.8],
    [1e-9, 0, 1.8],
    [2e-9, 1.8, 0],
  ],
};

const signals: LayoutSignals = {
  schema_version: 1,
  engine: "ngspice",
  engine_version: "46",
  status: "pass",
  corner_count: 1,
  default_corner_id: "tt/1.800V/27C",
  passed: 1,
  failed: 0,
  errored: 0,
  measurements: [],
  corners: [
    {
      corner_id: "tt/1.800V/27C",
      process: "tt",
      supply_v: { vdd: 1.8 },
      temperature_c: 27,
      status: "pass",
      runtime_s: 2.6,
      measurements: [],
      diagnostics: [],
      waveform: "signals/tt_1.800V_27C.json",
    },
  ],
};

function mockFetch() {
  return vi.fn((input: string | URL | Request) => {
    const url = String(input);
    if (url.includes("/models/")) {
      return Promise.resolve({
        ok: true,
        status: 200,
        text: () => Promise.resolve("* fake vendored model deck\n.model foo\n"),
      } as Response);
    }
    return Promise.resolve({
      ok: true,
      json: () => Promise.resolve(ttWaveform),
    } as Response);
  });
}

beforeEach(() => {
  failNextStart = false;
  setNetListArgs.length = 0;
  resetPlaygroundEngine();
  vi.stubGlobal("fetch", mockFetch());
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  resetPlaygroundEngine();
});

describe("StimulusPlayground", () => {
  it("edits a stimulus parameter, re-runs, and updates the WaveformViewer plot", async () => {
    render(<StimulusPlayground slug="sky130_fd_sc_hd__inv_1" signals={signals} />);

    // Static signals load first, same as the plain WaveformViewer.
    await waitFor(() => expect(screen.getByTestId("waveform-plot")).toBeInTheDocument());
    const beforeCount = screen.getByTestId("waveform-plot").querySelectorAll("path").length;

    // Edit the supply voltage field.
    const supplyInput = screen.getByLabelText(/Supply \(V\)/);
    fireEvent.change(supplyInput, { target: { value: "1.5" } });

    fireEvent.click(screen.getByRole("button", { name: "Re-run" }));

    await waitFor(() => {
      expect(screen.getByLabelText("Toggle corner live (tt)")).toBeInTheDocument();
      expect(screen.getByLabelText("Toggle corner live (tt)")).toBeChecked();
    });

    await waitFor(() => {
      const afterCount = screen.getByTestId("waveform-plot").querySelectorAll("path").length;
      expect(afterCount).toBeGreaterThan(beforeCount);
    });

    expect(screen.getByText(/Simulated with ngspice-45\.2\+/)).toBeInTheDocument();
    expect(setNetListArgs).toHaveLength(1);
    expect(setNetListArgs[0]).toContain("VVPWR VPWR 0 DC 1.5");
  });

  it("falls back to the static signals view when the engine is unavailable, with no error state", async () => {
    failNextStart = true;
    render(<StimulusPlayground slug="sky130_fd_sc_hd__inv_1" signals={signals} />);

    await waitFor(() => expect(screen.getByTestId("waveform-plot")).toBeInTheDocument());
    expect(screen.getByTestId("stimulus-playground")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Re-run" }));

    // Degrades to the plain, unchanged static view — no more stimulus form.
    await waitFor(() => expect(screen.queryByTestId("stimulus-playground")).not.toBeInTheDocument());
    expect(screen.getByTestId("waveform-viewer")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("waveform-plot")).toBeInTheDocument());

    // No error text anywhere in the rendered output.
    expect(screen.queryByText(/error/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/unavailable/i)).not.toBeInTheDocument();
  });

  it("does not eagerly load the engine on mount (lazy-load-on-interaction contract)", () => {
    render(<StimulusPlayground slug="sky130_fd_sc_hd__inv_1" signals={signals} />);
    // Fetch is used for the static signals waveform, but nothing here calls
    // into `eecircuit-engine` before a "Re-run" click.
    expect(setNetListArgs).toHaveLength(0);
  });
});
