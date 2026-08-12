//! JSON in/out shapes for the `klt mom` Method-of-Moments capacitance core.
//!
//! This is the "documented JSON interface" the Rust core exposes to the
//! `klayout_tools` Python package (`src/klayout_tools/mom.py`) over the pyo3
//! binding in `lib.rs`. It is deliberately a *narrower* contract than the
//! `klt mom` CLI's own output (see `docs/cli/mom.md`): the Python layer is
//! responsible for GDS/stackup parsing and for wrapping this shape into the
//! shared `klt` envelope (`schema_version`, `error.command`/`error.message`)
//! documented in `docs/json-contract.md` -- this module only defines the
//! solver's own request/response payload.

use serde::{Deserialize, Serialize};

/// Default panel edge length (micrometers) used when a request omits
/// `panel_size_um`. Chosen as a reasonable default for typical on-chip
/// MOM-cap / coax test geometries (single-digit micrometers), not tuned
/// against any specific accuracy target -- accuracy-vs-panel-count tradeoffs
/// are owned by the validation issue (#719).
pub const DEFAULT_PANEL_SIZE_UM: f64 = 0.5;

/// Default PEEC filament edge length (micrometers) used when a request sets
/// `compute_inductance` but omits `filament_size_um`. See `peec.rs`'s
/// module docs for the filament discretisation this feeds.
pub const DEFAULT_FILAMENT_SIZE_UM: f64 = 1.0;

/// Default axial mesh segment edge length (micrometers) used when a request
/// sets `frequencies_hz` but omits `segment_size_um`. See `fullwave.rs`'s
/// module docs for the axial discretisation this feeds -- larger than
/// `DEFAULT_FILAMENT_SIZE_UM` since it targets the (typically longer)
/// current-flow axis rather than a cross-section.
pub const DEFAULT_SEGMENT_SIZE_UM: f64 = 5.0;

/// Default port reference impedance (ohms) used when a `ports` entry omits
/// `reference_impedance_ohm` -- 50 ohms, the standard RF/microwave
/// convention (issue #894, Phase 2b of the MoM epic #701).
pub const DEFAULT_PORT_REFERENCE_IMPEDANCE_OHM: f64 = 50.0;

#[derive(Debug, Deserialize)]
pub struct MomRequest {
    /// Relative permittivity of the uniform background dielectric. The MVP
    /// solves in a single homogeneous medium (no per-layer dielectric
    /// stack) -- see docs/cli/mom.md's "Scope and limitations".
    pub background_permittivity: f64,
    /// Target panel edge length in micrometers used to discretise each
    /// conductor face. Smaller values give more panels (more accurate, more
    /// expensive); omit to use `DEFAULT_PANEL_SIZE_UM`.
    #[serde(default)]
    pub panel_size_um: Option<f64>,
    pub conductors: Vec<ConductorRequest>,
    /// Opt into the PEEC (Partial Element Equivalent Circuit) partial
    /// self/mutual-inductance and DC-resistance solve (`peec.rs`) alongside
    /// the capacitance solve. `false` (the default) preserves the original
    /// capacitance-only contract exactly -- every existing caller/fixture
    /// keeps working unchanged. When `true`, every conductor must reduce to
    /// a single bar-shaped box (see `peec::MIN_BAR_ASPECT_RATIO`) sharing a
    /// common current-flow axis and axial extent with every other
    /// conductor, and must set `conductivity_s_per_m` -- see
    /// docs/cli/mom.md's "PEEC inductance/resistance" section for the full
    /// scope and the reasoning behind these MVP restrictions.
    #[serde(default)]
    pub compute_inductance: bool,
    /// Target filament edge length in micrometers used to discretise each
    /// PEEC-eligible conductor's cross-section into current-carrying
    /// filaments. Smaller values give more filaments (more accurate, more
    /// expensive); omit to use `DEFAULT_FILAMENT_SIZE_UM`. Only consulted
    /// when `compute_inductance` is set.
    #[serde(default)]
    pub filament_size_um: Option<f64>,
    /// Opt into the frequency-domain, full-wave (retarded Green's function)
    /// partial-impedance solve (`fullwave.rs`, issue #893) alongside the
    /// quasi-static capacitance solve. Empty (the default) preserves the
    /// original contract exactly. Each entry is a frequency in Hz (must be
    /// positive and finite); every conductor must satisfy the same
    /// bar-shaped-conductor MVP restriction `compute_inductance` uses (see
    /// docs/cli/mom.md's "Full-wave frequency sweep" section) -- independent
    /// of whether `compute_inductance` is also set.
    #[serde(default)]
    pub frequencies_hz: Vec<f64>,
    /// Target axial mesh segment edge length in micrometers used to
    /// discretise the shared current-flow axis for the full-wave solve.
    /// Smaller values give more segments (more accurate, more expensive);
    /// omit to use `DEFAULT_SEGMENT_SIZE_UM`. Only consulted when
    /// `frequencies_hz` is non-empty.
    #[serde(default)]
    pub segment_size_um: Option<f64>,
    /// Opt into port definition + de-embedding (issue #894, Phase 2b of the
    /// MoM epic #701): report S-parameters, referenced to each port's
    /// reference plane and reference impedance, alongside the raw partial
    /// impedance matrix `full_wave_sweep` already reports. Empty (the
    /// default) preserves the original contract exactly. When non-empty,
    /// must have **exactly two** entries (the MVP's canonical two-port
    /// case), `frequencies_hz` must be non-empty, and the request must have
    /// exactly two conductors satisfying the same bar-shaped-conductor
    /// restriction the full-wave solve itself requires -- see
    /// `fullwave.rs`'s "Ports and de-embedding" module docs.
    #[serde(default)]
    pub ports: Vec<PortRequest>,
}

