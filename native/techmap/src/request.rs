//! `klt.synth.techmap.request/1` / `klt.synth.techmap.response/1`, exactly
//! per `docs/design/synth-techmap-stage-contract.md` section 6.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Default, Deserialize)]
pub struct Constraints {
    #[serde(default)]
    pub clock_period_ns: Option<f64>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Request {
    #[allow(dead_code)]
    pub schema: Option<String>,
    pub generic_netlist: String,
    pub liberty: String,
    #[serde(default)]
    pub constraints: Constraints,
}

#[derive(Debug, Clone, Serialize)]
pub struct Timing {
    pub source: String,
    pub critical_path_ps: Option<f64>,
    pub delay_target_ps: Option<f64>,
}

#[derive(Debug, Clone, Serialize)]
pub struct Response {
    pub schema_version: u32,
    pub top: String,
    pub status: String,
    pub instance_count: usize,
    pub area_um2: f64,
    pub instance_counts_by_type: BTreeMap<String, usize>,
    pub timing: Option<Timing>,
    pub mapped_netlist_path: String,
    /// **Additive** field, issue #875 -- the pre-mapping generic netlist
    /// (`request.generic_netlist`) re-emitted as self-contained behavioral
    /// Verilog (`verilog::write_generic`), for use as `klt equiv`'s `gold`
    /// side against `mapped_netlist_path`'s own `gate` side. Per
    /// `docs/json-contract.md`'s convention this does not bump
    /// `schema_version` -- no existing field changed shape or meaning.
    pub generic_netlist_verilog_path: String,
}
