//! `klt wave build` against a **real** canary testbench trace
//! (`tests/fixtures/modexp_canary.vcd` -- see `tests/fixtures/README.md`
//! for how it was generated from `examples/functional-verification/
//! modexp.v`, the sky130-modexp canary's own RTL). Confirms the build
//! output is queryable: re-opens the resulting store independently via
//! `fst-reader` (standing in for issue #1599's `klt wave query`, not yet
//! landed at the time this issue was implemented -- see this crate's own
//! `NOTICE`/PR description) and asserts a known handshake count, the
//! concrete scenario Epic #1585's own Test Plan names.

use std::fs;
use std::path::PathBuf;

use fst_reader::{FstFilter, FstReader, FstSignalHandle, FstSignalValue};
use klt_wave_native::build;

fn fixture_path(name: &str) -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("fixtures")
        .join(name)
}

#[test]
fn build_from_modexp_canary_and_count_done_handshakes() {
    let tmp = tempfile::tempdir().unwrap();
    let trace_path = fixture_path("modexp_canary.vcd");
    let request = format!(
        r#"{{
            "schema": "klt.wave_build.request/1",
            "trace": {{ "path": {trace_path:?}, "format": "vcd" }},
            "clock": {{ "signal": "tb_modexp.clk", "edge": "rising" }},
            "reset": {{ "signal": "tb_modexp.rst_n", "active": "low" }},
            "signals": null,
            "store": {{ "path": ".klt/wave/modexp_canary.klwave" }}
        }}"#,
    );
    let request_path = tmp.path().join("request.json");
    fs::write(&request_path, &request).unwrap();

    let response = build::run(&request_path).expect("build should succeed against a real trace");

    assert_eq!(response.trace.format, "vcd");
    assert_eq!(response.signal_count, 42);
    assert_eq!(response.value_change_count, 2106);

    let clock = response.clock.expect("clock block should be present");
    assert_eq!(clock.period_ns, Some(10.0));

    let reset = response.reset.expect("reset block should be present");
    let release = reset.release.expect("reset release should be resolved");
    assert_eq!(release.time_ns, 25.0);
    assert_eq!(release.cycle, Some(0));

    assert_eq!(response.time_range.to.time_ns, 3585.0);

    // Re-open the store independently (not through this crate's own
    // writer state) and confirm `tb_modexp.done` rises exactly 3 times --
    // one per `run_case` handshake in `tests/fixtures/README.md`'s
    // regeneration recipe (modexp(2,10,100)=24, modexp(3,7,50)=37,
    // modexp(7,13,11)=2, each cross-checked against Python's own
    // `pow(base, exp, mod)` when the fixture was generated).
    let store_path = tmp.path().join(".klt/wave/modexp_canary.klwave");
    let file = fs::File::open(&store_path).unwrap();
    let mut reader = FstReader::open_and_read_time_table(std::io::BufReader::new(file)).unwrap();

    let mut done_handle = None;
    let mut scope = Vec::new();
    reader
        .read_hierarchy(|entry| match entry {
            fst_reader::FstHierarchyEntry::Scope { name, .. } => scope.push(name),
            fst_reader::FstHierarchyEntry::UpScope => {
                scope.pop();
            }
            fst_reader::FstHierarchyEntry::Var { name, handle, .. } => {
                let full = if scope.is_empty() {
                    name.clone()
                } else {
                    format!("{}.{}", scope.join("."), name)
                };
                if full == "tb_modexp.done" {
                    done_handle = Some(handle);
                }
            }
            _ => {}
        })
        .unwrap();
    let done_handle: FstSignalHandle = done_handle.expect("tb_modexp.done must be in the store");

    let mut rises = 0u32;
    let mut prev = String::new();
    reader
        .read_signals(
            &FstFilter::filter_signals(vec![done_handle]),
            |_t, _h, v| {
                if let FstSignalValue::String(bits) = v {
                    let s = String::from_utf8_lossy(bits).to_string();
                    if s == "1" && prev != "1" {
                        rises += 1;
                    }
                    prev = s;
                }
                Ok::<(), ()>(())
            },
        )
        .unwrap();

    assert_eq!(
        rises, 3,
        "expected exactly 3 done handshakes (one per run_case vector)"
    );
}
