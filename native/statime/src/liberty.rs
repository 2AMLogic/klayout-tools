//! A liberty (`.lib`) parser scoped to what NLDM gate-level STA needs:
//! library-level units, and per-cell pin direction/capacitance plus
//! `timing()` arcs (`cell_rise`/`cell_fall`/`rise_transition`/`fall_transition`
//! 2D lookup tables).
//!
//! This is **not** a general liberty parser -- it does not model power
//! tables, `bus`/`bundle` pins, statetable-based sequential cells, or
//! multi-corner `operating_conditions` selection (real STA does; this spike
//! reads whichever single `.lib` file it is pointed at, exactly the same
//! single-corner scope `klt synthesize`'s own `abc -liberty` invocation
//! already has, per `docs/design/yosys-synthesis-spike.md`). It *is* a
//! general recursive-descent parser for liberty's group/attribute grammar
//! (`group_name (args) { ... }`, `simple_attr : value ;`,
//! `complex_attr (v1, v2, ...) ;`), so any well-formed `.lib` file parses
//! structurally even where semantic extraction below only reads a subset of
//! it -- the tokenizer/parser is not hand-tuned to sky130's file.

mod liberty_parse;

use liberty_parse::{parse_top, Group, Member, Value};
use std::collections::HashMap;
use std::fmt;

// ---------------------------------------------------------------------
// Semantic model
// ---------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Direction {
    Input,
    Output,
    Inout,
}

/// `timing_sense` -- determines which input edge maps to which output edge
/// for a given arc. `NonUnate` is also this parser's default when the
/// attribute is absent, which is the conservative choice (evaluate both
/// input edges for either output edge) rather than assuming a polarity
/// that might be wrong.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TimingSense {
    Positive,
    Negative,
    NonUnate,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ArcKind {
    /// `timing_type` absent, or one of the `combinational*` values.
    Combinational,
    /// `timing_type: rising_edge` -- the clock-to-Q arc of a positive-edge
    /// register.
    RisingEdge,
    /// `timing_type: falling_edge`.
    FallingEdge,
    /// Anything else (setup/hold/recovery/removal/min_pulse_width/clear/
    /// preset/three_state...) -- recorded but never used for delay
    /// propagation. See module docs: async set/reset paths and constraint
    /// checks are out of scope for this spike.
    Other,
}

/// A 2D NLDM lookup table: rows indexed by `index_1` (input transition,
/// ns), columns by `index_2` (output load, pF).
#[derive(Debug, Clone)]
pub struct Table2D {
    pub index1: Vec<f64>,
    pub index2: Vec<f64>,
    pub values: Vec<Vec<f64>>,
}

#[derive(Debug, Clone)]
pub struct TimingArcTables {
    pub related_pin: String,
    pub kind: Option<ArcKind>,
    pub sense: TimingSense,
    pub cell_rise: Option<Table2D>,
    pub cell_fall: Option<Table2D>,
    pub rise_transition: Option<Table2D>,
    pub fall_transition: Option<Table2D>,
}

#[derive(Debug, Clone)]
pub struct Pin {
    pub name: String,
    pub direction: Direction,
    /// Average of rise/fall input capacitance (pF), or the bare
    /// `capacitance` attribute when rise/fall aren't split. 0 for outputs.
    pub capacitance_pf: f64,
    pub arcs: Vec<TimingArcTables>,
}

#[derive(Debug, Clone)]
pub struct Cell {
    pub name: String,
    pub pins: HashMap<String, Pin>,
}

#[derive(Debug, Clone)]
pub struct Library {
    pub name: String,
    /// Multiply liberty time-unit values by this to get nanoseconds.
    pub time_unit_to_ns: f64,
    /// Multiply liberty cap-unit values by this to get picofarads.
    pub cap_unit_to_pf: f64,
    pub cells: HashMap<String, Cell>,
}

#[derive(Debug)]
pub struct LibertyError(pub String);

impl fmt::Display for LibertyError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

fn parse_time_unit(s: &str) -> f64 {
    // e.g. "1ns", "1ps", "10ps"
    let s = s.trim();
    let split_at = s
        .find(|c: char| !c.is_ascii_digit() && c != '.')
        .unwrap_or(s.len());
    let (num, unit) = s.split_at(split_at);
    let num: f64 = if num.is_empty() {
        1.0
    } else {
        num.parse().unwrap_or(1.0)
    };
    let unit = unit.trim().to_ascii_lowercase();
    let base_to_ns = match unit.as_str() {
        "ns" => 1.0,
        "ps" => 1e-3,
        "us" => 1e3,
        "s" => 1e9,
        _ => 1.0,
    };
    num * base_to_ns
}

