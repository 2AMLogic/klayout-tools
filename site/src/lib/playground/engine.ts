/**
 * Client-side SPICE engine wrapper for the in-browser playground (Epic #90
 * phase B, issue #150 — parent #148).
 *
 * Wraps `eecircuit-engine` (MIT wrapper over a BSD-3 ngspice WASM core; see
 * `docs/design/wasm-spice-playground-spike.md`) with the three integration
 * concerns the spike identified:
 *
 *   1. **Lazy-load on interaction, not on page load.** The engine is only ever
 *      pulled via a dynamic `import("eecircuit-engine")` inside
 *      {@link loadSimulation}. Nothing in this module is imported eagerly by a
 *      page (`DetailPage` does not reference it), and the `import type` below is
 *      erased at build time, so the ~19 MB (5.75 MB gzip) engine bundle is a
 *      fetch-when-invoked cost — a visitor who never triggers the playground
 *      never pays it. This mirrors how `WaveformViewer` already gates its
 *      optional section behind interaction.
 *   2. **One engine instance per page visit, cached across re-runs.**
 *      `sim.start()` (the one-time ~600 ms WASM instantiation measured by the
 *      spike) runs exactly once: {@link loadSimulation} memoizes the
 *      start-promise, so every "edit stimulus, re-run" reuses the same
 *      `Simulation`. {@link resetPlaygroundEngine} exists for tests and for a
 *      cold-start perf probe.
 *   3. **Vendored, checksummed models — not the engine's baked-in blobs.**
 *      {@link runPlayground} fetches the phase-A staged model deck and splices
 *      it into the netlist (via `assembleNetlist`); the engine's bundled model
 *      constants are never referenced.
 *
 * Version pinning (spike open question "Engine version pinning"): the
 * `eecircuit-engine` dependency is pinned to an exact version in
 * `site/package.json`, and the ngspice version it embeds is recorded in
 * {@link EMBEDDED_NGSPICE_VERSION} below (and confirmed at runtime from each
 * result header via {@link extractNgspiceVersion}) — the same way `klt sim`'s
 * `environment.engine_version` records the server-side engine.
 */

import type { ResultType, Simulation } from "eecircuit-engine";
import type { WaveformData, WaveformVariable } from "@/components/waveform/types";
import { blockAssetUrl } from "@/lib/blockAssets";
import { assembleNetlist, type StimulusParams } from "./netlist";

/**
 * Exact `eecircuit-engine` version this wrapper is pinned to (kept in sync with
 * `site/package.json`). Pinning is deliberate: the engine tracks whatever
 * ngspice tag was latest at its own build time, so an unpinned bump could
 * silently change the embedded numerics.
 */
export const EECIRCUIT_ENGINE_VERSION = "1.7.0";

/**
 * The ngspice version embedded in {@link EECIRCUIT_ENGINE_VERSION}, recorded
 * from the result header's `Command:` line at pin time
 * (`Command: ngspice-45.2+, Build Tue Mar 24 02:02:56 UTC 2026`). Verified at
 * runtime by {@link extractNgspiceVersion}; update alongside any engine bump.
 */
export const EMBEDDED_NGSPICE_VERSION = "ngspice-45.2+";

/** The ngspice build date string recorded at pin time (informational). */
export const EMBEDDED_NGSPICE_BUILD = "Tue Mar 24 02:02:56 UTC 2026";

/**
 * Memoized start-promise for the single `Simulation` instance. Held at module
 * scope so it survives across re-runs within a page visit but is fresh per full
 * page load (a new document re-evaluates the module).
 */
let simulationPromise: Promise<Simulation> | null = null;

/**
 * Lazily load `eecircuit-engine`, construct one `Simulation`, and run its
 * one-time `start()` — memoized, so repeated calls return the same started
 * instance and `start()` is invoked at most once. This is the seam that makes
 * both the lazy-load and the instance-reuse acceptance criteria hold.
 */
export function loadSimulation(): Promise<Simulation> {
  if (simulationPromise === null) {
    simulationPromise = (async () => {
      const mod = await import("eecircuit-engine");
      const sim = new mod.Simulation();
      await sim.start();
      return sim;
    })().catch((err) => {
      // Don't cache a failed start — allow a later retry to re-attempt.
      simulationPromise = null;
      throw err;
    });
  }
  return simulationPromise;
}

/**
 * Drop the cached engine instance. The next {@link loadSimulation} will
 * re-import and re-`start()`. Used by tests and by {@link perfProbe}'s
 * cold-start measurement — not by normal re-run flow (which must reuse).
 */
export function resetPlaygroundEngine(): void {
  simulationPromise = null;
}

/**
 * Fetch a phase-A vendored, checksummed model deck for a block/corner from the
 * staged static asset (`/blocks/<slug>/models/<corner>.spice`). Returns the raw
 * SPICE text to splice into the netlist.
 */
