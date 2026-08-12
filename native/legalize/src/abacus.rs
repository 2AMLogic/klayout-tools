//! Abacus-style row legalization (Spindler/Johannes, "Abacus: Fast Legal-
//! ization of Standard Cell Circuits with Minimal Movement", ISPD 2008) --
//! zero-overlap, minimal (least-squares) displacement, on the site grid.
//!
//! Scope, stated honestly (this is a go/no-go spike, issue #784): single-
//! height standard cells only (every row in this corpus's fixtures shares
//! one site height, matching `sky130_fd_sc_hd`'s own single-height `unithd`
//! site -- no `unithddbl` cell is exercised); row *assignment* uses a
//! simple nearest-row-by-y heuristic rather than Abacus's own full dynamic-
//! programming row-assignment search (the row-assignment step is a much
//! smaller lever than the row-*legalization* step this spike is actually
//! evaluating -- see `docs/design/place-and-route-improvements-survey.md`
//! section 3.5). Cluster collapsing (the actual Abacus algorithm) is exact;
//! a final deterministic left-to-right sweep additionally guarantees zero
//! overlap even if the cluster math's own boundary handling has an edge
//! case this corpus's own two fixtures did not exercise -- `legalize()`
//! reports whether that safety-net sweep ever had to move anything, so a
//! nonzero count is itself an observable signal the cluster algorithm
//! needs a closer look before this crate is trusted beyond a spike.

use crate::def::{Component, Def};
use std::collections::HashMap;

pub struct LegalizeResult {
    pub components: Vec<Component>,
    pub total_displacement_dbu: i64,
    pub max_displacement_dbu: i64,
    /// Cells the final safety-net sweep had to move *again*, after the
    /// Abacus cluster algorithm already placed them -- see this module's
    /// own docstring. Zero on both corpus fixtures is the expected/desired
    /// result; nonzero does not itself violate the "zero overlap" AC (the
    /// sweep still guarantees that), but would be a flag that the cluster
    /// math needs closer review before trusting it beyond this spike.
    pub safety_net_moves: usize,
}

struct Cell {
    idx: usize, // index into the original `Def::components` vec
    target_x_dbu: i64,
    width_dbu: i64,
}

/// A single Abacus cluster: `n` cells occupying `[x, x + width)`, whose
/// optimal (unclamped) least-squares position is `sum_e / n`, where
/// `sum_e = sum(target_x_i - offset_i)` and `offset_i` is cell `i`'s
/// cumulative left-edge offset from the cluster's own start.
#[derive(Clone)]
struct Cluster {
    cells: Vec<usize>, // indices into the row's own `cells` slice, left to right
    width_dbu: i64,
    sum_e: f64,
    x_dbu: i64, // the cluster's own optimal (rounded) start position
}

impl Cluster {
    /// Rounds the cluster's own least-squares-optimal start to the
    /// *nearest site-grid multiple* (`site_step_dbu`), not merely the
    /// nearest database unit -- required for the "on-site-grid" part of
    /// this spike's own acceptance criteria: real standard cells may only
    /// ever start on a site boundary, and a cluster start that lands
    /// between two grid points would leave a *sub-grid* gap to its right
    /// neighbor once expanded -- neither zero (abutting) nor a whole
    /// number of empty sites, which is exactly the "too-close-but-not-
    /// touching" placement a real design-rule spacing check (`li1.space.1`
    /// etc.) flags. Measured live on this spike's own corpus fixtures:
    /// rounding to the nearest database unit instead of the nearest site
    /// produced a legally "zero-bbox-overlap" placement that was still
    /// badly DRC-dirty (over a thousand spacing violations) -- this is the
    /// fix for that, not a defensive-only guard.
    fn recompute_x(&mut self, site_step_dbu: i64) {
        let raw = self.sum_e / self.cells.len() as f64;
        let steps = (raw / site_step_dbu as f64).round() as i64;
        self.x_dbu = steps * site_step_dbu;
    }
}

