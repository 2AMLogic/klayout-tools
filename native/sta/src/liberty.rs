//! A deliberately partial Liberty (`.lib`) reader: everything gate-level STA
//! needs from an NLDM library, and nothing else.
//!
//! Scope, stated up front because "partial liberty parser" is otherwise a
//! trap: this reads `cell` -> `pin` -> `timing` groups and the four NLDM
//! delay/transition tables (`cell_rise`, `cell_fall`, `rise_transition`,
//! `fall_transition`) plus the two constraint tables (`rise_constraint`,
//! `fall_constraint`), the pin capacitances, pin direction, and the library's
//! `time_unit`/`capacitive_load_unit`. Every other group -- `internal_power`,
//! `leakage_power`, `pg_pin`, `ccsn_*`, CCS current waveforms, `wire_load`,
//! `operating_conditions`, `normalized_driver_waveform` -- is skipped by
//! brace matching without ever being materialised, which is most of why this
//! reads sky130's 8.0 MB `tt_025C_1v80` liberty in well under a second.
//!
//! Two consequences are load-bearing for the spike's accuracy claim and are
//! called out in `../README.md` rather than hidden here:
//!
//! * **CCS is ignored.** sky130's liberty is NLDM-first; the CCS groups it
//!   also carries are not read, exactly as OpenSTA does by default.
//! * **Table templates are only a fallback.** Every sky130 delay table
//!   repeats its own `index_1`/`index_2` inline, so the `lu_table_template`
//!   indices are used only when a table omits them (permitted by the format,
//!   just unused by this library).

use std::collections::HashMap;

/// A 1-D or 2-D NLDM lookup table.
///
/// `index1` is the row axis (input net transition, or the related-pin
/// transition for a constraint table); `index2` is the column axis (total
/// output net capacitance, or the constrained-pin transition). A 1-D table
/// leaves `index2` empty and stores a single row.
#[derive(Debug, Clone, Default)]
pub struct Lut {
    pub index1: Vec<f64>,
    pub index2: Vec<f64>,
    /// Row-major: `values[i][j]` is at `(index1[i], index2[j])`.
    pub values: Vec<Vec<f64>>,
}

impl Lut {
    /// Bilinear interpolation, with **linear extrapolation** off the ends of
    /// either axis (the nearest segment's slope is continued, the value is
    /// not clamped).
    ///
    /// Extrapolation is not an embellishment: with an ideal clock, OpenSTA
    /// evaluates every register's CLK->Q arc at an input transition of
    /// exactly `0.0`, which is below the first `index_1` entry (`0.01 ns`) of
    /// every sky130 delay table. Clamping there instead of extrapolating
    /// changes the reported CLK->Q delay of every path in the design.
    pub fn eval(&self, x: f64, y: f64) -> f64 {
        if self.values.is_empty() {
            return 0.0;
        }
        if self.index2.is_empty() || self.values[0].len() == 1 {
            // 1-D table (or a degenerate single-column 2-D one).
            let col: Vec<f64> = self.values.iter().map(|r| r[0]).collect();
            return interp1(&self.index1, &col, x);
        }
        if self.index1.len() == 1 {
            return interp1(&self.index2, &self.values[0], y);
        }
        let (i, tx) = axis_frac(&self.index1, x);
        let (j, ty) = axis_frac(&self.index2, y);
        let v00 = self.values[i][j];
        let v01 = self.values[i][j + 1];
        let v10 = self.values[i + 1][j];
        let v11 = self.values[i + 1][j + 1];
        (1.0 - tx) * (1.0 - ty) * v00
            + (1.0 - tx) * ty * v01
            + tx * (1.0 - ty) * v10
            + tx * ty * v11
    }

    pub fn is_empty(&self) -> bool {
        self.values.is_empty()
    }
}

