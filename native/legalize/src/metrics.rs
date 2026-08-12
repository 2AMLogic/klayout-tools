//! Comparison metrics this spike's own acceptance criteria ask for: overlap
//! count (must be 0), and an HPWL (half-perimeter wirelength) proxy for
//! `wirelength_um`. Deliberately independent of `def::Def` (which never
//! parses `NETS`/`PINS`, by design -- see that module's docstring): this
//! module reads those two sections read-only, purely to compute a metric,
//! never to feed them back into a written DEF.
//!
//! **HPWL simplification, stated honestly**: each net's per-instance
//! terminal location uses that instance's own placed bounding-box
//! *center* (`x + width/2, y + height/2`), not the real LEF `PIN` offset
//! within the cell -- this repo's real per-pin LEF geometry exists
//! (`lef.rs` does not parse it, to keep this spike bounded). This makes
//! the absolute HPWL figure an approximation of OpenROAD's own
//! `wirelength_um` metric (which uses real pin geometry), **not** a
//! drop-in replacement for it -- reported as `hpwl_um` (a distinctly named
//! field), never as `wirelength_um`. The same simplification is applied
//! identically to every DEF this crate measures (the GP input, OpenROAD's
//! own oracle-legalized output, and this crate's own legalized output), so
//! the *delta* between two legalizations of the same net list is still a
//! fair, apples-to-apples comparison even though the absolute number is
//! not the routed-wirelength ground truth.

use crate::def::Component;
use std::collections::HashMap;

pub struct Overlap {
    pub a: String,
    pub b: String,
}

/// Every pairwise overlap among placed components (checked per output row,
/// since rows never overlap each other in a legal single-height floorplan;
/// a component's `y` values group it into rows already). O(n log n) per
/// row via a sorted sweep, not O(n^2) globally.
pub fn find_overlaps(
    components: &[Component],
    macro_widths_um: &HashMap<String, (f64, f64)>,
    dbu: f64,
) -> Result<Vec<Overlap>, String> {
    let mut by_row: HashMap<i64, Vec<(i64, i64, &Component)>> = HashMap::new();
    for c in components {
        let (width_um, _height_um) = macro_widths_um
            .get(&c.macro_name)
            .ok_or_else(|| format!("no LEF SIZE known for macro '{}'", c.macro_name))?;
        let width_dbu = (width_um * dbu).round() as i64;
        by_row
            .entry(c.y_dbu)
            .or_default()
            .push((c.x_dbu, c.x_dbu + width_dbu, c));
    }

    let mut overlaps = Vec::new();
    for row in by_row.values_mut() {
        row.sort_by_key(|(x0, _, _)| *x0);
        for pair in row.windows(2) {
            let (_, x1_end, c1) = &pair[0];
            let (x2_start, _, c2) = &pair[1];
            if x2_start < x1_end {
                overlaps.push(Overlap {
                    a: c1.instance.clone(),
                    b: c2.instance.clone(),
                });
            }
        }
    }
    Ok(overlaps)
}

struct Terminal {
    x_dbu: f64,
    y_dbu: f64,
}

/// HPWL over every net in `def_text`'s own `NETS`/`PINS` sections -- see
/// this module's own docstring for the bbox-center-proxy simplification.
pub fn hpwl_um(
    def_text: &str,
    components: &[Component],
    macro_widths_um: &HashMap<String, (f64, f64)>,
    dbu: f64,
) -> Result<f64, String> {
    let component_centers: HashMap<&str, (f64, f64)> = components
        .iter()
        .map(|c| {
            let (w, h) = macro_widths_um
                .get(c.macro_name.as_str())
                .map(|(w, h)| (w * dbu, h * dbu))
                .unwrap_or((0.0, 0.0));
            (
                c.instance.as_str(),
                (c.x_dbu as f64 + w / 2.0, c.y_dbu as f64 + h / 2.0),
            )
        })
        .collect();

    let pin_locations = parse_pin_locations(def_text);

    let mut total_hpwl_dbu = 0.0f64;
    let mut in_nets = false;
    for line in def_text.lines() {
        let trimmed = line.trim();
        if trimmed.starts_with("NETS ") {
            in_nets = true;
            continue;
        }
        if trimmed == "END NETS" {
            break;
        }
        if !in_nets || !trimmed.starts_with("- ") {
            continue;
        }

        let mut terminals: Vec<Terminal> = Vec::new();
        let mut rest = trimmed;
        while let Some(open) = rest.find('(') {
            let close = rest[open..]
                .find(')')
                .map(|i| open + i)
                .ok_or_else(|| "unterminated '(' in NETS line".to_string())?;
            let inside = rest[open + 1..close].trim();
            let mut tokens = inside.split_whitespace();
            let first = tokens.next().unwrap_or("");
            let second = tokens.next().unwrap_or("");
            if first == "PIN" {
                if let Some((x, y)) = pin_locations.get(second) {
                    terminals.push(Terminal {
                        x_dbu: *x,
                        y_dbu: *y,
                    });
                }
            } else if let Some((x, y)) = component_centers.get(first) {
                let _ = second; // pin name ignored -- see module docstring
                terminals.push(Terminal {
                    x_dbu: *x,
                    y_dbu: *y,
                });
            }
            rest = &rest[close + 1..];
        }

        if terminals.len() >= 2 {
            let min_x = terminals.iter().map(|t| t.x_dbu).fold(f64::MAX, f64::min);
            let max_x = terminals.iter().map(|t| t.x_dbu).fold(f64::MIN, f64::max);
            let min_y = terminals.iter().map(|t| t.y_dbu).fold(f64::MAX, f64::min);
            let max_y = terminals.iter().map(|t| t.y_dbu).fold(f64::MIN, f64::max);
            total_hpwl_dbu += (max_x - min_x) + (max_y - min_y);
        }
    }

    Ok(total_hpwl_dbu / dbu)
}

