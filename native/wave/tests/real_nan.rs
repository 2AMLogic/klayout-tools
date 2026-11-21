//! Compatibility of real-signal values across the fst-writer 0.3.2 upgrade.
use std::path::{Path, PathBuf};
use std::process::Command;

use klt_wave_native::cache::ColumnCache;
use serde_json::{json, Value};

fn fixture(name: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures")
        .join(name)
}

fn run(dir: &Path, verb: &str, request: Value) -> Value {
    let path = dir.join("request.json");
    std::fs::write(&path, request.to_string()).unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_klt-wave"))
        .arg(verb)
        .arg(path)
        .args(["--format", "json"])
        .output()
        .unwrap();
    assert!(output.status.success(), "{output:?}");
    serde_json::from_slice(&output.stdout).unwrap()
}

fn build(dir: &Path, trace: &Path, store: &Path) {
    run(
        dir,
        "build",
        json!({"trace": {"path": trace}, "store": {"path": store}}),
    );
}

fn check_store(dir: &Path, store: &Path) {
    let cases = [
        ("tb.leading", 5, "x"),
        ("tb.leading", 15, "NaN"),
        ("tb.leading", 30, "3.5"),
        ("tb.initial", 0, "NaN"),
        ("tb.never", 30, "x"),
        ("tb.mid", 5, "1.25"),
        ("tb.mid", 15, "NaN"),
    ];
    let ops: Vec<_> = cases
        .iter()
        .map(|(signal, tick, _)| json!({"op": "value", "signal": signal, "at": {"time_ns": tick}}))
        .collect();
    let response = run(dir, "query", json!({"store": store, "ops": ops}));
    for (i, (signal, tick, expected)) in cases.iter().enumerate() {
        assert_eq!(
            response["results"][i]["value"], *expected,
            "{store:?}: {signal} at {tick}"
        );
    }

    // Full-stream and prefetched reads must agree with uncached point queries.
    let cache = ColumnCache::load_from_file(store).unwrap();
    let leading = cache.match_one_signal("tb.leading").unwrap();
    let never = cache.match_one_signal("tb.never").unwrap();
    assert_eq!(
        cache.read_transitions(leading),
        [(10, "NaN".into()), (30, "3.5".into())]
    );
    assert!(cache.read_transitions(never).is_empty());
    cache.prefetch_window(&[leading, never], 30);
    assert_eq!(cache.value_at_tick_direct(leading, 5), "x");
    assert_eq!(cache.value_at_tick_direct(leading, 15), "NaN");
    assert_eq!(cache.value_at_tick_direct(never, 15), "x");
}

#[test]
fn first_nan_in_legacy_store() {
    let dir = tempfile::tempdir().unwrap();
    check_store(dir.path(), &fixture("leading_nan_0_3_1.fst"));
}

#[test]
fn first_nan_survives_vcd_build_and_fst_rebuild() {
    let dir = tempfile::tempdir().unwrap();
    let store = dir.path().join("new.fst");
    build(dir.path(), &fixture("leading_nan.vcd"), &store);
    check_store(dir.path(), &store);
    for source in [store, fixture("leading_nan_0_3_1.fst")] {
        let rebuilt = dir.path().join("rebuilt.fst");
        build(dir.path(), &source, &rebuilt);
        check_store(dir.path(), &rebuilt);
    }
}
