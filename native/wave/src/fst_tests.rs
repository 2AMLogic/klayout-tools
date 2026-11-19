//! Unit tests for `fst.rs`. Kept in a sibling file (wired in from
//! `fst.rs` via `#[cfg(test)] #[path = "fst_tests.rs"] mod tests;`), not
//! inline, so `fst.rs` stays within its per-file size ratchet ceiling.

use super::*;

/// Regression pin for the fst-writer 0.3.2 bump (upstream
/// ekiwi/fst-writer#12): 0.3.2 fills never-written *real* frame slots
/// with `f64::NAN` instead of ASCII 'x' bytes, so without the second
/// `REAL_FRAME_GARBAGE_BITS` pattern every real signal whose first
/// change comes after the block's frame tick gained a spurious leading
/// `(0, "NaN")` transition and its pre-first-change value flipped from
/// `"x"` to `"NaN"` in `klt wave query` output. This exercises the
/// production write path (`StoreWriter`, two real signals) and the
/// production read path (`ColumnCache::load_from_file`) end to end.
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

/// `canon_value` marks only the 0.3.1 ASCII-'x' fill as garbage (it can never be a genuine
/// value). A NaN passes through as `"NaN"` -- it is frame fill (0.3.2, ekiwi/fst-writer#12) or a
/// genuine sample depending on *position*, which `canon_value` cannot see; that call is made by
/// `strip_real_frame_garbage`.
#[test]
fn canon_value_marks_x_fill_only_and_passes_nan_through() {
    let x_fill = f64::from_bits(u64::from_le_bytes([b'x'; 8]));
    assert_eq!(
        canon_value(&FstSignalValue::Real(x_fill), true),
        Some(String::new()),
        "the 0.3.1 'x'-bytes fill must be treated as frame garbage"
    );
    assert_eq!(
        canon_value(&FstSignalValue::Real(f64::NAN), true),
        Some("NaN".to_string()),
        "NaN must survive canon_value so the position-scoped strip can decide"
    );
    assert_eq!(
        canon_value(&FstSignalValue::Real(1.25), true),
        Some("1.25".to_string())
    );
    assert_eq!(
        canon_value(&FstSignalValue::Real(f64::NAN), false),
        Some("NaN".to_string())
    );
}

/// The 0.3.2 NaN fill is stripped only while it leads the stream; a NaN after any non-NaN sample
/// is a genuine change and stays. The 'x' marker goes everywhere; bit-vectors are untouched.
#[test]
fn strip_real_frame_garbage_strips_only_the_leading_nan_run() {
    let s = |t: u64, v: &str| (t, v.to_string());
    let mut leading = vec![
        s(0, "NaN"),
        s(5, "NaN"),
        s(10, "1.25"),
        s(20, "NaN"),
        s(30, "2.5"),
    ];
    strip_real_frame_garbage(&mut leading, true);
    assert_eq!(leading, vec![s(10, "1.25"), s(20, "NaN"), s(30, "2.5")]);

    let mut all_nan = vec![s(0, "NaN"), s(40, "NaN")];
    strip_real_frame_garbage(&mut all_nan, true);
    assert!(all_nan.is_empty(), "never-written real: nothing but fill");

    let mut marker = vec![s(0, ""), s(10, "1.25"), s(20, ""), s(30, "NaN")];
    strip_real_frame_garbage(&mut marker, true);
    assert_eq!(marker, vec![s(10, "1.25"), s(30, "NaN")]);

    let mut bits = vec![s(0, "NaN"), s(10, "")];
    strip_real_frame_garbage(&mut bits, false);
    assert_eq!(bits, vec![s(0, "NaN"), s(10, "")], "non-real untouched");
}

/// Regression pin (review of PR #5928): a genuine mid-trace NaN on a real signal must survive the
/// store round trip. `StoreWriter::record_change` writes every unparseable real token as exactly
/// `f64::NAN` -- the bit pattern fst-writer 0.3.2 uses as frame fill -- so a bits-based filter in
/// the reader erased these transitions and `value_at_tick` kept reporting the last good voltage
/// (a plausible wrong value for a diverged node). Covers `nan`, a malformed token, and the `x`
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
