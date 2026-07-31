/**
 * TypeScript types for the `layout.json` data contract (schema v1).
 *
 * `layout.json` is the per-block metrics artifact produced by `klt
 * layout-metrics` (issue #61, "Gallery: per-layout metrics extractor",
 * landed on `main` in PR #75). This file mirrors that command's landed
 * schema exactly — see `src/klayout_tools/layout_metrics.py` and
 * `docs/cli/layout-metrics.md` for the authoritative contract. The source
 * of truth is #61's own documentation; keep this in sync with it, and note
 * that additions there must be additive-only (no renames, no type changes)
 * until `schema_version` is bumped.
 *
 * Optional-field rule: a missing source artifact means the field is
 * OMITTED, never emitted as `null`. Accordingly every optional field is
 * typed `?: T` (not `T | null`). Downstream code should treat a missing key
 * as "unknown".
 */

/** Schema version this loader understands. Documents with a different major
 *  version are skipped (forward-compat guard). */
export const SCHEMA_VERSION = 1;

/** Layout status enum.
 *  - "ok": layout.json present with full metrics.
 *  - "partial": layout.json present but some fields could not be computed
 *    (e.g. DRC not run yet).
 *  - "no_artifacts": no layout.json found for this block (or it failed to
 *    parse) — the loader synthesizes this stub rather than crashing.
 */
export type LayoutStatus = "ok" | "partial" | "no_artifacts";

/** DRC run status, from `klt drc` — `"clean"` or `"violations"`. */
export type DrcStatus = "clean" | "violations";

/**
 * The `drc` sub-object, present only when `klt layout-metrics --deck` was
 * supplied and the DRC run succeeded (see `docs/cli/layout-metrics.md`).
 */
export interface LayoutDrc {
  /** The deck name passed via `--deck`, e.g. "sky130". */
  deck: string;
  status: DrcStatus;
  violation_count: number;
}

/**
 * A fully-parsed layout record matching `layout.json` schema v1.
 *
 * Required fields (always present): `schema_version`, `generated_at`,
 * `slug`, `name`, `status`. All other fields are optional and omitted when
 * the underlying artifact/metric is unavailable.
 */
export interface Layout {
  schema_version: number;
  generated_at: string;
  slug: string;
  /** Display name — from `meta.json`, else a title-cased fallback of `slug`. */
  name: string;
  status: LayoutStatus;
  description?: string;
  /** The layout file used, relative to the block dir. Omitted when `no_artifacts`. */
  layout_file?: string;
  /** From `klt layers` — number of distinct (layer, datatype) pairs. */
  layer_count?: number;
  /** From `klt cells` — total cell count in the hierarchy. */
  cell_count?: number;
  /** From `klt cells` — total instance count across the hierarchy. */
  instance_count?: number;
  /** From `klt drc`, present only when a DRC deck has been run for this block. */
  drc?: LayoutDrc;
  /** Map of render id -> path relative to the layout.json location (#60). */
  renders?: Record<string, string>;
  /**
   * Whether `layout_file` may be linked as a download on the block detail
   * page (#64). NOT part of the `klt layout-metrics` contract above — this
   * is a presence-check flag reserved for the content pipeline / public-repo
   * gate (#62), which is not yet wired up. Until #62 sets it, this field is
   * always absent, so every detail page omits its download section; once
   * #62 starts emitting `downloadable: true` for blocks sourced from public
   * repos, the download link appears with no changes to this page.
   */
  downloadable?: boolean;
  /**
   * PVT corner simulation results (Epic #90 Phase 2, issue #100), produced
   * by the signals pipeline (issue #99 — "Gallery: signals pipeline —
   * per-block ngspice runs via `klt sim`, extend layout.json"). Omitted
   * entirely for every block until that pipeline lands; the detail page's
   * Signals section is gated on this field the same way `renders` /
   * `downloadable` / `drc` are gated (presence, not truthiness).
   *
   * SCHEMA STATUS: at the time issue #100 shipped this type, #99 (which
   * owns the authoritative shape) had not yet merged. This shape is a
   * best-effort design mirroring `klt sim`'s own JSON contract
   * (`docs/cli/sim.md`) as closely as possible — same `corner_id`/
   * `process`/`supply_v`/`temperature_c`/`status`/`measurements` fields per
   * corner, and the same "never inline waveform samples" convention (a
   * `waveform` field is a *path*, relative to the block's `output/`
   * directory, staged by `copy-renders.mjs` exactly like `renders` values,
   * not inline sample data) — so reconciling with #99's landed schema
   * should be a small, mostly-additive diff. Treat this as provisional
   * until #99 merges.
   */
  signals?: LayoutSignals;
}

/** A single PVT-corner measurement result (mirrors `klt sim`'s per-corner
 *  `measurements[]` entries — see `docs/cli/sim.md`). */
export interface SignalsMeasurement {
  name: string;
  /** `null` when the measurement was unextractable (an `"error"` corner). */
  value: number | null;
  unit?: string;
  status: "pass" | "fail" | "error";
  /** Signed distance to the nearest limit (positive = headroom). `null` when
   *  `value` is `null` or no `limits` were declared. */
  margin?: number | null;
}

/**
 * One expanded PVT corner's simulation result, mirroring `klt sim`'s
 * `corners[]` response entries (`docs/cli/sim.md`).
 */
export interface SignalsCorner {
  /** `<process>/<supply>V/<temp>C`, e.g. `"tt/1.800V/27C"` — see `klt sim`'s
   *  `corner_id` convention. Stable identifier used for corner selection. */
  corner_id: string;
  process?: string | null;
  /** Supply values for this corner, keyed by source/`.param` name. */
  supply_v: Record<string, number>;
  temperature_c: number;
  status: "pass" | "fail" | "error";
  measurements?: SignalsMeasurement[];
  /**
   * Path to this corner's waveform JSON artifact, relative to the block's
   * `output/` directory (the same convention `renders` values use) — staged
   * by `copy-renders.mjs` into `public/blocks/<slug>/...` and fetched
   * client-side by the waveform viewer. Omitted when no waveform was
   * captured for this corner (e.g. `options.waveforms` was false).
   *
   * The referenced file's shape matches `klt sim`'s `artifacts.waveform`
   * artifact exactly (see `docs/cli/sim.md` and
   * `site/src/components/waveform/types.ts`'s `WaveformData`):
   * `{ plotname, variables: [{index, name, type}], points: number[][] }`.
   */
  waveform?: string;
}

/** The `signals` section of `layout.json` (schema v1, provisional pending
 *  #99 — see the field doc on `Layout.signals` above). */
export interface LayoutSignals {
  /** Versioned independently of the top-level `layout.json` schema_version,
   *  since this sub-section's shape is still provisional pending #99. */
  schema_version: number;
  /** `corner_id` of the corner the viewer should select by default. Falls
   *  back to `corners[0]` when omitted or unmatched. */
  default_corner_id?: string;
  corners: SignalsCorner[];
}
