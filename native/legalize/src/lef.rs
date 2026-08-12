//! Minimal LEF reader -- only the fields this spike's Abacus legalizer
//! actually needs: a named `SITE`'s `SIZE` (row height/site width, from the
//! tech LEF) and each `MACRO`'s `SIZE` (per-instance width, from the cell
//! LEF). Never a general-purpose LEF parser -- PIN/PORT/OBS geometry,
//! layer rules, and every other LEF construct are ignored (this spike's
//! legality objective is purely 1-D row placement: zero overlap on the
//! site grid, not routing/DRC geometry).

use std::collections::HashMap;

/// One `SITE <name> ... SIZE <w> BY <h> ; ... END <name>` block's size, in
/// database units (already scaled by the DEF's own `UNITS DISTANCE MICRONS
/// <dbu>` -- see `def::Def::dbu`).
#[derive(Debug, Clone, Copy)]
pub struct SiteSize {
    pub width_dbu: i64,
    pub height_dbu: i64,
}

/// Find one named `SITE`'s `SIZE` in a tech LEF's text, in microns (caller
/// scales to DEF database units once the DEF's own `dbu` is known -- LEF
/// itself is always in microns for `SIZE`, unlike DEF).
pub fn find_site_size_um(tech_lef_text: &str, site_name: &str) -> Option<(f64, f64)> {
    let needle = format!("SITE {site_name}\n");
    let start = tech_lef_text.find(&needle)?;
    let block_end = tech_lef_text[start..].find(&format!("END {site_name}"))?;
    let block = &tech_lef_text[start..start + block_end];
    parse_size_line(block)
}

/// Every `MACRO <name> ... SIZE <w> BY <h> ; ... END <name>` block's width
/// in a cell LEF, in microns, keyed by macro name.
pub fn parse_macro_sizes_um(cell_lef_text: &str) -> HashMap<String, (f64, f64)> {
    let mut sizes = HashMap::new();
    let mut cursor = 0usize;
    while let Some(rel) = cell_lef_text[cursor..].find("MACRO ") {
        let macro_start = cursor + rel;
        // The macro name is the token right after "MACRO ", up to the
        // newline (LEF's own `MACRO <name>` header line has no other
        // tokens).
        let after = &cell_lef_text[macro_start + "MACRO ".len()..];
        let name_end = after.find('\n').unwrap_or(after.len());
        let name = after[..name_end].trim().to_string();

        let end_marker = format!("\nEND {name}");
        let Some(end_rel) = cell_lef_text[macro_start..].find(&end_marker) else {
            cursor = macro_start + "MACRO ".len();
            continue;
        };
        let block = &cell_lef_text[macro_start..macro_start + end_rel];
        if let Some(size) = parse_size_line(block) {
            sizes.insert(name, size);
        }
        cursor = macro_start + end_rel + end_marker.len();
    }
    sizes
}

/// Parses a `SIZE <w> BY <h> ;` line found anywhere in `block`.
fn parse_size_line(block: &str) -> Option<(f64, f64)> {
    for line in block.lines() {
        let trimmed = line.trim();
        if let Some(rest) = trimmed.strip_prefix("SIZE ") {
            let rest = rest.trim_end_matches(';').trim();
            let mut parts = rest.split_whitespace();
            let w: f64 = parts.next()?.parse().ok()?;
            let by = parts.next()?;
            if by != "BY" {
                return None;
            }
            let h: f64 = parts.next()?.parse().ok()?;
            return Some((w, h));
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn finds_site_size() {
        let lef = "\
SITE unithddbl
  SYMMETRY Y ;
  CLASS CORE ;
  SIZE 0.46 BY 5.44 ;
END unithddbl

SITE unithd
  SYMMETRY Y ;
  CLASS CORE ;
  SIZE 0.46 BY 2.72 ;
END unithd
";
        let (w, h) = find_site_size_um(lef, "unithd").unwrap();
        assert!((w - 0.46).abs() < 1e-9);
        assert!((h - 2.72).abs() < 1e-9);
    }

    #[test]
    fn parses_macro_sizes() {
        let lef = "\
MACRO sky130_fd_sc_hd__inv_1
  CLASS CORE ;
  ORIGIN 0.000 0.000 ;
  SIZE 1.380 BY 2.720 ;
  PIN A
    DIRECTION INPUT ;
  END A
END sky130_fd_sc_hd__inv_1

MACRO sky130_fd_sc_hd__buf_4
  CLASS CORE ;
  SIZE 2.760 BY 2.720 ;
END sky130_fd_sc_hd__buf_4
";
        let sizes = parse_macro_sizes_um(lef);
        assert_eq!(sizes.len(), 2);
        assert!((sizes["sky130_fd_sc_hd__inv_1"].0 - 1.380).abs() < 1e-9);
        assert!((sizes["sky130_fd_sc_hd__buf_4"].0 - 2.760).abs() < 1e-9);
    }
}
