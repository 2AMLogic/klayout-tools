/**
 * Types for the geode-fem site export (`docs/schemas/em-site-export.schema.json`,
 * issue #849, Phase 1a of Epic #840) — the shape `PatchAntennaResult.tsx`
 * (issue #850) and `SpiralInductorResult.tsx` (issue #877) fetch and wire
 * into `FieldViewer` / `WaveformViewer`.
 *
 * `mesh`/`frames` reuse `FieldMesh`/`FieldFrame` from
 * `@/components/field/types` directly (the schema deliberately shapes them
 * as a strict superset match — see that schema's own `mesh`/`frames`
 * description). Only the `s_parameters` fields this component actually
 * reads are typed here; `additionalProperties: true` on the schema's
 * `s_parameters`/`radiation_pattern`/`provenance` objects means the real
 * export carries more fields than are declared below — that's fine, this
 * is a read-only, intentionally-partial view.
 */
import type { FieldFrame, FieldMesh } from "@/components/field/types";

/**
 * One `frames[]` entry as the export actually ships it — `FieldFrame` plus
 * the `frequency_hz` the schema documents (`["number", "null"]`, "the drive
 * frequency this frame was solved at ... or `null` for a non-frequency-swept
 * frame") but `FieldFrame` itself omits (FieldViewer has no use for it).
 * Needed here to look up the nearest-available field frame for a selected
 * S-parameter frequency — see `emMath.ts`'s `nearestFrameIndex`.
 */
export interface EmFrame extends FieldFrame {
  frequency_hz: number | null;
}

/** One swept-frequency point of `s_parameters.points[]`. */
export interface EmSParameterPoint {
  frequency_hz: number;
  s11_db: number;
  z_re_ohm?: number;
  z_im_ohm?: number;
  s11_re?: number;
  s11_im?: number;
  p_in?: number;
  p_rad?: number;
  efficiency?: number;
  solve_residual_rel?: number;
  /** Spiral-inductor-family extras (issue #877) — inductance/resistance/
   *  quality factor at this sweep point, carried through verbatim from
   *  geode-fem's `results.toml`. Absent on benchmarks that don't extract a
   *  lumped L/R/Q (e.g. the patch antenna). */
  l_nh?: number;
  r_ohm?: number;
  q?: number;
}

/** The `s_parameters` sub-object — the frequency-swept port response a
 *  generic mesh+field viewer has no concept of. */
export interface EmSParameters {
  ports: string[];
  reference_impedance_ohm?: number;
  /** One entry per swept frequency point, ascending by `frequency_hz` (the
   *  same ordering convention `WaveformViewer`'s own artifacts use). */
  points: EmSParameterPoint[];
}

/**
 * Reproducibility record (`docs/schemas/em-site-export.schema.json`'s
 * `provenance` object) — the exact solver build, geometry, and solve
 * parameters a given export traces back to. Rendered by `ProvenancePanel`
 * (issue #851) alongside each gallery entry's field/S-parameter result, so
 * every visual is auditable rather than a bare "trust me". Only the fields
 * `ProvenancePanel` actually reads are typed here; the schema marks
 * `provenance`/`generator`/`geometry` as `additionalProperties: true`, so a
 * real export may carry more fields than declared below.
 */
export interface EmProvenance {
  generator: {
    repo: string;
    license?: string;
    /** Full git commit hash of the geode-fem checkout the solve ran against. */
    commit: string;
    version?: string | null;
    backend?: string;
    build_profile?: string;
  };
  geometry: {
    fixture: string;
    fixture_sha256?: string | null;
    description?: string;
    /** klayout-tools source-repo provenance (issue #842's real-fleet-geometry
     *  entries only, e.g. `interconnect_coupling`) -- distinct from
     *  `generator`, which identifies the geode-fem *solver*, not the
     *  klayout-tools commit the input geometry traces to. Absent for a
     *  benchmark whose geometry is geode-fem's own bundled fixture (e.g.
     *  `patch_antenna`). */
    source_repo?: string;
    source_commit?: string;
    source_block?: string;
    source_layer?: string;
  };
  /** Solver boundary-condition / material parameters — shape varies by
   *  benchmark family, so this is a read-only string-keyed bag rendered
   *  generically (key/value rows) rather than typed field-by-field. */
  solve_parameters?: Record<string, string | number | boolean | null>;
  /** UTC timestamp this export file was generated (not necessarily when the
   *  solve itself ran — see the schema's own field description). */
  generated_at: string;
}

/**
 * One conductor pair's Maxwell capacitance-matrix result
 * (`docs/schemas/em-site-export.schema.json`'s `capacitance` object) — the
 * sibling of `s_parameters` for a benchmark with no driven-port frequency
 * sweep at all (issue #842's `interconnect_coupling` benchmark: a DC
 * multi-conductor electrostatic extraction). A document carries
 * `s_parameters`, `capacitance`, or both — never neither (enforced by the
 * schema's own `anyOf`). Only the fields `InterconnectCouplingResult`
 * actually reads are typed here; the schema marks `capacitance`/`oracle`
 * as `additionalProperties: true`.
 */
export interface EmCapacitanceOracle {
  description?: string;
  source?: string;
  conductor?: string;
  predicted_self_capacitance_farad: number;
  fem_self_capacitance_farad: number;
  rel_err: number | null;
}

export interface EmCapacitance {
  /** Conductor names in `matrix_farad` row/column order, e.g.
   *  `["vgnd", "vpwr"]`. */
  conductors: string[];
  /** The N x N Maxwell capacitance matrix (farads). */
  matrix_farad: number[][];
  matrix_flux_diag_farad?: (number | null)[];
  /** Maxwell-reciprocity hard check — near-zero for a correct extraction,
   *  regardless of mesh quality (see the schema's own field description). */
  max_rel_asymmetry?: number;
  /** Honest, wide-tolerance independent sanity cross-check (e.g. a public
   *  PDK-published parasitic-capacitance model) — `null`/omitted when no
   *  cross-check applies. */
  oracle?: EmCapacitanceOracle | null;
}

/**
 * The full geode-fem site export document. NOTE: `frames.length` and
 * `s_parameters.points.length` are **not** index-aligned in general — the
 * committed patch-antenna export has exactly 1 frame (the FEM resonant
 * frequency) but 13 S-parameter sweep points on a different frequency grid.
 * Never assume a shared integer index between the two arrays.
 *
 * `s_parameters` and `capacitance` are each optional, but the schema's own
 * `anyOf` guarantees at least one is present on any real export (issue
 * #842's addendum) — `patch_antenna` carries `s_parameters` only,
 * `interconnect_coupling` carries `capacitance` only.
 */
export interface EmSiteExport {
  schema_version: number;
  benchmark: string;
  mesh: FieldMesh;
  frames: EmFrame[];
  s_parameters?: EmSParameters;
  capacitance?: EmCapacitance;
  provenance: EmProvenance;
}
