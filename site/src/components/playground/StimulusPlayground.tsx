import { useId, useState } from "react";
import type { LayoutSignals } from "@/data/types";
import { WaveformViewer } from "@/components/waveform";
import type { LiveWaveform } from "@/components/waveform";
import { DEFAULT_STIMULUS, runPlayground, type StimulusParams } from "@/lib/playground";
import type { PlaygroundStatus, StimulusPlaygroundProps } from "./types";

/**
 * Stimulus-editing playground for a sky130 gallery cell's detail page
 * (Epic #90 phase C, issue #151 — parent #148). Wires phase B's client-side
 * engine wrapper (`@/lib/playground`, issue #150) to the existing
 * `WaveformViewer` (issue #100): editing a stimulus knob and clicking
 * "Re-run" runs ngspice in the browser and overlays the result on the same
 * plot the static `signals` data populates.
 *
 * **Parameterized editing only** — the form below edits exactly the
 * {@link StimulusParams} fields the vendored testbench template
 * parameterizes (supply, input slew, pulse timing, load cap, temperature).
 * There is no free-form netlist text box; that scope boundary is
 * deliberate (`docs/design/wasm-spice-playground-spike.md`, "Recommended
 * scope boundary for the follow-up build") — free-form editing would let a
 * visitor's netlist reference a device this repo hasn't vendored, exactly
 * what the phase-A trimming strategy was designed to close off.
 *
 * Graceful degradation: any failure while loading or running the
 * client-side engine (old browser, blocked worker, `import("eecircuit-
 * engine")` failure, a missing model deck, ...) degrades the playground
 * permanently for this page visit — the component falls back to rendering
 * the plain `WaveformViewer`, i.e. the unchanged static #99 signals view,
 * with no error state shown. This mirrors phase B's own lazy-load design
 * (`@/lib/playground/engine.ts`'s docstring): the engine is only ever
 * requested from the "Re-run" click handler, never on mount, so a visitor
 * who never opens the playground never pays its cost.
 */

const CORNERS = ["tt", "ss", "ff"] as const;
type Corner = (typeof CORNERS)[number];

/** Field descriptor for one editable `StimulusParams` knob. Values are
 *  stored in `StimulusParams`' own SI base units; `scale` converts to/from
 *  a display unit that's actually pleasant to type (e.g. picoseconds
 *  instead of `1e-10`). */
interface FieldSpec {
  key: keyof StimulusParams;
  label: string;
  unit: string;
  scale: number;
  step: number;
  min?: number;
}

const FIELDS: FieldSpec[] = [
  { key: "supplyV", label: "Supply", unit: "V", scale: 1, step: 0.05, min: 0 },
  { key: "riseS", label: "Input rise (slew)", unit: "ps", scale: 1e12, step: 1, min: 0 },
  { key: "fallS", label: "Input fall (slew)", unit: "ps", scale: 1e12, step: 1, min: 0 },
  { key: "pulseWidthS", label: "Pulse width", unit: "ns", scale: 1e9, step: 0.5, min: 0 },
  { key: "periodS", label: "Period", unit: "ns", scale: 1e9, step: 0.5, min: 0 },
  { key: "loadCapF", label: "Load cap", unit: "fF", scale: 1e15, step: 0.5, min: 0 },
  { key: "temperatureC", label: "Temperature", unit: "°C", scale: 1, step: 1 },
];

/** Round a display-unit value to avoid float-noise digits (e.g. `1.7999999999999998`). */
function round(value: number): number {
  return Math.round(value * 1e6) / 1e6;
}

/** Pick the initial corner from `signals.default_corner_id`'s own process
 *  label, falling back to `"tt"` when it doesn't match a playground corner. */
function initialCorner(signals: LayoutSignals): Corner {
  const defaultCorner = signals.corners.find((c) => c.corner_id === signals.default_corner_id);
  const process = (defaultCorner ?? signals.corners[0])?.process;
  return (CORNERS as readonly string[]).includes(process ?? "") ? (process as Corner) : "tt";
}

