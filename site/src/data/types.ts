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
}
