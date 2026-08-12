//! A structural-Verilog reader for exactly what `write_verilog -noattr`
//! emits after a liberty technology mapping -- the netlist `klt synthesize`
//! already writes to `.klt/synthesize/<top>_synth.v`.
//!
//! This is not a Verilog parser. It handles module headers, `input`/`output`/
//! `wire` declarations (scalar and packed-vector), named-port cell
//! instantiations, and `assign` aliases -- the complete grammar of a mapped
//! gate netlist. Anything else (`always`, `generate`, expressions on the
//! right of an `assign` beyond a single signal or constant) is a hard error,
//! not a silent skip, so an unsupported input can never be mistaken for a
//! correctly-read one.

use std::collections::HashMap;
use std::fmt;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PortDir {
    Input,
    Output,
    Inout,
}

#[derive(Debug, Clone)]
pub struct Instance {
    pub name: String,
    pub cell: String,
    /// `(formal pin, actual net)` in declaration order.
    pub conns: Vec<(String, String)>,
}

#[derive(Debug, Clone, Default)]
pub struct Netlist {
    pub top: String,
    /// Bit-expanded port names, e.g. `a_in[0]`, in declaration order.
    pub ports: Vec<(String, PortDir)>,
    pub instances: Vec<Instance>,
    /// `assign lhs = rhs` aliases, resolved into a canonical name per net.
    pub aliases: HashMap<String, String>,
}

impl Netlist {
    /// Resolve an alias chain to the canonical net name.
    pub fn canonical<'a>(&'a self, net: &'a str) -> &'a str {
        let mut cur = net;
        for _ in 0..64 {
            match self.aliases.get(cur) {
                Some(next) => cur = next.as_str(),
                None => return cur,
            }
        }
        cur
    }
}

#[derive(Debug)]
pub struct ParseError(pub String);

impl fmt::Display for ParseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "netlist parse error: {}", self.0)
    }
}

impl std::error::Error for ParseError {}

/// Strip `//` and `/* */` comments without disturbing string literals.
fn strip_comments(src: &str) -> String {
    let b = src.as_bytes();
    let mut out = String::with_capacity(src.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'/' && i + 1 < b.len() && b[i + 1] == b'/' {
            while i < b.len() && b[i] != b'\n' {
                i += 1;
            }
        } else if b[i] == b'/' && i + 1 < b.len() && b[i + 1] == b'*' {
            i += 2;
            while i + 1 < b.len() && !(b[i] == b'*' && b[i + 1] == b'/') {
                i += 1;
            }
            i = (i + 2).min(b.len());
            out.push(' ');
        } else if b[i] == b'"' {
            out.push('"');
            i += 1;
            while i < b.len() && b[i] != b'"' {
                out.push(b[i] as char);
                i += 1;
            }
            out.push('"');
            i += 1;
        } else {
            out.push(b[i] as char);
            i += 1;
        }
    }
    out
}

struct Lexer {
    toks: Vec<String>,
    i: usize,
}

/// Tokenise into identifiers (including Verilog `\escaped` names), numeric
/// literals, and single-character punctuation.
fn tokenize(src: &str) -> Vec<String> {
    let b = src.as_bytes();
    let mut out = Vec::new();
    let mut i = 0;
    while i < b.len() {
        let c = b[i];
        if (c as char).is_ascii_whitespace() {
            i += 1;
        } else if c == b'\\' {
            // Escaped identifier: runs to the next whitespace, which is a
            // delimiter and not part of the name.
            let start = i;
            i += 1;
            while i < b.len() && !(b[i] as char).is_ascii_whitespace() {
                i += 1;
            }
            out.push(String::from_utf8_lossy(&b[start..i]).into_owned());
        } else if c.is_ascii_alphanumeric() || c == b'_' || c == b'$' {
            let start = i;
            while i < b.len()
                && (b[i].is_ascii_alphanumeric()
                    || b[i] == b'_'
                    || b[i] == b'$'
                    || b[i] == b'\''
                    || (b[i] == b'.' && b[i - 1].is_ascii_digit()))
            {
                i += 1;
            }
            out.push(String::from_utf8_lossy(&b[start..i]).into_owned());
        } else {
            out.push((c as char).to_string());
            i += 1;
        }
    }
    out
}

