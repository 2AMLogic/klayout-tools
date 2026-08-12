//! `klt-statime-native` -- a native-Rust liberty/NLDM gate-level static
//! timing engine, originally a go/no-go spike for issue #809 (Epic #704
//! Phase 1c; see `README.md` for that verdict and the measured comparison
//! against OpenSTA) and now wired into `klt synthesize` as a production
//! `pyo3`/`maturin` extension (issue #925, the follow-on `README.md`'s own
//! "Not in scope" section named). The Python<->Rust boundary is a single
//! `#[pyfunction]`, [`critical_path_json`], mirroring `native/mom/src/lib.rs`'s
//! `solve_mom_json` shape (plain scalar/string args in, a JSON string out --
//! `StaResult` already derives `serde::Serialize`, so no new serialisation
//! code was needed for this boundary).

pub mod liberty;
pub mod netlist;
pub mod nldm;
pub mod sta;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

/// Run this crate's static-timing analysis end to end and return the
/// result (`sta::StaResult`, JSON-serialised) as a Python string.
///
/// `netlist_path` is a `write_verilog -noattr`-produced structural Verilog
/// file (`netlist::parse`'s own input shape); `liberty_path` is the `.lib`
/// file the same synthesis run mapped against; `top` is the module name to
/// analyze. `input_transition_ns`/`output_load_pf` are the uniform boundary
/// condition applied to every primary input/output (see `README.md`'s
/// "Known simplifications" #2 -- no SDC, no `create_clock`, no per-pin
/// overrides; this is the same boundary condition the standalone
/// `klt-statime critical-path` CLI's own defaults use).
///
/// Raises `ValueError` (surfaced to Python as a plain exception the caller
/// maps to its own error type, matching `solve_mom_json`'s convention) for
/// an unreadable file, a malformed liberty/netlist, or an analysis failure
/// (e.g. a cell type absent from the liberty).
///
/// This crate's `liberty.rs`/`netlist.rs` parsers are hand-written
/// recursive-descent parsers that `panic!` on unexpected input, rather than
/// threading a `Result` through every token match (acceptable for the
/// standalone `klt-statime` CLI's own error mode -- a panic there just
/// exits nonzero) -- but a panic crossing the Python/Rust boundary would
/// otherwise surface as an opaque `pyo3_runtime.PanicException` that
/// `sta.py`'s `except ValueError` (mirroring `solve_mom_json`'s own
/// `ValueError`-only convention) does not catch, defeating `klt
/// synthesize`'s "degrade to `sta: null`, never crash the run" contract for
/// this additive stage (issue #925 AC). `catch_unwind` below turns any such
/// panic into the same `ValueError` every other failure mode already
/// raises.
#[pyfunction]
fn critical_path_json(
    netlist_path: &str,
    liberty_path: &str,
    top: &str,
    input_transition_ns: f64,
    output_load_pf: f64,
) -> PyResult<String> {
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        run_critical_path(
            netlist_path,
            liberty_path,
            top,
            input_transition_ns,
            output_load_pf,
        )
    }))
    .unwrap_or_else(|payload| Err(PyValueError::new_err(panic_message(&payload))))
}

fn panic_message(payload: &(dyn std::any::Any + Send)) -> String {
    if let Some(s) = payload.downcast_ref::<&str>() {
        format!("static-timing analysis panicked: {s}")
    } else if let Some(s) = payload.downcast_ref::<String>() {
        format!("static-timing analysis panicked: {s}")
    } else {
        "static-timing analysis panicked (malformed liberty/netlist input?)".to_string()
    }
}

fn run_critical_path(
    netlist_path: &str,
    liberty_path: &str,
    top: &str,
    input_transition_ns: f64,
    output_load_pf: f64,
) -> PyResult<String> {
    let liberty_src = std::fs::read_to_string(liberty_path)
        .map_err(|e| PyValueError::new_err(format!("read liberty '{liberty_path}': {e}")))?;
    let lib = liberty::parse(&liberty_src).map_err(|e| PyValueError::new_err(e.to_string()))?;

    let netlist_src = std::fs::read_to_string(netlist_path)
        .map_err(|e| PyValueError::new_err(format!("read netlist '{netlist_path}': {e}")))?;
    let module =
        netlist::parse(&netlist_src, top).map_err(|e| PyValueError::new_err(e.to_string()))?;

    let cfg = sta::StaConfig {
        input_transition_ns,
        output_load_pf,
    };
    let result =
        sta::analyze(&module, &lib, &cfg).map_err(|e| PyValueError::new_err(e.to_string()))?;

    serde_json::to_string(&result)
        .map_err(|e| PyValueError::new_err(format!("failed to serialise sta result: {e}")))
}

/// The `klt_statime_native` Python extension module.
#[pymodule]
fn klt_statime_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(critical_path_json, m)?)?;
    Ok(())
}
