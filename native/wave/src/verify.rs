//! Post-write validation for a just-built store. Written fresh -- not a
//! port.
//!
//! **Why this exists**: while implementing this issue, a real,
//! data-dependent correctness bug was found in the stock `fst-writer`
//! 0.3.1 crate this crate depends on (decision record section 11's
//! "depend on stock `fst-writer`, not booley's vendored fork" call).
//! `fst-writer`'s `write_time_table` (`src/io.rs`) chooses whether to
//! zlib-compress the time-table section with `if compressed.len() >
//! time_table.len() { store raw } else { store compressed }` -- but
//! `fst-reader`'s `read_zlib_compressed_bytes` (`src/io.rs`) decides
//! whether a section *was* stored raw purely by comparing the recorded
//! `uncompressed_length == compressed_length`. When compression happens
//! to produce a result **exactly** the same size as the input (a real,
//! reproducible occurrence for small-to-medium time tables with mostly
//! uniform delta values -- e.g. a free-running clock's own edge spacing,
//! `klt wave build`'s single most common input shape), the writer takes
//! the "store compressed" branch while the reader's own ambiguous
//! length-equality heuristic takes the "was stored raw" branch --
//! silently decoding the raw zlib-compressed bytes as if they were the
//! uncompressed varint time deltas. The result is a store that opens
//! without error but reports a corrupted time table (confirmed via a
//! minimal, from-scratch `fst-writer`/`fst-reader` reproduction outside
//! this crate, not merely inferred). See this issue's PR description and
//! the filed follow-up issue for the reproduction and upstream-reporting
//! status; this module is the mitigation landed alongside the ported
//! code so `klt wave build` never *silently* ships a store this bug has
//! corrupted. (fst-writer 0.3.2 has since fixed the writer side: its
//! `write_time_table` now stores the section raw whenever zlib fails to
//! shrink it, so the ambiguous equal-length case only arises when the
//! section genuinely is raw. This module stays as defense-in-depth over
//! the reader's decode path.)
//!
//! **What this checks**: re-opens the just-written store through
//! `fst-reader` (independent of this crate's own writer state) and
//! confirms both (a) its header's `end_time` and (b) the *last entry of
//! its own decoded time table* match the last tick this crate's own
//! `StoreWriter` observed while streaming. Check (a) alone is not
//! sufficient -- `end_time` is a plain header field written and read
//! outside the buggy compressed-time-table section, so it stays correct
//! even when the time table itself is corrupted; check (b) is what
//! actually exercises the affected decode path (`get_time_table`), and
//! reliably produces an implausibly large last entry (the misread zlib
//! bytes decode as a run of large deltas) when the defect is triggered.

use std::io::BufReader;
use std::path::Path;

use fst_reader::FstReader;

