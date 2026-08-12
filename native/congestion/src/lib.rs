//! `klt_congestion_native` -- the Rust core behind the FLUTE/RUDY-family
//! post-placement congestion pre-check for the fleet DSE loop (issue #785,
//! Epic #700 Phase 1 §3.6 of
//! `docs/design/place-and-route-improvements-survey.md`).
//!
//! This is this repo's **second** Rust component (after `native/mom/`,
//! issue #718), following the same standalone `maturin`-built pyo3
//! extension convention (`native/<engine>/`, per `docs/ARCHITECTURE.md`'s
//! "Rewrite rule") -- its own `Cargo.toml` + `pyproject.toml`, reached from
//! the top-level `pyproject.toml` via a PEP 735 dependency group.
//!
//! Turning a placement DEF into this crate's request shape (parsing
//! `COMPONENTS`/`NETS`/`PINS`) is the Python layer's job
//! (`src/klayout_tools/congestion.py`) -- this crate only implements the
//! numerically hot part: per-net RSMT-family length estimation (`rsmt.rs`)
//! and RUDY-style demand-grid distribution + summary statistics
//! (`grid.rs`). See `contract.rs` for the full request/response shape and
//! `docs/design/flute-congestion-precheck-results.md` for the correlation
//! study this pre-check's own acceptance criteria require before any
//! caller wires it into a real gating decision.
//!
//! **This is a research-spike component, not a shipped `klt` CLI verb.**
//! Nothing here changes `klt place-and-route`'s request/response contract
//! (issue #785's own explicit acceptance criterion) -- it exists to be
//! called from an internal pre-check inside `digital_fleet.py`'s
//! candidate-ranking loop (#445), once/if the correlation study justifies
//! that integration.

mod contract;
mod grid;
mod rsmt;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use contract::CongestionRequest;

/// Run the congestion pre-check end to end: parse `request_json`, estimate
/// every net's RSMT-family length, distribute it RUDY-style across the
/// requested tile grid, and return the response as a JSON string (see
/// `contract::CongestionResponse`).
///
/// Raises `ValueError` (surfaced to Python as
/// `klayout_tools.congestion.CongestionError`) for malformed input JSON or
/// an invalid grid/die-box request.
#[pyfunction]
fn estimate_congestion_json(request_json: &str) -> PyResult<String> {
    let request: CongestionRequest = serde_json::from_str(request_json)
        .map_err(|e| PyValueError::new_err(format!("invalid congestion request JSON: {e}")))?;

    let response = grid::estimate(
        request.die_box_um,
        request.grid_cols,
        request.grid_rows,
        request.layer_count,
        request.track_pitch_um,
        &request.nets,
    )
    .map_err(PyValueError::new_err)?;

    serde_json::to_string(&response)
        .map_err(|e| PyValueError::new_err(format!("failed to serialise congestion response: {e}")))
}

/// The `klt_congestion_native` Python extension module.
#[pymodule]
fn klt_congestion_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(estimate_congestion_json, m)?)?;
    Ok(())
}
