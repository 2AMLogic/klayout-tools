//! Classifies every cell in a parsed [`Library`] against the ten-primitive
//! generic-cell vocabulary
//! (`docs/design/synth-techmap-stage-contract.md` section 2.2), **by truth
//! table** (`boolfn.rs`) for combinational cells and by `ff`-group shape
//! for sequential ones -- not by cell-name pattern matching, so this
//! module works against any liberty library using the same grammar, not
//! only `sky130_fd_sc_hd`'s own naming convention.
//!
//! The result is a [`CellLibrary`]: for each generic type, every
//! liberty cell (across every drive strength) capable of realising it,
//! each carrying an `area_um2` and a representative `delay_ns` (`nldm.rs`,
//! at a fixed nominal operating point -- this is a v1 cell-selection
//! heuristic, not a path-based STA number; see module docs on
//! [`best_candidate`]). `map.rs` picks among these per generic-netlist
//! node.

use std::collections::HashMap;

use crate::boolfn;
use crate::liberty::{ArcKind, Cell, Direction, Library, Pin};
use crate::nldm;

/// The representative operating point used for every candidate's `delay_ns`
/// score -- matching `native/statime`'s own corpus-benchmark convention
/// (`tests/corpus/statime/compare.py`'s `INPUT_TRANSITION_NS`/
/// `OUTPUT_LOAD_PF`), so a number produced here is at least anchored to a
/// precedent already used elsewhere in this epic, not invented fresh.
pub const DEFAULT_INPUT_TRANSITION_NS: f64 = 0.05;
pub const DEFAULT_OUTPUT_LOAD_PF: f64 = 0.03;

#[derive(Debug, Clone)]
pub struct CombCandidate {
    pub cell_name: String,
    pub area_um2: f64,
    pub delay_ns: f64,
    /// Generic pin name (`"A"`, `"B"`, `"S"`, `"Y"`) -> this liberty
    /// cell's own pin name.
    pub pin_map: HashMap<&'static str, String>,
}

#[derive(Debug, Clone)]
pub struct DffCandidate {
    pub cell_name: String,
    pub area_um2: f64,
    pub delay_ns: f64,
    pub clk_pin: String,
    pub d_pin: String,
    pub q_pin: String,
    /// `Some((pin_name, active_low))` for the reset family; `None` for the
    /// plain family.
    pub reset: Option<(String, bool)>,
}

/// A dedicated constant-value ("tie") cell candidate -- a cell with an
/// output pin whose `function` text is the literal `"0"` or `"1"` (no free
/// variables), e.g. `sky130_fd_sc_hd__conb_1`'s `HI`/`LO` pins. Realises
/// this stage's own answer to
/// `docs/design/synth-techmap-stage-contract.md` section 8's open
/// question ("how a tie-hi/tie-lo net... is resolved"): the interim
/// on-ramp converter (`tests/corpus/techmap/`) represents a constant net
/// as the reserved pseudo-net name `"$const0"`/`"$const1"`, and
/// `map.rs` resolves each one it finds to a single shared instance of the
/// cheapest candidate below -- the additive-schema-field answer the
/// contract asked this issue to propose, not a directly-wired power rail
/// (which is not a valid logic net for a standard-cell input pin).
#[derive(Debug, Clone)]
pub struct TieCandidate {
    pub cell_name: String,
    pub pin_name: String,
    pub area_um2: f64,
}

#[derive(Debug, Default)]
pub struct CellLibrary {
    pub comb: HashMap<&'static str, Vec<CombCandidate>>,
    pub dff_plain: Vec<DffCandidate>,
    pub dff_reset: Vec<DffCandidate>,
    pub tie_hi: Vec<TieCandidate>,
    pub tie_lo: Vec<TieCandidate>,
}