impl Lexer {
    fn peek(&self) -> &str {
        self.toks.get(self.i).map(|s| s.as_str()).unwrap_or("")
    }
    fn next(&mut self) -> String {
        let t = self.toks.get(self.i).cloned().unwrap_or_default();
        self.i += 1;
        t
    }
    fn expect(&mut self, want: &str) -> Result<(), ParseError> {
        let got = self.next();
        if got == want {
            Ok(())
        } else {
            Err(ParseError(format!("expected `{want}`, got `{got}`")))
        }
    }
    fn eof(&self) -> bool {
        self.i >= self.toks.len()
    }
}

fn parse_int(tok: &str) -> Result<i64, ParseError> {
    tok.parse::<i64>()
        .map_err(|_| ParseError(format!("expected an integer, got `{tok}`")))
}

/// Expand a declaration into bit-level net names: `[3:0] x` -> `x[3] … x[0]`.
fn expand(name: &str, range: Option<(i64, i64)>) -> Vec<String> {
    match range {
        None => vec![name.to_string()],
        Some((hi, lo)) => {
            let (a, b) = if hi >= lo { (lo, hi) } else { (hi, lo) };
            (a..=b).map(|i| format!("{name}[{i}]")).collect()
        }
    }
}

/// Parse the mapped netlist, selecting the module named `top`.
///
/// A mapped netlist has exactly one module in practice; `top` is still
/// required so a multi-module file selects deterministically instead of
/// picking the first one and hoping.
pub fn parse_netlist(src: &str, top: &str) -> Result<Netlist, ParseError> {
    let clean = strip_comments(src);
    let mut lx = Lexer {
        toks: tokenize(&clean),
        i: 0,
    };
    let mut nl = Netlist {
        top: top.to_string(),
        ..Default::default()
    };
    let mut in_target = false;
    let mut seen_modules: Vec<String> = Vec::new();
    // Declared packed widths, so a bare vector name in an `assign` can be
    // bit-blasted: `assign raddr = rptr[3:0];` is one statement standing for
    // four single-bit aliases, and a mapped netlist's `assign`s are the only
    // place a multi-bit signal reference appears (cell pins are always
    // scalar).
    let mut decls: HashMap<String, (i64, i64)> = HashMap::new();

    while !lx.eof() {
        let t = lx.next();
        match t.as_str() {
            "module" => {
                let name = lx.next();
                seen_modules.push(name.clone());
                in_target = name == top;
                // Skip the port list header; the real directions come from
                // the `input`/`output` declarations that follow.
                while !lx.eof() && lx.peek() != ";" {
                    lx.next();
                }
                lx.expect(";")?;
            }
            "endmodule" => in_target = false,
            "input" | "output" | "inout" | "wire" | "reg" if in_target => {
                let dir = match t.as_str() {
                    "input" => Some(PortDir::Input),
                    "output" => Some(PortDir::Output),
                    "inout" => Some(PortDir::Inout),
                    _ => None,
                };
                // Optional packed range.
                let mut range = None;
                if lx.peek() == "[" {
                    lx.next();
                    let hi = parse_int(&lx.next())?;
                    lx.expect(":")?;
                    let lo = parse_int(&lx.next())?;
                    lx.expect("]")?;
                    range = Some((hi, lo));
                }
                loop {
                    let name = lx.next();
                    if let Some(r) = range {
                        decls.insert(name.clone(), r);
                    }
                    if let Some(d) = dir {
                        for bit in expand(&name, range) {
                            if !nl.ports.iter().any(|(n, _)| *n == bit) {
                                nl.ports.push((bit, d));
                            }
                        }
                    }
                    match lx.peek() {
                        "," => {
                            lx.next();
                        }
                        _ => break,
                    }
                }
                if lx.peek() == ";" {
                    lx.next();
                }
            }
            "assign" if in_target => {
                let lhs = parse_signal(&mut lx, &decls)?;
                lx.expect("=")?;
                let rhs = parse_signal(&mut lx, &decls)?;
                lx.expect(";")?;
                if lhs.len() != rhs.len() {
                    return Err(ParseError(format!(
                        "width mismatch in `assign`: {} bits = {} bits",
                        lhs.len(),
                        rhs.len()
                    )));
                }
                for (l, r) in lhs.into_iter().zip(rhs) {
                    nl.aliases.insert(l, r);
                }
            }
            _ if in_target && !t.is_empty() && lx.peek() != "" => {
                // Candidate instantiation: `<celltype> <instname> ( .P(n), ... );`
                if is_identifier(&t) && is_identifier(lx.peek()) {
                    let cell = t;
                    let name = lx.next();
                    if lx.peek() != "(" {
                        return Err(ParseError(format!(
                            "unsupported construct near `{cell} {name}`"
                        )));
                    }
                    lx.expect("(")?;
                    let mut conns = Vec::new();
                    while lx.peek() != ")" && !lx.eof() {
                        if lx.peek() == "," {
                            lx.next();
                            continue;
                        }
                        lx.expect(".")?;
                        let formal = lx.next();
                        lx.expect("(")?;
                        let actual = if lx.peek() == ")" {
                            Vec::new() // unconnected: `.PIN()`
                        } else {
                            parse_signal(&mut lx, &decls)?
                        };
                        lx.expect(")")?;
                        match actual.len() {
                            0 => {}
                            1 => conns.push((formal, actual.into_iter().next().unwrap())),
                            n => {
                                return Err(ParseError(format!(
                                    "cell `{name}` pin `{formal}` is connected to a \
                                     {n}-bit signal; mapped-cell pins are scalar"
                                )))
                            }
                        }
                    }
                    lx.expect(")")?;
                    lx.expect(";")?;
                    nl.instances.push(Instance { name, cell, conns });
                } else if t != ";" {
                    return Err(ParseError(format!("unsupported token `{t}`")));
                }
            }
            _ => {}
        }
    }

    if !seen_modules.iter().any(|m| m == top) {
        return Err(ParseError(format!(
            "module `{top}` not found (netlist defines: {})",
            seen_modules.join(", ")
        )));
    }
    Ok(nl)
}