/// Re-open `store_path` and confirm both its header `end_time` and its
/// own decoded time table's last entry match `expected_max_tick` (the
/// last tick this crate's own accumulator observed). Returns `Err` with
/// an actionable message -- including a pointer to the known
/// `fst-writer` 0.3.1 defect this check was landed against (fixed
/// upstream in 0.3.2) -- on any mismatch or read failure, rather than
/// letting a corrupted store escape silently.
pub fn verify_store(store_path: &Path, expected_max_tick: Option<u64>) -> Result<(), String> {
    let Some(expected_max_tick) = expected_max_tick else {
        // An empty trace (no value changes at all) has nothing to
        // cross-check against; `StoreWriter` still produces a valid
        // (trivial) store in that case.
        return Ok(());
    };

    let file = std::fs::File::open(store_path).map_err(|e| {
        format!(
            "post-write verification: cannot reopen store '{}': {e}",
            store_path.display()
        )
    })?;
    let reader = FstReader::open_and_read_time_table(BufReader::new(file)).map_err(|e| {
        format!(
            "post-write verification: '{}' is not a readable FST file: {e:?} -- \
             the store this run just wrote is unqueryable",
            store_path.display()
        )
    })?;
    let header = reader.get_header();
    let last_table_entry = reader.get_time_table().and_then(|t| t.last().copied());

    let corrupted = header.end_time != expected_max_tick
        || last_table_entry.is_some_and(|t| t != expected_max_tick);

    if corrupted {
        let store_display = store_path.display();
        let end_time = header.end_time;
        return Err(format!(
            "post-write verification failed: store '{store_display}' reports \
             end_time={end_time}, decoded time-table last entry={last_table_entry:?}, but \
             this build observed the trace's last timestamp at tick {expected_max_tick} -- \
             the store is corrupted. fst-writer 0.3.1 had a known time-table encoding \
             defect of exactly this shape (a zlib-compressed time-table section whose \
             compressed size exactly equals its raw size was misread as uncompressed by \
             fst-reader's own length-equality heuristic; fixed upstream in 0.3.2 -- see \
             this crate's verify.rs doc comment and the PR/issue that landed it for the \
             minimal reproduction), so a mismatch here now means some other writer or \
             reader fault. The store was not returned as built."
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cycle::Edge;
    use crate::store_writer::{DeclSignal, StoreWriter};

    #[test]
    fn verify_store_accepts_a_correctly_written_store() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("ok.klwave");
        let decls = [DeclSignal {
            id: "!",
            full_name: "tb.clk",
            width: 1,
            var_type: "wire",
            is_real: false,
        }];
        let mut store =
            StoreWriter::create(&path, &decls, -9, 1.0, None, Edge::Rising, None, true).unwrap();
        let mut val = 1u8;
        for tick in [5u64, 10, 15, 20] {
            store.advance_time(tick).unwrap();
            store.record_change("!", &[b'0' + val]);
            val = 1 - val;
        }
        store.finish().unwrap();
        verify_store(&path, Some(20)).expect("a correctly written store must verify clean");
    }

    /// The delta pattern that empirically triggered the real
    /// fst-writer 0.3.1 defect this module exists to catch: a time
    /// table whose zlib-compressed encoding happens to be exactly the
    /// same byte length as its uncompressed form is written as
    /// compressed but misread by `fst-reader` as raw. This specific
    /// delta sequence (mostly-uniform steps of 5, with two short steps
    /// breaking the run) was empirically confirmed, outside this crate,
    /// to hit that exact size tie -- see this module's doc comment.
    /// fst-writer 0.3.2 fixed the writer side (it now stores the time
    /// table raw whenever zlib does not shrink it), so the same pattern
    /// now verifies clean; kept as a regression pin in case a future
    /// bump ever reintroduces the behavior.
    #[test]
    fn verify_store_accepts_the_pattern_that_corrupted_fst_writer_0_3_1() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("formerly-corrupt.klwave");
        let decls = [DeclSignal {
            id: "!",
            full_name: "tb.clk",
            width: 1,
            var_type: "wire",
            is_real: false,
        }];
        let mut store =
            StoreWriter::create(&path, &decls, -9, 1.0, None, Edge::Rising, None, true).unwrap();
        let ticks: &[u64] = &[5, 10, 15, 20, 25, 30, 35, 37, 40, 45, 50, 55, 60, 65, 70];
        let mut val = 1u8;
        for &tick in ticks {
            store.advance_time(tick).unwrap();
            store.record_change("!", &[b'0' + val]);
            val = 1 - val;
        }
        store.finish().unwrap();
        verify_store(&path, Some(70))
            .expect("the delta pattern that corrupted fst-writer 0.3.1 must verify clean on 0.3.2");
    }

    /// Keeps `verify_store`'s rejection branch covered now that the
    /// 0.3.1 writer defect above can no longer be reproduced through
    /// the stock writer: a store whose header/time table disagree with
    /// the max tick this crate observed must be rejected.
    #[test]
    fn verify_store_rejects_a_max_tick_mismatch() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("mismatch.klwave");
        let decls = [DeclSignal {
            id: "!",
            full_name: "tb.clk",
            width: 1,
            var_type: "wire",
            is_real: false,
        }];
        let mut store =
            StoreWriter::create(&path, &decls, -9, 1.0, None, Edge::Rising, None, true).unwrap();
        for tick in [5u64, 10] {
            store.advance_time(tick).unwrap();
            store.record_change("!", b"1");
        }
        store.finish().unwrap();
        let err = verify_store(&path, Some(10_000_000))
            .expect_err("a store whose max tick disagrees with the observed one must be rejected");
        assert!(
            err.contains("post-write verification failed"),
            "message was: {err}"
        );
    }
}