fn representative_delay(cell: &Cell, out_pin: &Pin) -> f64 {
    let mut worst = 0.0f64;
    for arc in &out_pin.arcs {
        let rise = arc
            .cell_rise
            .as_ref()
            .map(|t| nldm::interpolate(t, DEFAULT_INPUT_TRANSITION_NS, DEFAULT_OUTPUT_LOAD_PF))
            .unwrap_or(0.0);
        let fall = arc
            .cell_fall
            .as_ref()
            .map(|t| nldm::interpolate(t, DEFAULT_INPUT_TRANSITION_NS, DEFAULT_OUTPUT_LOAD_PF))
            .unwrap_or(0.0);
        let avg = (rise + fall) / 2.0;
        if avg > worst {
            worst = avg;
        }
    }
    let _ = cell; // reserved: a future revision may use cell-level attrs too
    worst
}

const AND2_TT: [bool; 4] = [false, false, false, true];
const NAND2_TT: [bool; 4] = [true, true, true, false];
const OR2_TT: [bool; 4] = [false, true, true, true];
const NOR2_TT: [bool; 4] = [true, false, false, false];
const XOR2_TT: [bool; 4] = [false, true, true, false];
const XNOR2_TT: [bool; 4] = [true, false, false, true];

/// Classify a single-output combinational cell against the vocabulary's
/// `$not`/`$buf`/2-input/`$mux2` shapes, by evaluating the output pin's
/// `function` truth table. Returns the matched generic type plus a
/// generic-pin -> liberty-pin map, or `None` if this cell isn't a
/// recognisable v1 shape (a complex compound gate, a cell with no
/// `function` attribute, a multi-output cell, ...).
fn classify_combinational(cell: &Cell) -> Option<(&'static str, HashMap<&'static str, String>)> {
    let outputs: Vec<&Pin> = cell
        .pins
        .values()
        .filter(|p| p.direction == Direction::Output)
        .collect();
    if outputs.len() != 1 {
        return None;
    }
    let out = outputs[0];
    let func_text = out.function.as_ref()?;
    let expr = boolfn::parse(func_text).ok()?;
    let vars = boolfn::free_vars(&expr);

    match vars.len() {
        1 => {
            let tt = boolfn::truth_table(&expr, &vars);
            let mut map = HashMap::new();
            map.insert("A", vars[0].clone());
            map.insert("Y", out.name.clone());
            if tt == [false, true] {
                Some(("$buf", map))
            } else if tt == [true, false] {
                Some(("$not", map))
            } else {
                None
            }
        }
        2 => {
            let tt = boolfn::truth_table(&expr, &vars);
            let mut map = HashMap::new();
            map.insert("A", vars[0].clone());
            map.insert("B", vars[1].clone());
            map.insert("Y", out.name.clone());
            let tt: [bool; 4] = [tt[0], tt[1], tt[2], tt[3]];
            if tt == AND2_TT {
                Some(("$and2", map))
            } else if tt == NAND2_TT {
                Some(("$nand2", map))
            } else if tt == OR2_TT {
                Some(("$or2", map))
            } else if tt == NOR2_TT {
                Some(("$nor2", map))
            } else if tt == XOR2_TT {
                Some(("$xor2", map))
            } else if tt == XNOR2_TT {
                Some(("$xnor2", map))
            } else {
                None
            }
        }
        3 => classify_mux(&expr, &vars, out),
        _ => None,
    }
}

/// `$mux2`: `Y = S ? B : A`. Not symmetric, so (unlike the 2-input
/// functions above) which of the 3 free variables is the select matters --
/// try each of the 3 as a candidate `S`, both role assignments of the
/// remaining two as `A`/`B`.
fn classify_mux(
    expr: &boolfn::Expr,
    vars: &[String],
    out: &Pin,
) -> Option<(&'static str, HashMap<&'static str, String>)> {
    for s_idx in 0..3 {
        let s = &vars[s_idx];
        let others: Vec<&String> = vars
            .iter()
            .enumerate()
            .filter(|(i, _)| *i != s_idx)
            .map(|(_, v)| v)
            .collect();
        let (p, q) = (others[0].clone(), others[1].clone());
        // Evaluate the full truth table with a fixed order [p, q, s].
        let order = vec![p.clone(), q.clone(), s.clone()];
        let tt = boolfn::truth_table(expr, &order);
        let expected_a_is_p = |row: usize| -> bool {
            let p_bit = (row >> 2) & 1 == 1;
            let q_bit = (row >> 1) & 1 == 1;
            let s_bit = row & 1 == 1;
            if s_bit {
                q_bit
            } else {
                p_bit
            }
        };
        let expected_a_is_q = |row: usize| -> bool {
            let p_bit = (row >> 2) & 1 == 1;
            let q_bit = (row >> 1) & 1 == 1;
            let s_bit = row & 1 == 1;
            if s_bit {
                p_bit
            } else {
                q_bit
            }
        };
        if (0..8).all(|r| tt[r] == expected_a_is_p(r)) {
            let mut map = HashMap::new();
            map.insert("A", p);
            map.insert("B", q);
            map.insert("S", s.clone());
            map.insert("Y", out.name.clone());
            return Some(("$mux2", map));
        }
        if (0..8).all(|r| tt[r] == expected_a_is_q(r)) {
            let mut map = HashMap::new();
            map.insert("A", q);
            map.insert("B", p);
            map.insert("S", s.clone());
            map.insert("Y", out.name.clone());
            return Some(("$mux2", map));
        }
    }
    None
}

