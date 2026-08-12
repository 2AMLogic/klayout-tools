//! A liberty (`.lib`) parser scoped to what Liberty-driven technology
//! mapping needs: per-cell `area`, per-pin `direction`/`function` (the
//! output pin's Boolean function, used by `celllib.rs` to classify a cell
//! against the generic-cell vocabulary
//! `docs/design/synth-techmap-stage-contract.md` section 2.2 defines), and
//! `timing()` NLDM arcs (reused for the area/delay cell-selection score,
//! `nldm.rs` + `celllib.rs`), plus each cell's optional `ff (...)` group
//! (`clocked_on`/`next_state`/`clear`/`preset`) for sequential-cell
//! classification.
//!
//! This is a **fork**, not a shared dependency, of
//! `native/statime/src/liberty.rs` -- the tokenizer/parser core (grammar:
//! `group_name (args) { ... }` / `simple_attr : value ;` / `complex_attr
//! (v1, v2, ...) ;`) is identical by construction (both parse the same
//! liberty grammar), but the semantic-extraction layer captures a
//! different, non-overlapping subset of attributes (`area`, `function`,
//! `ff`, vs. that crate's NLDM-STA-only `timing()` extraction). Forking
//! avoided a premature `native/liberty-common/` shared-crate split for a
//! second consumer with a narrow, still-evolving attribute set; revisit if
//! a third native crate needs the same file.
//!
//! Same explicit non-goals as the `native/statime` original: no power
//! tables, no `bus`/`bundle` pins, no multi-corner `operating_conditions`
//! selection -- a single `.lib` file per invocation, matching
//! `docs/design/synth-techmap-stage-contract.md` section 3.

use std::collections::HashMap;
use std::fmt;

// ---------------------------------------------------------------------
// Tokenizer
// ---------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq)]
enum Token {
    Ident(String),
    Str(String),
    LParen,
    RParen,
    LBrace,
    RBrace,
    Comma,
    Colon,
    Semi,
}

fn tokenize(src: &str) -> Vec<Token> {
    let joined = src.replace("\\\r\n", "").replace("\\\n", "");

    let mut tokens = Vec::new();
    let bytes = joined.as_bytes();
    let mut i = 0;
    let n = bytes.len();
    while i < n {
        let c = bytes[i] as char;
        if c.is_whitespace() {
            i += 1;
            continue;
        }
        if c == '/' && i + 1 < n && bytes[i + 1] as char == '*' {
            i += 2;
            while i + 1 < n && !(bytes[i] as char == '*' && bytes[i + 1] as char == '/') {
                i += 1;
            }
            i += 2;
            continue;
        }
        if c == '/' && i + 1 < n && bytes[i + 1] as char == '/' {
            while i < n && bytes[i] as char != '\n' {
                i += 1;
            }
            continue;
        }
        match c {
            '(' => {
                tokens.push(Token::LParen);
                i += 1;
            }
            ')' => {
                tokens.push(Token::RParen);
                i += 1;
            }
            '{' => {
                tokens.push(Token::LBrace);
                i += 1;
            }
            '}' => {
                tokens.push(Token::RBrace);
                i += 1;
            }
            ',' => {
                tokens.push(Token::Comma);
                i += 1;
            }
            ':' => {
                tokens.push(Token::Colon);
                i += 1;
            }
            ';' => {
                tokens.push(Token::Semi);
                i += 1;
            }
            '"' => {
                let start = i + 1;
                let mut j = start;
                while j < n && bytes[j] as char != '"' {
                    j += 1;
                }
                let s = joined[start..j].to_string();
                tokens.push(Token::Str(s));
                i = j + 1;
            }
            _ => {
                let start = i;
                while i < n {
                    let ch = bytes[i] as char;
                    if ch.is_whitespace()
                        || matches!(ch, '(' | ')' | '{' | '}' | ',' | ':' | ';' | '"')
                    {
                        break;
                    }
                    i += 1;
                }
                tokens.push(Token::Ident(joined[start..i].to_string()));
            }
        }
    }
    tokens
}

// ---------------------------------------------------------------------
// Parse tree
// ---------------------------------------------------------------------

#[derive(Debug, Clone)]
enum Value {
    Ident(String),
    Str(String),
}

impl Value {
    fn as_str(&self) -> &str {
        match self {
            Value::Ident(s) | Value::Str(s) => s,
        }
    }
}

impl fmt::Display for Value {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.as_str())
    }
}

#[derive(Debug, Clone)]
struct Group {
    name: String,
    args: Vec<Value>,
    members: Vec<Member>,
}

