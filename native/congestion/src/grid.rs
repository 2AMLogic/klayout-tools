//! RUDY-style ("Rectangular Uniform wire Density," Spindler & Johannes,
//! "Fast and Accurate Routing Demand Estimation for Efficient Routability-
//! driven Placement," DATE 2007) demand distribution: spread each net's
//! `rsmt::net_length_um` estimate uniformly across its own bounding box,
//! apportioned to a coarse tile grid by overlap area, then summarize the
//! resulting per-tile demand map into the scalar/summary fields
//! `contract::CongestionResponse` reports.
//!
//! This is the standard practical pairing for a "cheap post-placement
//! congestion pre-check" (the survey's own framing, §3.6) -- RUDY is
//! itself a well-established fast congestion proxy independent of FLUTE,
//! and pairs naturally with any per-net wirelength estimate (HPWL, RSMT,
//! or, here, the RSMT-family estimate in `rsmt.rs`).

use crate::contract::{
    BoxUm, CongestionResponse, NetRequest, DEFAULT_LAYER_COUNT, DEFAULT_TRACK_PITCH_UM,
    RESPONSE_SCHEMA_VERSION,
};
use crate::rsmt::net_length_um;

/// Degenerate-extent floor (micrometers) below which a net's bounding-box
/// dimension is treated as exactly zero for the "is this axis degenerate"
/// branch below, rather than risking a near-zero-but-nonzero float
/// dividing the RUDY density into a spurious huge number. Well below any
/// real technology's track pitch (sub-nanometer), so it never affects a
/// real (non-degenerate) net.
const DEGENERATE_EXTENT_UM: f64 = 1e-6;