/// Classify a sequential (`ff`-bearing) cell as either the plain-D or
/// async-clear family (`docs/design/synth-techmap-stage-contract.md`
/// section 2.2's `$dff` -- posedge clock, optional active-high async reset
/// to `Q=0`). Cells whose `next_state` isn't a bare data-pin reference
/// (clock-enable, scan, `preset`-based set-to-1 cells) are deliberately
/// **not** classified here -- `map.rs` realises a generic `$dff`'s `EN`
/// input by wiring a `$mux2` feedback chain in front of a plain/reset
/// candidate instead (see that module's docs), so this classifier only
/// needs the two base shapes.
fn classify_sequential(cell: &Cell) -> Option<DffCandidate> {
    let ff = cell.ff.as_ref()?;
    let clocked_on = ff.clocked_on.trim();
    if clocked_on.starts_with('!') {
        return None; // negedge clock: out of v1 scope
    }
    if !cell.pins.contains_key(clocked_on) {
        return None;
    }
    let d_pin_name = ff.next_state.trim();
    let d_pin = cell.pins.get(d_pin_name)?;
    if d_pin.direction != Direction::Input {
        return None;
    }
    // Find the Q output pin: the output pin with a rising-edge arc related
    // to the clock -- the same structural heuristic
    // `native/statime/README.md` documents for this same library
    // ("Register data pins identified by the literal name... Correct for
    // every sky130_fd_sc_hd sequential cell this corpus uses"). Walked in
    // `pin_order` (declaration order from the `.lib` file), not
    // `cell.pins.values()` -- a `HashMap` iterates in an unspecified,
    // run-to-run-varying order, so a cell exposing both `Q` and `Q_N` with
    // matching rising-edge arcs on each (a real liberty shape) could
    // otherwise get wired to either pin nondeterministically between runs.
    let q_pin = cell.pin_order.iter().find_map(|name| {
        let p = cell.pins.get(name)?;
        (p.direction == Direction::Output
            && p.arcs
                .iter()
                .any(|a| a.kind == ArcKind::RisingEdge && a.related_pin == clocked_on))
        .then_some(p)
    })?;

    if ff.preset.is_some() {
        return None; // Q=1 async set: out of v1 scope (see contract §2.2)
    }

    let reset = match &ff.clear {
        None => None,
        Some(clear_text) => {
            let clear_text = clear_text.trim();
            let (active_low, pin_name) = match clear_text.strip_prefix('!') {
                Some(rest) => (true, rest.trim()),
                None => (false, clear_text),
            };
            if !cell.pins.contains_key(pin_name) {
                return None;
            }
            Some((pin_name.to_string(), active_low))
        }
    };

    Some(DffCandidate {
        cell_name: cell.name.clone(),
        area_um2: cell.area_um2,
        delay_ns: representative_delay(cell, q_pin),
        clk_pin: clocked_on.to_string(),
        d_pin: d_pin_name.to_string(),
        q_pin: q_pin.name.clone(),
        reset,
    })
}