#[derive(Debug, Clone)]
enum Member {
    Group(Group),
    SimpleAttr(String, Value),
    ComplexAttr(String, Vec<Value>),
}

struct Parser {
    tokens: Vec<Token>,
    pos: usize,
}

impl Parser {
    fn peek(&self) -> Option<&Token> {
        self.tokens.get(self.pos)
    }

    fn next(&mut self) -> Option<Token> {
        let t = self.tokens.get(self.pos).cloned();
        self.pos += 1;
        t
    }

    fn parse_group(&mut self) -> Result<Option<Group>, LibertyError> {
        let name = match self.next() {
            None => return Ok(None),
            Some(Token::Ident(s)) => s,
            Some(other) => {
                return Err(LibertyError(format!(
                    "expected group/attr name, got {:?}",
                    other
                )))
            }
        };
        let mut args = Vec::new();
        if self.peek() == Some(&Token::LParen) {
            self.next();
            loop {
                match self.peek() {
                    Some(Token::RParen) => {
                        self.next();
                        break;
                    }
                    Some(Token::Comma) => {
                        self.next();
                    }
                    Some(Token::Ident(_)) | Some(Token::Str(_)) => {
                        let v = match self.next().unwrap() {
                            Token::Ident(s) => Value::Ident(s),
                            Token::Str(s) => Value::Str(s),
                            _ => unreachable!(),
                        };
                        args.push(v);
                    }
                    other => {
                        return Err(LibertyError(format!(
                            "unexpected token in arg list: {:?}",
                            other
                        )))
                    }
                }
            }
        }
        match self.peek() {
            Some(Token::LBrace) => {
                self.next();
                let mut members = Vec::new();
                loop {
                    match self.peek() {
                        Some(Token::RBrace) => {
                            self.next();
                            break;
                        }
                        Some(Token::Semi) => {
                            self.next();
                        }
                        Some(Token::Ident(_)) => {
                            members.push(self.parse_member()?);
                        }
                        other => {
                            return Err(LibertyError(format!(
                                "unexpected token in group body: {:?}",
                                other
                            )))
                        }
                    }
                }
                Ok(Some(Group {
                    name,
                    args,
                    members,
                }))
            }
            Some(Token::Semi) => {
                self.next();
                Ok(Some(Group {
                    name,
                    args,
                    members: Vec::new(),
                }))
            }
            other => Err(LibertyError(format!(
                "expected '{{' or ';' after group header, got {:?}",
                other
            ))),
        }
    }

    fn parse_member(&mut self) -> Result<Member, LibertyError> {
        let name = match self.next() {
            Some(Token::Ident(s)) => s,
            other => {
                return Err(LibertyError(format!(
                    "expected member name, got {:?}",
                    other
                )))
            }
        };
        match self.peek() {
            Some(Token::Colon) => {
                self.next();
                let v = match self.next() {
                    Some(Token::Ident(s)) => Value::Ident(s),
                    Some(Token::Str(s)) => Value::Str(s),
                    other => {
                        return Err(LibertyError(format!(
                            "expected value after ':', got {:?}",
                            other
                        )))
                    }
                };
                if self.peek() == Some(&Token::Semi) {
                    self.next();
                }
                Ok(Member::SimpleAttr(name, v))
            }
            Some(Token::LParen) => {
                self.next();
                let mut args = Vec::new();
                loop {
                    match self.peek() {
                        Some(Token::RParen) => {
                            self.next();
                            break;
                        }
                        Some(Token::Comma) => {
                            self.next();
                        }
                        Some(Token::Ident(_)) | Some(Token::Str(_)) => {
                            let v = match self.next().unwrap() {
                                Token::Ident(s) => Value::Ident(s),
                                Token::Str(s) => Value::Str(s),
                                _ => unreachable!(),
                            };
                            args.push(v);
                        }
                        other => {
                            return Err(LibertyError(format!(
                                "unexpected token in complex-attr args: {:?}",
                                other
                            )))
                        }
                    }
                }
                match self.peek() {
                    Some(Token::LBrace) => {
                        self.next();
                        let mut members = Vec::new();
                        loop {
                            match self.peek() {
                                Some(Token::RBrace) => {
                                    self.next();
                                    break;
                                }
                                Some(Token::Semi) => {
                                    self.next();
                                }
                                Some(Token::Ident(_)) => {
                                    members.push(self.parse_member()?);
                                }
                                other => {
                                    return Err(LibertyError(format!(
                                        "unexpected token in group body: {:?}",
                                        other
                                    )))
                                }
                            }
                        }
                        Ok(Member::Group(Group {
                            name,
                            args,
                            members,
                        }))
                    }
                    Some(Token::Semi) => {
                        self.next();
                        Ok(Member::ComplexAttr(name, args))
                    }
                    other => Err(LibertyError(format!(
                        "expected '{{' or ';' after complex attr, got {:?}",
                        other
                    ))),
                }
            }
            other => Err(LibertyError(format!(
                "expected ':' or '(' after member name '{}', got {:?}",
                name, other
            ))),
        }
    }
}

