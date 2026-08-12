import { useEffect, useMemo, useState } from "react";
import { FieldViewer } from "@/components/field/FieldViewer";
import type { FieldViewerData } from "@/components/field/types";
import { WaveformViewer } from "@/components/waveform/WaveformViewer";
import type { LayoutSignals } from "@/data/types";
import { nearestIndex, formatEngineering } from "@/components/waveform/waveformMath";
import { nearestFrameIndex, s11ToWaveformData } from "./emMath";
import type { EmSiteExport } from "./types";

/**
 * EM-gallery integration layer (Epic #840 Phase 1b, issue #850): wires the
 * geode-fem site export (Phase 1a, issue #849) into the reusable
 * `FieldViewer` (issue #841) and the existing `WaveformViewer`, driven by
 * one shared frequency slider.
 *
 * `FieldViewer` consumes `{mesh, frames}` directly (no translation layer --
 * the export format is a deliberate strict superset of `FieldViewerData`).
 * `WaveformViewer` consumes the S11 sweep (`s_parameters.points[]`) via its
 * `liveWaveform` overlay -- it has no first-class "S-parameter" concept, so
 * this component synthesizes a `WaveformData` (`frequency_hz` sweep axis,
 * `s11_db` trace, via `emMath.ts`'s `s11ToWaveformData`) and pairs it with a
 * minimal stub `LayoutSignals` (`corners: []`) to satisfy `WaveformViewer`'s
 * required `signals` prop -- see that component's own docstring for how it
 * handles an all-`liveWaveform`, zero-real-corners caller.
 *
 * Frequency-slider wiring gotcha (verified against the committed
 * `patch_antenna.em-export.json`): `frames[]` (1 entry, the FEM resonant
 * frequency) and `s_parameters.points[]` (13 entries, a 2.0-3.0 GHz sweep)
 * are on **different, unaligned frequency grids** -- sharing one integer
 * index between them would throw or show garbage past `frames.length`. This
 * component instead drives the slider off `s_parameters.points[]` (the
 * finer-grained axis) for the S11 trace, and maps the selected frequency to
 * the *nearest* available field frame (`emMath.ts`'s `nearestFrameIndex`)
 * for `FieldViewer` -- honest for the current single-frame export (always
 * resolves to frame 0) and correct without changes once a future export
 * ships more than one frame.
 *
 * No illustrative/faked fields: every value rendered here traces directly
 * to the fetched, committed export -- nothing is synthesized except the
 * `WaveformData`/`LayoutSignals` *wrapper shapes* `WaveformViewer`'s prop
 * contract requires, whose values are copied verbatim from the export.
 */
export interface PatchAntennaResultProps {
  /**
   * Where the staged export JSON lives (`site/scripts/stage-em-data.mjs`
   * stages `examples/em/patch_antenna/patch_antenna.em-export.json` here at
   * build time). Overridable for tests.
   */
  dataUrl?: string;
}

type LoadState = "loading" | "error" | EmSiteExport;

export function PatchAntennaResult({
  dataUrl = "/em/patch_antenna/patch_antenna.em-export.json",
}: PatchAntennaResultProps) {
  const [state, setState] = useState<LoadState>("loading");
  const [pointIndex, setPointIndex] = useState(0);

  // Client-side fetch only (never during SSR/prerender), matching
  // `WaveformViewer`'s own client-fetch convention.
  useEffect(() => {
    let cancelled = false;
    setState("loading");
    fetch(dataUrl)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json() as Promise<EmSiteExport>;
      })
      .then((data) => {
        if (!cancelled) setState(data);
      })
      .catch(() => {
        if (!cancelled) setState("error");
      });
    return () => {
      cancelled = true;
    };
  }, [dataUrl]);

  const data = typeof state === "object" ? state : null;
  const points = data?.s_parameters.points ?? [];
  const clampedIndex = Math.min(Math.max(pointIndex, 0), Math.max(points.length - 1, 0));
  const activePoint = points[clampedIndex];

  const fieldData: FieldViewerData | null = data ? { mesh: data.mesh, frames: data.frames } : null;
  const fieldFrameIndex = useMemo(() => {
    if (!data || !activePoint) return 0;
    return nearestFrameIndex(data.frames, activePoint.frequency_hz);
  }, [data, activePoint]);

  const waveformData = useMemo(() => {
    if (!data) return null;
    return s11ToWaveformData(data.benchmark, points);
  }, [data, points]);

  // Minimal stub satisfying `WaveformViewer`'s required `signals` prop --
  // there are no real PVT corners here, only the synthesized S11
  // `liveWaveform` overlay. See this component's docstring.
  const stubSignals: LayoutSignals = useMemo(
    () => ({
      schema_version: 1,
      engine: "geode-fem",
      engine_version: null,
      status: "pass",
      corner_count: 0,
      passed: 0,
      failed: 0,
      errored: 0,
      measurements: [],
      corners: [],
    }),
    [],
  );

  function handleCursorChange(x: number) {
    const idx = nearestIndex(
      points.map((p) => [p.frequency_hz]),
      x,
    );
    if (idx >= 0) setPointIndex(idx);
  }

  if (state === "loading") {
    return (
      <p className="text-fog-dim" data-testid="patch-antenna-loading">
        Loading patch antenna solve…
      </p>
    );
  }

  if (state === "error" || !data || !fieldData || !waveformData) {
    return (
      <p className="text-red" data-testid="patch-antenna-error">
        Failed to load the patch antenna's exported solver data.
      </p>
    );
  }

  const activeFrame = data.frames[fieldFrameIndex];

  return (
    <div className="flex flex-col gap-6" data-testid="patch-antenna-result">
      <div className="flex flex-col gap-2">
        <FieldViewer data={fieldData} parameterIndex={fieldFrameIndex} />
        {data.frames.length === 1 && (
          <p className="text-[0.8rem] text-fog-dim" data-testid="field-frame-note">
            Field overlay is pinned to {activeFrame?.label ?? "the solved frame"} — the only field
            sample in this export.
          </p>
        )}
      </div>

      {activePoint && (
        <div className="flex flex-col gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <label className="text-[0.8rem] text-fog-dim" htmlFor="patch-antenna-frequency">
              Frequency
            </label>
            <input
              id="patch-antenna-frequency"
              type="range"
              min={0}
              max={Math.max(points.length - 1, 0)}
              step={1}
              value={clampedIndex}
              onChange={(event) => setPointIndex(Number(event.target.value))}
              aria-label="S-parameter frequency"
              className="accent-cyan"
            />
            <span className="font-mono text-[0.8rem] text-fog" data-testid="patch-antenna-frequency-label">
              {formatEngineering(activePoint.frequency_hz, "Hz")} · S11 {activePoint.s11_db.toFixed(2)} dB
            </span>
          </div>
          <WaveformViewer
            slug={data.benchmark}
            signals={stubSignals}
            liveWaveform={{ id: "s11", label: waveformData.plotname, data: waveformData }}
            cursorX={activePoint.frequency_hz}
            onCursorXChange={handleCursorChange}
          />
        </div>
      )}
    </div>
  );
}
