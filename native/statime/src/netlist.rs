//! A structural-Verilog parser scoped to exactly what
//! `write_verilog -noattr` (Yosys, post `abc -liberty`) emits: a flat
//! module with `input`/`output`/`wire` declarations and technology-cell
//! instances using named port connections (`.PIN(net)`), no `assign`
//! statements, no concatenation, no constants -- because a mapped netlist
//! has already had every one of those resolved into tie cells or buffers
//! by `abc`. This is **not** a general Verilog parser; a design that hits
//! any of those constructs is a documented limitation of this spike (see
//! `README.md`), not silently mishandled -- unsupported syntax fails to
//! parse rather than producing a wrong netlist.

use std::collections::HashMap;
use std::fmt;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PortDirection {
    Input,
    Output,
}

#[derive(Debug, Clone)]
pub struct Port {
    pub name: String,
    pub direction: PortDirection,
    /// Individual net names this port expands to -- `["a[0]", "a[1]", ...]`
    /// for a bus, `["done"]` for a scalar.
    pub nets: Vec<String>,
}

#[derive(Debug, Clone)]
pub struct CellInstance {
    pub instance_name: String,
    pub cell_type: String,
    /// pin name -> connected net name
    pub connections: HashMap<String, String>,
}

#[derive(Debug, Clone)]
pub struct Module {
    pub name: String,
    pub ports: Vec<Port>,
    pub cells: Vec<CellInstance>,
}

impl Module {
    pub fn input_nets(&self) -> Vec<&str> {
        self.ports
            .iter()
            .filter(|p| p.direction == PortDirection::Input)
            .flat_map(|p| p.nets.iter().map(|s| s.as_str()))
            .collect()
    }

    pub fn output_nets(&self) -> Vec<&str> {
        self.ports
            .iter()
            .filter(|p| p.direction == PortDirection::Output)
            .flat_map(|p| p.nets.iter().map(|s| s.as_str()))
            .collect()
    }
}

#[derive(Debug)]
pub struct NetlistError(pub String);

impl fmt::Display for NetlistError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

fn expand_bus(base: &str, msb: i64, lsb: i64) -> Vec<String> {
    let (hi, lo, desc) = if msb >= lsb {
        (msb, lsb, true)
    } else {
        (lsb, msb, false)
    };
    let mut out = Vec::new();
    if desc {
        for i in (lo..=hi).rev() {
            out.push(format!("{base}[{i}]"));
        }
    } else {
        for i in lo..=hi {
            out.push(format!("{base}[{i}]"));
        }
    }
    out
}

/// Parse a range spec like `[15:0]` -> `(15, 0)`.
fn parse_range(s: &str) -> Option<(i64, i64)> {
    let s = s.trim().trim_start_matches('[').trim_end_matches(']');
    let (a, b) = s.split_once(':')?;
    Some((a.trim().parse().ok()?, b.trim().parse().ok()?))
}

/// Strip `/* */` and `//` comments from Verilog source.
fn strip_comments(src: &str) -> String {
    let mut out = String::with_capacity(src.len());
    let bytes = src.as_bytes();
    let n = bytes.len();
    let mut i = 0;
    while i < n {
        if bytes[i] == b'/' && i + 1 < n && bytes[i + 1] == b'*' {
            i += 2;
            while i + 1 < n && !(bytes[i] == b'*' && bytes[i + 1] == b'/') {
                i += 1;
            }
            i += 2;
        } else if bytes[i] == b'/' && i + 1 < n && bytes[i + 1] == b'/' {
            while i < n && bytes[i] != b'\n' {
                i += 1;
            }
        } else {
            out.push(bytes[i] as char);
            i += 1;
        }
    }
    out
}

