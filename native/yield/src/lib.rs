//! `klt_yield_native` -- the Rust statistics core behind `klt yield`
//! (issue #816, Phase 1a of the statistical/yield epic #710).
//!
//! This is this repo's **third** Rust component (after `native/mom/`, issue
//! #718, and `native/congestion/`, issue #785), following the same
//! standalone `maturin`-built pyo3 extension convention (`native/<engine>/`,
//! per `docs/ARCHITECTURE.md`'s "Where Rust code lives") -- its own
//! `Cargo.toml` + `pyproject.toml`, reached from the top-level
//! `pyproject.toml` via a PEP 735 dependency group.
//!
//! The split with Python mirrors `mom.py`/`congestion.py`: reading a `klt
//! sim` Monte Carlo report (or a plain sample-set document) and turning it
//! into this crate's request shape is the Python layer's job
//! (`src/klayout_tools/yield_analysis.py`); everything statistical --
//! distribution fit, exact and parametric yield estimates, their confidence
//! intervals, Cp/Cpk/sigma-to-spec, and the sample-size verdict -- lives
//! here.
//!
//! **The one rule this crate enforces structurally**: a yield number never
//! travels without its confidence interval and its sample count. The
//! response types make an interval-less estimate unrepresentable
//! (`contract::Estimate` has no such variant), and
//! `estimate::guard_no_bare_point_estimate` rejects a degenerate interval
//! before anything is serialised. A request that could only produce a bare
//! point estimate (one sample, a confidence of 0 or 1) is an **error**, not
//! a warning -- see `docs/cli/yield.md`, "Never a bare point estimate".

mod contract;
mod estimate;
mod sensitivity;
mod stats;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use contract::{SensitivityRequest, YieldRequest};

/// The whole analysis as plain Rust: JSON string in, JSON string out, with
/// a plain `String` error.
///
/// Kept separate from the `#[pyfunction]` wrapper below so `cargo test` can
/// exercise the end-to-end path without an initialised CPython interpreter
/// (constructing a `PyErr` needs one, so a test that touched `PyResult`
/// would panic under a plain `cargo test`).
pub fn run_json(request_json: &str) -> Result<String, String> {
    let request: YieldRequest = serde_json::from_str(request_json)
        .map_err(|e| format!("invalid yield request JSON: {e}"))?;

    let response = estimate::analyze(&request)?;

    serde_json::to_string(&response).map_err(|e| format!("failed to serialise yield response: {e}"))
}

/// Analyze a Monte Carlo sample set against spec limits: parse
/// `request_json`, fit and estimate, and return the response as a JSON
/// string (see `contract::YieldResponse`).
///
/// Raises `ValueError` (surfaced to Python as
/// `klayout_tools.yield_analysis.YieldError`) for malformed input JSON, an
/// unusable request (no limits, a degenerate confidence level), or a sample
/// set too small to carry a confidence interval.
#[pyfunction]
fn analyze_yield_json(request_json: &str) -> PyResult<String> {
    run_json(request_json).map_err(PyValueError::new_err)
}

/// The whole sensitivity ranking as plain Rust: JSON string in, JSON string
/// out. Kept separate from the `#[pyfunction]` wrapper for the same reason
/// as [`run_json`] -- `cargo test` exercises it with no CPython interpreter
/// involved.
pub fn run_sensitivity_json(request_json: &str) -> Result<String, String> {
    let request: SensitivityRequest = serde_json::from_str(request_json)
        .map_err(|e| format!("invalid sensitivity request JSON: {e}"))?;

    let response = sensitivity::analyze(&request)?;

    serde_json::to_string(&response)
        .map_err(|e| format!("failed to serialise sensitivity response: {e}"))
}

/// Rank a completed campaign's device/process parameters by their
/// contribution to an output metric's variance: parse `request_json`, rank,
/// and return the response as a JSON string (see
/// `contract::SensitivityResponse`).
///
/// Raises `ValueError` (surfaced to Python as
/// `klayout_tools.yield_sensitivity.SensitivityError`) for malformed input
/// JSON, too few usable samples, a constant output, or every declared
/// parameter being constant.
#[pyfunction]
fn analyze_sensitivity_json(request_json: &str) -> PyResult<String> {
    run_sensitivity_json(request_json).map_err(PyValueError::new_err)
}

/// The `klt_yield_native` Python extension module.
#[pymodule]
fn klt_yield_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(analyze_yield_json, m)?)?;
    m.add_function(wrap_pyfunction!(analyze_sensitivity_json, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn analyze_yield_json_round_trips_a_minimal_request() {
        let request = r#"{
            "measurements": [
                {
                    "name": "vref",
                    "unit": "V",
                    "samples": [1.20, 1.21, 1.19, 1.205, 1.195, 1.202, 1.198, 1.207],
                    "limits": {"min": 1.15, "max": 1.25}
                }
            ]
        }"#;
        let response = run_json(request).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&response).unwrap();
        assert_eq!(parsed["schema_version"], 1);
        assert_eq!(parsed["confidence"], 0.95);
        assert_eq!(parsed["measurements"][0]["n"], 8);
        assert_eq!(
            parsed["measurements"][0]["yield"]["empirical"]["estimate"],
            1.0
        );
        assert!(
            parsed["measurements"][0]["yield"]["empirical"]["confidence_interval"]["low"]
                .as_f64()
                .unwrap()
                > 0.0
        );
    }

    #[test]
    fn analyze_yield_json_rejects_a_sample_set_that_cannot_carry_an_interval() {
        let request = r#"{
            "measurements": [
                {"name": "vref", "samples": [1.2], "limits": {"min": 1.15, "max": 1.25}}
            ]
        }"#;
        let err = run_json(request).unwrap_err();
        assert!(err.contains("confidence interval"), "{err}");
    }

    #[test]
    fn run_sensitivity_json_round_trips_a_minimal_request() {
        let request = r#"{
            "measurements": [
                {
                    "name": "gain_db",
                    "unit": "dB",
                    "parameters": [
                        {"x1": 0.1, "x2": 0.9},
                        {"x1": 0.4, "x2": 0.2},
                        {"x1": 0.9, "x2": 0.5},
                        {"x1": 0.2, "x2": 0.7},
                        {"x1": 0.6, "x2": 0.1},
                        {"x1": 0.8, "x2": 0.4}
                    ],
                    "output": [1.1, 4.3, 9.2, 2.3, 6.2, 8.1]
                }
            ]
        }"#;
        let response = run_sensitivity_json(request).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&response).unwrap();
        assert_eq!(parsed["schema_version"], 1);
        assert_eq!(parsed["measurements"][0]["n"], 6);
        assert_eq!(parsed["measurements"][0]["ranking"][0]["parameter"], "x1");
        assert_eq!(parsed["measurements"][0]["ranking"][0]["rank"], 1);
    }

    #[test]
    fn run_sensitivity_json_rejects_a_request_below_the_sample_floor() {
        let request = r#"{
            "measurements": [
                {
                    "name": "gain_db",
                    "parameters": [{"x1": 0.1}, {"x1": 0.4}],
                    "output": [1.1, 4.3]
                }
            ]
        }"#;
        let err = run_sensitivity_json(request).unwrap_err();
        assert!(err.contains("at least"), "{err}");
    }
}
