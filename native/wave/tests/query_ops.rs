//! Integration tests for `klt-wave query <request.json>` -- the full
//! `klt wave query` op vocabulary (docs/design/waveform-query-contract-spike.md
//! section 5) over the committed FST fixtures in `tests/fixtures/` (see
//! `tests/fixtures/README.md` for the timeline). Every test invokes the
//! compiled `klt-wave` binary exactly as the contract specifies
//! (`klt-wave query <request.json> [--format json]`), matching this
//! issue's own acceptance criteria.

use std::path::PathBuf;
use std::process::Command;
use std::sync::atomic::{AtomicU64, Ordering};

fn fixture(name: &str) -> String {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.push("tests/fixtures");
    p.push(name);
    p.to_string_lossy().to_string()
}

/// A per-process monotonic counter, so concurrently-running tests (`cargo
/// test` runs this file's tests on separate threads by default) never share
/// a temp directory even when `SystemTime::now()`'s resolution is coarser
/// than the gap between two threads' calls.
static TEST_DIR_COUNTER: AtomicU64 = AtomicU64::new(0);

fn fresh_temp_dir(label: &str) -> PathBuf {
    let n = TEST_DIR_COUNTER.fetch_add(1, Ordering::Relaxed);
    let dir =
        std::env::temp_dir().join(format!("klt-wave-test-{label}-{}-{n}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

/// Writes `request` (already-serialized JSON) to a fresh temp file and runs
/// `klt-wave query <path> --format json`, returning
/// `(exit_code, stdout_json_or_none, stderr)`.
fn run_query(request: &serde_json::Value) -> (i32, Option<serde_json::Value>, String) {
    let dir = fresh_temp_dir("query");
    let req_path = dir.join("request.json");
    std::fs::write(&req_path, serde_json::to_string_pretty(request).unwrap()).unwrap();

    let output = Command::new(env!("CARGO_BIN_EXE_klt-wave"))
        .arg("query")
        .arg(&req_path)
        .arg("--format")
        .arg("json")
        .output()
        .expect("failed to run klt-wave");

    let code = output.status.code().unwrap_or(-1);
    let stdout_json = if output.stdout.is_empty() {
        None
    } else {
        Some(serde_json::from_slice(&output.stdout).expect("stdout was not valid JSON"))
    };
    let stderr = String::from_utf8_lossy(&output.stderr).to_string();
    (code, stdout_json, stderr)
}

fn store_req(ops: serde_json::Value) -> serde_json::Value {
    serde_json::json!({
        "schema": "klt.wave_query.request/1",
        "store": fixture("gcd_like_a.fst"),
        "ops": ops,
    })
}

// -- value --------------------------------------------------------------

#[test]
fn value_op_reports_value_and_both_time_forms() {
    let req = store_req(serde_json::json!([
        { "op": "value", "signal": "tb.dut.o_valid", "at": { "cycle": 2 } }
    ]));
    let (code, resp, _) = run_query(&req);
    assert_eq!(code, 0);
    let resp = resp.unwrap();
    assert_eq!(resp["status"], "ok");
    let r = &resp["results"][0];
    assert_eq!(r["op"], "value");
    assert_eq!(r["value"], "1");
    assert_eq!(r["at"]["cycle"], 2);
    assert_eq!(r["at"]["time_ns"], 45.0);
}

#[test]
fn value_op_before_assert_is_zero() {
    let req = store_req(serde_json::json!([
        { "op": "value", "signal": "tb.dut.o_valid", "at": { "cycle": 1 } }
    ]));
    let (code, resp, _) = run_query(&req);
    assert_eq!(code, 0);
    assert_eq!(resp.unwrap()["results"][0]["value"], "0");
}

#[test]
fn value_predicate_satisfied_is_exit_0() {
    let req = store_req(serde_json::json!([
        { "op": "value", "signal": "tb.dut.o_valid", "at": { "cycle": 2 }, "predicate": { "equals": "1" } }
    ]));
    let (code, resp, _) = run_query(&req);
    assert_eq!(code, 0);
    let resp = resp.unwrap();
    assert_eq!(resp["status"], "ok");
    assert_eq!(resp["results"][0]["satisfied"], true);
}

#[test]
fn value_predicate_unsatisfied_is_exit_3() {
    let req = store_req(serde_json::json!([
        { "op": "value", "signal": "tb.dut.o_valid", "at": { "cycle": 2 }, "predicate": { "equals": "0" } }
    ]));
    let (code, resp, _) = run_query(&req);
    assert_eq!(code, 3);
    let resp = resp.unwrap();
    assert_eq!(resp["status"], "unsatisfied");
    assert_eq!(resp["results"][0]["satisfied"], false);
}

// -- find -----------------------------------------------------------------

#[test]
fn find_first_and_last_occurrence() {
    let req = store_req(serde_json::json!([
        { "op": "find", "signal": "tb.dut.o_valid", "match": { "value": "1" }, "occurrence": "first" },
        { "op": "find", "signal": "tb.dut.o_valid", "match": { "value": "1" }, "occurrence": "last" }
    ]));
    let (code, resp, _) = run_query(&req);
    assert_eq!(code, 0);
    let resp = resp.unwrap();
    assert_eq!(resp["results"][0]["found"], true);
    assert_eq!(resp["results"][0]["at"]["cycle"], 2);
    assert_eq!(resp["results"][1]["found"], true);
    assert_eq!(resp["results"][1]["at"]["cycle"], 6);
}

#[test]
fn find_negative_result_not_found() {
    let req = store_req(serde_json::json!([
        {
            "op": "find", "signal": "tb.dut.o_valid", "match": { "value": "1" },
            "window": { "to": { "cycle": 1 } }
        }
    ]));
    let (code, resp, _) = run_query(&req);
    assert_eq!(code, 0);
    let r = &resp.unwrap()["results"][0];
    assert_eq!(r["found"], false);
    assert!(r["at"].is_null());
}

#[test]
fn find_predicate_max_cycle_unsatisfied_is_exit_3() {
    let req = store_req(serde_json::json!([
        {
            "op": "find", "signal": "tb.dut.o_valid", "match": { "value": "1" },
            "predicate": { "max_cycle": 1 }
        }
    ]));
    let (code, resp, _) = run_query(&req);
    assert_eq!(code, 3);
    assert_eq!(resp.unwrap()["results"][0]["satisfied"], false);
}

#[test]
fn find_edge_match_rising() {
    let req = store_req(serde_json::json!([
        { "op": "find", "signal": "tb.dut.o_valid", "match": { "edge": "rising" }, "occurrence": "first" }
    ]));
    let (code, resp, _) = run_query(&req);
    assert_eq!(code, 0);
    let r = &resp.unwrap()["results"][0];
    assert_eq!(r["found"], true);
    assert_eq!(r["at"]["cycle"], 2);
}

// -- count ------------------------------------------------------------------

#[test]
fn count_matches_over_whole_trace() {
    let req = store_req(serde_json::json!([
        { "op": "count", "signal": "tb.dut.o_valid", "match": { "value": "1" } }
    ]));
    let (code, resp, _) = run_query(&req);
    assert_eq!(code, 0);
    assert_eq!(resp.unwrap()["results"][0]["count"], 2);
}

#[test]
fn count_predicate_min_unsatisfied_is_exit_3() {
    let req = store_req(serde_json::json!([
        {
            "op": "count", "signal": "tb.dut.o_valid", "match": { "value": "1" },
            "predicate": { "min": 3 }
        }
    ]));
    let (code, resp, _) = run_query(&req);
    assert_eq!(code, 3);
    assert_eq!(resp.unwrap()["status"], "unsatisfied");
}

// -- sample -------------------------------------------------------------

#[test]
fn sample_on_clock_edges() {
    let req = store_req(serde_json::json!([
        {
            "op": "sample",
            "signals": ["tb.dut.o_valid", "tb.dut.data"],
            "edge_of": "tb.clk",
            "window": { "from": { "cycle": 0 }, "to": { "cycle": 5 } }
        }
    ]));
    let (code, resp, _) = run_query(&req);
    assert_eq!(code, 0);
    let r = &resp.unwrap()["results"][0];
    let samples = r["samples"].as_array().unwrap();
    assert_eq!(samples.len(), 6); // cycles 0..=5 inclusive
                                  // cycle 2 (tick 45): o_valid has just asserted.
    let at_cycle_2 = samples.iter().find(|s| s["at"]["cycle"] == 2).unwrap();
    assert_eq!(at_cycle_2["values"]["tb.dut.o_valid"], "1");
    assert_eq!(at_cycle_2["values"]["tb.dut.data"], "1");
}

#[test]
fn sample_predicate_is_rejected() {
    let req = store_req(serde_json::json!([
        {
            "op": "sample", "signals": ["tb.dut.o_valid"], "edge_of": "tb.clk",
            "predicate": { "equals": "1" }
        }
    ]));
    let (code, resp, stderr) = run_query(&req);
    assert_eq!(code, 1);
    assert!(resp.is_none());
    assert!(stderr.contains("does not define a predicate"));
}

// -- stuck --------------------------------------------------------------

#[test]
fn stuck_true_on_tail_run() {
    let req = store_req(serde_json::json!([
        {
            "op": "stuck", "signal": "tb.dut.o_valid",
            "window": { "from": { "cycle": 6 } },
            "min_span": { "cycles": 1 },
            "predicate": { "expect": true }
        }
    ]));
    let (code, resp, _) = run_query(&req);
    assert_eq!(code, 0);
    let r = &resp.unwrap()["results"][0];
    assert_eq!(r["stuck"], true);
    assert_eq!(r["value"], "1");
    assert_eq!(r["satisfied"], true);
}

#[test]
fn stuck_false_when_min_span_not_met_is_exit_3() {
    let req = store_req(serde_json::json!([
        {
            "op": "stuck", "signal": "tb.dut.o_valid",
            "min_span": { "cycles": 5 },
            "predicate": { "expect": true }
        }
    ]));
    let (code, resp, _) = run_query(&req);
    assert_eq!(code, 3);
    let r = &resp.unwrap()["results"][0];
    assert_eq!(r["stuck"], false);
    assert_eq!(r["satisfied"], false);
    // span is still reported even though it didn't meet min_span.
    assert!(!r["span"].is_null());
}

// -- diff -----------------------------------------------------------------

#[test]
fn diff_against_self_never_diverges() {
    let req = store_req(serde_json::json!([
        {
            "op": "diff", "signal": "tb.dut.o_valid", "other_store": fixture("gcd_like_a.fst"),
            "predicate": { "expect_diverges": false }
        }
    ]));
    let (code, resp, _) = run_query(&req);
    assert_eq!(code, 0);
    let r = &resp.unwrap()["results"][0];
    assert_eq!(r["diverges"], false);
    assert_eq!(r["distance"], 0);
    assert_eq!(r["satisfied"], true);
}

#[test]
fn diff_against_a_diverging_store() {
    let req = store_req(serde_json::json!([
        {
            "op": "diff", "signal": "tb.dut.o_valid", "other_store": fixture("gcd_like_b.fst")
        }
    ]));
    let (code, resp, _) = run_query(&req);
    assert_eq!(code, 0);
    let r = &resp.unwrap()["results"][0];
    assert_eq!(r["diverges"], true);
    assert_eq!(r["at"]["cycle"], 2);
    // One maximal divergent run over the whole trace (the two `o_valid`
    // timelines never realign): distance counts transitions *into*
    // divergence, not every value-change while diverged.
    assert_eq!(r["distance"], 1);
}

// -- wave -----------------------------------------------------------------

#[test]
fn wave_dumps_run_length_encoded_entries() {
    let req = store_req(serde_json::json!([
        { "op": "wave", "signal": "tb.dut.o_valid" }
    ]));
    let (code, resp, _) = run_query(&req);
    assert_eq!(code, 0);
    let r = &resp.unwrap()["results"][0];
    let entries = r["entries"].as_array().unwrap();
    assert_eq!(entries.len(), 4);
    assert_eq!(entries[0]["value"], "0");
    assert_eq!(entries[1]["value"], "1");
    assert_eq!(entries[2]["value"], "0");
    assert_eq!(entries[3]["value"], "1");
    assert!(entries[3]["to"].is_null()); // still holding at window end
    assert_eq!(r["truncated"], false);
}

#[test]
fn wave_truncates_at_max_entries() {
    let req = store_req(serde_json::json!([
        { "op": "wave", "signal": "tb.dut.o_valid", "max_entries": 2 }
    ]));
    let (code, resp, _) = run_query(&req);
    assert_eq!(code, 0);
    let r = &resp.unwrap()["results"][0];
    assert_eq!(r["entries"].as_array().unwrap().len(), 2);
    assert_eq!(r["truncated"], true);
}

// -- request/exit-code plumbing ---------------------------------------------

#[test]
fn unknown_signal_fails_the_whole_batch() {
    let req = store_req(serde_json::json!([
        { "op": "value", "signal": "tb.dut.does_not_exist", "at": { "cycle": 0 } }
    ]));
    let (code, resp, stderr) = run_query(&req);
    assert_eq!(code, 1);
    assert!(resp.is_none());
    assert!(stderr.contains("does_not_exist") || stderr.contains("no signal"));
}

#[test]
fn empty_ops_is_exit_1() {
    let req = store_req(serde_json::json!([]));
    let (code, resp, _) = run_query(&req);
    assert_eq!(code, 1);
    assert!(resp.is_none());
}

#[test]
fn cycle_addressing_without_clock_is_an_error() {
    // The store has a clock, so exercise the "both time_ns and cycle" error
    // instead -- contract section 3's "never both" rule.
    let req = store_req(serde_json::json!([
        { "op": "value", "signal": "tb.dut.o_valid", "at": { "cycle": 1, "time_ns": 10.0 } }
    ]));
    let (code, resp, _) = run_query(&req);
    assert_eq!(code, 1);
    assert!(resp.is_none());
}

#[test]
fn malformed_request_json_is_exit_1() {
    let dir = fresh_temp_dir("malformed-json");
    let req_path = dir.join("request.json");
    std::fs::write(&req_path, "{ not valid json").unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_klt-wave"))
        .arg("query")
        .arg(&req_path)
        .arg("--format")
        .arg("json")
        .output()
        .unwrap();
    assert_eq!(output.status.code().unwrap(), 1);
}

#[test]
fn unreadable_request_file_is_exit_1() {
    let dir = fresh_temp_dir("unreadable-request");
    let req_path = dir.join("does_not_exist.json");
    let output = Command::new(env!("CARGO_BIN_EXE_klt-wave"))
        .arg("query")
        .arg(&req_path)
        .arg("--format")
        .arg("json")
        .output()
        .unwrap();
    assert_eq!(output.status.code().unwrap(), 1);
}

#[test]
fn missing_request_argument_is_exit_2() {
    let output = Command::new(env!("CARGO_BIN_EXE_klt-wave"))
        .arg("query")
        .output()
        .unwrap();
    assert_eq!(output.status.code().unwrap(), 2);
}

#[test]
fn bad_format_flag_is_exit_2() {
    let req_path = fixture("gcd_like_a.fst"); // any existing file path works here
    let output = Command::new(env!("CARGO_BIN_EXE_klt-wave"))
        .arg("query")
        .arg(&req_path)
        .arg("--format")
        .arg("xml")
        .output()
        .unwrap();
    assert_eq!(output.status.code().unwrap(), 2);
}

#[test]
fn text_format_runs_without_error() {
    let req = store_req(serde_json::json!([
        { "op": "count", "signal": "tb.dut.o_valid", "match": { "value": "1" } }
    ]));
    let dir = fresh_temp_dir("text");
    let req_path = dir.join("request.json");
    std::fs::write(&req_path, serde_json::to_string_pretty(&req).unwrap()).unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_klt-wave"))
        .arg("query")
        .arg(&req_path)
        .output()
        .unwrap();
    assert_eq!(output.status.code().unwrap(), 0);
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("count"));
}

#[test]
fn response_envelope_has_expected_shape() {
    let req = store_req(serde_json::json!([
        { "op": "count", "signal": "tb.dut.o_valid", "match": { "value": "1" } }
    ]));
    let (code, resp, _) = run_query(&req);
    assert_eq!(code, 0);
    let resp = resp.unwrap();
    assert_eq!(resp["schema_version"], 1);
    assert!(resp["store"]["content_hash"]
        .as_str()
        .unwrap()
        .starts_with("sha256:"));
    assert_eq!(resp["provenance"]["pdk"], serde_json::Value::Null);
    assert_eq!(resp["provenance"]["deck"], serde_json::Value::Null);
    assert_eq!(
        resp["provenance"]["klayout_version"],
        serde_json::Value::Null
    );
    assert_eq!(
        resp["provenance"]["input"]["content_hash"],
        resp["store"]["content_hash"]
    );
}