fn parse_top(src: &str) -> Result<Group, LibertyError> {
    let tokens = tokenize(src);
    let mut p = Parser { tokens, pos: 0 };
    p.parse_group()?
        .ok_or_else(|| LibertyError("empty liberty file".to_string()))
}

// ---------------------------------------------------------------------
// Semantic model
// ---------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Direction {
    Input,
    Output,
    Inout,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ArcKind {
    Combinational,
    RisingEdge,
    FallingEdge,
    Other,
}

#[derive(Debug, Clone)]
pub struct Table2D {
    pub index1: Vec<f64>,
    pub index2: Vec<f64>,
    pub values: Vec<Vec<f64>>,
}

#[derive(Debug, Clone)]
pub struct TimingArcTables {
    pub related_pin: String,
    pub kind: ArcKind,
    pub cell_rise: Option<Table2D>,
    pub cell_fall: Option<Table2D>,
}

#[derive(Debug, Clone)]
pub struct Pin {
    pub name: String,
    pub direction: Direction,
    /// Average of rise/fall input capacitance (pF), or the bare
    /// `capacitance` attribute when rise/fall aren't split. 0 for outputs.
    pub capacitance_pf: f64,
    /// The `function` attribute on an output pin -- a Boolean expression
    /// over this cell's input pin names (e.g. `"(A&B)"`), used by
    /// `celllib.rs`'s truth-table classifier. `None` for input pins and for
    /// output pins liberty doesn't characterise a function for (rare for a
    /// real standard-cell library, but not assumed).
    pub function: Option<String>,
    pub arcs: Vec<TimingArcTables>,
}

/// A cell's `ff (<Q>, <QN>) { clocked_on: ...; next_state: ...; clear: ...;
/// preset: ...; }` group -- present only on sequential (flip-flop) cells.
/// `native/statime/src/liberty.rs` didn't need this (it identifies a
/// register's D/Q pins structurally); this crate does, to classify which
/// generic `$dff` shape (plain / async-reset) a given liberty cell
/// realises (`celllib.rs`).
#[derive(Debug, Clone, Default)]
pub struct FfInfo {
    /// The internal current-state variable name (`ff`'s first arg, e.g.
    /// `"IQ"`) -- the identifier `next_state` uses to refer to the flop's
    /// own held value.
    pub state_var: String,
    /// The pin name (or `!pin` for an active-low/negedge signal)
    /// `clocked_on` names.
    pub clocked_on: String,
    /// `next_state`'s raw Boolean-expression text.
    pub next_state: String,
    /// `clear`'s raw text (async reset-to-0), e.g. `"RST"` or `"!RESET_B"`.
    pub clear: Option<String>,
    /// `preset`'s raw text (async set-to-1) -- recorded but not mapped by
    /// `celllib.rs` (the generic `$dff` vocabulary is reset-to-0 only, per
    /// `docs/design/synth-techmap-stage-contract.md` section 2.2).
    pub preset: Option<String>,
}

#[derive(Debug, Clone)]
pub struct Cell {
    pub name: String,
    pub area_um2: f64,
    pub pins: HashMap<String, Pin>,
    /// Pin declaration order, as they appeared in the `.lib` file --
    /// `celllib.rs` uses this for deterministic input-pin ordering when a
    /// cell's truth table is built (the pin `HashMap` above has none).
    pub pin_order: Vec<String>,
    pub ff: Option<FfInfo>,
}

#[derive(Debug, Clone)]
pub struct Library {
    pub name: String,
    pub time_unit_to_ns: f64,
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
    let function = find_simple_attr(&group.members, "function").map(|v| v.as_str().to_string());

    let mut arcs = Vec::new();
    for timing_group in find_groups(&group.members, "timing") {
        let related_pin = find_simple_attr(&timing_group.members, "related_pin")
            .map(|v| v.as_str().to_string())
            .unwrap_or_default();
        let kind = parse_arc_kind(&timing_group.members);
        let cell_rise = find_groups(&timing_group.members, "cell_rise")
            .first()
            .map(|g| parse_table(g));
        let cell_fall = find_groups(&timing_group.members, "cell_fall")
            .first()
            .map(|g| parse_table(g));
        arcs.push(TimingArcTables {
            related_pin,
            kind,
            cell_rise,
            cell_fall,
        });
    }