fn parse_cap_unit(args: &[Value]) -> f64 {
    // capacitive_load_unit(1.0, "pf") or (1.0, "ff")
    if args.len() < 2 {
        return 1.0;
    }
    let num: f64 = args[0].as_str().parse().unwrap_or(1.0);
    let unit = args[1].as_str().to_ascii_lowercase();
    let base_to_pf = match unit.as_str() {
        "pf" => 1.0,
        "ff" => 1e-3,
        "nf" => 1e3,
        _ => 1.0,
    };
    num * base_to_pf
}

fn parse_number_list(s: &str) -> Vec<f64> {
    s.split(',')
        .filter_map(|tok| tok.trim().parse::<f64>().ok())
        .collect()
}

fn find_complex_attr<'a>(members: &'a [Member], name: &str) -> Option<&'a Vec<Value>> {
    members.iter().find_map(|m| match m {
        Member::ComplexAttr(n, args) if n == name => Some(args),
        _ => None,
    })
}

fn find_simple_attr<'a>(members: &'a [Member], name: &str) -> Option<&'a Value> {
    members.iter().find_map(|m| match m {
        Member::SimpleAttr(n, v) if n == name => Some(v),
        _ => None,
    })
}

fn find_groups<'a>(members: &'a [Member], name: &str) -> Vec<&'a Group> {
    members
        .iter()
        .filter_map(|m| match m {
            Member::Group(g) if g.name == name => Some(g),
            _ => None,
        })
        .collect()
}

fn parse_table(group: &Group) -> Table2D {
    let index1 = find_complex_attr(&group.members, "index_1")
        .and_then(|v| v.first())
        .map(|v| parse_number_list(v.as_str()))
        .unwrap_or_default();
    let index2 = find_complex_attr(&group.members, "index_2")
        .and_then(|v| v.first())
        .map(|v| parse_number_list(v.as_str()))
        .unwrap_or_default();
    let values_args = find_complex_attr(&group.members, "values")
        .cloned()
        .unwrap_or_default();
    let values: Vec<Vec<f64>> = values_args
        .iter()
        .map(|v| parse_number_list(v.as_str()))
        .collect();
    Table2D {
        index1,
        index2,
        values,
    }
}

fn parse_timing_sense(members: &[Member]) -> TimingSense {
    match find_simple_attr(members, "timing_sense").map(|v| v.as_str()) {
        Some("positive_unate") => TimingSense::Positive,
        Some("negative_unate") => TimingSense::Negative,
        _ => TimingSense::NonUnate,
    }
}

fn parse_arc_kind(members: &[Member]) -> ArcKind {
    match find_simple_attr(members, "timing_type").map(|v| v.as_str()) {
        None => ArcKind::Combinational,
        Some("combinational") | Some("combinational_rise") | Some("combinational_fall") => {
            ArcKind::Combinational
        }
        Some("rising_edge") => ArcKind::RisingEdge,
        Some("falling_edge") => ArcKind::FallingEdge,
        Some(_) => ArcKind::Other,
    }
}

fn parse_pin(group: &Group) -> Pin {
    let name = group
        .args
        .first()
        .map(|v| v.as_str().to_string())
        .unwrap_or_default();
    let direction = match find_simple_attr(&group.members, "direction").map(|v| v.as_str()) {
        Some("output") => Direction::Output,
        Some("inout") => Direction::Inout,
        _ => Direction::Input,
    };
    let rise_cap = find_simple_attr(&group.members, "rise_capacitance")
        .and_then(|v| v.as_str().parse::<f64>().ok());
    let fall_cap = find_simple_attr(&group.members, "fall_capacitance")
        .and_then(|v| v.as_str().parse::<f64>().ok());
    let bare_cap = find_simple_attr(&group.members, "capacitance")
        .and_then(|v| v.as_str().parse::<f64>().ok());
    let capacitance_pf = match (rise_cap, fall_cap, bare_cap) {
        (Some(r), Some(f), _) => (r + f) / 2.0,
        (_, _, Some(c)) => c,
        _ => 0.0,
    };

    let mut arcs = Vec::new();
    for timing_group in find_groups(&group.members, "timing") {
        let related_pin = find_simple_attr(&timing_group.members, "related_pin")
            .map(|v| v.as_str().to_string())
            .unwrap_or_default();
        let kind = parse_arc_kind(&timing_group.members);
        let sense = parse_timing_sense(&timing_group.members);
        let cell_rise = find_groups(&timing_group.members, "cell_rise")
            .first()
            .map(|g| parse_table(g));
        let cell_fall = find_groups(&timing_group.members, "cell_fall")
            .first()
            .map(|g| parse_table(g));
        let rise_transition = find_groups(&timing_group.members, "rise_transition")
            .first()
            .map(|g| parse_table(g));
        let fall_transition = find_groups(&timing_group.members, "fall_transition")
            .first()
            .map(|g| parse_table(g));
        arcs.push(TimingArcTables {
            related_pin,
            kind: Some(kind),
            sense,
            cell_rise,
            cell_fall,
            rise_transition,
            fall_transition,
        });
    }

    Pin {
        name,
        direction,
        capacitance_pf,
        arcs,
    }
}

