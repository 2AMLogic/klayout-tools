//! Unit tests for `fst.rs`. Kept in a sibling file (wired in from
//! `fst.rs` via `#[cfg(test)] #[path = "fst_tests.rs"] mod tests;`), not
//! inline, so `fst.rs` stays within its per-file size ratchet ceiling.

use super::*;

/// fst-writer 0.3.2 initializes real frame slots with NaN. Queries must still
/// return `x` before a real signal's first write and throughout an unwritten signal.
#[test]
fn real_signal_first_change_after_frame_tick_has_no_leading_garbage() {
    use crate::cycle::Edge;
    use crate::store_writer::{DeclSignal, StoreWriter};

    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("reals.klwave");
    let decls = [
        DeclSignal {
            id: "ra",
            full_name: "tb.never_changes",
            width: 64,
            var_type: "real",
            is_real: true,
        },
        DeclSignal {
            id: "rb",
            full_name: "tb.late_init",
            width: 64,
            var_type: "real",
            is_real: true,
        },
    ];
    let mut store =
        StoreWriter::create(&path, &decls, -9, 1.0, None, Edge::Rising, None, true).unwrap();
    store.advance_time(10).unwrap();
    store.record_change("rb", b"1.25");
    store.advance_time(20).unwrap();
    store.record_change("rb", b"2.5");
    store.finish().unwrap();

    let cache = ColumnCache::load_from_file(&path).expect("store must load");
    let idx_never = cache
        .signals
        .iter()
        .position(|s| s.name == "tb.never_changes")
        .expect("never_changes declared");
    let idx_late = cache
        .signals
        .iter()
        .position(|s| s.name == "tb.late_init")
        .expect("late_init declared");

    // Never-initialized real: no transitions at all -- in particular no
    // `(0, NaN)` frame-fill leak (fst-writer 0.3.2 regression).
    assert_eq!(
        cache.read_transitions(idx_never),
        Vec::<(u64, String)>::new()
    );
    assert_eq!(cache.value_at_tick_direct(idx_never, 5), "x");

    // First change after the frame tick: the leading `(0, NaN)`
    // frame-fill sample must be stripped, leaving only real changes.
    assert_eq!(
        cache.read_transitions(idx_late),
        vec![(10, "1.25".to_string()), (20, "2.5".to_string())]
    );
    // Pre-first-change value stays the bit-vector-x default, not "NaN".
    assert_eq!(cache.value_at_tick_direct(idx_late, 0), "x");
    assert_eq!(cache.value_at_tick_direct(idx_late, 10), "1.25");
    assert_eq!(cache.value_at_tick_direct(idx_late, 20), "2.5");
}

/// Regression pin (review of PR #5928): a genuine mid-trace NaN on a real signal must survive the
/// store round trip. Filtering all NaNs would leave the last good voltage visible after a
/// diverged sample. Covers `nan`, a malformed token, and the `x`
/// that `$dumpoff` emits via `Handler::on_vector` for every watched id (IEEE 1364-2005 sec. 18).
#[test]
fn genuine_mid_trace_nan_survives_the_store_round_trip() {
    use crate::cycle::Edge;
    use crate::store_writer::{DeclSignal, StoreWriter};

    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("nan.klwave");
    let real = |id: &'static str, full_name: &'static str| DeclSignal {
        id,
        full_name,
        width: 64,
        var_type: "real",
        is_real: true,
    };
    let decls = [real("ra", "tb.bad_token"), real("rb", "tb.dumpoff")];
    let mut store =
        StoreWriter::create(&path, &decls, -9, 1.0, None, Edge::Rising, None, true).unwrap();
    store.advance_time(10).unwrap();
    store.record_change("ra", b"1.25");
    store.record_change("rb", b"1.25");
    store.advance_time(20).unwrap();
    store.record_change("ra", b"nan");
    store.record_change("rb", b"x"); // `$dumpoff`
    store.advance_time(30).unwrap();
    store.record_change("ra", b"not-a-number");
    store.advance_time(40).unwrap();
    store.record_change("ra", b"3.5");
    store.record_change("rb", b"3.5");
    store.finish().unwrap();

    let cache = ColumnCache::load_from_file(&path).expect("store must load");
    let idx = |name: &str| cache.signals.iter().position(|s| s.name == name).unwrap();
    let (bad, off) = (idx("tb.bad_token"), idx("tb.dumpoff"));
    let nan = |t: u64| (t, "NaN".to_string());

    let transitions = cache.read_transitions(bad);
    assert_eq!(transitions.first(), Some(&(10, "1.25".to_string())));
    assert!(
        transitions.contains(&nan(20)),
        "`nan` at tick 20 erased: {transitions:?}"
    );
    assert_eq!(transitions.last(), Some(&(40, "3.5".to_string())));
    assert_eq!(cache.value_at_tick_direct(bad, 25), "NaN");
    assert_eq!(
        cache.value_at_tick_direct(bad, 35),
        "NaN",
        "malformed token read as last good value"
    );

    assert_eq!(
        cache.read_transitions(off),
        vec![(10, "1.25".to_string()), nan(20), (40, "3.5".to_string())]
    );
    assert_eq!(
        cache.value_at_tick_direct(off, 25),
        "NaN",
        "$dumpoff read as the last good value"
    );
    assert_eq!(cache.value_at_tick_direct(off, 40), "3.5");
}