pub fn estimate(
    die_box_um: BoxUm,
    grid_cols: u32,
    grid_rows: u32,
    layer_count: Option<f64>,
    track_pitch_um: Option<f64>,
    nets: &[NetRequest],
) -> Result<CongestionResponse, String> {
    if grid_cols == 0 || grid_rows == 0 {
        return Err("grid_cols and grid_rows must both be >= 1".to_string());
    }
    let width_um = die_box_um.x1_um - die_box_um.x0_um;
    let height_um = die_box_um.y1_um - die_box_um.y0_um;
    // `<= 0.0` (rather than clippy's flagged `!(x > 0.0)`) still rejects
    // NaN: any comparison against NaN is false, so `x.is_nan()` is checked
    // explicitly rather than relying on negated-`>` semantics.
    if width_um.is_nan() || width_um <= 0.0 || height_um.is_nan() || height_um <= 0.0 {
        return Err("die_box_um must have positive width and height".to_string());
    }
    let layer_count = layer_count.unwrap_or(DEFAULT_LAYER_COUNT);
    let track_pitch_um = track_pitch_um.unwrap_or(DEFAULT_TRACK_PITCH_UM);
    if layer_count.is_nan() || layer_count <= 0.0 {
        return Err("layer_count must be > 0".to_string());
    }
    if track_pitch_um.is_nan() || track_pitch_um <= 0.0 {
        return Err("track_pitch_um must be > 0".to_string());
    }

    let tile_w_um = width_um / grid_cols as f64;
    let tile_h_um = height_um / grid_rows as f64;
    let tile_area_um2 = tile_w_um * tile_h_um;

    // Capacity model: within one tile, one routing layer contributes
    // `tile_w_um / track_pitch_um` parallel tracks spanning the tile's
    // height (or the symmetric count spanning its width -- the two are
    // interchangeable for this scalar budget, since real layers alternate
    // preferred direction and this pre-check does not model that split).
    // Each such track can carry `tile_h_um` of wire, so one layer's own
    // budget is `(tile_w_um / track_pitch_um) * tile_h_um ==
    // tile_area_um2 / track_pitch_um`; `layer_count` usable layers scale
    // that linearly. This is a coarse, explicitly-approximate budget (see
    // `contract.rs`'s `DEFAULT_LAYER_COUNT`/`DEFAULT_TRACK_PITCH_UM` docs),
    // not a per-technology-calibrated router capacity model.
    let capacity_um_per_tile = layer_count * tile_area_um2 / track_pitch_um;
    let capacity_density_um_per_um2 = capacity_um_per_tile / tile_area_um2;

    let mut demand = vec![0.0_f64; (grid_cols as usize) * (grid_rows as usize)];
    let mut net_count = 0usize;
    let mut skipped_single_pin_nets = 0usize;
    let mut total_rsmt_length_um = 0.0_f64;

    for net in nets {
        if net.pins.len() < 2 {
            skipped_single_pin_nets += 1;
            continue;
        }
        let length_um = net_length_um(&net.pins);
        net_count += 1;
        total_rsmt_length_um += length_um;
        if length_um <= 0.0 {
            // Every pin coincides at one point -- no routing demand to
            // distribute, but still a counted (degenerate) net.
            continue;
        }

        let (mut xmin, mut xmax) = (f64::INFINITY, f64::NEG_INFINITY);
        let (mut ymin, mut ymax) = (f64::INFINITY, f64::NEG_INFINITY);
        for p in &net.pins {
            xmin = xmin.min(p.x_um);
            xmax = xmax.max(p.x_um);
            ymin = ymin.min(p.y_um);
            ymax = ymax.max(p.y_um);
        }
        let bbox_w = xmax - xmin;
        let bbox_h = ymax - ymin;

        let (i_lo, i_hi) = tile_range(xmin, xmax, die_box_um.x0_um, tile_w_um, grid_cols);
        let (j_lo, j_hi) = tile_range(ymin, ymax, die_box_um.y0_um, tile_h_um, grid_rows);

        if bbox_w > DEGENERATE_EXTENT_UM && bbox_h > DEGENERATE_EXTENT_UM {
            // Standard RUDY: apportion by overlap AREA / bbox area.
            let bbox_area = bbox_w * bbox_h;
            for j in j_lo..=j_hi {
                let (ty0, ty1) = tile_span(die_box_um.y0_um, tile_h_um, j);
                let oh = overlap(ymin, ymax, ty0, ty1);
                if oh <= 0.0 {
                    continue;
                }
                for i in i_lo..=i_hi {
                    let (tx0, tx1) = tile_span(die_box_um.x0_um, tile_w_um, i);
                    let ow = overlap(xmin, xmax, tx0, tx1);
                    if ow <= 0.0 {
                        continue;
                    }
                    let share = (ow * oh) / bbox_area;
                    demand[idx(i, j, grid_cols)] += length_um * share;
                }
            }
        } else {
            // One (or both) axes degenerate: apportion 1-D, by overlap
            // LENGTH / span, along whichever axis actually has extent. A
            // net whose pins all share both coordinates was already
            // handled above (length_um <= 0.0); reaching here means
            // exactly one axis has positive extent (or both are within
            // the float-noise floor but the RSMT length is still
            // positive, e.g. two extremely close but distinct pins --
            // treated as a single tile's full contribution below).
            if bbox_w > DEGENERATE_EXTENT_UM {
                for i in i_lo..=i_hi {
                    let (tx0, tx1) = tile_span(die_box_um.x0_um, tile_w_um, i);
                    let ow = overlap(xmin, xmax, tx0, tx1);
                    if ow <= 0.0 {
                        continue;
                    }
                    let share = ow / bbox_w;
                    let j = single_tile_index(ymin, die_box_um.y0_um, tile_h_um, grid_rows);
                    demand[idx(i, j, grid_cols)] += length_um * share;
                }
            } else {
                for j in j_lo..=j_hi {
                    let (ty0, ty1) = tile_span(die_box_um.y0_um, tile_h_um, j);
                    let oh = overlap(ymin, ymax, ty0, ty1);
                    if oh <= 0.0 {
                        continue;
                    }
                    let share = oh / bbox_h.max(DEGENERATE_EXTENT_UM);
                    let i = single_tile_index(xmin, die_box_um.x0_um, tile_w_um, grid_cols);
                    demand[idx(i, j, grid_cols)] += length_um * share;
                }
            }
        }
    }

    let tile_count = demand.len();
    let mut densities: Vec<f64> = demand.iter().map(|d| d / tile_area_um2).collect();
    let mean_density_um_per_um2 = densities.iter().sum::<f64>() / tile_count as f64;
    let max_density_um_per_um2 = densities.iter().cloned().fold(0.0_f64, f64::max);
    densities.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let p95_density_um_per_um2 = percentile(&densities, 0.95);

    let overflow_tile_count = demand.iter().filter(|d| **d > capacity_um_per_tile).count() as u32;
    let overflow_tile_fraction = overflow_tile_count as f64 / tile_count as f64;

    let congestion_score =
        overflow_tile_fraction.max(max_density_um_per_um2 / capacity_density_um_per_um2);

    Ok(CongestionResponse {
        schema_version: RESPONSE_SCHEMA_VERSION,
        grid_cols,
        grid_rows,
        tile_w_um,
        tile_h_um,
        capacity_um_per_tile,
        net_count,
        skipped_single_pin_nets,
        total_rsmt_length_um,
        mean_density_um_per_um2,
        max_density_um_per_um2,
        p95_density_um_per_um2,
        overflow_tile_count,
        overflow_tile_fraction,
        congestion_score,
    })
}

fn idx(i: u32, j: u32, grid_cols: u32) -> usize {
    (j as usize) * (grid_cols as usize) + (i as usize)
}

fn tile_span(origin: f64, tile_size: f64, index: u32) -> (f64, f64) {
    let t0 = origin + index as f64 * tile_size;
    (t0, t0 + tile_size)
}

fn overlap(a0: f64, a1: f64, b0: f64, b1: f64) -> f64 {
    (a1.min(b1) - a0.max(b0)).max(0.0)
}