    Pin {
        name,
        direction,
        capacitance_pf,
        function,
        arcs,
    }
}

fn parse_ff(group: &Group) -> FfInfo {
    let state_var = group
        .args
        .first()
        .map(|v| v.as_str().to_string())
        .unwrap_or_default();
    let clocked_on = find_simple_attr(&group.members, "clocked_on")
        .map(|v| v.as_str().to_string())
        .unwrap_or_default();
    let next_state = find_simple_attr(&group.members, "next_state")
        .map(|v| v.as_str().to_string())
        .unwrap_or_default();
    let clear = find_simple_attr(&group.members, "clear").map(|v| v.as_str().to_string());
    let preset = find_simple_attr(&group.members, "preset").map(|v| v.as_str().to_string());
    FfInfo {
        state_var,
        clocked_on,
        next_state,
        clear,
        preset,
    }
}

fn parse_cell(group: &Group) -> Cell {
    let name = group
        .args
        .first()
        .map(|v| v.as_str().to_string())
        .unwrap_or_default();
    let area_um2 = find_simple_attr(&group.members, "area")
        .and_then(|v| v.as_str().parse::<f64>().ok())
        .unwrap_or(0.0);
    let mut pins = HashMap::new();
    let mut pin_order = Vec::new();
    for pin_group in find_groups(&group.members, "pin") {
        let pin = parse_pin(pin_group);
        pin_order.push(pin.name.clone());
        pins.insert(pin.name.clone(), pin);
    }
    let ff = find_groups(&group.members, "ff")
        .first()
        .map(|g| parse_ff(g));
    Cell {
        name,
        area_um2,
        pins,
        pin_order,
        ff,
    }
}

/// Parse an entire liberty file's text into a [`Library`]. Every `cell()`
/// group is parsed, matching `native/statime/src/liberty.rs`'s own
/// "selective parsing is a future optimisation, not this parser's job"
/// posture.
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

    cell ("AND2") {
        area : 6.256;
        pin ("A") {
            direction : "input";
            capacitance : 0.0016;
        }
        pin ("B") {
            direction : "input";
            capacitance : 0.0016;
        }
        pin ("X") {
            direction : "output";
            function : "(A&B)";
            timing () {
                related_pin : "A";
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
            }
        }
    }

    cell ("DFRTP") {
        ff ("IQ", "IQN") {
            clocked_on : "CLK";
            next_state : "D";
            clear : "!RESET_B";
        }
        area : 12.0;
        pin ("CLK") {
            direction : "input";
        }
        pin ("D") {
            direction : "input";
        }
        pin ("RESET_B") {
            direction : "input";
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
            }
        }
    }
}
"#;

    #[test]
    fn parses_area_and_function() {
        let lib = parse(MINI_LIB).unwrap();
        let and2 = &lib.cells["AND2"];
        assert!((and2.area_um2 - 6.256).abs() < 1e-9);
        let x = &and2.pins["X"];
        assert_eq!(x.function.as_deref(), Some("(A&B)"));
        assert_eq!(and2.pin_order, vec!["A", "B", "X"]);
    }

    #[test]
    fn parses_ff_group_with_clear() {
        let lib = parse(MINI_LIB).unwrap();
        let dfrtp = &lib.cells["DFRTP"];
        let ff = dfrtp.ff.as_ref().unwrap();
        assert_eq!(ff.state_var, "IQ");
        assert_eq!(ff.clocked_on, "CLK");
        assert_eq!(ff.next_state, "D");
        assert_eq!(ff.clear.as_deref(), Some("!RESET_B"));
        assert_eq!(ff.preset, None);
        let q = &dfrtp.pins["Q"];
        assert_eq!(q.arcs[0].kind, ArcKind::RisingEdge);
    }

    #[test]
    fn malformed_syntax_returns_err_not_panic() {
        // Missing '{' / ';' after the group header.
        assert!(parse(r#"library ("test_lib") area : 6.256; }"#).is_err());
        // Unclosed arg list — a '}' appears where ',' or ')' is expected.
        assert!(parse(r#"library ("test_lib") { cell (1, 2, 3 }"#).is_err());
        // Truncated complex-attr arg list, never closed.
        assert!(parse(r#"library ("test_lib") { cell ("AND2") { pin ("A"#).is_err());
        // Empty input entirely.
        assert!(parse("").is_err());
        // Member with neither ':' nor '(' after its name.
        assert!(parse(r#"library ("test_lib") { cell ("AND2") { area } }"#).is_err());
    }
}
