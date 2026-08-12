/**
 * Pure helpers backing `PatchAntennaResult` (issue #850) — split out from
 * the component so they're unit-testable without a DOM, matching the
 * `fieldMath.ts` / `waveformMath.ts` convention in the sibling viewers.
 *
 * The load-bearing gotcha these exist to handle: the geode-fem site export's
 * `frames[]` (field-overlay samples) and `s_parameters.points[]` (the S11
 * sweep) are **not** index-aligned — the committed patch-antenna export has
 * 1 frame but 13 S-parameter points on a different frequency grid entirely.
 * A single frequency slider driving both views cannot share one integer
 * index between them; it must map through frequency instead.
 */
import type { WaveformData } from "@/components/waveform/types";
import type { EmFrame, EmSParameterPoint } from "./types";

/**
 * The index into `frames` whose `frequency_hz` is closest to `targetHz`.
 * Frames with `frequency_hz === null` are never chosen unless every frame
 * lacks a frequency (falls back to index 0). Returns `0` for an empty
 * `frames` array (the caller is expected to guard on `frames.length === 0`
 * separately -- `FieldViewer` itself already handles a `frames[0] ===
 * undefined` frame gracefully).
 *
 * For the current single-frame patch-antenna export this always resolves to
 * `0` regardless of `targetHz` -- there is nothing else to pick -- but this
 * degrades gracefully once a future export ships more than one frame,
 * rather than baking in an assumption that `frames.length ===
 * s_parameters.points.length`.
 */
export function nearestFrameIndex(frames: EmFrame[], targetHz: number): number {
  let bestIndex = 0;
  let bestDiff = Infinity;
  frames.forEach((frame, index) => {
    if (frame.frequency_hz === null) return;
    const diff = Math.abs(frame.frequency_hz - targetHz);
    if (diff < bestDiff) {
      bestDiff = diff;
      bestIndex = index;
    }
  });
  return bestIndex;
}

/**
 * Convert `s_parameters.points[]` into the `WaveformData` shape
 * `WaveformViewer` consumes (via its `liveWaveform` overlay prop) --
 * `variables[0]` is the sweep variable (frequency), `variables[1]` is S11 in
 * dB, matching the `klt sim` waveform-artifact convention `WaveformViewer`
 * already expects (`variables[0]` sweep, one row per `variables[].index`).
 */
export function s11ToWaveformData(benchmark: string, points: EmSParameterPoint[]): WaveformData {
  return {
    plotname: `S11 (${benchmark})`,
    variables: [
      { index: 0, name: "frequency_hz", type: "frequency" },
      { index: 1, name: "s11_db", type: "gain" },
    ],
    points: points.map((p) => [p.frequency_hz, p.s11_db]),
  };
}