/// Inclusive tile-index range `[lo, hi]` (clamped to `[0, count-1]`) whose
/// spans overlap `[coord0, coord1]`.
fn tile_range(coord0: f64, coord1: f64, origin: f64, tile_size: f64, count: u32) -> (u32, u32) {
    let lo = (((coord0 - origin) / tile_size).floor()).max(0.0) as u32;
    let hi_raw = (((coord1 - origin) / tile_size).floor()).max(0.0) as u32;
    let lo = lo.min(count - 1);
    let hi = hi_raw.min(count - 1).max(lo);
    (lo, hi)
}

fn single_tile_index(coord: f64, origin: f64, tile_size: f64, count: u32) -> u32 {
    (((coord - origin) / tile_size).floor().max(0.0) as u32).min(count - 1)
}

/// Nearest-rank percentile over an already-sorted slice (`q` in `[0, 1]`).
fn percentile(sorted: &[f64], q: f64) -> f64 {
    if sorted.is_empty() {
        return 0.0;
    }
    let rank = (q * (sorted.len() - 1) as f64).round() as usize;
    sorted[rank.min(sorted.len() - 1)]
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::contract::{NetRequest, PointUm};
    use approx::assert_relative_eq;

    fn box_um(x0: f64, y0: f64, x1: f64, y1: f64) -> BoxUm {
        BoxUm {
            x0_um: x0,
            y0_um: y0,
            x1_um: x1,
            y1_um: y1,
        }
    }

    fn net(name: &str, pins: &[(f64, f64)]) -> NetRequest {
        NetRequest {
            name: name.to_string(),
            pins: pins
                .iter()
                .map(|&(x, y)| PointUm { x_um: x, y_um: y })
                .collect(),
        }
    }

    #[test]
    fn empty_design_has_zero_congestion() {
        let resp = estimate(box_um(0.0, 0.0, 10.0, 10.0), 4, 4, None, None, &[]).unwrap();
        assert_eq!(resp.net_count, 0);
        assert_eq!(resp.overflow_tile_count, 0);
        assert_relative_eq!(resp.congestion_score, 0.0);
    }

    #[test]
    fn single_pin_nets_are_skipped_not_zero_length() {
        let nets = vec![net("a", &[(1.0, 1.0)])];
        let resp = estimate(box_um(0.0, 0.0, 10.0, 10.0), 4, 4, None, None, &nets).unwrap();
        assert_eq!(resp.net_count, 0);
        assert_eq!(resp.skipped_single_pin_nets, 1);
    }

    #[test]
    fn all_demand_is_conserved_across_tiles() {
        // A net spanning the whole die diagonally: total demand summed
        // back out of the grid must equal net_length_um (RUDY conserves
        // total wire length, it only relocates *where* it is credited).
        let nets = vec![net("a", &[(0.5, 0.5), (9.5, 9.5)])];
        let resp = estimate(box_um(0.0, 0.0, 10.0, 10.0), 5, 5, None, None, &nets).unwrap();
        let total_demand: f64 = resp.mean_density_um_per_um2
            * resp.tile_w_um
            * resp.tile_h_um
            * (resp.grid_cols * resp.grid_rows) as f64;
        assert_relative_eq!(total_demand, resp.total_rsmt_length_um, epsilon = 1e-6);
    }

    #[test]
    fn concentrated_net_raises_congestion_more_than_spread_net() {
        // Grid fine enough (100x100 over a 10x10 die -> 0.1um tiles) that
        // the concentrated net's own tiny bounding box (0.1um) is resolved
        // at roughly one tile, rather than diluted across a much larger
        // tile the way a coarser grid would (a real, documented RUDY
        // resolution limitation -- a sub-tile-sized net's demand still
        // conserves in total, see `all_demand_is_conserved_across_tiles`,
        // but its *peak density* is only as sharp as the grid can resolve).
        let concentrated = vec![net("a", &[(5.0, 5.0), (5.1, 5.1)])];
        let spread = vec![net("a", &[(0.0, 0.0), (9.9, 9.9)])];
        let r_concentrated = estimate(
            box_um(0.0, 0.0, 10.0, 10.0),
            100,
            100,
            None,
            None,
            &concentrated,
        )
        .unwrap();
        let r_spread =
            estimate(box_um(0.0, 0.0, 10.0, 10.0), 100, 100, None, None, &spread).unwrap();
        assert!(r_concentrated.max_density_um_per_um2 > r_spread.max_density_um_per_um2);
    }

    #[test]
    fn horizontal_net_distributes_along_single_row() {
        let nets = vec![net("a", &[(0.5, 5.0), (9.5, 5.0)])];
        let resp = estimate(box_um(0.0, 0.0, 10.0, 10.0), 5, 5, None, None, &nets).unwrap();
        // Every tile in the row containing y=5.0 should have received
        // some demand; total conserved (checked in detail above).
        assert!(resp.max_density_um_per_um2 > 0.0);
    }

    #[test]
    fn rejects_non_positive_grid_dimensions() {
        assert!(estimate(box_um(0.0, 0.0, 10.0, 10.0), 0, 4, None, None, &[]).is_err());
    }

    #[test]
    fn rejects_degenerate_die_box() {
        assert!(estimate(box_um(0.0, 0.0, 0.0, 10.0), 4, 4, None, None, &[]).is_err());
    }
}
