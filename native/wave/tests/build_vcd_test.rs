//! End-to-end test for `klt wave build` against a committed small VCD
//! fixture (`tests/fixtures/counter.vcd`): a 3-signal free-running-clock
//! testbench (`tb.clk`, `tb.rst_n`, `tb.count[3:0]`) with a mid-period
//! reset release, used to exercise clock-period detection, reset-release
//! cycle-0 anchoring, and vector (multi-bit) value changes together.
//! Confirms the resulting store is a real, independently-readable FST
//! file (via `fst-reader`, standing in for issue #1599's `klt wave
//! query` until that lands) -- not just that this crate's own response
//! JSON looks right.

use std::fs;
use std::io::BufReader;
use std::path::PathBuf;

use fst_reader::{FstFilter, FstHierarchyEntry, FstReader, FstSignalValue};
use klt_wave_native::build;

fn fixture_path(name: &str) -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("fixtures")
        .join(name)
}

/// Writes a request JSON into a fresh temp dir, pointing `trace.path` at
/// the committed fixture by absolute path and `store.path` at a relative
/// `.klt/wave/<name>.klwave` inside the temp dir -- mirroring how a real
/// `.klt/wave/` output tree is laid out next to a request file.
fn write_request(dir: &std::path::Path, body: &str) -> PathBuf {
    let request_path = dir.join("request.json");
    fs::write(&request_path, body).unwrap();
    request_path
}

#[test]
fn build_from_vcd_with_clock_and_reset() {
    let tmp = tempfile::tempdir().unwrap();
    let trace_path = fixture_path("counter.vcd");
    let request = format!(
        r#"{{
            "schema": "klt.wave_build.request/1",
            "trace": {{ "path": {trace_path:?}, "format": "vcd" }},
            "clock": {{ "signal": "tb.clk", "edge": "rising" }},
            "reset": {{ "signal": "tb.rst_n", "active": "low" }},
            "signals": null,
            "store": {{ "path": ".klt/wave/counter.klwave" }}
        }}"#,
    );
    let request_path = write_request(tmp.path(), &request);

    let response = build::run(&request_path).expect("build should succeed");

    assert_eq!(response.schema_version, 1);
    assert_eq!(response.trace.format, "vcd");
    assert!(response.trace.size_bytes > 0);

    assert_eq!(response.signal_count, 3);
    assert_eq!(response.value_change_count, 30);

    assert_eq!(response.timescale.unit, "ns");
    assert_eq!(response.timescale.value, 1);

    let clock = response.clock.expect("clock block should be present");
    assert_eq!(clock.signal, "tb.clk");
    assert_eq!(clock.edge, "rising");
    assert_eq!(clock.period_ns, Some(10.0));

    let reset = response.reset.expect("reset block should be present");
    assert_eq!(reset.signal, "tb.rst_n");
    assert_eq!(reset.active, "low");
    let release = reset.release.expect("reset release should be resolved");
    // Reset deasserts at tick 38 (not itself a clock edge); cycle 0 is the
    // first clock edge at/after that -- the rising edge at tick 45 (edges
    // at 5, 15, 25, 35, 45, ... every 10 ticks).
    assert_eq!(release.time_ns, 45.0);
    assert_eq!(release.cycle, Some(0));

    assert_eq!(response.time_range.from.time_ns, 0.0);
    assert_eq!(
        response.time_range.from.cycle, None,
        "tick 0 precedes cycle 0's anchor at tick 45"
    );
    assert_eq!(response.time_range.to.time_ns, 100.0);
    assert_eq!(response.time_range.to.cycle, Some(5));

    assert!(response.store.size_bytes > 0);
    assert!(response.store.content_hash.starts_with("sha256:"));

    // Shared `provenance` block (docs/json-contract.md): `pdk`/`deck`/
    // `klayout_version` always null (`klt wave` resolves neither and never
    // invokes the `klayout` engine); `input.content_hash` is the **trace**
    // file's hash, not the store's.
    assert!(response.provenance.klayout_version.is_none());
    assert!(response.provenance.pdk.is_none());
    assert!(response.provenance.deck.is_none());
    assert!(!response.provenance.klt_version.is_empty());
    assert!(response
        .provenance
        .input
        .content_hash
        .starts_with("sha256:"));
    assert_ne!(
        response.provenance.input.content_hash, response.store.content_hash,
        "provenance.input.content_hash is the trace's hash, not the store's"
    );

    // The store is queryable: re-open it as a plain FST file (independent
    // of this crate's own writer) and confirm the declared hierarchy and
    // final signal values match what the VCD fixture encodes.
    let store_path = tmp.path().join(".klt/wave/counter.klwave");
    assert_eq!(
        fs::metadata(&store_path).unwrap().len(),
        response.store.size_bytes
    );

    let file = std::fs::File::open(&store_path).unwrap();
    let mut reader = FstReader::open_and_read_time_table(BufReader::new(file)).unwrap();

    let mut names = Vec::new();
    let mut scope = Vec::new();
    let mut count_handle = None;
    reader
        .read_hierarchy(|entry| match entry {
            FstHierarchyEntry::Scope { name, .. } => scope.push(name),
            FstHierarchyEntry::UpScope => {
                scope.pop();
            }
            FstHierarchyEntry::Var { name, handle, .. } => {
                let full = if scope.is_empty() {
                    name.clone()
                } else {
                    format!("{}.{}", scope.join("."), name)
                };
                if full == "tb.count[3:0]" {
                    count_handle = Some(handle);
                }
                names.push(full);
            }
            _ => {}
        })
        .unwrap();
    names.sort();
    assert_eq!(names, vec!["tb.clk", "tb.count[3:0]", "tb.rst_n"]);

    let count_handle = count_handle.expect("tb.count should be declared in the store");
    let mut last_count_value: Option<String> = None;
    reader
        .read_signals(
            &FstFilter::filter_signals(vec![count_handle]),
            |_time_idx, _handle, value| {
                if let FstSignalValue::String(bits) = value {
                    last_count_value = Some(String::from_utf8_lossy(bits).to_string());
                }
                Ok::<(), ()>(())
            },
        )
        .unwrap();
    assert_eq!(last_count_value.as_deref(), Some("0110"));
}