/// One port definition for the full-wave solve's S-parameter extraction
/// (issue #894): a reference plane location along the shared current-flow
/// axis, plus a reference impedance. See `fullwave.rs`'s "Ports and
/// de-embedding" module docs for how these are used.
#[derive(Debug, Deserialize, Clone, Copy)]
pub struct PortRequest {
    /// Axial position of this port's reference plane, in micrometers,
    /// measured along the full-wave solve's shared current-flow axis (the
    /// same axis `classify_full_wave_bars` resolves). Must lie within
    /// `[axis_lo_um, axis_hi_um]` of the modeled bar span -- a port outside
    /// the modeled geometry has nothing to de-embed against.
    pub position_um: f64,
    /// Port reference impedance, ohms (real, positive). Omit to use
    /// `DEFAULT_PORT_REFERENCE_IMPEDANCE_OHM` (50 ohms, the standard RF
    /// convention).
    #[serde(default)]
    pub reference_impedance_ohm: Option<f64>,
}

#[derive(Debug, Deserialize)]
pub struct ConductorRequest {
    /// Electrical node name (e.g. a net name). Multiple boxes may share a
    /// name -- the Python layer uses this to group several GDS shapes
    /// (possibly on different layers, e.g. a coax outer conductor's four
    /// wall segments) into one electrical conductor.
    pub name: String,
    pub boxes: Vec<BoxRequest>,
    /// Conductor bulk conductivity, siemens per meter. Required (and used
    /// only) when the request's `compute_inductance` is `true`, to compute
    /// this conductor's DC resistance (`R = length / (conductivity *
    /// cross_sectional_area)`) -- see `peec.rs`. There is no default: an
    /// omitted value with `compute_inductance: true` is a request error, not
    /// a silently-assumed conductivity (e.g. copper vs. aluminum vs. a
    /// silicided diffusion are an order of magnitude apart -- guessing would
    /// be worse than refusing).
    #[serde(default)]
    pub conductivity_s_per_m: Option<f64>,
}

