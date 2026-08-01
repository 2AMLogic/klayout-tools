import type { LayoutSignals } from "@/data/types";

/**
 * sky130 gallery cells with a vendored playground model deck staged (Epic
 * #90 phase A, issue #149's `stage-models.mjs`). Kept in sync with that
 * script's `SKY130.slugs` deliberately: the interactive playground only
 * offers itself on a block whose `/blocks/<slug>/models/<corner>.spice`
 * decks actually exist, so a "Re-run" click never fails on a 404. gf180mcu
 * is explicitly out of scope for phase A/B/C (see the spike's scope
 * boundary, `docs/design/wasm-spice-playground-spike.md`).
 */
export const PLAYGROUND_SLUGS: readonly string[] = [
  "sky130_fd_sc_hd__inv_1",
  "sky130_fd_sc_hd__nand2_2",
  "sky130_fd_sc_hd__buf_4",
  "sky130_fd_sc_hd__dfxtp_2",
];

/**
 * True when `slug` has a staged playground model deck — the gate
 * `DetailPage` uses to decide whether a block offers the interactive
 * stimulus-editing playground in place of the plain (static-signals-only)
 * waveform viewer.
 */
export function isPlaygroundEligible(slug: string): boolean {
  return PLAYGROUND_SLUGS.includes(slug);
}

/**
 * - `"idle"`: no re-run in flight; either nothing has run yet, or the last
 *   run succeeded.
 * - `"running"`: a re-run is in flight (engine loading and/or `runSim()`).
 * - `"unavailable"`: a re-run attempt failed (old browser, blocked worker,
 *   `import("eecircuit-engine")` failure, ...) — the playground degrades
 *   permanently for this page visit, falling back to the plain
 *   `WaveformViewer` (the unchanged static #99 view).
 */
export type PlaygroundStatus = "idle" | "running" | "unavailable";

export interface StimulusPlaygroundProps {
  /** Block slug, e.g. `sky130_fd_sc_hd__inv_1`. Must be one of
   *  {@link PLAYGROUND_SLUGS} for a re-run to succeed. */
  slug: string;
  signals: LayoutSignals;
}
