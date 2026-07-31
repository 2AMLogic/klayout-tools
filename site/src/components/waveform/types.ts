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