pub fn legalize(
    def: &Def,
    site_size_um: (f64, f64),
    macro_widths_um: &HashMap<String, (f64, f64)>,
) -> Result<LegalizeResult, String> {
    let dbu = def.dbu as f64;
    let site_height_dbu = (site_size_um.1 * dbu).round() as i64;

    let mut components = def.components.clone();

    // Group component indices by nearest row.
    if def.rows.is_empty() {
        return Err("DEF has no ROW definitions".to_string());
    }
    let mut rows_sorted = def.rows.clone();
    rows_sorted.sort_by_key(|r| r.y_dbu);
    let row0_y = rows_sorted[0].y_dbu;
    let row_pitch = if rows_sorted.len() > 1 {
        rows_sorted[1].y_dbu - rows_sorted[0].y_dbu
    } else {
        site_height_dbu
    };
    if row_pitch <= 0 {
        return Err("non-positive row pitch".to_string());
    }

    let mut by_row: Vec<Vec<Cell>> = (0..rows_sorted.len()).map(|_| Vec::new()).collect();
    for (idx, c) in components.iter().enumerate() {
        let (width_um, height_um) = macro_widths_um
            .get(&c.macro_name)
            .ok_or_else(|| format!("no LEF SIZE known for macro '{}'", c.macro_name))?;
        let width_dbu = (width_um * dbu).round() as i64;
        let height_dbu = (height_um * dbu).round() as i64;
        if height_dbu != site_height_dbu {
            return Err(format!(
                "component '{}' (macro '{}') has height {height_dbu} dbu, expected the \
                 single row height {site_height_dbu} dbu -- multi-height legalization is out \
                 of scope for this spike",
                c.instance, c.macro_name
            ));
        }
        let mut row_index = ((c.y_dbu - row0_y) as f64 / row_pitch as f64).round() as i64;
        row_index = row_index.clamp(0, rows_sorted.len() as i64 - 1);
        by_row[row_index as usize].push(Cell {
            idx,
            target_x_dbu: c.x_dbu,
            width_dbu,
        });
    }

    let site_step_dbu = (site_size_um.0 * dbu).round() as i64;
    if site_step_dbu <= 0 {
        return Err("non-positive site width".to_string());
    }

    let mut total_displacement_dbu: i64 = 0;
    let mut max_displacement_dbu: i64 = 0;
    let mut safety_net_moves: usize = 0;

    for (row_i, row) in rows_sorted.iter().enumerate() {
        let mut cells = std::mem::take(&mut by_row[row_i]);
        if cells.is_empty() {
            continue;
        }
        cells.sort_by_key(|c| c.target_x_dbu);

        let x_min = row.x_min_dbu();
        let x_max = row.x_max_dbu();
        let placed = legalize_row(&cells, x_min, x_max, site_step_dbu);

        let mut cur_x = x_min;
        let mut final_positions = placed;
        for (i, cell) in cells.iter().enumerate() {
            if final_positions[i] < cur_x {
                safety_net_moves += 1;
                final_positions[i] = cur_x;
            }
            cur_x = final_positions[i] + cell.width_dbu;
        }
        if cur_x > x_max {
            // Extremely dense row (should not happen on a legal GP
            // solution's own row assignment) -- shift the whole row's
            // final placement left just enough to fit, preserving
            // relative order/spacing exactly (still zero-overlap).
            let shift = cur_x - x_max;
            for pos in final_positions.iter_mut() {
                *pos -= shift;
            }
        }

        for (i, cell) in cells.iter().enumerate() {
            let x = final_positions[i];
            let displacement =
                (x - cell.target_x_dbu).abs() + (row.y_dbu - components[cell.idx].y_dbu).abs();
            total_displacement_dbu += displacement;
            max_displacement_dbu = max_displacement_dbu.max(displacement);
            components[cell.idx].x_dbu = x;
            components[cell.idx].y_dbu = row.y_dbu;
            components[cell.idx].orient = row.orient.clone();
        }
    }

    Ok(LegalizeResult {
        components,
        total_displacement_dbu,
        max_displacement_dbu,
        safety_net_moves,
    })
}