/// Cell-name substrings this stage never considers as a general-logic
/// candidate, regardless of what their `function` truth table says --
/// this stage's own, minimal equivalent of `abc -dont_use`
/// (`docs/design/synthesize-qor-improvements-survey.md` documents that
/// flag's role in the existing Yosys+ABC flow). Measured, not
/// theoretical: `sky130_fd_sc_hd__lpflow_inputiso1p_1` (a power-domain
/// input-isolation cell) has `function : "(A) | (SLEEP)"` over its own
/// two declared input pins -- a functionally exact, but never
/// synthesis-intended, 2-input OR realisation that the truth-table
/// classifier alone cannot distinguish from a real `sky130_fd_sc_hd__or2`
/// cell. Every `lpflow_*` cell in `sky130_fd_sc_hd` is a power-management/
/// isolation/level-shifter primitive (clock/decap/tap variants included),
/// never appropriate general logic, so the whole family is excluded by
/// name rather than trying to characterise "isolation-cell shaped"
/// automatically.
const CELL_NAME_DENYLIST_SUBSTRINGS: &[&str] = &["lpflow"];

fn is_denylisted(cell_name: &str) -> bool {
    CELL_NAME_DENYLIST_SUBSTRINGS
        .iter()
        .any(|s| cell_name.contains(s))
}

/// Build the full [`CellLibrary`] from a parsed liberty [`Library`].
/// Iterates every cell (deterministic, name-sorted order); cells that
/// don't classify against the v1 vocabulary are silently excluded (not an
/// error -- a real standard-cell library has hundreds of cells outside
/// this stage's 10-primitive scope, e.g. compound AOI/OAI gates, scan
/// flops, tie/fill/decap cells), as are cells matching
/// [`CELL_NAME_DENYLIST_SUBSTRINGS`].
pub fn build(lib: &Library) -> CellLibrary {
    let mut out = CellLibrary::default();
    let mut names: Vec<&String> = lib.cells.keys().collect();
    names.sort();
    for name in names {
        if is_denylisted(name) {
            continue;
        }
        let cell = &lib.cells[name];
        if cell.ff.is_some() {
            if let Some(cand) = classify_sequential(cell) {
                if cand.reset.is_some() {
                    out.dff_reset.push(cand);
                } else {
                    out.dff_plain.push(cand);
                }
            }
            continue;
        }
        if let Some((gtype, pin_map)) = classify_combinational(cell) {
            let out_pin_name = pin_map.get("Y").cloned().unwrap_or_default();
            let out_pin = cell.pins.get(&out_pin_name);
            let delay_ns = out_pin
                .map(|p| representative_delay(cell, p))
                .unwrap_or(0.0);
            out.comb.entry(gtype).or_default().push(CombCandidate {
                cell_name: cell.name.clone(),
                area_um2: cell.area_um2,
                delay_ns,
                pin_map,
            });
        }
        // Tie-cell detection is independent of (and runs alongside) the
        // single-output-pin combinational classifier above -- a real tie
        // cell (e.g. `conb`) typically has *two* output pins (HI and LO
        // together), which the single-output classifier already excludes.
        for pin in cell.pins.values() {
            if pin.direction != Direction::Output {
                continue;
            }
            match pin.function.as_deref().map(str::trim) {
                Some("1") => out.tie_hi.push(TieCandidate {
                    cell_name: cell.name.clone(),
                    pin_name: pin.name.clone(),
                    area_um2: cell.area_um2,
                }),
                Some("0") => out.tie_lo.push(TieCandidate {
                    cell_name: cell.name.clone(),
                    pin_name: pin.name.clone(),
                    area_um2: cell.area_um2,
                }),
                _ => {}
            }
        }
    }
    for v in out.comb.values_mut() {
        v.sort_by(|a, b| a.area_um2.partial_cmp(&b.area_um2).unwrap());
    }
    out.dff_plain
        .sort_by(|a, b| a.area_um2.partial_cmp(&b.area_um2).unwrap());
    out.dff_reset
        .sort_by(|a, b| a.area_um2.partial_cmp(&b.area_um2).unwrap());
    out.tie_hi
        .sort_by(|a, b| a.area_um2.partial_cmp(&b.area_um2).unwrap());
    out.tie_lo
        .sort_by(|a, b| a.area_um2.partial_cmp(&b.area_um2).unwrap());
    out
}

