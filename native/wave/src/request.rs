//! `klt.wave_query.request/1` / response types, written fresh against
//! `docs/design/waveform-query-contract-spike.md` section 5 -- not ported
//! from `boldaxolotl/booley` (bwave's own CLI-flag request shape does not
//! resemble this JSON request/response document contract at all).

use serde::{Deserialize, Serialize};
use serde_json::Value;

// -- time addressing (contract section 3) -----------------------------------

/// A request-side time point: carries exactly one of `time_ns` / `cycle`.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct TimePointReq {
    #[serde(default)]
    pub time_ns: Option<f64>,
    #[serde(default)]
    pub cycle: Option<i64>,
}

/// A response-side time point: both forms, always reported together.
#[derive(Debug, Clone, Serialize)]
pub struct TimePointResp {
    pub time_ns: f64,
    pub cycle: Option<i64>,
}

/// A bounded span, `{"from": <time-point>, "to": <time-point>}`; both sides
/// optional (`from` omitted -> start of trace, `to` omitted -> end of trace).
#[derive(Debug, Clone, Default, Deserialize)]
pub struct WindowReq {
    #[serde(default)]
    pub from: Option<TimePointReq>,
    #[serde(default)]
    pub to: Option<TimePointReq>,
}

/// A duration, `{"cycles": <int>}` or `{"time_ns": <number>}` -- distinct
/// from a time point (contract section 5, "Durations vs. points").
#[derive(Debug, Clone, Default, Deserialize)]
pub struct DurationReq {
    #[serde(default)]
    pub cycles: Option<i64>,
    #[serde(default)]
    pub time_ns: Option<f64>,
}

/// A level match (`{"value": "1"}`) or an edge match
/// (`{"edge": "rising"|"falling"|"any"}`) -- shared by `find` and `count`.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct MatchReq {
    #[serde(default)]
    pub value: Option<String>,
    #[serde(default)]
    pub edge: Option<String>,
}

fn default_occurrence() -> String {
    "first".to_string()
}

fn default_sample_edge() -> String {
    "rising".to_string()
}

fn default_max_entries() -> usize {
    256
}

/// One query op (contract section 5, "Query op vocabulary").
#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
pub enum OpReq {
    Value {
        signal: String,
        at: TimePointReq,
        #[serde(default)]
        predicate: Option<Value>,
    },
    Find {
        signal: String,
        #[serde(rename = "match")]
        match_: MatchReq,
        #[serde(default = "default_occurrence")]
        occurrence: String,
        #[serde(default)]
        window: WindowReq,
        #[serde(default)]
        predicate: Option<Value>,
    },
    Count {
        signal: String,
        #[serde(rename = "match")]
        match_: MatchReq,
        #[serde(default)]
        window: WindowReq,
        #[serde(default)]
        predicate: Option<Value>,
    },
    Sample {
        signals: Vec<String>,
        edge_of: String,
        #[serde(default = "default_sample_edge")]
        edge: String,
        #[serde(default)]
        window: WindowReq,
        #[serde(default)]
        predicate: Option<Value>,
    },
    Stuck {
        signal: String,
        #[serde(default)]
        window: WindowReq,
        min_span: DurationReq,
        #[serde(default)]
        predicate: Option<Value>,
    },
    Diff {
        signal: String,
        other_store: String,
        #[serde(default)]
        window: WindowReq,
        #[serde(default)]
        predicate: Option<Value>,
    },
    Wave {
        signal: String,
        #[serde(default)]
        window: WindowReq,
        #[serde(default = "default_max_entries")]
        max_entries: usize,
        #[serde(default)]
        predicate: Option<Value>,
    },
}

/// `klt.wave_query.request/1` (contract section 5).
#[derive(Debug, Clone, Deserialize)]
pub struct QueryRequest {
    #[allow(dead_code)]
    #[serde(default)]
    pub schema: Option<String>,
    pub store: String,
    pub ops: Vec<OpReq>,
}

// -- response -----------------------------------------------------------

#[derive(Debug, Clone, Serialize)]
pub struct StoreRef {
    pub path: String,
    pub content_hash: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct InputRef {
    pub content_hash: String,
}

/// Shared `provenance` block (docs/json-contract.md), with `pdk`/`deck`/
/// `klayout_version` always `null` per contract section 2 -- `klt wave`
/// resolves neither a PDK nor a rule deck, and never invokes the
/// `klayout`/`pya` engine.
#[derive(Debug, Clone, Serialize)]
pub struct Provenance {
    pub klt_version: String,
    pub klayout_version: Option<String>,
    pub pdk: Option<String>,
    pub deck: Option<String>,
    pub input: InputRef,
}

/// `klt.wave_query.response/1` (contract section 5).
#[derive(Debug, Clone, Serialize)]
pub struct QueryResponse {
    pub schema_version: u32,
    pub store: StoreRef,
    pub status: String,
    pub results: Vec<Value>,
    pub provenance: Provenance,
}