fn is_identifier(t: &str) -> bool {
    let Some(c) = t.chars().next() else {
        return false;
    };
    c.is_ascii_alphabetic() || c == '_' || c == '\\'
}

/// Bit-blast a sized Verilog literal (`<size>'<radix><digits>`, e.g. `2'h0`,
/// `4'b1010`) into `size` constant bit names, MSB first.
///
/// `write_verilog`'s mapped netlists only emit fully-resolved 0/1 constants
/// (no `x`/`z`/`?`), so `digits` is parsed as a plain unsigned integer in the
/// literal's radix. Sizes beyond 128 bits treat the unrepresented high bits
/// as `0` -- a pragmatic bound, not seen in practice on this spike's corpus,
/// called out here rather than silently wrong.
fn literal_bits(tok: &str) -> Option<Vec<String>> {
    let (size_str, rest) = tok.split_once('\'')?;
    let size: usize = if size_str.is_empty() {
        32
    } else {
        size_str.parse().ok()?
    };
    let mut chars = rest.chars();
    let radix_c = chars.next()?;
    let digits: String = chars.collect();
    let radix = match radix_c.to_ascii_lowercase() {
        'h' => 16,
        'b' => 2,
        'o' => 8,
        'd' => 10,
        _ => return None,
    };
    let value = u128::from_str_radix(&digits, radix).ok()?;
    let mut bits = Vec::with_capacity(size);
    for i in (0..size).rev() {
        let bit = if i < 128 { (value >> i) & 1 } else { 0 };
        bits.push(if bit == 1 {
            "<const1>".to_string()
        } else {
            "<const0>".to_string()
        });
    }
    Some(bits)
}

