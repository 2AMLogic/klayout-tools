/**
 * Types for the waveform JSON artifact a `signals[].corners[].waveform`
 * path (see `@/data/types.ts`) resolves to at runtime.
 *
 * This mirrors `klt sim`'s `artifacts.waveform` shape exactly
 * (`docs/cli/sim.md`, "Waveform artifact (optional, first-class)"):
 *
 * ```json
 * {
 *   "plotname": "Transient Analysis",
 *   "variables": [
 *     { "index": 0, "name": "time", "type": "time" },
 *     { "index": 1, "name": "v(out)", "type": "voltage" }
 *   ],
 *   "points": [[0.0, 0.81], [1e-11, 0.81], ...]
 * }
 * ```
 *
 * `variables[0]` is always the sweep variable; every `points[]` row has one
 * value per declared variable, in `variables[].index` order.
 */
export interface WaveformVariable {
  index: number;
  name: string;
  type: string;
}

export interface WaveformData {
  plotname: string;
  variables: WaveformVariable[];
  points: number[][];
}

/**
 * A client-computed waveform to overlay on `WaveformViewer`'s plot alongside
 * (or instead of) the static per-corner `signals` data — fed in by the
 * stimulus-editing playground (Epic #90 phase C, issue #151) after a
 * re-run. Rendered as one more selectable "corner" entry: no fetch is
 * needed (the data is already resolved), and it is auto-selected so a
 * re-run's result appears in the same plot the static signals populate.
 */
export interface LiveWaveform {
  /** Distinguishing id for the overlay entry — must not collide with a real
   *  `signals.corners[].corner_id`. */
  id: string;
  /** Short label shown in the corner-selection legend in place of a real
   *  `corner_id`, e.g. `"live (tt)"`. */
  label: string;
  data: WaveformData;
}