/// Locate the interpolation segment for `v` and return `(lo_index, frac)`.
///
/// `frac` is unclamped, so a `v` below `axis[0]` yields a negative fraction
/// and a `v` above the last entry yields one greater than 1 -- which is what
/// makes [`Lut::eval`] extrapolate rather than saturate.
fn axis_frac(axis: &[f64], v: f64) -> (usize, f64) {
    let n = axis.len();
    debug_assert!(n >= 2);
    let mut i = 0usize;
    if v <= axis[0] {
        i = 0;
    } else if v >= axis[n - 1] {
        i = n - 2;
    } else {
        while i + 2 < n && axis[i + 1] < v {
            i += 1;
        }
    }
    let (a, b) = (axis[i], axis[i + 1]);
    let denom = b - a;
    let frac = if denom == 0.0 { 0.0 } else { (v - a) / denom };
    (i, frac)
}

fn interp1(axis: &[f64], vals: &[f64], v: f64) -> f64 {
    if axis.len() < 2 || vals.len() < 2 {
        return vals.first().copied().unwrap_or(0.0);
    }
    let (i, t) = axis_frac(axis, v);
    vals[i] + t * (vals[i + 1] - vals[i])
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Sense {
    Positive,
    Negative,
    NonUnate,
}

impl Sense {
    /// Does an input edge `in_rise` produce an output edge `out_rise`?
    pub fn allows(&self, in_rise: bool, out_rise: bool) -> bool {
        match self {
            Sense::Positive => in_rise == out_rise,
            Sense::Negative => in_rise != out_rise,
            Sense::NonUnate => true,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ArcKind {
    /// A combinational delay arc (`timing_type: combinational`, or absent).
    Combinational,
    /// An asynchronous set/clear delay arc (`clear`, `preset`).
    Async,
    /// A clock-edge-to-output delay arc (`rising_edge`, `falling_edge`).
    Edge { rising: bool },
    /// A setup constraint (`setup_rising`, `setup_falling`).
    Setup { rising: bool },
    /// Anything else: hold, recovery, removal, min_pulse_width, ... -- parsed
    /// so the arc is accounted for, never used for max-delay propagation.
    Other,
}

#[derive(Debug, Clone)]
pub struct TimingArc {
    pub related_pin: String,
    pub sense: Sense,
    pub kind: ArcKind,
    /// The `when` condition, if the arc is state-dependent. Recorded but not
    /// evaluated -- see `../README.md` on `default_arc_mode : worst_edges`.
    pub when: Option<String>,
    pub cell_rise: Lut,
    pub cell_fall: Lut,
    pub rise_transition: Lut,
    pub fall_transition: Lut,
    pub rise_constraint: Lut,
    pub fall_constraint: Lut,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Direction {
    Input,
    Output,
    Inout,
    Internal,
}

#[derive(Debug, Clone)]
pub struct LibPin {
    pub name: String,
    pub direction: Direction,
    pub capacitance: f64,
    pub rise_capacitance: f64,
    pub fall_capacitance: f64,
    pub is_clock: bool,
    /// Arcs whose *endpoint* is this pin: delay arcs on an output pin,
    /// constraint arcs on an input pin.
    pub arcs: Vec<TimingArc>,
}

impl LibPin {
    /// Load capacitance this pin presents to a driver making the given edge.
    ///
    /// Verified against OpenSTA: for a **falling** driver output the load is
    /// the sum of the sinks' `fall_capacitance`, not their `capacitance`
    /// (`native/sta/README.md`, "Pinning the delay model down").
    pub fn load_cap(&self, rise: bool) -> f64 {
        let edge = if rise {
            self.rise_capacitance
        } else {
            self.fall_capacitance
        };
        if edge > 0.0 {
            edge
        } else {
            self.capacitance
        }
    }
}

#[derive(Debug, Clone)]
pub struct LibCell {
    pub name: String,
    pub area: f64,
    pub pins: Vec<LibPin>,
    index: HashMap<String, usize>,
}

impl LibCell {
    pub fn pin(&self, name: &str) -> Option<&LibPin> {
        self.index.get(name).map(|i| &self.pins[*i])
    }

    /// True when the cell has at least one clock-edge-triggered delay arc --
    /// i.e. it is a flip-flop (or any sequential cell with an edge arc).
    pub fn is_sequential(&self) -> bool {
        self.pins.iter().any(|p| {
            p.arcs
                .iter()
                .any(|a| matches!(a.kind, ArcKind::Edge { .. }))
        })
    }
}

#[derive(Debug, Clone, Default)]
pub struct Library {
    pub name: String,
    /// `time_unit` expressed in nanoseconds (sky130: `"1ns"` -> 1.0).
    pub time_unit_ns: f64,
    /// `capacitive_load_unit` expressed in picofarads (sky130: `pf` -> 1.0).
    pub cap_unit_pf: f64,
    pub cells: HashMap<String, LibCell>,
}

// ---------------------------------------------------------------------------
// Lexer / parser
// ---------------------------------------------------------------------------

struct Scanner<'a> {
    s: &'a [u8],
    i: usize,
}

impl<'a> Scanner<'a> {
    fn new(s: &'a str) -> Self {
        Scanner {
            s: s.as_bytes(),
            i: 0,
        }
    }

    fn eof(&self) -> bool {
        self.i >= self.s.len()
    }

    /// Skip whitespace, `/* */` comments, and `\`-newline line continuations.
    fn skip_ws(&mut self) {
        loop {
            while self.i < self.s.len() && (self.s[self.i] as char).is_ascii_whitespace() {
                self.i += 1;
            }
            if self.i + 1 < self.s.len() && self.s[self.i] == b'/' && self.s[self.i + 1] == b'*' {
                self.i += 2;
                while self.i + 1 < self.s.len()
                    && !(self.s[self.i] == b'*' && self.s[self.i + 1] == b'/')
                {
                    self.i += 1;
                }
                self.i = (self.i + 2).min(self.s.len());
                continue;
            }
            if self.i + 1 < self.s.len() && self.s[self.i] == b'/' && self.s[self.i + 1] == b'/' {
                while self.i < self.s.len() && self.s[self.i] != b'\n' {
                    self.i += 1;
                }
                continue;
            }
            if self.s[self.i.min(self.s.len().saturating_sub(1))] == b'\\' {
                // Line continuation.
                let mut j = self.i + 1;
                while j < self.s.len() && (self.s[j] == b'\r') {
                    j += 1;
                }
                if j < self.s.len() && self.s[j] == b'\n' {
                    self.i = j + 1;
                    continue;
                }
            }
            break;
        }
    }

    fn peek(&mut self) -> u8 {
        self.skip_ws();
        if self.eof() {
            0
        } else {
            self.s[self.i]
        }
    }

    fn bump(&mut self) -> u8 {
        let c = self.peek();
        if !self.eof() {
            self.i += 1;
        }
        c
    }

    /// Read a bare identifier (`cell`, `positive_unate`, `1.5e-3`, ...).
    fn ident(&mut self) -> String {
        self.skip_ws();
        let start = self.i;
        while self.i < self.s.len() {
            let c = self.s[self.i];
            if c.is_ascii_alphanumeric() || matches!(c, b'_' | b'.' | b'+' | b'-' | b'[' | b']') {
                self.i += 1;
            } else {
                break;
            }
        }
        String::from_utf8_lossy(&self.s[start..self.i]).into_owned()
    }

    /// Read a `"..."`-quoted string, honouring `\`-newline continuations.
    fn string(&mut self) -> String {
        // caller has consumed the opening quote
        let mut out = String::new();
        while self.i < self.s.len() {
            let c = self.s[self.i];
            if c == b'"' {
                self.i += 1;
                break;
            }
            if c == b'\\' {
                self.i += 1;
                continue;
            }
            out.push(c as char);
            self.i += 1;
        }
        out
    }

    /// Read the comma-separated argument list of `name ( ... )`; the caller
    /// has consumed the `(`.
    fn args(&mut self) -> Vec<String> {
        let mut out = Vec::new();
        loop {
            let c = self.peek();
            if c == 0 || c == b')' {
                self.bump();
                break;
            }
            if c == b',' {
                self.bump();
                continue;
            }
            if c == b'"' {
                self.bump();
                out.push(self.string());
            } else {
                out.push(self.ident());
            }
        }
        out
    }

    /// Read a simple attribute's value: `name : <value> ;`
    fn value(&mut self) -> String {
        let c = self.peek();
        if c == b'"' {
            self.bump();
            let v = self.string();
            // consume trailing ';'
            if self.peek() == b';' {
                self.bump();
            }
            v
        } else {
            let start = self.i;
            while self.i < self.s.len() && self.s[self.i] != b';' && self.s[self.i] != b'\n' {
                self.i += 1;
            }
            let v = String::from_utf8_lossy(&self.s[start..self.i])
                .trim()
                .to_owned();
            if self.i < self.s.len() && self.s[self.i] == b';' {
                self.i += 1;
            }
            v
        }
    }

    /// Skip a `{ ... }` body by brace matching; the caller has consumed `{`.
    fn skip_group(&mut self) {
        let mut depth = 1usize;
        while self.i < self.s.len() && depth > 0 {
            match self.s[self.i] {
                b'"' => {
                    self.i += 1;
                    while self.i < self.s.len() && self.s[self.i] != b'"' {
                        if self.s[self.i] == b'\\' {
                            self.i += 1;
                        }
                        self.i += 1;
                    }
                }
                b'/' if self.i + 1 < self.s.len() && self.s[self.i + 1] == b'*' => {
                    self.i += 2;
                    while self.i + 1 < self.s.len()
                        && !(self.s[self.i] == b'*' && self.s[self.i + 1] == b'/')
                    {
                        self.i += 1;
                    }
                    self.i += 1;
                }
                b'{' => depth += 1,
                b'}' => depth -= 1,
                _ => {}
            }
            self.i += 1;
        }
    }
}

/// One statement inside a group body.
enum Stmt {
    /// `name (args) {` -- body not yet consumed.
    GroupOpen(String, Vec<String>),
    /// `name : value ;`
    Simple(String, String),
    /// `name (args) ;`
    Complex(String, Vec<String>),
    /// `}` -- end of the enclosing group.
    Close,
    Eof,
}

fn next_stmt(sc: &mut Scanner) -> Stmt {
    let c = sc.peek();
    if c == 0 {
        return Stmt::Eof;
    }
    if c == b'}' {
        sc.bump();
        return Stmt::Close;
    }
    if c == b';' {
        sc.bump();
        return next_stmt(sc);
    }
    let name = sc.ident();
    if name.is_empty() {
        // Unexpected punctuation; consume it so we always make progress.
        sc.bump();
        return next_stmt(sc);
    }
    match sc.peek() {
        b'(' => {
            sc.bump();
            let args = sc.args();
            if sc.peek() == b'{' {
                sc.bump();
                Stmt::GroupOpen(name, args)
            } else {
                if sc.peek() == b';' {
                    sc.bump();
                }
                Stmt::Complex(name, args)
            }
        }
        b':' => {
            sc.bump();
            let v = sc.value();
            Stmt::Simple(name, v)
        }
        _ => {
            sc.bump();
            next_stmt(sc)
        }
    }
}

fn parse_floats(s: &str) -> Vec<f64> {
    s.split(',')
        .filter_map(|t| t.trim().parse::<f64>().ok())
        .collect()
}

fn parse_lut(
    sc: &mut Scanner,
    templates: &HashMap<String, (Vec<f64>, Vec<f64>)>,
    tmpl: &str,
) -> Lut {
    let mut lut = Lut::default();
    loop {
        match next_stmt(sc) {
            Stmt::Complex(name, args) => match name.as_str() {
                "index_1" => lut.index1 = parse_floats(&args.join(",")),
                "index_2" => lut.index2 = parse_floats(&args.join(",")),
                "values" => {
                    lut.values = args.iter().map(|r| parse_floats(r)).collect();
                }
                _ => {}
            },
            Stmt::GroupOpen(_, _) => sc.skip_group(),
            Stmt::Simple(_, _) => {}
            Stmt::Close | Stmt::Eof => break,
        }
    }
    if lut.index1.is_empty() {
        if let Some((i1, i2)) = templates.get(tmpl) {
            lut.index1 = i1.clone();
            if lut.index2.is_empty() {
                lut.index2 = i2.clone();
            }
        }
    }
    lut
}

fn parse_timing(sc: &mut Scanner, templates: &HashMap<String, (Vec<f64>, Vec<f64>)>) -> TimingArc {
    let mut arc = TimingArc {
        related_pin: String::new(),
        sense: Sense::NonUnate,
        kind: ArcKind::Combinational,
        when: None,
        cell_rise: Lut::default(),
        cell_fall: Lut::default(),
        rise_transition: Lut::default(),
        fall_transition: Lut::default(),
        rise_constraint: Lut::default(),
        fall_constraint: Lut::default(),
    };
    let mut sense_seen = false;
    loop {
        match next_stmt(sc) {
            Stmt::Simple(name, v) => match name.as_str() {
                "related_pin" => arc.related_pin = v,
                "when" => arc.when = Some(v),
                "timing_sense" => {
                    sense_seen = true;
                    arc.sense = match v.as_str() {
                        "positive_unate" => Sense::Positive,
                        "negative_unate" => Sense::Negative,
                        _ => Sense::NonUnate,
                    };
                }
                "timing_type" => {
                    arc.kind = match v.as_str() {
                        "combinational" | "combinational_rise" | "combinational_fall" => {
                            ArcKind::Combinational
                        }
                        "rising_edge" => ArcKind::Edge { rising: true },
                        "falling_edge" => ArcKind::Edge { rising: false },
                        "clear" | "preset" => ArcKind::Async,
                        "setup_rising" => ArcKind::Setup { rising: true },
                        "setup_falling" => ArcKind::Setup { rising: false },
                        _ => ArcKind::Other,
                    };
                }
                _ => {}
            },
            Stmt::GroupOpen(name, args) => {
                let tmpl = args.first().cloned().unwrap_or_default();
                match name.as_str() {
                    "cell_rise" => arc.cell_rise = parse_lut(sc, templates, &tmpl),
                    "cell_fall" => arc.cell_fall = parse_lut(sc, templates, &tmpl),
                    "rise_transition" => arc.rise_transition = parse_lut(sc, templates, &tmpl),
                    "fall_transition" => arc.fall_transition = parse_lut(sc, templates, &tmpl),
                    "rise_constraint" => arc.rise_constraint = parse_lut(sc, templates, &tmpl),
                    "fall_constraint" => arc.fall_constraint = parse_lut(sc, templates, &tmpl),
                    _ => sc.skip_group(),
                }
            }
            Stmt::Complex(_, _) => {}
            Stmt::Close | Stmt::Eof => break,
        }
    }
    if !sense_seen {
        // A `rising_edge` arc without an explicit sense drives both edges.
        arc.sense = Sense::NonUnate;
    }
    arc
}

fn parse_pin(
    sc: &mut Scanner,
    name: String,
    templates: &HashMap<String, (Vec<f64>, Vec<f64>)>,
) -> LibPin {
    let mut pin = LibPin {
        name,
        direction: Direction::Internal,
        capacitance: 0.0,
        rise_capacitance: 0.0,
        fall_capacitance: 0.0,
        is_clock: false,
        arcs: Vec::new(),
    };
    loop {
        match next_stmt(sc) {
            Stmt::Simple(k, v) => match k.as_str() {
                "direction" => {
                    pin.direction = match v.as_str() {
                        "input" => Direction::Input,
                        "output" => Direction::Output,
                        "inout" => Direction::Inout,
                        _ => Direction::Internal,
                    }
                }
                "capacitance" => pin.capacitance = v.parse().unwrap_or(0.0),
                "rise_capacitance" => pin.rise_capacitance = v.parse().unwrap_or(0.0),
                "fall_capacitance" => pin.fall_capacitance = v.parse().unwrap_or(0.0),
                "clock" => pin.is_clock = v == "true",
                _ => {}
            },
            Stmt::GroupOpen(k, _) => {
                if k == "timing" {
                    pin.arcs.push(parse_timing(sc, templates));
                } else {
                    sc.skip_group();
                }
            }
            Stmt::Complex(_, _) => {}
            Stmt::Close | Stmt::Eof => break,
        }
    }
    pin
}

fn parse_cell(
    sc: &mut Scanner,
    name: String,
    templates: &HashMap<String, (Vec<f64>, Vec<f64>)>,
) -> LibCell {
    let mut cell = LibCell {
        name,
        area: 0.0,
        pins: Vec::new(),
        index: HashMap::new(),
    };
    loop {
        match next_stmt(sc) {
            Stmt::Simple(k, v) => {
                if k == "area" {
                    cell.area = v.parse().unwrap_or(0.0);
                }
            }
            Stmt::GroupOpen(k, args) => {
                if k == "pin" {
                    let pname = args.first().cloned().unwrap_or_default();
                    let pin = parse_pin(sc, pname, templates);
                    cell.index.insert(pin.name.clone(), cell.pins.len());
                    cell.pins.push(pin);
                } else if k == "bus" {
                    // Not produced by sky130's standard-cell libraries; skip
                    // rather than half-support a bussed pin.
                    sc.skip_group();
                } else {
                    sc.skip_group();
                }
            }
            Stmt::Complex(_, _) => {}
            Stmt::Close | Stmt::Eof => break,
        }
    }
    cell
}

fn unit_scale(spec: &str, base: &str) -> f64 {
    // e.g. "1ns" with base "s": returns nanoseconds-per-unit relative to ns.
    let s = spec.trim();
    let num: String = s
        .chars()
        .take_while(|c| c.is_ascii_digit() || *c == '.')
        .collect();
    let mult: f64 = num.parse().unwrap_or(1.0);
    let suffix = s[num.len()..].trim().to_ascii_lowercase();
    let scale = match (suffix.as_str(), base) {
        ("ns", "s") => 1.0,
        ("ps", "s") => 1e-3,
        ("us", "s") => 1e3,
        ("ms", "s") => 1e6,
        ("1ps", "s") => 1e-3,
        _ => 1.0,
    };
    mult * scale
}

/// Parse a liberty file's text into the [`Library`] subset STA needs.
pub fn parse_library(text: &str) -> Library {
    let mut lib = Library {
        name: String::new(),
        time_unit_ns: 1.0,
        cap_unit_pf: 1.0,
        cells: HashMap::new(),
    };
    let mut templates: HashMap<String, (Vec<f64>, Vec<f64>)> = HashMap::new();
    let mut sc = Scanner::new(text);
    // Find the top-level `library (...) {`.
    loop {
        match next_stmt(&mut sc) {
            Stmt::GroupOpen(k, args) => {
                if k == "library" {
                    lib.name = args.first().cloned().unwrap_or_default();
                    break;
                }
                sc.skip_group();
            }
            Stmt::Eof => return lib,
            _ => {}
        }
    }
    loop {
        match next_stmt(&mut sc) {
            Stmt::Simple(k, v) => {
                if k == "time_unit" {
                    lib.time_unit_ns = unit_scale(&v, "s");
                }
            }
            Stmt::Complex(k, args) => {
                if k == "capacitive_load_unit" {
                    let mult: f64 = args.first().and_then(|a| a.parse().ok()).unwrap_or(1.0);
                    let unit = args
                        .get(1)
                        .map(|s| s.to_ascii_lowercase())
                        .unwrap_or_default();
                    lib.cap_unit_pf = mult
                        * match unit.as_str() {
                            "pf" => 1.0,
                            "ff" => 1e-3,
                            "nf" => 1e3,
                            _ => 1.0,
                        };
                }
            }
            Stmt::GroupOpen(k, args) => match k.as_str() {
                "cell" => {
                    let name = args.first().cloned().unwrap_or_default();
                    let cell = parse_cell(&mut sc, name.clone(), &templates);
                    lib.cells.insert(name, cell);
                }
                "lu_table_template" => {
                    let name = args.first().cloned().unwrap_or_default();
                    let lut = parse_lut(&mut sc, &HashMap::new(), "");
                    templates.insert(name, (lut.index1, lut.index2));
                }
                _ => sc.skip_group(),
            },
            Stmt::Close | Stmt::Eof => break,
        }
    }
    lib
}

#[cfg(test)]
mod tests {
    use super::*;

    const TINY: &str = r#"
    library ("tiny") {
        time_unit : "1ns";
        capacitive_load_unit(1.0, "pf");
        lu_table_template ("t2") {
            variable_1 : "input_net_transition";
            variable_2 : "total_output_net_capacitance";
            index_1("1, 2");
            index_2("1, 2");
        }
        cell ("INV") {
            area : 3.75;
            pin ("A") {
                direction : "input";
                capacitance : 0.002;
                rise_capacitance : 0.0021;
                fall_capacitance : 0.0019;
            }
            pin ("Y") {
                direction : "output";
                function : "!A";
                timing () {
                    related_pin : "A";
                    timing_sense : "negative_unate";
                    timing_type : "combinational";
                    cell_rise ("t2") {
                        index_1("0.01, 0.1");
                        index_2("0.001, 0.01");
                        values("0.10, 0.20", \
                               "0.12, 0.24");
                    }
                    cell_fall ("t2") {
                        index_1("0.01, 0.1");
                        index_2("0.001, 0.01");
                        values("0.05, 0.15", \
                               "0.07, 0.19");
                    }
                    rise_transition ("t2") {
                        index_1("0.01, 0.1");
                        index_2("0.001, 0.01");
                        values("0.02, 0.30", \
                               "0.03, 0.33");
                    }
                    fall_transition ("t2") {
                        index_1("0.01, 0.1");
                        index_2("0.001, 0.01");
                        values("0.01, 0.20", \
                               "0.02, 0.23");
                    }
                }
            }
        }
        cell ("DFF") {
            area : 20.0;
            pin ("CLK") {
                direction : "input";
                clock : "true";
                capacitance : 0.0018;
            }
            pin ("D") {
                direction : "input";
                capacitance : 0.002;
                timing () {
                    related_pin : "CLK";
                    timing_type : "setup_rising";
                    rise_constraint ("t2") {
                        index_1("0.01, 0.1");
                        index_2("0.01, 0.1");
                        values("0.10, 0.11", "0.12, 0.13");
                    }
                    fall_constraint ("t2") {
                        index_1("0.01, 0.1");
                        index_2("0.01, 0.1");
                        values("0.20, 0.21", "0.22, 0.23");
                    }
                }
            }
            pin ("Q") {
                direction : "output";
                timing () {
                    related_pin : "CLK";
                    timing_sense : "non_unate";
                    timing_type : "rising_edge";
                    cell_rise ("t2") {
                        index_1("0.01, 0.1");
                        index_2("0.001, 0.01");
                        values("0.30, 0.40", "0.31, 0.42");
                    }
                    cell_fall ("t2") {
                        index_1("0.01, 0.1");
                        index_2("0.001, 0.01");
                        values("0.32, 0.44", "0.33, 0.46");
                    }
                    rise_transition ("t2") {
                        index_1("0.01, 0.1");
                        index_2("0.001, 0.01");
                        values("0.04, 0.20", "0.05, 0.22");
                    }
                    fall_transition ("t2") {
                        index_1("0.01, 0.1");
                        index_2("0.001, 0.01");
                        values("0.03, 0.18", "0.04, 0.19");
                    }
                }
            }
        }
    }
    "#;

    pub fn tiny_library() -> Library {
        parse_library(TINY)
    }

    #[test]
    fn parses_cells_pins_and_arcs() {
        let lib = tiny_library();
        assert_eq!(lib.name, "tiny");
        assert_eq!(lib.time_unit_ns, 1.0);
        assert_eq!(lib.cap_unit_pf, 1.0);
        assert_eq!(lib.cells.len(), 2);

        let inv = lib.cells.get("INV").expect("INV");
        assert_eq!(inv.area, 3.75);
        assert!(!inv.is_sequential());
        let a = inv.pin("A").expect("A");
        assert_eq!(a.direction, Direction::Input);
        assert_eq!(a.load_cap(true), 0.0021);
        assert_eq!(a.load_cap(false), 0.0019);
        let y = inv.pin("Y").expect("Y");
        assert_eq!(y.direction, Direction::Output);
        assert_eq!(y.arcs.len(), 1);
        assert_eq!(y.arcs[0].related_pin, "A");
        assert_eq!(y.arcs[0].sense, Sense::Negative);
        assert_eq!(y.arcs[0].kind, ArcKind::Combinational);

        let dff = lib.cells.get("DFF").expect("DFF");
        assert!(dff.is_sequential());
        assert!(dff.pin("CLK").expect("CLK").is_clock);
        assert_eq!(
            dff.pin("D").expect("D").arcs[0].kind,
            ArcKind::Setup { rising: true }
        );
        assert_eq!(
            dff.pin("Q").expect("Q").arcs[0].kind,
            ArcKind::Edge { rising: true }
        );
    }

    #[test]
    fn falls_back_to_pin_capacitance_when_edge_caps_absent() {
        let lib = tiny_library();
        let d = lib.cells["DFF"].pin("D").expect("D");
        assert_eq!(d.rise_capacitance, 0.0);
        assert_eq!(d.load_cap(true), 0.002);
        assert_eq!(d.load_cap(false), 0.002);
    }

    #[test]
    fn bilinear_interpolation_at_grid_points_and_midpoints() {
        let lut = Lut {
            index1: vec![0.0, 1.0],
            index2: vec![0.0, 1.0],
            values: vec![vec![0.0, 1.0], vec![2.0, 4.0]],
        };
        assert_eq!(lut.eval(0.0, 0.0), 0.0);
        assert_eq!(lut.eval(1.0, 1.0), 4.0);
        assert_eq!(lut.eval(0.0, 1.0), 1.0);
        assert_eq!(lut.eval(1.0, 0.0), 2.0);
        assert!((lut.eval(0.5, 0.5) - 1.75).abs() < 1e-12);
    }

    #[test]
    fn extrapolates_linearly_below_and_above_the_table() {
        // This is the behaviour an ideal clock (slew 0.0, below every sky130
        // table's 0.01 first index) depends on.
        let lut = Lut {
            index1: vec![1.0, 2.0],
            index2: vec![0.0, 1.0],
            values: vec![vec![10.0, 10.0], vec![20.0, 20.0]],
        };
        assert!(
            (lut.eval(0.0, 0.0) - 0.0).abs() < 1e-12,
            "extrapolates down"
        );
        assert!((lut.eval(3.0, 0.0) - 30.0).abs() < 1e-12, "extrapolates up");
    }

    #[test]
    fn one_dimensional_table_uses_index_1_only() {
        let lut = Lut {
            index1: vec![0.0, 2.0],
            index2: vec![],
            values: vec![vec![1.0], vec![3.0]],
        };
        assert!((lut.eval(1.0, 99.0) - 2.0).abs() < 1e-12);
    }

    #[test]
    fn unateness_gates_edge_propagation() {
        assert!(Sense::Positive.allows(true, true));
        assert!(!Sense::Positive.allows(true, false));
        assert!(Sense::Negative.allows(true, false));
        assert!(!Sense::Negative.allows(false, false));
        assert!(Sense::NonUnate.allows(true, false));
        assert!(Sense::NonUnate.allows(false, false));
    }
}
