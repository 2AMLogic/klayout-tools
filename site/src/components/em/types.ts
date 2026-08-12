/**
 * Types for the geode-fem site export (`docs/schemas/em-site-export.schema.json`,
 * issue #849, Phase 1a of Epic #840) — the shape `PatchAntennaResult.tsx`
 * (issue #850) fetches and wires into `FieldViewer` / `WaveformViewer`.
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
 * The full geode-fem site export document. NOTE: `frames.length` and
 * `s_parameters.points.length` are **not** index-aligned in general — the
 * committed patch-antenna export has exactly 1 frame (the FEM resonant
 * frequency) but 13 S-parameter sweep points on a different frequency grid.
 * Never assume a shared integer index between the two arrays.
 */
export interface EmSiteExport {
  schema_version: number;
  benchmark: string;
  mesh: FieldMesh;
  frames: EmFrame[];
  s_parameters: EmSParameters;
  provenance: EmProvenance;
}
