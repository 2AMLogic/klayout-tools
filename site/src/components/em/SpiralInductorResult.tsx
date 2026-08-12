import { useEffect, useMemo, useState } from "react";
import { FieldViewer } from "@/components/field/FieldViewer";
import type { FieldViewerData } from "@/components/field/types";
import { WaveformViewer } from "@/components/waveform/WaveformViewer";
import type { LayoutSignals } from "@/data/types";
import { nearestIndex, formatEngineering } from "@/components/waveform/waveformMath";
import { nearestFrameIndex, s11ToWaveformData } from "./emMath";
import type { EmSiteExport } from "./types";

/**
 * Second "Electromagnetics" gallery entry (Epic #840, issue #877): the
 * spiral-inductor L/Q benchmark, wired the same way `PatchAntennaResult`
 * (issue #850) wires the patch antenna — fetch the committed geode-fem site
 * export, drive `FieldViewer` + `WaveformViewer` off one shared frequency
 * slider, and (via `onLoad`) let a gallery-entry wrapper render a
 * `ProvenancePanel` from the same fetched document. See that component's
 * own docstring for the full rationale behind the `WaveformViewer`
 * stub-signals pairing and the frames/points frequency-grid misalignment
 * this mirrors.
 *
 * The one substantive difference: this benchmark's headline figures are
 * inductance (`L`) and quality factor (`Q`) vs. frequency, not just S11 —
 * `examples/em/spiral_inductor/generate.py` carries those through onto
 * every `s_parameters.points[]` entry (`l_nh`/`r_ohm`/`q`, the schema's
 * `additionalProperties: true` allows), so the frequency-readout label
 * surfaces L/Q alongside S11 instead of S11 alone.
 *
 * No illustrative/faked fields: every value rendered here traces directly
 * to the fetched, committed `spiral_inductor.em-export.json` — itself a
 * real `cargo run -p spiral_inductor --release` solve against a local
 * geode-fem checkout, not a fixture (see `generate.py`'s own docstring).
 */
export interface SpiralInductorResultProps {
  /**
   * Where the staged export JSON lives (`site/scripts/stage-em-data.mjs`
   * stages `examples/em/spiral_inductor/spiral_inductor.em-export.json`
   * here at build time). Overridable for tests.
   */
  dataUrl?: string;
  /**
   * Fired once the export fetch resolves successfully, with the full parsed
   * document — lets a gallery-entry wrapper render a `ProvenancePanel`
   * alongside this component from the *same* fetched document, rather than
   * re-fetching `dataUrl` a second time just to reach `data.provenance`
   * (mirrors `PatchAntennaResult`'s `onLoad`). Never called on a
   * failed/loading fetch.
   */
  onLoad?: (data: EmSiteExport) => void;
}

type LoadState = "loading" | "error" | EmSiteExport;

export function SpiralInductorResult({
  dataUrl = "/em/spiral_inductor/spiral_inductor.em-export.json",
  onLoad,
}: SpiralInductorResultProps) {
  const [state, setState] = useState<LoadState>("loading");
  const [pointIndex, setPointIndex] = useState(0);

  // Client-side fetch only (never during SSR/prerender), matching
  // `PatchAntennaResult`'s / `WaveformViewer`'s own client-fetch convention.
  useEffect(() => {
    let cancelled = false;
    setState("loading");
    fetch(dataUrl)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json() as Promise<EmSiteExport>;
      })
      .then((data) => {
        if (cancelled) return;
        setState(data);
        onLoad?.(data);
      })
      .catch(() => {
        if (!cancelled) setState("error");
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- onLoad is a
    // caller-supplied callback, not a re-fetch trigger; see
    // `PatchAntennaResult`'s identical effect for the full rationale.
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
  // `liveWaveform` overlay. See `PatchAntennaResult`'s docstring.
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
      <p className="text-fog-dim" data-testid="spiral-inductor-loading">
        Loading spiral inductor solve…
      </p>
    );
  }

  if (state === "error" || !data || !fieldData || !waveformData) {
    return (
      <p className="text-red" data-testid="spiral-inductor-error">
        Failed to load the spiral inductor's exported solver data.
      </p>
    );
  }

  const activeFrame = data.frames[fieldFrameIndex];

  return (
    <div className="flex flex-col gap-6" data-testid="spiral-inductor-result">
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
            <label className="text-[0.8rem] text-fog-dim" htmlFor="spiral-inductor-frequency">
              Frequency
            </label>
            <input
              id="spiral-inductor-frequency"
              type="range"
              min={0}
              max={Math.max(points.length - 1, 0)}
              step={1}
              value={clampedIndex}
              onChange={(event) => setPointIndex(Number(event.target.value))}
              aria-label="S-parameter frequency"
              className="accent-cyan"
            />
            <span className="font-mono text-[0.8rem] text-fog" data-testid="spiral-inductor-frequency-label">
              {formatEngineering(activePoint.frequency_hz, "Hz")} · S11 {activePoint.s11_db.toFixed(2)} dB
              {activePoint.l_nh !== undefined && ` · L ${activePoint.l_nh.toFixed(2)} nH`}
              {activePoint.q !== undefined && ` · Q ${activePoint.q.toFixed(1)}`}
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
