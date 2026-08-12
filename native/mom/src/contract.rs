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

/// Default number of sub-filaments per axis used by the PEEC
/// (inductance/resistance) path when a request omits
/// `filament_subdivisions`. `1` means "one filament per box"; `n` splits
/// every box into `n^3` filaments (`n` along the current-flow axis and `n`
/// along each transverse axis).
///
/// The partial-element integrals are evaluated in closed form (see
/// `peec.rs`), so refining this mesh is a *self-consistency* axis rather
/// than an accuracy axis -- it must reproduce the same inductance to within
/// numerical precision. `docs/design/mom-validation.md` measures that.
pub const DEFAULT_FILAMENT_SUBDIVISIONS: usize = 1;

/// Upper bound on `filament_subdivisions`. The partial-inductance matrix is
/// dense and `O(n_filaments^2)` to fill, and `n_filaments` grows as the
/// *cube* of this knob, so a large value is far more expensive than it
/// looks; the filament-count cap in `geometry.rs` is the real guard, this
/// just rejects an obviously-wrong value early.
pub const MAX_FILAMENT_SUBDIVISIONS: usize = 8;

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
    /// Sub-filaments per axis for the PEEC inductance solve; omit to use
    /// `DEFAULT_FILAMENT_SUBDIVISIONS`. Ignored entirely when no conductor
    /// declares a conductivity (the capacitance-only path).
    #[serde(default)]
    pub filament_subdivisions: Option<usize>,
    pub conductors: Vec<ConductorRequest>,
}

#[derive(Debug, Deserialize)]
pub struct ConductorRequest {
    /// Electrical node name (e.g. a net name). Multiple boxes may share a
    /// name -- the Python layer uses this to group several GDS shapes
    /// (possibly on different layers, e.g. a coax outer conductor's four
    /// wall segments) into one electrical conductor.
    pub name: String,
    pub boxes: Vec<BoxRequest>,
    /// Bulk DC conductivity in siemens per metre (e.g. ~5.8e7 for copper,
    /// ~3.77e7 for aluminium). **Declaring it on any conductor switches the
    /// solve into PEEC mode** -- partial inductance + DC resistance are
    /// computed alongside the capacitance matrix, and every conductor must
    /// then declare one (a partial declaration is rejected rather than
    /// silently treated as a perfect conductor). Omitted everywhere ⇒ the
    /// original capacitance-only solve, byte-for-byte unchanged.
    #[serde(default, rename = "conductivity_S_per_m")]
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
    /// The PEEC partial-inductance matrix, in nanohenries, in the same
    /// conductor order as `conductors`: `inductance_matrix_nh[j][k]` is the
    /// partial mutual inductance between conductor `j`'s and conductor `k`'s
    /// current path (the diagonal is each conductor's partial *self*
    /// inductance). Symmetric. `null` when no conductor declared a
    /// conductivity (capacitance-only solve).
    ///
    /// These are *partial* elements in Ruehli's sense -- they are defined
    /// per conductor segment without reference to a return path, and only
    /// become a loop inductance once a caller closes the circuit. See
    /// docs/cli/mom.md's "Scope and limitations".
    pub inductance_matrix_nh: Option<Vec<Vec<f64>>>,
    /// Per-conductor DC (series) resistance in ohms, in the same order as
    /// `conductors`: the series sum over that conductor's boxes of
    /// `length / (conductivity * cross_sectional_area)`. `null` when no
    /// conductor declared a conductivity.
    pub resistance_ohm: Option<Vec<f64>>,
    /// Total PEEC filament count across every conductor (`0` on a
    /// capacitance-only solve) -- the inductance-side analogue of
    /// `panel_count`.
    pub filament_count: usize,
    /// Non-fatal diagnostics about the *physicality* of the returned matrix
    /// (see `solver::physicality_warnings`). Empty on a well-resolved solve.
    /// A populated list means the numbers came back but should not be
    /// trusted -- almost always a `panel_size_um` too coarse relative to the
    /// smallest conductor-to-conductor separation.
    pub warnings: Vec<String>,
}

/// Bumped `1 -> 2` by issue #797, which added `inductance_matrix_nh`,
/// `resistance_ohm` and `filament_count` to the response (and
/// `conductivity_S_per_m` / `filament_subdivisions` to the request).
/// Kept in sync manually with `src/klayout_tools/mom.py`'s `SCHEMA_VERSION`
/// -- there is no shared source of truth across the Rust/Python boundary,
/// the same way `DEFAULT_PANEL_SIZE_UM` is mirrored by hand.
pub const RESPONSE_SCHEMA_VERSION: u32 = 2;