/// Parse one signal reference into its constituent **bit** names, MSB first.
///
/// Handles `name` (scalar, or every bit of a declared vector), `name[i]`,
/// `name[hi:lo]`, sized constants like `1'h0`, and `{ ..., ... }`
/// concatenations (recursively, so a concatenation may itself contain a
/// concatenation). Replication (`{n{...}}`) and other expressions are
/// rejected rather than approximated.
fn parse_signal(
    lx: &mut Lexer,
    decls: &HashMap<String, (i64, i64)>,
) -> Result<Vec<String>, ParseError> {
    if lx.peek() == "{" {
        lx.next();
        let mut bits = Vec::new();
        loop {
            bits.extend(parse_signal(lx, decls)?);
            match lx.peek() {
                "," => {
                    lx.next();
                }
                "}" => {
                    lx.next();
                    break;
                }
                other => {
                    return Err(ParseError(format!(
                        "expected `,` or `}}` in concatenation, got `{other}`"
                    )))
                }
            }
        }
        return Ok(bits);
    }
    let base = lx.next();
    if base.contains('\'') {
        // Sized literal: `1'h0`, `2'b10`. Canonicalise to per-bit constant
        // net names; constants have no driver, so they never carry an
        // arrival.
        return literal_bits(&base)
            .ok_or_else(|| ParseError(format!("unsupported literal `{base}`")));
    }
    if !is_identifier(&base) {
        return Err(ParseError(format!("expected a signal, got `{base}`")));
    }
    if lx.peek() == "[" {
        lx.next();
        let first = lx.next();
        if lx.peek() == ":" {
            lx.next();
            let last = lx.next();
            lx.expect("]")?;
            let (hi, lo) = (parse_int(&first)?, parse_int(&last)?);
            let step: i64 = if hi >= lo { -1 } else { 1 };
            let mut bits = Vec::new();
            let mut i = hi;
            loop {
                bits.push(format!("{base}[{i}]"));
                if i == lo {
                    break;
                }
                i += step;
            }
            return Ok(bits);
        }
        lx.expect("]")?;
        return Ok(vec![format!("{base}[{first}]")]);
    }
    // A bare name: every bit of it, if it was declared as a vector.
    if let Some(&(hi, lo)) = decls.get(&base) {
        let mut bits = expand(&base, Some((hi, lo)));
        bits.sort_by_key(|b| {
            std::cmp::Reverse(
                b.rsplit('[')
                    .next()
                    .and_then(|s| s.trim_end_matches(']').parse::<i64>().ok())
                    .unwrap_or(0),
            )
        });
        return Ok(bits);
    }
    Ok(vec![base])
}

#[cfg(test)]
mod tests {
    use super::*;

    const SRC: &str = r#"
/* Generated by Yosys */
module top(clk, a_in, y);
  input clk;
  wire clk;
  input [1:0] a_in;
  wire [1:0] a_in;
  output y;
  wire y;
  wire _000_;
  sky130_fd_sc_hd__inv_1 _100_ (
    .A(a_in[0]),
    .Y(_000_)
  );
  sky130_fd_sc_hd__dfxtp_1 _101_ (
    .CLK(clk),
    .D(_000_),
    .Q(y)
  );
  assign \escaped$net  = _000_;
endmodule
"#;

    #[test]
    fn parses_ports_instances_and_aliases() {
        let nl = parse_netlist(SRC, "top").expect("parse");
        assert_eq!(nl.top, "top");
        let names: Vec<&str> = nl.ports.iter().map(|(n, _)| n.as_str()).collect();
        assert_eq!(names, vec!["clk", "a_in[0]", "a_in[1]", "y"]);
        assert_eq!(nl.ports[0].1, PortDir::Input);
        assert_eq!(nl.ports[3].1, PortDir::Output);
        assert_eq!(nl.instances.len(), 2);
        assert_eq!(nl.instances[0].cell, "sky130_fd_sc_hd__inv_1");
        assert_eq!(nl.instances[0].name, "_100_");
        assert_eq!(
            nl.instances[0].conns,
            vec![
                ("A".to_string(), "a_in[0]".to_string()),
                ("Y".to_string(), "_000_".to_string())
            ]
        );
        assert_eq!(
            nl.aliases.get("\\escaped$net").map(|s| s.as_str()),
            Some("_000_")
        );
        assert_eq!(nl.canonical("\\escaped$net"), "_000_");
    }