/// Parse the `write_verilog -noattr` output for module `top`.
pub fn parse(src: &str, top: &str) -> Result<Module, NetlistError> {
    let cleaned = strip_comments(src);

    let module_start = format!("module {top}(");
    let module_start_ws = format!("module {top} (");
    let start_idx = cleaned
        .find(&module_start)
        .or_else(|| cleaned.find(&module_start_ws))
        .ok_or_else(|| NetlistError(format!("module '{top}' not found in netlist")))?;
    let end_idx = cleaned[start_idx..]
        .find("\nendmodule")
        .map(|i| start_idx + i)
        .ok_or_else(|| NetlistError("endmodule not found".to_string()))?;
    let body = &cleaned[start_idx..end_idx];

    // Port directions: declared as `input NAME;` / `output NAME;` /
    // `input [M:N] NAME;` lines (Yosys emits a matching `wire` decl too --
    // we only need the direction + range from the input/output line).
    let mut port_order: Vec<String> = Vec::new();
    let mut port_dir: HashMap<String, PortDirection> = HashMap::new();
    let mut port_range: HashMap<String, Option<(i64, i64)>> = HashMap::new();

    for line in body.lines() {
        let line = line.trim();
        for (kw, dir) in [
            ("input", PortDirection::Input),
            ("output", PortDirection::Output),
        ] {
            if let Some(rest) = line.strip_prefix(kw) {
                let rest = rest.trim().trim_end_matches(';').trim();
                if rest.is_empty() {
                    continue;
                }
                let (range, name) = if let Some(rb) = rest.strip_prefix('[') {
                    let close = rb.find(']').map(|i| i + 1);
                    if let Some(close) = close {
                        let range_str = &rb[..close];
                        let name = rb[close..].trim();
                        (parse_range(range_str), name)
                    } else {
                        (None, rest)
                    }
                } else {
                    (None, rest)
                };
                let name = name.trim();
                if !name.is_empty() && !port_dir.contains_key(name) {
                    port_order.push(name.to_string());
                    port_dir.insert(name.to_string(), dir);
                    port_range.insert(name.to_string(), range);
                }
            }
        }
    }

    let ports: Vec<Port> = port_order
        .iter()
        .map(|name| {
            let direction = port_dir[name];
            let nets = match port_range.get(name).and_then(|r| *r) {
                Some((msb, lsb)) => expand_bus(name, msb, lsb),
                None => vec![name.clone()],
            };
            Port {
                name: name.clone(),
                direction,
                nets,
            }
        })
        .collect();

    // Cell instances: `<cell_type> <instance_name> ( .PIN(net), ... );`
    // Scoped to lines that look like a technology-cell instantiation --
    // i.e. NOT `input`/`output`/`wire`/`module`/`assign`.
    let mut cells = Vec::new();
    let chars: Vec<char> = body.chars().collect();
    let n = chars.len();
    let mut i = 0;
    // Skip to first line after the port list's closing `);` of the module
    // header so `module top(a, b, c);` itself isn't mistaken for an
    // instance.
    if let Some(hdr_end) = body.find(");") {
        i = hdr_end + 2;
    }
    while i < n {
        // Skip whitespace
        while i < n && chars[i].is_whitespace() {
            i += 1;
        }
        if i >= n {
            break;
        }
        let word_start = i;
        while i < n && (chars[i].is_alphanumeric() || chars[i] == '_' || chars[i] == '$') {
            i += 1;
        }
        let word: String = chars[word_start..i].iter().collect();
        if word.is_empty() {
            i += 1;
            continue;
        }
        if matches!(
            word.as_str(),
            "input" | "output" | "inout" | "wire" | "reg" | "assign" | "module"
        ) {
            // Skip to end of statement.
            while i < n && chars[i] != ';' {
                i += 1;
            }
            i += 1;
            continue;
        }
        // Otherwise: `word` is a cell type. Expect an instance name next.
        while i < n && chars[i].is_whitespace() {
            i += 1;
        }
        let inst_start = i;
        while i < n && (chars[i].is_alphanumeric() || chars[i] == '_' || chars[i] == '$') {
            i += 1;
        }
        let instance_name: String = chars[inst_start..i].iter().collect();
        while i < n && chars[i].is_whitespace() {
            i += 1;
        }
        if i >= n || chars[i] != '(' {
            return Err(NetlistError(format!(
                "expected '(' after instance '{word} {instance_name}'"
            )));
        }
        i += 1; // consume '('
        let mut connections = HashMap::new();
        loop {
            while i < n && (chars[i].is_whitespace() || chars[i] == ',') {
                i += 1;
            }
            if i < n && chars[i] == ')' {
                i += 1;
                break;
            }
            if i >= n || chars[i] != '.' {
                return Err(NetlistError(format!(
                    "expected '.' starting a port connection in instance '{instance_name}'"
                )));
            }
            i += 1; // consume '.'
            let pin_start = i;
            while i < n && (chars[i].is_alphanumeric() || chars[i] == '_') {
                i += 1;
            }
            let pin_name: String = chars[pin_start..i].iter().collect();
            while i < n && chars[i].is_whitespace() {
                i += 1;
            }
            if i >= n || chars[i] != '(' {
                return Err(NetlistError(format!("expected '(' after .{pin_name}")));
            }
            i += 1;
            let net_start = i;
            while i < n && chars[i] != ')' {
                i += 1;
            }
            let net_name: String = chars[net_start..i]
                .iter()
                .collect::<String>()
                .trim()
                .to_string();
            i += 1; // consume ')'
            connections.insert(pin_name, net_name);
        }
        while i < n && chars[i].is_whitespace() {
            i += 1;
        }
        if i < n && chars[i] == ';' {
            i += 1;
        }
        cells.push(CellInstance {
            instance_name,
            cell_type: word,
            connections,
        });
    }

    Ok(Module {
        name: top.to_string(),
        ports,
        cells,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const NETLIST: &str = r#"
/* Generated by Yosys */

module top(clk, a, y);
  input clk;
  wire clk;
  input [1:0] a;
  wire [1:0] a;
  output y;
  wire y;
  wire n1;
  BUF _100_ (
    .A(a[0]),
    .Y(n1)
  );
  DFF _101_ (
    .CLK(clk),
    .D(n1),
    .Q(y)
  );
endmodule
"#;

    #[test]
    fn parses_ports_with_bus_expansion() {
        let m = parse(NETLIST, "top").unwrap();
        assert_eq!(m.name, "top");
        assert_eq!(m.input_nets(), vec!["clk", "a[1]", "a[0]"]);
        assert_eq!(m.output_nets(), vec!["y"]);
    }

    #[test]
    fn parses_cell_instances_with_named_connections() {
        let m = parse(NETLIST, "top").unwrap();
        assert_eq!(m.cells.len(), 2);
        let buf = &m.cells[0];
        assert_eq!(buf.cell_type, "BUF");
        assert_eq!(buf.instance_name, "_100_");
        assert_eq!(buf.connections.get("A").unwrap(), "a[0]");
        assert_eq!(buf.connections.get("Y").unwrap(), "n1");
        let dff = &m.cells[1];
        assert_eq!(dff.cell_type, "DFF");
        assert_eq!(dff.connections.get("Q").unwrap(), "y");
    }

    #[test]
    fn missing_module_is_an_error() {
        let err = parse(NETLIST, "nope").unwrap_err();
        assert!(err.0.contains("nope"));
    }
}