/// One axis-aligned rectangular prism contributing surface to a conductor.
///
/// All values are in micrometers. `z0_um == z1_um` (zero thickness) is
/// treated as an idealised flat plate (a single face is discretised,
/// avoiding six coincident zero-separation faces that would otherwise make
/// the potential-coefficient matrix singular). `x0_um`/`x1_um` and
/// `y0_um`/`y1_um` need not be pre-sorted; the solver normalises min/max
/// itself.
#[derive(Debug, Deserialize, Clone, Copy)]
pub struct BoxRequest {
    pub x0_um: f64,
    pub y0_um: f64,
    pub x1_um: f64,
    pub y1_um: f64,
    pub z0_um: f64,
    pub z1_um: f64,
}

#[derive(Debug, Serialize)]
pub struct MomResponse {
    pub schema_version: u32,
    /// Conductor names in the same order as `capacitance_matrix_ff`'s rows
    /// and columns.
    pub conductors: Vec<String>,
    /// The Maxwell (short-circuit) capacitance matrix, in femtofarads:
    /// `capacitance_matrix_ff[j][k]` is the charge (fF, i.e. divided by 1V)
    /// induced on conductor `j` when conductor `k` is held at 1V and every
    /// other conductor is grounded. Diagonal entries are positive; typical
    /// off-diagonal entries are negative (see docs/cli/mom.md).
    pub capacitance_matrix_ff: Vec<Vec<f64>>,
    /// Total panel count across every conductor (informational -- lets a
    /// caller sanity-check discretisation density without re-deriving it).
    pub panel_count: usize,
    /// Non-fatal diagnostics about the *physicality* of the returned matrix
    /// (see `solver::physicality_warnings`). Empty on a well-resolved solve.
    /// A populated list means the numbers came back but should not be
    /// trusted -- almost always a `panel_size_um` too coarse relative to the
    /// smallest conductor-to-conductor separation.
    pub warnings: Vec<String>,
    /// The partial-inductance matrix, in nanohenries:
    /// `inductance_matrix_nh[j][k]` is conductor `j`'s partial self
    /// inductance (`j == k`) or the partial mutual inductance between
    /// conductors `j` and `k` (`j != k`), in the Ruehli PEEC sense (no
    /// implied return path). `Some` only when the request set
    /// `compute_inductance: true`; `None` (omitted from the JSON entirely)
    /// otherwise, so the original capacitance-only response shape is
    /// unchanged when the feature is not used. See `peec.rs`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub inductance_matrix_nh: Option<Vec<Vec<f64>>>,
    /// Per-conductor DC resistance, in ohms, in the same order as
    /// `conductors`: `resistance_ohm[j] = length_j / (conductivity_j *
    /// cross_sectional_area_j)`. `Some` only when the request set
    /// `compute_inductance: true` (resistance is computed alongside
    /// inductance, from the same bar geometry and the conductor's
    /// `conductivity_s_per_m`).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub resistance_ohm: Option<Vec<f64>>,
    /// Total PEEC filament count across every conductor (informational,
    /// mirrors `panel_count`). `Some` only when `compute_inductance` was set.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub filament_count: Option<usize>,
    /// One entry per `frequencies_hz` request entry, in the same order --
    /// the frequency-domain full-wave sweep (`fullwave.rs`, issue #893).
    /// `Some` only when the request's `frequencies_hz` was non-empty; `None`
    /// (omitted from the JSON entirely) otherwise, so a request that never
    /// asked for a full-wave solve gets a byte-for-byte unchanged response
    /// shape -- this addition is purely additive, no `schema_version` bump.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub full_wave_sweep: Option<Vec<FullWavePoint>>,
    /// Total axial segment count across every conductor for the full-wave
    /// solve's mesh (informational, mirrors `panel_count`/`filament_count`;
    /// identical at every swept frequency since the mesh does not depend on
    /// frequency). `Some` only when `full_wave_sweep` is.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub full_wave_segment_count: Option<usize>,
}