/// The v1 cell-selection score: `area_um2 + delay_weight * delay_ns`.
/// `delay_weight == 0.0` (no `constraints.clock_period_ns` in the request,
/// per the contract's `null: map for area only, no delay target`) reduces
/// this to pure area minimisation; a positive weight biases toward lower
/// intrinsic delay at an area cost. This is a deliberately simple, stated
/// v1 heuristic -- not a critical-path/slack-aware sizing loop (that's
/// Epic #704 Phase 3's "timing-driven optimization" scope, per
/// `docs/design/synth-techmap-stage-contract.md`'s own "Related" list),
/// but it is the "tunable cell-selection scoring function" section 7
/// names as this stage's unlock.
pub fn best_comb(candidates: &[CombCandidate], delay_weight: f64) -> Option<&CombCandidate> {
    candidates.iter().min_by(|a, b| {
        let ca = a.area_um2 + delay_weight * a.delay_ns;
        let cb = b.area_um2 + delay_weight * b.delay_ns;
        ca.partial_cmp(&cb).unwrap()
    })
}

pub fn best_dff(candidates: &[DffCandidate], delay_weight: f64) -> Option<&DffCandidate> {
    candidates.iter().min_by(|a, b| {
        let ca = a.area_um2 + delay_weight * a.delay_ns;
        let cb = b.area_um2 + delay_weight * b.delay_ns;
        ca.partial_cmp(&cb).unwrap()
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::liberty;

    const MINI_LIB: &str = r#"
library ("test_lib") {
    time_unit : "1ns";
    capacitive_load_unit(1.0, "pf");

    cell ("AND2_0") {
        area : 6.0;
        pin ("A") { direction : "input"; }
        pin ("B") { direction : "input"; }
        pin ("X") {
            direction : "output";
            function : "(A&B)";
            timing () {
                related_pin : "A";
                cell_rise ("d") { index_1 ("0.05"); index_2 ("0.03"); values ("0.10"); }
                cell_fall ("d") { index_1 ("0.05"); index_2 ("0.03"); values ("0.12"); }
            }
        }
    }

    cell ("AND2_2") {
        area : 10.0;
        pin ("A") { direction : "input"; }
        pin ("B") { direction : "input"; }
        pin ("X") {
            direction : "output";
            function : "(A&B)";
            timing () {
                related_pin : "A";
                cell_rise ("d") { index_1 ("0.05"); index_2 ("0.03"); values ("0.05"); }
                cell_fall ("d") { index_1 ("0.05"); index_2 ("0.03"); values ("0.06"); }
            }
        }
    }

    cell ("CONB") {
        area : 3.75;
        pin ("HI") { direction : "output"; function : "1"; }
        pin ("LO") { direction : "output"; function : "0"; }
    }

    cell ("sky130_fd_sc_hd__lpflow_inputiso1p_1") {
        area : 1.0;
        pin ("A") { direction : "input"; }
        pin ("SLEEP") { direction : "input"; }
        pin ("X") { direction : "output"; function : "(A) | (SLEEP)"; }
    }

    cell ("MUX2") {
        area : 8.0;
        pin ("A0") { direction : "input"; }
        pin ("A1") { direction : "input"; }
        pin ("S") { direction : "input"; }
        pin ("X") {
            direction : "output";
            function : "(A0&!S) | (A1&S)";
        }
    }

    cell ("DFXTP") {
        area : 15.0;
        ff ("IQ", "IQN") {
            clocked_on : "CLK";
            next_state : "D";
        }
        pin ("CLK") { direction : "input"; }
        pin ("D") { direction : "input"; }
        pin ("Q") {
            direction : "output";
            timing () {
                related_pin : "CLK";
                timing_type : "rising_edge";
                cell_rise ("d") { index_1 ("0.05"); index_2 ("0.03"); values ("0.30"); }
                cell_fall ("d") { index_1 ("0.05"); index_2 ("0.03"); values ("0.32"); }
            }
        }
    }

    cell ("DFRTP") {
        area : 18.0;
        ff ("IQ", "IQN") {
            clocked_on : "CLK";
            next_state : "D";
            clear : "!RESET_B";
        }
        pin ("CLK") { direction : "input"; }
        pin ("D") { direction : "input"; }
        pin ("RESET_B") { direction : "input"; }
        pin ("Q") {
            direction : "output";
            timing () {
                related_pin : "CLK";
                timing_type : "rising_edge";
                cell_rise ("d") { index_1 ("0.05"); index_2 ("0.03"); values ("0.35"); }
                cell_fall ("d") { index_1 ("0.05"); index_2 ("0.03"); values ("0.37"); }
            }
        }
    }
}
"#;

    /// A dff cell exposing both `Q` and `Q_N` with matching rising-edge
    /// arcs on `CLK` -- built directly (not via `liberty::parse`) so its
    /// `pin_order` is explicit and this test doesn't perturb the shared
    /// `MINI_LIB` fixture (and its cardinality assertions) other tests
    /// depend on.
    fn dff_with_q_and_qn() -> Cell {
        use crate::liberty::{ArcKind, Direction, FfInfo, TimingArcTables};

        let rising_edge_arc = || TimingArcTables {
            related_pin: "CLK".to_string(),
            kind: ArcKind::RisingEdge,
            cell_rise: None,
            cell_fall: None,
        };
        let mut pins = HashMap::new();
        pins.insert(
            "CLK".to_string(),
            crate::liberty::Pin {
                name: "CLK".to_string(),
                direction: Direction::Input,
                capacitance_pf: 0.0,
                function: None,
                arcs: Vec::new(),
            },
        );
        pins.insert(
            "D".to_string(),
            crate::liberty::Pin {
                name: "D".to_string(),
                direction: Direction::Input,
                capacitance_pf: 0.0,
                function: None,
                arcs: Vec::new(),
            },
        );
        pins.insert(
            "Q".to_string(),
            crate::liberty::Pin {
                name: "Q".to_string(),
                direction: Direction::Output,
                capacitance_pf: 0.0,
                function: None,
                arcs: vec![rising_edge_arc()],
            },
        );
        pins.insert(
            "Q_N".to_string(),
            crate::liberty::Pin {
                name: "Q_N".to_string(),
                direction: Direction::Output,
                capacitance_pf: 0.0,
                function: None,
                arcs: vec![rising_edge_arc()],
            },
        );
        Cell {
            name: "DFXTP_QN".to_string(),
            area_um2: 20.0,
            pins,
            pin_order: vec![
                "CLK".to_string(),
                "D".to_string(),
                "Q".to_string(),
                "Q_N".to_string(),
            ],
            ff: Some(FfInfo {
                state_var: "IQ".to_string(),
                clocked_on: "CLK".to_string(),
                next_state: "D".to_string(),
                clear: None,
                preset: None,
            }),
        }
    }

    fn build_lib() -> CellLibrary {
        let lib = liberty::parse(MINI_LIB).unwrap();
        build(&lib)
    }

    #[test]
    fn classifies_and2_candidates_by_area() {
        let cl = build_lib();
        let cands = cl.comb.get("$and2").unwrap();
        assert_eq!(cands.len(), 2);
        assert_eq!(cands[0].cell_name, "AND2_0"); // smaller area first
        assert_eq!(cands[0].pin_map.get("A").unwrap(), "A");
        assert_eq!(cands[0].pin_map.get("Y").unwrap(), "X");
    }

    #[test]
    fn classifies_mux2() {
        let cl = build_lib();
        let cands = cl.comb.get("$mux2").unwrap();
        assert_eq!(cands.len(), 1);
        let m = &cands[0];
        assert_eq!(m.pin_map.get("A").unwrap(), "A0");
        assert_eq!(m.pin_map.get("B").unwrap(), "A1");
        assert_eq!(m.pin_map.get("S").unwrap(), "S");
    }

    #[test]
    fn classifies_dff_plain_and_reset() {
        let cl = build_lib();
        assert_eq!(cl.dff_plain.len(), 1);
        assert_eq!(cl.dff_plain[0].cell_name, "DFXTP");
        assert!(cl.dff_plain[0].reset.is_none());

        assert_eq!(cl.dff_reset.len(), 1);
        let r = &cl.dff_reset[0];
        assert_eq!(r.cell_name, "DFRTP");
        let (pin, active_low) = r.reset.as_ref().unwrap();
        assert_eq!(pin, "RESET_B");
        assert!(*active_low);
    }

    #[test]
    fn denylists_lpflow_isolation_cells_despite_a_matching_truth_table() {
        // sky130_fd_sc_hd__lpflow_inputiso1p_1's own `function` genuinely
        // truth-table-matches $or2 (and it has an artificially tiny area
        // in this fixture, so it would otherwise always win) -- it must
        // still never appear as an $or2 candidate.
        let cl = build_lib();
        let ors = cl.comb.get("$or2").cloned().unwrap_or_default();
        assert!(
            ors.iter().all(|c| !c.cell_name.contains("lpflow")),
            "denylisted cell leaked into $or2 candidates: {ors:?}"
        );
    }

    #[test]
    fn single_output_constant_cell_is_not_misclassified_as_buf() {
        // A PDK with separate single-output tie-hi/tie-lo cells (unlike
        // sky130's own two-output `conb`) must not have its constant
        // function `"1"`/`"0"` truth-table-match `$buf` -- see
        // `boolfn::bare_constants_are_not_free_variables`.
        const TIE_LIB: &str = r#"
library ("tie_lib") {
    time_unit : "1ns";
    capacitive_load_unit(1.0, "pf");

    cell ("TIEHI") {
        area : 1.0;
        pin ("HI") { direction : "output"; function : "1"; }
    }

    cell ("TIELO") {
        area : 1.0;
        pin ("LO") { direction : "output"; function : "0"; }
    }
}
"#;
        let lib = liberty::parse(TIE_LIB).unwrap();
        let cl = build(&lib);
        assert!(
            cl.comb.get("$buf").map(|v| v.is_empty()).unwrap_or(true),
            "constant tie cell must not appear as a $buf candidate: {:?}",
            cl.comb.get("$buf")
        );
        assert_eq!(cl.tie_hi.len(), 1);
        assert_eq!(cl.tie_hi[0].cell_name, "TIEHI");
        assert_eq!(cl.tie_lo.len(), 1);
        assert_eq!(cl.tie_lo[0].cell_name, "TIELO");
    }

    #[test]
    fn classifies_tie_cells() {
        let cl = build_lib();
        assert_eq!(cl.tie_hi.len(), 1);
        assert_eq!(cl.tie_hi[0].cell_name, "CONB");
        assert_eq!(cl.tie_hi[0].pin_name, "HI");
        assert_eq!(cl.tie_lo.len(), 1);
        assert_eq!(cl.tie_lo[0].pin_name, "LO");
    }

    #[test]
    fn area_only_score_picks_cheapest() {
        let cl = build_lib();
        let cands = cl.comb.get("$and2").unwrap();
        let best = best_comb(cands, 0.0).unwrap();
        assert_eq!(best.cell_name, "AND2_0");
    }

    #[test]
    fn q_pin_selection_is_deterministic_not_hashmap_order() {
        // A dff exposing both Q and Q_N with matching rising-edge arcs on
        // CLK -- classify_sequential must always pick the pin declared
        // first in the liberty file (`pin_order`), not whichever pin a
        // `HashMap` iterator happens to yield first.
        let cell = dff_with_q_and_qn();
        for _ in 0..20 {
            let cand = classify_sequential(&cell).expect("should classify as a plain dff");
            assert_eq!(cand.q_pin, "Q");
        }
    }

    #[test]
    fn delay_weighted_score_can_prefer_faster_larger_cell() {
        let cl = build_lib();
        let cands = cl.comb.get("$and2").unwrap();
        // AND2_0: area 6.0, delay ~0.11; AND2_2: area 10.0, delay ~0.055.
        // A large enough delay_weight should flip the choice to AND2_2.
        let best = best_comb(cands, 1000.0).unwrap();
        assert_eq!(best.cell_name, "AND2_2");
    }
}
