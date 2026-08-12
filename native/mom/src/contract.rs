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
}

/// `1` -- capacitance-only (issue #718/#719).
/// `2` -- adds `inductance_matrix_nh`/`resistance_ohm`/`filament_count`
///        (issue #797), each `null`/omitted unless the request opted in via
///        `compute_inductance`. Existing capacitance-only requests/responses
///        are byte-for-byte unaffected; the version bumps because the
///        *response shape* gained new fields, per this contract's own
///        convention (see docs/cli/mom.md's JSON schema table).
pub const RESPONSE_SCHEMA_VERSION: u32 = 2;