/// One frequency point of the full-wave sweep (`fullwave.rs`): the complex
/// partial-impedance matrix between every conductor pair, plus -- for
/// exactly two conductors -- the derived characteristic impedance and
/// propagation constant of the modeled transmission-line segment. See
/// `fullwave.rs`'s module docs for the method and docs/cli/mom.md's
/// "Full-wave frequency sweep" section for how to read these fields.
#[derive(Debug, Serialize)]
pub struct FullWavePoint {
    pub frequency_hz: f64,
    /// Real part of the partial-impedance matrix, ohms:
    /// `impedance_matrix_real_ohm[j][k]` is conductor `j`'s partial self
    /// impedance (`j == k`) or the partial mutual impedance between
    /// conductors `j` and `k` (`j != k`) at this frequency, in the same
    /// Ruehli-PEEC sense as `inductance_matrix_nh` (no implied return path)
    /// -- generalised to nonzero frequency via the retarded Green's
    /// function. Same conductor order as `conductors`.
    pub impedance_matrix_real_ohm: Vec<Vec<f64>>,
    /// Imaginary part of the same matrix, ohms.
    pub impedance_matrix_imag_ohm: Vec<Vec<f64>>,
    /// Real part of the characteristic impedance, ohms. `Some` only when
    /// the request had exactly two conductors (the canonical
    /// two-conductor transmission-line case); `None` otherwise.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub characteristic_impedance_real_ohm: Option<f64>,
    /// Imaginary part of the characteristic impedance, ohms.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub characteristic_impedance_imag_ohm: Option<f64>,
    /// Attenuation constant (the real part of the propagation constant
    /// gamma = alpha + j*beta), nepers per meter.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub attenuation_np_per_m: Option<f64>,
    /// Phase constant (the imaginary part of gamma), radians per meter.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub phase_rad_per_m: Option<f64>,
    /// De-embedded 2-port S-parameters at this frequency, referenced to each
    /// port's `position_um`/`reference_impedance_ohm` (issue #894, Phase 2b
    /// of the MoM epic #701). `Some` only when the request's `ports` had
    /// exactly two entries; `None` (omitted from the JSON entirely)
    /// otherwise -- purely additive, no `schema_version` bump, same
    /// convention as this struct's own fields above. See `fullwave.rs`'s
    /// "Ports and de-embedding" module docs.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub s_parameters: Option<SParameters>,
}

/// One frequency point's de-embedded 2-port S-parameters (issue #894). Each
/// `sJK` is complex, split into real/imaginary parts for the JSON contract
/// (matching `impedance_matrix_real_ohm`/`impedance_matrix_imag_ohm`'s own
/// convention rather than a nested `{real, imag}` object) -- port 1 is
/// `ports[0]`, port 2 is `ports[1]`, in the request's order.
#[derive(Debug, Serialize)]
pub struct SParameters {
    pub s11_real: f64,
    pub s11_imag: f64,
    pub s12_real: f64,
    pub s12_imag: f64,
    pub s21_real: f64,
    pub s21_imag: f64,
    pub s22_real: f64,
    pub s22_imag: f64,
}

/// `1` -- capacitance-only (issue #718/#719).
/// `2` -- adds `inductance_matrix_nh`/`resistance_ohm`/`filament_count`
///        (issue #797), each `null`/omitted unless the request opted in via
///        `compute_inductance`. Existing capacitance-only requests/responses
///        are byte-for-byte unaffected; the version bumps because the
///        *response shape* gained new fields, per this contract's own
///        convention (see docs/cli/mom.md's JSON schema table).
///
/// Issue #893 adds `full_wave_sweep`/`full_wave_segment_count` (each
/// `null`/omitted unless the request set `frequencies_hz`) **without**
/// bumping this constant: per `docs/json-contract.md`'s general envelope
/// policy ("adding new fields does not require a bump"), which the `#2`
/// bump above predates and did not strictly need either -- see this issue's
/// acceptance criteria for the explicit "no schema_version bump" call.
///
/// Issue #894 adds `FullWavePoint::s_parameters` (`null`/omitted unless the
/// request set exactly two `ports`) under the same "purely additive, no
/// bump" convention #893 established.
pub const RESPONSE_SCHEMA_VERSION: u32 = 2;
