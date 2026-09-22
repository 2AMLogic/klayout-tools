//! Bilinear interpolation over a liberty NLDM 2D lookup table
//! (`index_1` = input transition, `index_2` = output load), with **linear
//! extrapolation** beyond the table's characterised range -- continuing the
//! slope of the nearest segment, the same convention OpenSTA itself uses
//! (verified empirically: see `native/statime/README.md` "Known
//! simplifications" for the measured case this matters on -- an
//! unbuffered, over-loaded net whose driven capacitance exceeds the
//! driving cell's own `max_capacitance`, which an earlier clamp-to-edge
//! version of this function underestimated by ~9% end to end on the
//! affected path). Clamping is *not* used because it was measured to
//! matter, not merely out of caution -- a bilinear surface's own slope,
//! continued past the last characterised point, is a much closer match to
//! the real device physics (increasing RC delay is not sub-linear) than
//! freezing at the last known value.
//!
//! Extracted (issue #2272) from `native/statime/src/nldm.rs`, which
//! `native/techmap/src/nldm.rs` had forked verbatim (issue #809's accepted
//! spike) -- the two copies had already started to drift (see issue #2272
//! for the diff) with no mechanism for a fix made in one to reach the
//! other. `native/statime/src/liberty.rs` and `native/techmap/src/liberty.rs`
//! each define their own `Table2D` struct and have genuinely diverged
//! (553 of ~845 lines differ) -- they are deliberately NOT touched by this
//! extraction and this crate depends on neither. Instead, each parent
//! crate's own (now-thin) `nldm.rs` implements [`Table2DLike`] for its own
//! `Table2D` type and re-exports [`interpolate`] from here.
//!
//! No `pyo3` dependency, no dependency on either parent crate -- matching
//! the original fork's own stated motivation (`native/techmap`'s deliberate
//! choice not to pull in all of `native/statime` for a single-point delay
//! estimate).

/// Anything that looks like a 2D liberty NLDM lookup table: two axes
/// (`index1`/`index2`) and a row-major grid of values, `values[i][j]`
/// corresponding to `(index1[i], index2[j])`. This is the minimal shared
/// contract [`interpolate`] needs -- deliberately a trait, not a shared
/// `Table2D` struct, so `native/statime` and `native/techmap` can keep
/// their own (independently evolved) `Table2D` definitions in
/// `liberty.rs` and just implement this trait for them.
pub trait Table2DLike {
    fn index1(&self) -> &[f64];
    fn index2(&self) -> &[f64];
    fn values(&self) -> &[Vec<f64>];
}

/// Find the bracketing index pair `(lo, hi, frac)` for `x` in `axis`.
/// `frac` is **not** clamped to `[0, 1]` -- a value outside the axis range
/// yields `frac < 0` or `frac > 1`, extrapolating along the nearest
/// segment's slope.
fn bracket(axis: &[f64], x: f64) -> (usize, usize, f64) {
    if axis.is_empty() {
        return (0, 0, 0.0);
    }
    if axis.len() == 1 {
        return (0, 0, 0.0);
    }
    if x <= axis[0] {
        let span = axis[1] - axis[0];
        let frac = if span.abs() < 1e-15 {
            0.0
        } else {
            (x - axis[0]) / span
        };
        return (0, 1, frac);
    }
    if x >= *axis.last().unwrap() {
        let last = axis.len() - 1;
        let span = axis[last] - axis[last - 1];
        let frac = if span.abs() < 1e-15 {
            1.0
        } else {
            1.0 + (x - axis[last]) / span
        };
        return (last - 1, last, frac);
    }
    for i in 0..axis.len() - 1 {
        if x >= axis[i] && x <= axis[i + 1] {
            let span = axis[i + 1] - axis[i];
            let frac = if span.abs() < 1e-15 {
                0.0
            } else {
                (x - axis[i]) / span
            };
            return (i, i + 1, frac);
        }
    }
    let last = axis.len() - 1;
    (last - 1, last, 1.0)
}

/// Interpolate (or extrapolate) `table` at
/// `(input_transition_ns, output_load_pf)`.
pub fn interpolate<T: Table2DLike + ?Sized>(
    table: &T,
    input_transition_ns: f64,
    output_load_pf: f64,
) -> f64 {
    let values = table.values();
    if values.is_empty() || values[0].is_empty() {
        return 0.0;
    }
    let (r0, r1, rf) = bracket(table.index1(), input_transition_ns);
    let (c0, c1, cf) = bracket(table.index2(), output_load_pf);

    let v00 = values[r0][c0];
    let v01 = values[r0][c1];
    let v10 = values[r1][c0];
    let v11 = values[r1][c1];

    let top = v00 + (v01 - v00) * cf;
    let bot = v10 + (v11 - v10) * cf;
    top + (bot - top) * rf
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Local stand-in for `native/statime`'s and `native/techmap`'s own
    /// `liberty::Table2D` -- this crate deliberately does not depend on
    /// either, so its tests exercise [`interpolate`] through a minimal
    /// implementation of [`Table2DLike`] instead.
    struct Table2D {
        index1: Vec<f64>,
        index2: Vec<f64>,
        values: Vec<Vec<f64>>,
    }

    impl Table2DLike for Table2D {
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

    fn table() -> Table2D {
        Table2D {
            index1: vec![0.0, 1.0],
            index2: vec![0.0, 1.0],
            values: vec![vec![0.0, 1.0], vec![1.0, 2.0]],
        }
    }

    #[test]
    fn exact_corners_match() {
        let t = table();
        assert_eq!(interpolate(&t, 0.0, 0.0), 0.0);
        assert_eq!(interpolate(&t, 0.0, 1.0), 1.0);
        assert_eq!(interpolate(&t, 1.0, 0.0), 1.0);
        assert_eq!(interpolate(&t, 1.0, 1.0), 2.0);
    }

    #[test]
    fn center_is_average() {
        let t = table();
        assert!((interpolate(&t, 0.5, 0.5) - 1.0).abs() < 1e-12);
    }

    #[test]
    fn out_of_range_extrapolates_along_last_segment() {
        let t = table();
        // Below the low edge: continue the (0,0)->(1,1) diagonal slope
        // backwards -- interpolate(-1, -1) should land on -2.0, not clamp
        // to 0.0.
        assert!((interpolate(&t, -1.0, -1.0) - (-2.0)).abs() < 1e-9);
        // Above the high edge: continue forwards past (1,1)=2.0.
        assert!((interpolate(&t, 2.0, 2.0) - 4.0).abs() < 1e-9);
    }

    #[test]
    fn single_row_or_column_table() {
        let t = Table2D {
            index1: vec![0.5],
            index2: vec![0.0, 1.0],
            values: vec![vec![3.0, 4.0]],
        };
        assert_eq!(interpolate(&t, 0.0, 0.0), 3.0);
        assert_eq!(interpolate(&t, 999.0, 1.0), 4.0);
        assert!((interpolate(&t, 0.0, 0.5) - 3.5).abs() < 1e-12);
    }
}
