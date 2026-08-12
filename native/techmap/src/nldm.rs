//! Bilinear interpolation over a liberty NLDM 2D lookup table, forked
//! verbatim from `native/statime/src/nldm.rs` (issue #809's accepted
//! spike) -- same table shape (`Table2D`), same linear-extrapolation
//! convention beyond the table's characterised range (matches OpenSTA's
//! own behaviour, per that module's docs). Used here for a single
//! representative-operating-point delay estimate per candidate cell
//! (`celllib.rs`'s area/delay cell-selection score), not for a full
//! path-based STA graph -- this crate does not build one (see
//! `docs/design/synth-techmap-stage-contract.md` section 8, `timing`
//! stays `null` until a follow-on issue wires `native/statime` in).

use crate::liberty::Table2D;

fn bracket(axis: &[f64], x: f64) -> (usize, usize, f64) {
    if axis.is_empty() || axis.len() == 1 {
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
pub fn interpolate(table: &Table2D, input_transition_ns: f64, output_load_pf: f64) -> f64 {
    if table.values.is_empty() || table.values[0].is_empty() {
        return 0.0;
    }
    let (r0, r1, rf) = bracket(&table.index1, input_transition_ns);
    let (c0, c1, cf) = bracket(&table.index2, output_load_pf);

    let v00 = table.values[r0][c0];
    let v01 = table.values[r0][c1];
    let v10 = table.values[r1][c0];
    let v11 = table.values[r1][c1];

    let top = v00 + (v01 - v00) * cf;
    let bot = v10 + (v11 - v10) * cf;
    top + (bot - top) * rf
}

#[cfg(test)]
mod tests {
    use super::*;

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
        assert_eq!(interpolate(&t, 1.0, 1.0), 2.0);
    }

    #[test]
    fn center_is_average() {
        let t = table();
        assert!((interpolate(&t, 0.5, 0.5) - 1.0).abs() < 1e-12);
    }
}
