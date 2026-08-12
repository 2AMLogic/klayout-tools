//! Per-net RSMT-family wirelength estimate.
//!
//! **Not literal FLUTE.** The survey this issue implements (§3.6,
//! `docs/design/place-and-route-improvements-survey.md`) names "FLUTE
//! family" loosely (its own text: "a fast RSMT/HPWL-based congestion
//! estimator"). Literal FLUTE (Chu & Wong, "FLUTE: Fast Lookup Table Based
//! Rectilinear Steiner Minimal Tree Algorithm for VLSI Design," IEEE TCAD
//! 2008) needs precomputed POWV/POST lookup tables for degree <= 9 plus a
//! recursive net-breaking heuristic beyond that -- reproducing those
//! tables correctly from scratch is its own multi-day validation project,
//! out of proportion to a research-spike issue explicitly scoped to
//! "ship only if the correlation is strong enough" (issue #785's own
//! acceptance criteria).
//!
//! This module implements the same *practical role* (a cheap, provably
//! correct RSMT-family upper/exact bound) via two well-established,
//! easy-to-verify techniques from the same pre-FLUTE literature FLUTE
//! itself descends from (Hwang, "An O(n log n) Algorithm for Rectilinear
//! Minimal Spanning Trees," JACM 1979; Ho/Vijayan/Wong, "New Algorithms
//! for the Rectilinear Steiner Tree Problem," IEEE TCAD 1990):
//!
//! - **Degree <= 3**: the Rectilinear Steiner Minimal Tree of at most 3
//!   terminals is *exactly* the bounding-box half-perimeter (HPWL) -- a
//!   textbook fact, not an approximation (any spanning tree over <= 3
//!   points on a Hanan grid whose bounding box has area zero along one
//!   axis reduces to HPWL; for 3 points, the median-point "L-shape"
//!   Steiner tree achieves HPWL exactly and no shorter rectilinear Steiner
//!   tree exists).
//! - **Degree >= 4**: the weight of the rectilinear Minimum Spanning Tree
//!   (Manhattan-distance MST) over the pins. This is a *provable upper
//!   bound* on the true RSMT (any Steiner tree is at most as long as the
//!   MST, since the MST is a valid, if unoptimized, Steiner topology), and
//!   the standard first-order approximation the pre-FLUTE literature
//!   above is built from -- empirically within single-digit percent of the
//!   true RSMT for typical standard-cell net degrees (RMST/RSMT ratio
//!   approaches 1.0 as pin count grows and pins spread across the bounding
//!   box, per the Ho/Vijayan/Wong result above).
//!
//! See `docs/design/flute-congestion-precheck-results.md` for the honest
//! accounting of this substitution's effect on the correlation study this
//! issue's acceptance criteria require, and a named follow-up if a tighter
//! (literal FLUTE) estimate turns out to matter.

use crate::contract::PointUm;

/// RSMT-family length estimate (see module docs) for one net's pins, in
/// micrometers. Returns 0.0 for 0 or 1 pins (no routing demand) --
/// callers should treat that as "skip this net," not "zero congestion
/// contribution is meaningful," which `grid.rs` does via
/// `skipped_single_pin_nets`.
pub fn net_length_um(pins: &[PointUm]) -> f64 {
    match pins.len() {
        0 | 1 => 0.0,
        2 | 3 => hpwl_um(pins),
        _ => rectilinear_mst_weight_um(pins),
    }
}

/// Half-perimeter wirelength of the pins' bounding box.
fn hpwl_um(pins: &[PointUm]) -> f64 {
    let (mut xmin, mut xmax) = (f64::INFINITY, f64::NEG_INFINITY);
    let (mut ymin, mut ymax) = (f64::INFINITY, f64::NEG_INFINITY);
    for p in pins {
        xmin = xmin.min(p.x_um);
        xmax = xmax.max(p.x_um);
        ymin = ymin.min(p.y_um);
        ymax = ymax.max(p.y_um);
    }
    (xmax - xmin) + (ymax - ymin)
}

