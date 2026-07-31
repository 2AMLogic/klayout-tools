/**
 * TypeScript types for the `layout.json` data contract (schema v1).
 *
 * `layout.json` is the per-block metrics artifact defined by issue #61
 * ("Gallery: per-layout metrics extractor"). As of this loader landing,
 * #61 has not shipped yet, so this schema is a **provisional bootstrap**
 * built against the fields #61 itself enumerates (name, slug, description,
 * layer count, cell/instance counts, DRC violation count, render image
 * paths) plus the `status`/envelope fields that mirror the equivalent
 * `board.json` contract in kicad-tools (rjwalters/kicad-tools#3676). Keep
 * this file in sync with #61's landed schema once it merges — the fields
 * below are expected to be additive-only, but the source of truth moves to
 * #61's own documentation at that point.
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

/**
 * A fully-parsed layout record matching `layout.json` schema v1.
 *
 * Required fields (always present): `$schema`, `schema_version`,
 * `generated_at`, `slug`, `status`. All other fields are optional and
 * omitted when the underlying artifact/metric is unavailable.
 */
export interface Layout {
  $schema: string;
  schema_version: number;
  generated_at: string;
  slug: string;
  status: LayoutStatus;
  /** Block/cell name as reported by `klt cells` (may differ from slug). */
  name?: string;
  description?: string;
  /** Open PDK the block targets, e.g. "sky130", "gf180mcu". */
  pdk?: string;
  /** From `klt layers` — number of distinct (layer, datatype) pairs. */
  layer_count?: number;
  /** From `klt cells` — total cell count in the hierarchy. */
  cell_count?: number;
  /** From `klt cells` — total instance count across the hierarchy. */
  instance_count?: number;
  /** From `klt drc`, when a DRC deck has been run for this block. */
  drc_violation_count?: number;
  /** Map of render id -> path relative to the layout.json location (#60). */
  renders?: Record<string, string>;
}