    #[test]
    fn rejects_a_missing_top_module() {
        let err = parse_netlist(SRC, "nope").unwrap_err();
        assert!(err.0.contains("not found"), "{}", err.0);
    }

    #[test]
    fn bit_blasts_a_vector_assign() {
        let src = r#"
            module t(y);
              output y; wire [3:0] a; wire [3:0] b;
              INV i0 (.A(a[0]), .Y(y));
              assign a = b[3:0];
            endmodule"#;
        let nl = parse_netlist(src, "t").expect("parse");
        assert_eq!(nl.aliases.len(), 4);
        assert_eq!(nl.canonical("a[2]"), "b[2]");
    }

    #[test]
    fn rejects_a_multi_bit_cell_pin_connection() {
        let src = r#"
            module t(y);
              output y; wire [3:0] a;
              INV i0 (.A(a), .Y(y));
            endmodule"#;
        let err = parse_netlist(src, "t").unwrap_err();
        assert!(err.0.contains("scalar"), "{}", err.0);
    }

    #[test]
    fn canonicalises_sized_constants() {
        let src = "module t(y); output y; sky130_fd_sc_hd__inv_1 i (.A(1'h0), .Y(y)); endmodule";
        let nl = parse_netlist(src, "t").expect("parse");
        assert_eq!(nl.instances[0].conns[0].1, "<const0>");
    }

    #[test]
    fn drops_explicitly_unconnected_pins() {
        let src = "module t(y); output y; sky130_fd_sc_hd__inv_1 i (.A(), .Y(y)); endmodule";
        let nl = parse_netlist(src, "t").expect("parse");
        assert_eq!(nl.instances[0].conns.len(), 1);
        assert_eq!(nl.instances[0].conns[0].0, "Y");
    }

    #[test]
    fn bit_blasts_a_multi_bit_sized_constant() {
        // `2'h0` = { <const0>, <const0> } -- this is the shape
        // `assign mm_m1 = { 2'h0, mod_r };` needs from real `write_verilog`
        // output (found on tests/corpus/sta/'s `modexp` fixture).
        let src = r#"
            module t(y);
              output y; wire [3:0] a;
              assign a = { 2'h0, 2'b10 };
              INV i0 (.A(a[0]), .Y(y));
            endmodule"#;
        let nl = parse_netlist(src, "t").expect("parse");
        assert_eq!(nl.canonical("a[3]"), "<const0>");
        assert_eq!(nl.canonical("a[2]"), "<const0>");
        assert_eq!(nl.canonical("a[1]"), "<const1>");
        assert_eq!(nl.canonical("a[0]"), "<const0>");
    }

    #[test]
    fn resolves_a_concatenation_assign_rhs() {
        // The exact construct `write_verilog` emits for a bit-widened alias:
        // `assign wide = { narrow, 1'h0 };`.
        let src = r#"
            module t(y);
              output y; wire [1:0] narrow; wire [2:0] wide;
              assign wide = { narrow, 1'h0 };
              INV i0 (.A(wide[0]), .Y(y));
            endmodule"#;
        let nl = parse_netlist(src, "t").expect("parse");
        assert_eq!(nl.canonical("wide[2]"), "narrow[1]");
        assert_eq!(nl.canonical("wide[1]"), "narrow[0]");
        assert_eq!(nl.canonical("wide[0]"), "<const0>");
    }

    #[test]
    fn rejects_replication_concatenation() {
        // `{n{...}}` replication is a distinct construct this parser does
        // not support; it must be a hard error, not a silently wrong parse.
        let src = r#"
            module t(y);
              output y; wire [1:0] a;
              assign a = { 2{1'h0} };
              INV i0 (.A(a[0]), .Y(y));
            endmodule"#;
        let err = parse_netlist(src, "t").unwrap_err();
        assert!(err.0.contains("expected a signal"), "{}", err.0);
    }
}