/// The core Abacus `PlaceRow` cluster-collapse procedure: processes cells
/// in target-x order, merging into a cluster whenever a naive placement
/// would overlap the previous cluster, always re-deriving the exact
/// least-squares-optimal start position for the merged cluster. Then
/// clamps the resulting cluster chain to `[x_min, x_max)` -- clamp the
/// leftmost cluster forward and cascade right, then clamp the rightmost
/// cluster backward and cascade left (the standard boundary-handling used
/// by row legalizers so a row-edge cluster doesn't just get chopped) --
/// *before* expanding clusters into per-cell positions, so the
/// grid/overlap-guaranteeing sweep in `legalize()` is a rarely-triggered
/// safety net rather than the primary mechanism absorbing every
/// boundary case.
fn legalize_row(cells: &[Cell], x_min: i64, _x_max: i64, site_step_dbu: i64) -> Vec<i64> {
    let mut stack: Vec<Cluster> = Vec::new();

    for (i, cell) in cells.iter().enumerate() {
        let mut new_cluster = Cluster {
            cells: vec![i],
            width_dbu: cell.width_dbu,
            sum_e: cell.target_x_dbu as f64,
            x_dbu: cell.target_x_dbu,
        };
        new_cluster.recompute_x(site_step_dbu); // grid-round even a singleton
        stack.push(new_cluster);

        // Collapse while the new/merged cluster would overlap its left
        // neighbor.
        while stack.len() >= 2 {
            let top = stack.pop().unwrap();
            let mut prev = stack.pop().unwrap();
            if prev.x_dbu + prev.width_dbu > top.x_dbu {
                // Merge `top` into `prev` (top is entirely to the right):
                // every cell in `top` gets `prev.width_dbu` added to its
                // within-cluster offset once merged.
                let width_prev = prev.width_dbu;
                prev.sum_e += top.sum_e - (top.cells.len() as f64) * (width_prev as f64);
                prev.cells.extend(top.cells);
                prev.width_dbu += top.width_dbu;
                prev.recompute_x(site_step_dbu);
                stack.push(prev);
            } else {
                stack.push(prev);
                stack.push(top);
                break;
            }
        }
    }

    // Boundary clamp + cascade: pin the leftmost cluster to the row's own
    // left edge, then cascade rightward -- this alone is enough to
    // guarantee no *left* overlap remains (each cluster is already >= its
    // left neighbor's right edge by construction; only the very first
    // cluster can start left of `x_min`). A symmetric right-to-left pass
    // was tried and **removed**: clamping the rightmost cluster to `x_max`
    // and cascading left can shove a cluster left of its own left
    // neighbor's right edge (re-introducing exactly the overlap the
    // forward pass just resolved) -- measured live, it raised (not
    // lowered) `legalize()`'s own safety-net trigger rate on both corpus
    // fixtures. A row overflowing `x_max` (total assigned cell width
    // exceeding the row's own capacity) should not occur for a legal GP
    // solution's own row assignment; `legalize()`'s caller-side fallback
    // (uniformly shifting an over-full row's cells left by the overflow
    // amount, preserving every inter-cell spacing) covers that residual
    // case without this function needing to.
    if let Some(first) = stack.first_mut() {
        if first.x_dbu < x_min {
            first.x_dbu = x_min;
        }
    }
    for i in 1..stack.len() {
        let min_x = stack[i - 1].x_dbu + stack[i - 1].width_dbu;
        if stack[i].x_dbu < min_x {
            stack[i].x_dbu = min_x;
        }
    }

    // Expand clusters back into per-cell positions, cell offsets computed
    // in the cells' own within-cluster (target-x sorted) order.
    let mut positions = vec![0i64; cells.len()];
    for cluster in &stack {
        let mut offset = 0i64;
        for &cell_i in &cluster.cells {
            positions[cell_i] = cluster.x_dbu + offset;
            offset += cells[cell_i].width_dbu;
        }
    }
    positions
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::def::Def;

    fn tiny_def(components: &str) -> String {
        format!(
            "VERSION 5.8 ;\nUNITS DISTANCE MICRONS 1000 ;\nDIEAREA ( 0 0 ) ( 10000 2720 ) ;\n\
             ROW ROW_0 unithd 0 0 N DO 20 BY 1 STEP 460 0 ;\n\
             COMPONENTS {n} ;\n{components}END COMPONENTS\nEND DESIGN\n",
            n = components.lines().count(),
            components = components
        )
    }

    fn widths() -> HashMap<String, (f64, f64)> {
        let mut m = HashMap::new();
        m.insert("cellA".to_string(), (0.46, 2.72));
        m
    }

    #[test]
    fn two_overlapping_cells_are_separated_with_zero_overlap() {
        // Both cells target the same x -- must be separated.
        let text = tiny_def(
            "    - u1 cellA + PLACED ( 1000 0 ) N ;\n    - u2 cellA + PLACED ( 1000 0 ) N ;\n",
        );
        let def = Def::parse(&text).unwrap();
        let result = legalize(&def, (0.46, 2.72), &widths()).unwrap();
        let mut xs: Vec<i64> = result.components.iter().map(|c| c.x_dbu).collect();
        xs.sort();
        assert_eq!(xs[1] - xs[0], 460); // cellA width in dbu
        assert_eq!(result.safety_net_moves, 0);
    }

    #[test]
    fn already_legal_row_is_left_alone() {
        let text = tiny_def(
            "    - u1 cellA + PLACED ( 0 0 ) N ;\n    - u2 cellA + PLACED ( 460 0 ) N ;\n",
        );
        let def = Def::parse(&text).unwrap();
        let result = legalize(&def, (0.46, 2.72), &widths()).unwrap();
        assert_eq!(result.total_displacement_dbu, 0);
    }

    #[test]
    fn dense_row_clamped_to_die_stays_zero_overlap() {
        // Ten cells, each 460 dbu wide, all targeting the far-right edge --
        // must spread out to fill available space with zero overlap.
        let mut components = String::new();
        for i in 0..10 {
            components.push_str(&format!("    - u{i} cellA + PLACED ( 9000 0 ) N ;\n"));
        }
        let text = tiny_def(&components);
        let def = Def::parse(&text).unwrap();
        let result = legalize(&def, (0.46, 2.72), &widths()).unwrap();
        let mut xs: Vec<i64> = result.components.iter().map(|c| c.x_dbu).collect();
        xs.sort();
        for w in xs.windows(2) {
            assert!(w[1] - w[0] >= 460, "overlap: {:?}", xs);
        }
    }
}