export function StimulusPlayground({ slug, signals }: StimulusPlaygroundProps) {
  const idPrefix = useId();
  const [params, setParams] = useState<StimulusParams>(DEFAULT_STIMULUS);
  const [corner, setCorner] = useState<Corner>(() => initialCorner(signals));
  const [status, setStatus] = useState<PlaygroundStatus>("idle");
  const [liveWaveform, setLiveWaveform] = useState<LiveWaveform | null>(null);
  const [ngspiceVersion, setNgspiceVersion] = useState<string | null>(null);

  if (status === "unavailable") {
    // Interactive re-run isn't available in this browser/session — fall
    // back to the existing static #99 signals view, unchanged.
    return <WaveformViewer slug={slug} signals={signals} />;
  }

  function updateField(spec: FieldSpec, raw: string) {
    const value = Number(raw);
    if (!Number.isFinite(value)) return;
    const next = value / spec.scale;
    setParams((prev) => {
      const updated: StimulusParams = { ...prev, [spec.key]: next };
      // Simulation-control fields (`stopTimeS`/`stepS`) aren't exposed as
      // their own controls — they're implementation detail, not "stimulus"
      // — so derive sensible values from the edited stimulus knob instead
      // of leaving them stuck at `DEFAULT_STIMULUS`'s values: cover one
      // full period, and keep the time step proportional to the edge rate.
      if (spec.key === "periodS") {
        updated.stopTimeS = next;
      }
      if (spec.key === "riseS" || spec.key === "fallS") {
        updated.stepS = Math.max(Math.min(updated.riseS, updated.fallS) / 10, 1e-15);
      }
      return updated;
    });
  }

  async function handleRerun() {
    setStatus("running");
    try {
      const result = await runPlayground({ slug, corner, params });
      setLiveWaveform({ id: `live:${corner}`, label: `live (${corner})`, data: result.waveform });
      setNgspiceVersion(result.ngspiceVersion);
      setStatus("idle");
    } catch {
      setStatus("unavailable");
    }
  }

  return (
    <div className="flex flex-col gap-4" data-testid="stimulus-playground">
      <p className="text-[0.85rem] text-fog-dim">
        Edit the stimulus below and re-run to simulate this cell with ngspice in your
        browser, against the same vendored sky130 device models used to generate the
        signals above.
      </p>

      <fieldset
        className="grid grid-cols-2 gap-x-4 gap-y-3 border-0 p-0 sm:grid-cols-4"
        aria-label="Stimulus parameters"
      >
        <label
          className="flex flex-col gap-1 text-[0.78rem] text-fog-dim"
          htmlFor={`${idPrefix}-corner`}
        >
          Corner
          <select
            id={`${idPrefix}-corner`}
            value={corner}
            onChange={(e) => setCorner(e.target.value as Corner)}
            className="rounded-lg border border-border bg-panel px-2 py-1 font-mono text-fog"
          >
            {CORNERS.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </label>
        {FIELDS.map((spec) => (
          <label
            key={spec.key}
            className="flex flex-col gap-1 text-[0.78rem] text-fog-dim"
            htmlFor={`${idPrefix}-${spec.key}`}
          >
            {spec.label} ({spec.unit})
            <input
              id={`${idPrefix}-${spec.key}`}
              type="number"
              step={spec.step}
              min={spec.min}
              value={round(params[spec.key] * spec.scale)}
              onChange={(e) => updateField(spec, e.target.value)}
              className="rounded-lg border border-border bg-panel px-2 py-1 font-mono text-fog"
            />
          </label>
        ))}
      </fieldset>

      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={handleRerun}
          disabled={status === "running"}
          className="rounded-lg border border-border bg-panel px-3 py-1.5 text-[0.85rem] text-fog hover:border-cyan disabled:opacity-40"
        >
          {status === "running" ? "Running…" : "Re-run"}
        </button>
        {ngspiceVersion && (
          <span className="font-mono text-[0.75rem] text-fog-dim">
            Simulated with {ngspiceVersion}
          </span>
        )}
      </div>

      <WaveformViewer slug={slug} signals={signals} liveWaveform={liveWaveform} />
    </div>
  );
}