/// Weight of the Manhattan-distance Minimum Spanning Tree over `pins`,
/// via a plain O(n^2) Prim's algorithm -- standard-cell net degree is
/// realistically a few to a few dozen pins, so the quadratic cost is
/// negligible versus the grid-distribution pass in `grid.rs`, and a plain
/// dense Prim avoids the added complexity (and failure surface) of
/// Hwang's O(n log n) Hanan-grid construction for a pre-check whose own
/// accuracy budget is already generous (see module docs).
fn rectilinear_mst_weight_um(pins: &[PointUm]) -> f64 {
    let n = pins.len();
    debug_assert!(n >= 4);

    let mut in_tree = vec![false; n];
    let mut best_dist = vec![f64::INFINITY; n];
    best_dist[0] = 0.0;
    let mut total = 0.0;

    for _ in 0..n {
        // Pick the not-yet-included vertex with the smallest edge to the
        // growing tree.
        let mut u = usize::MAX;
        let mut u_dist = f64::INFINITY;
        for (i, &included) in in_tree.iter().enumerate() {
            if !included && best_dist[i] < u_dist {
                u_dist = best_dist[i];
                u = i;
            }
        }
        debug_assert!(u != usize::MAX, "connected graph over a finite point set");
        in_tree[u] = true;
        total += u_dist;

        for v in 0..n {
            if in_tree[v] {
                continue;
            }
            let d = manhattan_um(&pins[u], &pins[v]);
            if d < best_dist[v] {
                best_dist[v] = d;
            }
        }
    }
    total
}

fn manhattan_um(a: &PointUm, b: &PointUm) -> f64 {
    (a.x_um - b.x_um).abs() + (a.y_um - b.y_um).abs()
}

#[cfg(test)]
mod tests {
    use super::*;
    use approx::assert_relative_eq;

    fn pt(x: f64, y: f64) -> PointUm {
        PointUm { x_um: x, y_um: y }
    }

    #[test]
    fn zero_or_one_pin_has_no_length() {
        assert_eq!(net_length_um(&[]), 0.0);
        assert_eq!(net_length_um(&[pt(1.0, 1.0)]), 0.0);
    }

    #[test]
    fn two_pin_net_is_manhattan_distance() {
        let pins = [pt(0.0, 0.0), pt(3.0, 4.0)];
        assert_relative_eq!(net_length_um(&pins), 7.0);
    }

    #[test]
    fn three_pin_net_matches_bounding_box_half_perimeter() {
        // A textbook 3-pin case: (0,0), (2,0), (1,3) -- HPWL = (2-0)+(3-0)=5,
        // and the true RSMT for <=3 terminals equals HPWL exactly.
        let pins = [pt(0.0, 0.0), pt(2.0, 0.0), pt(1.0, 3.0)];
        assert_relative_eq!(net_length_um(&pins), 5.0);
    }

    #[test]
    fn four_pin_net_never_exceeds_manhattan_mst_and_is_at_least_hpwl() {
        // A 2x2 "square" net: RMST connects 3 of the 4 edges (weight 3*1=3
        // for a unit square), while HPWL is 1+1=2. RSMT for this exact
        // configuration is known to be 2 (a single interior Steiner point
        // reaches all four corners) -- the MST bound (3) is a real,
        // expected overestimate for this shape, which is exactly why this
        // is documented as an upper bound, not the true RSMT.
        let pins = [pt(0.0, 0.0), pt(1.0, 0.0), pt(0.0, 1.0), pt(1.0, 1.0)];
        let hpwl = hpwl_um(&pins);
        let estimate = net_length_um(&pins);
        assert_relative_eq!(hpwl, 2.0);
        assert_relative_eq!(estimate, 3.0);
        assert!(
            estimate >= hpwl,
            "RMST must be >= true RSMT's own HPWL lower bound"
        );
    }

    #[test]
    fn collinear_pins_reduce_to_hpwl() {
        // Any number of collinear pins: MST weight along the line equals
        // the span, which equals HPWL (the other axis contributes zero).
        let pins = [
            pt(0.0, 5.0),
            pt(1.0, 5.0),
            pt(2.0, 5.0),
            pt(4.0, 5.0),
            pt(7.0, 5.0),
        ];
        assert_relative_eq!(net_length_um(&pins), hpwl_um(&pins));
    }

    #[test]
    fn scales_with_pin_spread_monotonically() {
        // A cheap sanity check the correlation study leans on implicitly:
        // spreading pins further apart (same topology, larger bounding
        // box) must not decrease the estimate.
        let tight = [
            pt(0.0, 0.0),
            pt(1.0, 0.0),
            pt(0.0, 1.0),
            pt(1.0, 1.0),
            pt(0.5, 0.5),
        ];
        let wide = [
            pt(0.0, 0.0),
            pt(2.0, 0.0),
            pt(0.0, 2.0),
            pt(2.0, 2.0),
            pt(1.0, 1.0),
        ];
        assert!(net_length_um(&wide) > net_length_um(&tight));
    }
}
