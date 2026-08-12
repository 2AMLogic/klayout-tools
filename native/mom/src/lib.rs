//! `klt_mom_native` -- the Rust core behind `klt mom` (2AMLogic/klayout-tools
//! issue #718, Phase 0/1 of the Method-of-Moments epic #701).
//!
//! This is the first Rust component in klayout-tools. It is packaged as a
//! standalone `maturin`-built pyo3 extension module (`native/mom/`, its own
//! `Cargo.toml` + `pyproject.toml`) rather than folding into the top-level
//! `pyproject.toml`'s `hatchling` build -- that keeps every other `klt`
//! command's packaging untouched while establishing where future Rust
//! engines live (`native/<engine>/`, per `docs/ARCHITECTURE.md`'s "Rewrite
//! rule"). See `docs/cli/mom.md` for the build/install instructions and the
//! full JSON contract this module implements.
//!
//! `solve_mom_json` is the single entry point exposed to Python: it accepts
//! and returns JSON strings (see `contract.rs`) so the Rust/Python boundary
//! stays a plain data contract, not a bespoke object graph -- consistent
//! with every other `klt` verb being JSON-contracted end to end
//! (`docs/json-contract.md`).

mod contract;
mod geometry;
mod peec;
mod solver;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use contract::{
    MomRequest, MomResponse, DEFAULT_FILAMENT_SUBDIVISIONS, DEFAULT_PANEL_SIZE_UM,
    RESPONSE_SCHEMA_VERSION,
};

/// Run the quasi-static solve end to end: parse `request_json`, discretise
/// every conductor's boxes into panels, fill the potential-coefficient
/// matrix, solve for the Maxwell capacitance matrix, and -- when any
/// conductor declares a `conductivity_S_per_m` -- additionally discretise
/// the same boxes into PEEC current filaments and return the partial
/// inductance matrix and per-conductor DC resistance. The response is a JSON
/// string (see `contract::MomResponse`).
///
/// Raises `ValueError` (surfaced to Python as `klayout_tools.mom.MomError`)
/// for malformed input JSON or any solver-level failure (empty/degenerate
/// geometry, singular potential-coefficient matrix, non-finite result, or a
/// geometry that cannot carry current in PEEC mode).
#[pyfunction]
fn solve_mom_json(request_json: &str) -> PyResult<String> {
    let request: MomRequest = serde_json::from_str(request_json)
        .map_err(|e| PyValueError::new_err(format!("invalid mom request JSON: {e}")))?;

    let panel_size_um = request.panel_size_um.unwrap_or(DEFAULT_PANEL_SIZE_UM);
    let panels =
        geometry::discretize(&request.conductors, panel_size_um).map_err(PyValueError::new_err)?;

    let conductor_count = request.conductors.len();
    let capacitance_matrix_ff = solver::solve_capacitance_matrix_ff(
        &panels,
        conductor_count,
        request.background_permittivity,
    )
    .map_err(PyValueError::new_err)?;

    // PEEC (inductance + resistance) is opt-in: declaring a conductivity on
    // any conductor turns it on. It is not on by default because the
    // capacitance path happily accepts zero-thickness plates, which have no
    // cross-section to carry current -- silently inventing a current path
    // for one would be worse than not extracting an inductance at all.
    let peec_requested = request
        .conductors
        .iter()
        .any(|c| c.conductivity_s_per_m.is_some());
    let mut peec_warnings = Vec::new();
    let mut inductance_matrix_nh = None;
    let mut resistance_ohm = None;
    let mut filament_count = 0;

    if peec_requested {
        let missing: Vec<&str> = request
            .conductors
            .iter()
            .filter(|c| c.conductivity_s_per_m.is_none())
            .map(|c| c.name.as_str())
            .collect();
        if !missing.is_empty() {
            return Err(PyValueError::new_err(format!(
                "conductivity_S_per_m is set on some conductors but not on {missing:?} \
                 -- set it on every conductor to extract inductance/resistance, or on \
                 none to run the capacitance-only solve (a partial declaration is \
                 rejected rather than silently assuming a perfect conductor)"
            )));
        }
        let subdivisions = request
            .filament_subdivisions
            .unwrap_or(DEFAULT_FILAMENT_SUBDIVISIONS);
        let (filaments, warnings) =
            geometry::discretize_filaments(&request.conductors, subdivisions)
                .map_err(PyValueError::new_err)?;
        peec_warnings = warnings;
        filament_count = filaments.len();
        inductance_matrix_nh = Some(
            peec::partial_inductance_matrix_nh(&filaments, conductor_count)
                .map_err(PyValueError::new_err)?,
        );
        resistance_ohm = Some(
            geometry::dc_resistance_ohm(&request.conductors, &filaments)
                .map_err(PyValueError::new_err)?,
        );
    }

    let conductors: Vec<String> = request.conductors.into_iter().map(|c| c.name).collect();
    let mut warnings = solver::physicality_warnings(&capacitance_matrix_ff, &conductors);
    warnings.extend(peec_warnings);

    let response = MomResponse {
        schema_version: RESPONSE_SCHEMA_VERSION,
        conductors,
        capacitance_matrix_ff,
        panel_count: panels.len(),
        inductance_matrix_nh,
        resistance_ohm,
        filament_count,
        warnings,
    };

    serde_json::to_string(&response)
        .map_err(|e| PyValueError::new_err(format!("failed to serialise mom response: {e}")))
}

/// The `klt_mom_native` Python extension module.
#[pymodule]
fn klt_mom_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(solve_mom_json, m)?)?;
    Ok(())
}