fn parse_cell(group: &Group) -> Cell {
    let name = group
        .args
        .first()
        .map(|v| v.as_str().to_string())
        .unwrap_or_default();
    let mut pins = HashMap::new();
    for pin_group in find_groups(&group.members, "pin") {
        let pin = parse_pin(pin_group);
        pins.insert(pin.name.clone(), pin);
    }
    Cell { name, pins }
}

/// Parse an entire liberty file's text into a [`Library`].
///
/// Every `cell()` group in the file is parsed (not just ones a particular
/// netlist references) -- selective parsing is a valid future optimisation
/// but this spike measures the full, general parse path.
pub fn parse(src: &str) -> Result<Library, LibertyError> {
    let top = parse_top(src)?;
    if top.name != "library" {
        return Err(LibertyError(format!(
            "expected top-level 'library' group, got '{}'",
            top.name
        )));
    }
    let name = top
        .args
        .first()
        .map(|v| v.as_str().to_string())
        .unwrap_or_default();
    let time_unit_to_ns = find_simple_attr(&top.members, "time_unit")
        .map(|v| parse_time_unit(v.as_str()))
        .unwrap_or(1.0);
    let cap_unit_to_pf = find_complex_attr(&top.members, "capacitive_load_unit")
        .map(|args| parse_cap_unit(args))
        .unwrap_or(1.0);

    let mut cells = HashMap::new();
    for cell_group in find_groups(&top.members, "cell") {
        let cell = parse_cell(cell_group);
        cells.insert(cell.name.clone(), cell);
    }

    Ok(Library {
        name,
        time_unit_to_ns,
        cap_unit_to_pf,
        cells,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const MINI_LIB: &str = r#"
library ("test_lib") {
    time_unit : "1ns";
    capacitive_load_unit(1.0, "pf");

    cell ("BUF1") {
        area : 1.0;
        pin ("A") {
            direction : "input";
            capacitance : 0.002;
        }
        pin ("Y") {
            direction : "output";
            timing () {
                related_pin : "A";
                timing_sense : "positive_unate";
                cell_rise ("d") {
                    index_1 ("0.01, 0.5");
                    index_2 ("0.005, 0.2");
                    values ("0.10, 0.20", \
                            "0.30, 0.40");
                }
                cell_fall ("d") {
                    index_1 ("0.01, 0.5");
                    index_2 ("0.005, 0.2");
                    values ("0.11, 0.21", \
                            "0.31, 0.41");
                }
                rise_transition ("d") {
                    index_1 ("0.01, 0.5");
                    index_2 ("0.005, 0.2");
                    values ("0.05, 0.06", \
                            "0.07, 0.08");
                }
                fall_transition ("d") {
                    index_1 ("0.01, 0.5");
                    index_2 ("0.005, 0.2");
                    values ("0.05, 0.06", \
                            "0.07, 0.08");
                }
            }
        }
    }

    cell ("DFF1") {
        ff (IQ, IQN) {
            clocked_on : "CLK";
            next_state : "D";
        }
        pin ("CLK") {
            direction : "input";
            capacitance : 0.003;
        }
        pin ("D") {
            direction : "input";
            capacitance : 0.002;
            timing () {
                related_pin : "CLK";
                timing_type : "setup_rising";
            }
        }
        pin ("Q") {
            direction : "output";
            timing () {
                related_pin : "CLK";
                timing_type : "rising_edge";
                cell_rise ("d") {
                    index_1 ("0.01, 0.5");
                    index_2 ("0.005, 0.2");
                    values ("0.40, 0.45", \
                            "0.50, 0.55");
                }
                cell_fall ("d") {
                    index_1 ("0.01, 0.5");
                    index_2 ("0.005, 0.2");
                    values ("0.40, 0.45", \
                            "0.50, 0.55");
                }
                rise_transition ("d") {
                    index_1 ("0.01, 0.5");
                    index_2 ("0.005, 0.2");
                    values ("0.08, 0.09", \
                            "0.10, 0.11");
                }
                fall_transition ("d") {
                    index_1 ("0.01, 0.5");
                    index_2 ("0.005, 0.2");
                    values ("0.08, 0.09", \
                            "0.10, 0.11");
                }
            }
        }
    }
}
"#;

    #[test]
    fn parses_library_units() {
        let lib = parse(MINI_LIB).unwrap();
        assert_eq!(lib.name, "test_lib");
        assert_eq!(lib.time_unit_to_ns, 1.0);
        assert_eq!(lib.cap_unit_to_pf, 1.0);
        assert_eq!(lib.cells.len(), 2);
    }

    #[test]
    fn parses_combinational_arc() {
        let lib = parse(MINI_LIB).unwrap();
        let cell = &lib.cells["BUF1"];
        let a = &cell.pins["A"];
        assert_eq!(a.direction, Direction::Input);
        assert!((a.capacitance_pf - 0.002).abs() < 1e-12);
        let y = &cell.pins["Y"];
        assert_eq!(y.direction, Direction::Output);
        assert_eq!(y.arcs.len(), 1);
        let arc = &y.arcs[0];
        assert_eq!(arc.related_pin, "A");
        assert_eq!(arc.kind, Some(ArcKind::Combinational));
        let table = arc.cell_rise.as_ref().unwrap();
        assert_eq!(table.index1, vec![0.01, 0.5]);
        assert_eq!(table.index2, vec![0.005, 0.2]);
        assert_eq!(table.values, vec![vec![0.10, 0.20], vec![0.30, 0.40]]);
    }

    #[test]
    fn parses_sequential_arc_and_skips_setup_check() {
        let lib = parse(MINI_LIB).unwrap();
        let cell = &lib.cells["DFF1"];
        let d = &cell.pins["D"];
        assert_eq!(d.arcs.len(), 1);
        assert_eq!(d.arcs[0].kind, Some(ArcKind::Other)); // setup_rising
        let q = &cell.pins["Q"];
        assert_eq!(q.arcs.len(), 1);
        assert_eq!(q.arcs[0].kind, Some(ArcKind::RisingEdge));
        assert_eq!(q.arcs[0].related_pin, "CLK");
    }

    #[test]
    fn line_continuation_is_joined() {
        // The MINI_LIB values() lists all use a trailing backslash before
        // the newline -- if that weren't handled, parsing would fail
        // outright (the continuation line has no leading attr name).
        let lib = parse(MINI_LIB).unwrap();
        let table = lib.cells["BUF1"].pins["Y"].arcs[0]
            .cell_rise
            .as_ref()
            .unwrap();
        assert_eq!(table.values.len(), 2);
        assert_eq!(table.values[1], vec![0.30, 0.40]);
    }

    #[test]
    fn truncated_group_returns_err_not_panic() {
        // Issue #5629: the file ends right after the opening brace -- EOF
        // inside an open group body must surface as a group-body
        // diagnostic through the `Result<_, LibertyError>` the public
        // signature already promises, never as a panic (a panic crossing
        // the pyo3 boundary surfaces as PanicException, which sta.py's
        // `except ValueError` does not catch).
        let err = parse("library (demo) {").unwrap_err();
        assert!(
            err.0.contains("unexpected token in group body"),
            "unexpected diagnostic: {}",
            err.0
        );
    }

    #[test]
    fn empty_input_returns_err_not_panic() {
        // Initial EOF is distinct from EOF inside an open group: the whole
        // file is empty, reported as such rather than as a group-body
        // diagnostic.
        let err = parse("").unwrap_err();
        assert_eq!(err.0, "empty liberty file");
    }

    #[test]
    fn malformed_nested_member_returns_err_not_panic() {
        // `area` with neither ':' nor '(' after it -- reached through the
        // nested-group recursion, same error path as the truncation above.
        let err = parse(r#"library (demo) { cell (A) { area } }"#).unwrap_err();
        assert!(
            err.0
                .contains("expected ':' or '(' after member name 'area'"),
            "unexpected diagnostic: {}",
            err.0
        );
    }
}