export async function fetchModelDeck(slug: string, corner: string): Promise<string> {
  const url = blockAssetUrl(slug, `models/${corner}.spice`);
  const res = await fetch(url);
  if (!res.ok) {
    throw new Error(`failed to fetch model deck ${url}: HTTP ${res.status}`);
  }
  return res.text();
}

/**
 * Extract the ngspice version token (`ngspice-45.2+`) from a result header's
 * `Command:` line. Returns `null` if the header carries no recognizable token.
 */
export function extractNgspiceVersion(header: string): string | null {
  const match = header.match(/ngspice-[0-9][\w.+-]*/i);
  return match ? match[0] : null;
}

/**
 * Convert an `eecircuit-engine` result into the `WaveformData` shape the rest
 * of the site already consumes (`@/components/waveform/types`), so a playground
 * re-run can feed the exact same viewer as the precomputed signals do.
 * `variables[0]` is the sweep (time); each `points[]` row has one value per
 * variable in declared order. Complex data (AC/noise) is reduced to its real
 * part; the transient case this playground uses is already real.
 */
export function resultToWaveform(result: ResultType): WaveformData {
  const variables: WaveformVariable[] = result.data.map((trace, index) => ({
    index,
    name: trace.name,
    type: trace.type,
  }));

  const points: number[][] = [];
  for (let i = 0; i < result.numPoints; i += 1) {
    const row: number[] = [];
    for (const trace of result.data) {
      const value = trace.values[i];
      row.push(typeof value === "number" ? value : (value?.real ?? Number.NaN));
    }
    points.push(row);
  }

  return {
    plotname: result.dataType === "complex" ? "AC Analysis" : "Transient Analysis",
    variables,
    points,
  };
}

/** Inputs to {@link runPlayground}. */
export interface RunPlaygroundOptions {
  /** Block slug, e.g. `sky130_fd_sc_hd__inv_1`. */
  slug: string;
  /** Process corner (`tt`/`ss`/`ff`) — selects which vendored deck to fetch. */
  corner: string;
  /** Editable stimulus parameters. */
  params: StimulusParams;
  /**
   * Optional testbench template override (defaults to the inverter testbench).
   * Also used as the `cell` label in the deck title; pass `cell` to override.
   */
  testbenchTemplate?: string;
  /** Cell label for the deck title (defaults to `slug`). */
  cell?: string;
}

/** Result of a playground re-run. */
export interface RunPlaygroundResult {
  /** Simulation output in the site's shared waveform shape. */
  waveform: WaveformData;
  /** ngspice version reported by this run's header (runtime confirmation). */
  ngspiceVersion: string | null;
  /** The assembled netlist that was simulated (useful for debugging / display). */
  netlist: string;
}

/**
 * Run one playground simulation: ensure the (cached) engine is loaded, fetch
 * the vendored model deck, assemble the netlist from the stimulus parameters,
 * and simulate. Reuses the cached `Simulation` across calls, so a re-run only
 * pays the ~0.6-1 s `runSim()` cost, not the one-time `start()` cost.
 */
export async function runPlayground(options: RunPlaygroundOptions): Promise<RunPlaygroundResult> {
  const [sim, modelText] = await Promise.all([
    loadSimulation(),
    fetchModelDeck(options.slug, options.corner),
  ]);

  const netlist = assembleNetlist({
    cell: options.cell ?? options.slug,
    corner: options.corner,
    params: options.params,
    modelText,
    testbenchTemplate: options.testbenchTemplate,
  });

  sim.setNetList(netlist);
  const result = await sim.runSim();

  return {
    waveform: resultToWaveform(result),
    ngspiceVersion: extractNgspiceVersion(result.header),
    netlist,
  };
}

/** Timing sample from {@link perfProbe} (all durations in milliseconds). */
export interface PerfSample {
  /** One-time engine load + `start()` (cold — the probe resets first). */
  startMs: number;
  /** First `runSim()` on the fresh instance. */
  firstRunMs: number;
  /** Second `runSim()` on the same instance ("edit stimulus, re-run"). */
  secondRunMs: number;
  /** ngspice version reported by the run header. */
  ngspiceVersion: string | null;
}

/**
 * Reproduce the spike's perf methodology (cold `start()`, then two `runSim()`
 * calls on one instance) against a ready-to-run netlist — usable from the Vite
 * dev server or a headless browser to validate the spike's Node-harness numbers
 * in a real browser tab (spike open question "Real-browser perf validation").
 * Resets the engine first so `startMs` reflects a true cold start; callers that
 * need the shared cached instance should use {@link runPlayground} instead.
 */
export async function perfProbe(netlist: string): Promise<PerfSample> {
  resetPlaygroundEngine();
  const t0 = performance.now();
  const sim = await loadSimulation();
  const t1 = performance.now();

  sim.setNetList(netlist);
  const first = await sim.runSim();
  const t2 = performance.now();

  sim.setNetList(netlist);
  await sim.runSim();
  const t3 = performance.now();

  return {
    startMs: t1 - t0,
    firstRunMs: t2 - t1,
    secondRunMs: t3 - t2,
    ngspiceVersion: extractNgspiceVersion(first.header),
  };
}
