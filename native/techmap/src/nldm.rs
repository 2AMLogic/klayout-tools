//! Bilinear interpolation over a liberty NLDM 2D lookup table -- thin
//! re-export of `native/nldm-interp` (issue #2272). This module used to be
//! a verbatim fork of `native/statime/src/nldm.rs` (issue #809's accepted
//! spike); the two copies had since drifted (statime's test suite grew
//! two edge-case tests this one never picked up), so issue #2272 extracted
//! the shared `bracket()`/`interpolate()` core -- and the union of both
//! test suites -- into `native/nldm-interp`. See
//! `native/nldm-interp/src/lib.rs` for the algorithm and its
//! linear-extrapolation-beyond-the-table convention. Used here for a
//! single representative-operating-point delay estimate per candidate
//! cell (`celllib.rs`'s area/delay cell-selection score), not for a full
//! path-based STA graph -- this crate does not build one (see
//! `docs/design/synth-techmap-stage-contract.md` section 8, `timing`
//! stays `null` until a follow-on issue wires `native/statime` in).
//!
//! This crate's own [`crate::liberty::Table2D`] is left as-is (it has
//! genuinely diverged from `native/statime`'s own copy -- see issue
//! #2272) -- this module just implements `klt_nldm_interp::Table2DLike`
//! for it so every existing `nldm::interpolate(...)` call site keeps
//! working unchanged.

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
