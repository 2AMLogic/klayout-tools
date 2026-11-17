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

/// Both fill patterns are recognized as frame garbage by `canon_value`:
/// the ASCII-'x'-bytes fill (fst-writer <= 0.3.1) and the NaN fill
/// (0.3.2+, ekiwi/fst-writer#12). Direct unit pins so both sides of
/// `REAL_FRAME_GARBAGE_BITS` stay covered regardless of which
/// fst-writer version happened to build the store in the test above.
#[test]
fn canon_value_marks_both_frame_fill_patterns_as_garbage() {
    let x_fill = f64::from_bits(u64::from_le_bytes([b'x'; 8]));
    assert_eq!(
        canon_value(&FstSignalValue::Real(x_fill), true),
        Some(String::new()),
        "the 0.3.1 'x'-bytes fill must be treated as frame garbage"
    );
    assert_eq!(
        canon_value(&FstSignalValue::Real(f64::NAN), true),
        Some(String::new()),
        "the 0.3.2 NaN fill must be treated as frame garbage"
    );
    // A genuine real value still decodes, and the garbage patterns do
    // not affect bit-vector signals.
    assert_eq!(
        canon_value(&FstSignalValue::Real(1.25), true),
        Some("1.25".to_string())
    );
    assert_eq!(
        canon_value(&FstSignalValue::Real(f64::NAN), false),
        Some("NaN".to_string())
    );
}
