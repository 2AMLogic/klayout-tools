import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// Track how many Simulation instances are constructed and how often start()
// runs, so we can assert the "one instance, start() once" reuse contract
// without loading the real ~19 MB WASM engine.
const constructed = { count: 0 };
const startCalls = { count: 0 };
const setNetListArgs: string[] = [];
let failNextStart = false;

const FAKE_HEADER = "Title: * playground\nDate: now\nPlotname: Transient Analysis\nCommand: ngspice-45.2+, Build Tue Mar 24 02:02:56 UTC 2026\n";

function fakeResult() {
  return {
    header: FAKE_HEADER,
    numVariables: 2,
    variableNames: ["time", "v(y)"],
    numPoints: 3,
    dataType: "real" as const,
    data: [
      { name: "time", type: "time" as const, values: [0, 1e-9, 2e-9] },
      { name: "v(y)", type: "voltage" as const, values: [0, 0.9, 1.8] },
    ],
  };
}

class FakeSimulation {
  constructor() {
    constructed.count += 1;
  }
  start = vi.fn(async () => {
    if (failNextStart) {
      failNextStart = false;
      throw new Error("boom");
    }
    startCalls.count += 1;
  });
  setNetList = vi.fn((input: string) => {
    setNetListArgs.push(input);
  });
  runSim = vi.fn(async () => fakeResult());
}

vi.mock("eecircuit-engine", () => ({ Simulation: FakeSimulation }));

import {
  loadSimulation,
  resetPlaygroundEngine,
  fetchModelDeck,
  extractNgspiceVersion,
  resultToWaveform,
  runPlayground,
  EECIRCUIT_ENGINE_VERSION,
  EMBEDDED_NGSPICE_VERSION,
} from "./engine";
import { DEFAULT_STIMULUS } from "./netlist";

beforeEach(() => {
  constructed.count = 0;
  startCalls.count = 0;
  setNetListArgs.length = 0;
  failNextStart = false;
  resetPlaygroundEngine();
});

afterEach(() => {
  vi.restoreAllMocks();
  resetPlaygroundEngine();
});

describe("loadSimulation", () => {
  it("constructs one instance and calls start() exactly once across re-runs", async () => {
    const a = await loadSimulation();
    const b = await loadSimulation();
    const c = await loadSimulation();
    expect(a).toBe(b);
    expect(b).toBe(c);
    expect(constructed.count).toBe(1);
    expect(startCalls.count).toBe(1);
  });

  it("does not cache a failed start (allows retry)", async () => {
    failNextStart = true;
    await expect(loadSimulation()).rejects.toThrow("boom");
    // A second attempt should re-construct and succeed.
    const sim = await loadSimulation();
    expect(sim).toBeDefined();
    expect(constructed.count).toBe(2);
    expect(startCalls.count).toBe(1);
  });

  it("resetPlaygroundEngine forces a fresh instance", async () => {
    await loadSimulation();
    resetPlaygroundEngine();
    await loadSimulation();
    expect(constructed.count).toBe(2);
    expect(startCalls.count).toBe(2);
  });
});

describe("extractNgspiceVersion", () => {
  it("parses the ngspice token from a result header", () => {
    expect(extractNgspiceVersion(FAKE_HEADER)).toBe("ngspice-45.2+");
  });

  it("returns null when no token is present", () => {
    expect(extractNgspiceVersion("Command: something-else")).toBeNull();
  });

  it("recorded EMBEDDED_NGSPICE_VERSION matches what a run header reports", () => {
    expect(extractNgspiceVersion(FAKE_HEADER)).toBe(EMBEDDED_NGSPICE_VERSION);
  });
});

describe("EECIRCUIT_ENGINE_VERSION", () => {
  it("is pinned to an exact semver (matches package.json pin)", () => {
    expect(EECIRCUIT_ENGINE_VERSION).toMatch(/^\d+\.\d+\.\d+$/);
  });
});

describe("resultToWaveform", () => {
  it("converts an engine result into the shared WaveformData shape", () => {
    const wf = resultToWaveform(fakeResult());
    expect(wf.plotname).toBe("Transient Analysis");
    expect(wf.variables).toEqual([
      { index: 0, name: "time", type: "time" },
      { index: 1, name: "v(y)", type: "voltage" },
    ]);
    expect(wf.points).toEqual([
      [0, 0],
      [1e-9, 0.9],
      [2e-9, 1.8],
    ]);
  });
});

describe("fetchModelDeck", () => {
  it("fetches the phase-A staged deck URL and returns its text", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      text: async () => "* tt deck",
    })) as unknown as typeof fetch;
    vi.stubGlobal("fetch", fetchMock);

    const text = await fetchModelDeck("sky130_fd_sc_hd__inv_1", "tt");
    expect(text).toBe("* tt deck");
    expect(fetchMock).toHaveBeenCalledWith(
      "/blocks/sky130_fd_sc_hd__inv_1/models/tt.spice",
    );
    vi.unstubAllGlobals();
  });

  it("throws on a non-ok response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: false, status: 404, text: async () => "" })) as unknown as typeof fetch,
    );
    await expect(fetchModelDeck("slug", "tt")).rejects.toThrow(/HTTP 404/);
    vi.unstubAllGlobals();
  });
});

describe("runPlayground", () => {
  it("reuses one engine, splices vendored models, and returns the version", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        status: 200,
        text: async () => "* VENDORED-MODEL-MARKER\n.model foo\n",
      })) as unknown as typeof fetch,
    );

    const first = await runPlayground({
      slug: "sky130_fd_sc_hd__inv_1",
      corner: "tt",
      params: DEFAULT_STIMULUS,
    });
    const second = await runPlayground({
      slug: "sky130_fd_sc_hd__inv_1",
      corner: "tt",
      params: { ...DEFAULT_STIMULUS, supplyV: 1.62 },
    });

    // One engine instance, one start() across both re-runs.
    expect(constructed.count).toBe(1);
    expect(startCalls.count).toBe(1);

    // The netlist handed to the engine includes the vendored model text, and
    // not any engine-bundled model reference.
    expect(setNetListArgs).toHaveLength(2);
    expect(setNetListArgs[0]).toContain("* VENDORED-MODEL-MARKER");
    expect(setNetListArgs[0]).toContain("VVPWR VPWR 0 DC 1.8");
    expect(setNetListArgs[1]).toContain("VVPWR VPWR 0 DC 1.62");

    expect(first.ngspiceVersion).toBe("ngspice-45.2+");
    expect(first.waveform.variables.map((v) => v.name)).toEqual(["time", "v(y)"]);
    expect(second.netlist).toContain("* VENDORED-MODEL-MARKER");

    vi.unstubAllGlobals();
  });
});
