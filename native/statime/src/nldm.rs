//! Bilinear interpolation over a liberty NLDM 2D lookup table -- thin
//! re-export of `native/nldm-interp` (issue #2272), which extracted this
//! module's `bracket()`/`interpolate()` core (and its test suite) after
//! `native/techmap/src/nldm.rs` forked it verbatim (issue #809's accepted
//! spike) and the two copies started to drift. See
//! `native/nldm-interp/src/lib.rs` for the algorithm, its
//! linear-extrapolation-beyond-the-table convention, and the full test
//! suite (including the `out_of_range_extrapolates_along_last_segment` and
//! `single_row_or_column_table` edge cases this crate's own `README.md`
//! "Known simplifications" section calls out).
//!
//! This crate's own [`crate::liberty::Table2D`] is left as-is (it has
//! genuinely diverged from `native/techmap`'s own copy -- see issue #2272)
//! -- this module just implements `klt_nldm_interp::Table2DLike` for it so
//! every existing `nldm::interpolate(...)` call site keeps working
//! unchanged.

use crate::liberty::Table2D;

impl klt_nldm_interp::Table2DLike for Table2D {
    fn index1(&self) -> &[f64] {
        &self.index1
    }
    fn index2(&self) -> &[f64] {
        &self.index2
    }
    fn values(&self) -> &[Vec<f64>] {
        &self.values
    }
}

pub use klt_nldm_interp::interpolate;
