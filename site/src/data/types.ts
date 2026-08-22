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
 *  - "in design — simulation evidence": a canary block repo ingested by
 *    `scripts/ingest-canary.py` (issue #62) that has no layout yet — real
 *    simulation evidence (`spec_summary`/`signals` below), no GDS-derived
 *    metrics. The literal string (not a snake_case enum value like the
 *    others) is the honest status chip text itself, emitted verbatim by the
 *    ingest script — see `blocks/README.md`.
 */
export type LayoutStatus = "ok" | "partial" | "no_artifacts" | "in design — simulation evidence";

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

/** Aggregate outcome of a `klt sim` PVT sweep -- see `docs/cli/sim.md`.
 *  `error` outranks `fail`, which outranks `pass`. */
export type SignalsStatus = "pass" | "fail" | "error";

/** One `.meas`-declared measurement, rolled up across every corner (the
 *  sweep-level `signals.measurements[]` entries — for the per-corner
 *  results the waveform viewer renders, see `SignalsCornerMeasurement`). */
export interface SignalsMeasurement {
  name: string;
  unit?: string;
  /** Always present (per `klt sim`'s own JSON shape) -- `null` when the
   *  request declared no `limits` for this measurement. */
  limits: { min?: number; max?: number } | null;
  /** `"pass"` when no `limits` were declared -- reported, never failed. */
  status: SignalsStatus;
  worst_case: {
    corner_id: string;
    /** `null` when the measurement was unextractable at its worst corner. */
    value: number | null;
    margin: number | null;
  } | null;
}

/** `{ severity: "error", code: "...", message: "..." }` -- see
 *  `docs/cli/sim.md`'s failure-classification table. */
export interface SignalsDiagnostic {
  severity: string;
  code: string;
  message: string;
}

/** A single PVT-corner measurement result (mirrors `klt sim`'s per-corner
 *  `measurements[]` entries — see `docs/cli/sim.md`). */
export interface SignalsCornerMeasurement {
  name: string;
  /** `null` when the measurement was unextractable (an `"error"` corner). */
  value: number | null;
  /** `null` when the request declared no unit for this measurement. */
  unit: string | null;
  status: SignalsStatus;
  /** Signed distance to the nearest limit (positive = headroom). `null` when
   *  `value` is `null` or no `limits` were declared. */
  margin: number | null;
}

/** One expanded PVT corner's result (a trimmed mirror of `klt sim`'s
 *  `corners[]` entry -- the response's `artifacts` object is replaced by
 *  the site-relative `waveform` path below, since the response's absolute
 *  work-directory paths are meaningless once committed). */
export interface SignalsCorner {
  /** `<process>/<supply>V/<temp>C`, e.g. `"tt/1.800V/27C"` — see `klt sim`'s
   *  `corner_id` convention. Stable identifier used for corner selection. */
  corner_id: string;
  process: string | null;
  /** Supply values for this corner, keyed by source/`.param` name. */
  supply_v: Record<string, number>;
  temperature_c: number;
  status: SignalsStatus;
  runtime_s: number;
  measurements: SignalsCornerMeasurement[];
  diagnostics: SignalsDiagnostic[];
  /**
   * Path to this corner's waveform JSON artifact, relative to the block's
   * `output/` directory (the same convention `renders` values use) — staged
   * by `copy-renders.mjs` into `public/blocks/<slug>/...` and fetched
   * client-side by the waveform viewer (issue #100). Present only for the
   * corners `scripts/gallery_signals.py` stages (the nominal-supply /
   * nominal-temperature point of each process corner); omitted for the PVT
   * extremes, whose full artifacts are not committed.
   *
   * The referenced file's shape matches `klt sim`'s `artifacts.waveform`
   * artifact (see `docs/cli/sim.md` and
   * `site/src/components/waveform/types.ts`'s `WaveformData`):
   * `{ plotname, variables: [{index, name, type}], points: number[][] }` —
   * projected down to the testbench's own pins, see that script's
   * "Waveform artifacts" docstring section.
   */
  waveform?: string;
}

/**
 * The `signals` sub-object (issue #99, epic #90 phase 2) -- present only
 * when the build-time pipeline (`scripts/gallery_signals.py`, invoked from
 * `scripts/bootstrap-gallery-blocks.py`) ran `klt sim` for this block
 * against a real, checksum-verified vendored transistor-level netlist
 * (`scripts/fetch-cell-netlists.sh`). Shape mirrors `klt sim`'s own JSON
 * response (`docs/cli/sim.md`), with each corner's `artifacts` object
 * replaced by the site-relative `waveform` path issue #100's viewer
 * consumes. Absent entirely for blocks without a signals run (e.g. no
 * vendored netlist available for that cell) -- never emitted as `null`,
 * same convention as every other optional `Layout` field.
 *
 * This is a strict superset of the provisional shape issue #100 shipped
 * ahead of this pipeline: `default_corner_id` and per-corner `waveform`
 * are preserved and now carry real simulator output rather than the
 * hand-built `inv_1` fixture they were introduced with.
 */
export interface LayoutSignals {
  schema_version: number;
  engine: string;
  /** `null` if the engine's own version banner couldn't be parsed from any
   *  corner's log. */
  engine_version: string | null;
  status: SignalsStatus;
  corner_count: number;
  /** `corner_id` of the corner the viewer should select by default (the
   *  nominal-process nominal-PVT point). Falls back to `corners[0]` when
   *  omitted or unmatched. */
  default_corner_id?: string;
  passed: number;
  failed: number;
  errored: number;
  measurements: SignalsMeasurement[];
  corners: SignalsCorner[];
  /**
   * Present only for the 3 gf180mcu gallery cells -- the vendored
   * standard-cell netlists instantiate `nfet_05v0`/`pfet_05v0` devices with
   * no matching bin in the currently published open primitive-model repo;
   * this records the documented `nfet_05v0`/`pfet_05v0` ->
   * `nmos_6p0`/`pmos_6p0` substitution applied to the generated testbench
   * (never to the vendored cell netlist itself) -- see
   * `scripts/gallery_signals.py`'s module docstring for the full
   * rationale and prior art.
   */
  device_substitution?: Record<string, string>;
}

/**
 * A block's schematic / logic-circuit diagram (issue #1121) — the explainer
 * for the layer renders below it, bridging "colored polygons" to "the
 * circuit those polygons implement" for a viewer who isn't an IC designer.
 * Present only for blocks with a staged `schematic.svg`; see
 * `site/README.md`'s "Schematic section" for the staging convention
 * (`blocks/<slug>/output/schematic.svg`, staged by
 * `site/scripts/copy-renders.mjs` like every other render/artifact path).
 */
export interface LayoutSchematic {
  /** Path to the schematic SVG, relative to the block's `output/`
   *  directory -- the same convention `renders`/`layout_file`/
   *  `signals.corners[].waveform` values use. */
  path: string;
  /**
   * Human-readable provenance line rendered under the diagram, e.g.
   * `"Source: 2AMLogic/sky130-bandgap @ a1b2c3d4e5f6"` or `"Drawn from
   * sky130_fd_sc_hd__inv_1.spice @ 9f8e7d6"` (source repo + commit, or
   * "drawn from <PDK> netlist `<file>@<hash>`" per the issue's provenance
   * convention). Free text, not a structured object, since the two
   * documented formats above don't share a common shape.
   */
  provenance: string;
}

/**
 * One row of a canary block's target-spec table (issue #62), parsed
 * verbatim from the source repo's `README.md` "Target specification"
 * section by `scripts/ingest-canary.py::parse_spec_summary`. Keys mirror
 * that table's own (slugified) column headers -- `parameter`/`target` are
 * present on every current canary; `stretch`/`corner_binding` are present
 * only when the source table has those columns (both wave-1 canaries do,
 * but a future one's table shape isn't hardcoded against). Values are the
 * cell text verbatim (e.g. `"1.20 V ±2% untrimmed"`) -- this is prose for
 * display, not a machine-comparable number.
 */
export interface LayoutSpecRow {
  parameter: string;
  target?: string;
  stretch?: string;
  corner_binding?: string;
  [key: string]: string | undefined;
}

/**
 * A canary block's target-spec summary (issue #62), present only for
 * blocks ingested by `scripts/ingest-canary.py` from a source repo whose
 * `README.md` has a "Target specification" section.
 */
export interface LayoutSpecSummary {
  /** The heading's parenthetical, e.g. `"RATIFIED 2026-07-31, see issue #1
   *  and #35"` or `"DRAFT — engineering to ratify, see issue #1"`. Omitted
   *  when the source heading has no parenthetical. */
  status_note?: string;
  rows: LayoutSpecRow[];
}

/**
 * Provenance for a block ingested from an external source repo (issue #62)
 * -- always present for canary blocks, always absent for #4-corpus /
 * design-pipeline blocks (which have no external source repo).
 */
export interface LayoutSource {
  /** `"<org>/<name>"`, e.g. `"2AMLogic/gf180-bandgap"`. */
  repo: string;
  /** The exact pinned commit sha this ingest run used (`git rev-parse
   *  HEAD` after checkout) -- always a full 40-char sha, never a branch
   *  name, so it stays meaningful after the source repo moves on. */
  ref: string;
  /** Path within the source repo this block corresponds to, relative to
   *  its root. Currently always `"."` -- every canary repo is one block --
   *  reserved for a future monorepo-style source. */
  path: string;
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
  /**
   * The PDK family this block targets (issue #1285) -- one of `klt`'s own
   * deck names (`"sky130"`, `"gf180mcu"`, `"sg13g2"`), the same vocabulary
   * `drc.deck` uses. Optional and omit-absent: it is written only by a
   * pipeline that actually knows the PDK (the corpus bootstrap, which walks
   * `tests/corpus/<pdk>/`; a canary ingest with `--pdk` or a recognised
   * `<pdk>-<name>` slug), never guessed into place. `DetailPage.tsx` prefers
   * it over its own slug heuristic when picking the GDS viewer's `pdk=`
   * identifier, and falls back to that heuristic for blocks that predate
   * this field.
   */
  pdk?: string;
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
  /**
   * Map of render id -> path relative to the layout.json location (#60).
   * `"overview"` is always the all-layers composite thumbnail (used by
   * `LayoutCard.tsx`); every other key is a human-readable render label --
   * a PDK layer name (e.g. `"li1.drawing"`, `"Metal2"`) when the content
   * pipeline recognised the layer, a `"layer_<n>_<n>"` fallback otherwise,
   * or `"center_crop"` for a zoomed composite (issue #942, "Option B" from
   * #651) -- never a bare `"67_20"`-style filename stem. `DetailPage.tsx`
   * renders the overview first, then every other entry grouped below it.
   */
  renders?: Record<string, string>;
  /**
   * Whether `layout_file` may be linked as a download on the block detail
   * page (#64). NOT part of the `klt layout-metrics` contract above — this
   * is a presence-check flag set by the content pipeline / public-repo gate
   * (#62): `scripts/ingest-canary.py` sets it to `true` unconditionally for
   * every block that passes the fail-closed public-repo check, regardless
   * of whether `layout_file` itself is present yet (the download section
   * still only renders once both are set — see `DetailPage.tsx`).
   */
  downloadable?: boolean;
  /**
   * Real-cell `klt sim` PVT sweep results (issue #99), consumed by the
   * detail page's Signals section / waveform viewer (issue #100). The
   * section is gated on this field the same way `renders` / `downloadable`
   * / `drc` are gated (presence, not truthiness). See `LayoutSignals`
   * above.
   *
   * Also reused, with the same shape, by `scripts/ingest-canary.py` (#62)
   * for canary blocks' *own* `sim/` evidence trail — a source repo's
   * append-only simulation records, not a `klt sim` run this repo performs
   * itself. See `blocks/README.md` and that script's module docstring.
   */
  signals?: LayoutSignals;
  /** Canary block target-spec summary (issue #62). See `LayoutSpecSummary`. */
  spec_summary?: LayoutSpecSummary;
  /** Canary block source-repo provenance (issue #62). See `LayoutSource`. */
  source?: LayoutSource;
  /**
   * Optional schematic / logic-circuit diagram (issue #1121), staged from
   * `blocks/<slug>/output/schematic.svg`. Gated the same omit-absent way as
   * `renders`/`downloadable`/`signals` -- a block with no diagram renders no
   * "Schematic" section. See `LayoutSchematic`.
   */
  schematic?: LayoutSchematic;
}