#[test]
fn build_rejects_empty_signals_allow_list() {
    let tmp = tempfile::tempdir().unwrap();
    let trace_path = fixture_path("counter.vcd");
    let request = format!(
        r#"{{
            "trace": {{ "path": {trace_path:?} }},
            "signals": [],
            "store": {{ "path": ".klt/wave/counter.klwave" }}
        }}"#,
    );
    let request_path = write_request(tmp.path(), &request);
    let err = build::run(&request_path).expect_err("empty signals array must be rejected");
    assert_eq!(err.0, 1);
    assert!(err.1.contains("empty"), "message was: {}", err.1);
}

#[test]
fn build_rejects_unresolvable_format() {
    let tmp = tempfile::tempdir().unwrap();
    // No extension and no explicit trace.format -> unresolvable.
    let odd_trace = tmp.path().join("trace_no_ext");
    fs::write(&odd_trace, "irrelevant").unwrap();
    let request = format!(
        r#"{{
            "trace": {{ "path": {odd_trace:?} }},
            "store": {{ "path": ".klt/wave/out.klwave" }}
        }}"#,
    );
    let request_path = write_request(tmp.path(), &request);
    let err = build::run(&request_path).expect_err("unresolvable format must be rejected");
    assert_eq!(err.0, 1);
}

#[test]
fn build_narrows_to_signals_allow_list() {
    let tmp = tempfile::tempdir().unwrap();
    let trace_path = fixture_path("counter.vcd");
    let request = format!(
        r#"{{
            "trace": {{ "path": {trace_path:?} }},
            "signals": ["tb.clk", "tb.count[3:0]"],
            "store": {{ "path": ".klt/wave/narrow.klwave" }}
        }}"#,
    );
    let request_path = write_request(tmp.path(), &request);
    let response = build::run(&request_path).expect("build should succeed");
    assert_eq!(response.signal_count, 2);
    // No clock/reset block in the request -> both omitted (null) in the
    // response, and cycle addressing is unavailable (never asserted here
    // -- issue #1599's query engine is what enforces that at query time).
    assert!(response.clock.is_none());
    assert!(response.reset.is_none());
}