/// `PINS` section: `- <name> + NET ... + PORT + LAYER ... + PLACED ( x y )
/// <orient> ;` -- only the name and the `PLACED` coordinate matter here.
fn parse_pin_locations(def_text: &str) -> HashMap<String, (f64, f64)> {
    let mut locations = HashMap::new();
    let mut in_pins = false;
    let mut current_name: Option<String> = None;
    for line in def_text.lines() {
        let trimmed = line.trim();
        if trimmed.starts_with("PINS ") {
            in_pins = true;
            continue;
        }
        if trimmed == "END PINS" {
            break;
        }
        if !in_pins {
            continue;
        }
        if let Some(rest) = trimmed.strip_prefix("- ") {
            current_name = rest.split_whitespace().next().map(|s| s.to_string());
        }
        if trimmed.contains("PLACED") {
            if let (Some(name), Some(open)) = (&current_name, trimmed.find('(')) {
                if let Some(close) = trimmed[open..].find(')') {
                    let inside = &trimmed[open + 1..open + close];
                    let mut nums = inside.split_whitespace();
                    if let (Some(x), Some(y)) = (nums.next(), nums.next()) {
                        if let (Ok(x), Ok(y)) = (x.parse::<f64>(), y.parse::<f64>()) {
                            locations.insert(name.clone(), (x, y));
                        }
                    }
                }
            }
        }
    }
    locations
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::def::Def;

    const SAMPLE: &str = "\
VERSION 5.8 ;
UNITS DISTANCE MICRONS 1000 ;
DIEAREA ( 0 0 ) ( 10000 5440 ) ;
ROW ROW_0 unithd 0 0 N DO 20 BY 1 STEP 460 0 ;
COMPONENTS 2 ;
    - u1 cellA + PLACED ( 0 0 ) N ;
    - u2 cellA + PLACED ( 1000 0 ) N ;
END COMPONENTS
PINS 1 ;
    - clk + NET clk + DIRECTION INPUT + USE SIGNAL
      + PORT
        + LAYER met2 ( -70 -242 ) ( 70 243 )
        + PLACED ( 2000 5440 ) N ;
END PINS
NETS 1 ;
    - n1 ( PIN clk ) ( u1 A ) ( u2 B ) + USE SIGNAL ;
END NETS
END DESIGN
";

    fn widths() -> HashMap<String, (f64, f64)> {
        let mut m = HashMap::new();
        m.insert("cellA".to_string(), (0.46, 2.72));
        m
    }

    #[test]
    fn no_overlap_when_cells_dont_touch() {
        let def = Def::parse(SAMPLE).unwrap();
        let overlaps = find_overlaps(&def.components, &widths(), def.dbu as f64).unwrap();
        assert!(overlaps.is_empty());
    }

    #[test]
    fn overlap_detected_when_cells_touch() {
        let text = SAMPLE.replace("PLACED ( 1000 0 )", "PLACED ( 100 0 )");
        let def = Def::parse(&text).unwrap();
        let overlaps = find_overlaps(&def.components, &widths(), def.dbu as f64).unwrap();
        assert_eq!(overlaps.len(), 1);
    }

    #[test]
    fn hpwl_matches_hand_computed_bbox() {
        let def = Def::parse(SAMPLE).unwrap();
        let hpwl = hpwl_um(SAMPLE, &def.components, &widths(), def.dbu as f64).unwrap();
        // u1 center (230, 1360), u2 center (1230, 1360), PIN clk (2000, 5440).
        // bbox: x [230, 2000] -> 1770, y [1360, 5440] -> 4080; sum in dbu =
        // 5850, / 1000 dbu-per-um = 5.85 um.
        assert!((hpwl - 5.85).abs() < 1e-6, "hpwl={hpwl}");
    }
}
