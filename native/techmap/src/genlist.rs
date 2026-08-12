//! Parses `klt.synth.generic-netlist/1`
//! (`docs/schemas/synth-generic-netlist.schema.json`, issue #873) -- this
//! stage's own input. A thin `serde`-derived model, not a general JSON
//! Schema validator: malformed input fails with a `serde_json` error,
//! which `main.rs` maps to exit code `1` per
//! `docs/design/synth-techmap-stage-contract.md` section 6 ("Exit
//! codes").

use std::collections::HashMap;

use serde::Deserialize;

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum PortDirection {
    Input,
    Output,
    Inout,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Port {
    pub name: String,
    pub direction: PortDirection,
    pub bits: Vec<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct GenericCell {
    pub name: String,
    #[serde(rename = "type")]
    pub cell_type: String,
    pub connections: HashMap<String, String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct GenericNetlist {
    pub schema: String,
    pub top: String,
    pub ports: Vec<Port>,
    pub cells: Vec<GenericCell>,
}

#[derive(Debug)]
pub struct GenericNetlistError(pub String);

impl std::fmt::Display for GenericNetlistError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

/// The ten `v1` generic-cell types this stage understands
/// (`docs/design/synth-techmap-stage-contract.md` section 2.2).
pub const GENERIC_TYPES: &[&str] = &[
    "$not", "$buf", "$and2", "$nand2", "$or2", "$nor2", "$xor2", "$xnor2", "$mux2", "$dff",
];

pub fn parse(src: &str) -> Result<GenericNetlist, GenericNetlistError> {
    let netlist: GenericNetlist =
        serde_json::from_str(src).map_err(|e| GenericNetlistError(format!("{e}")))?;
    if netlist.schema != "klt.synth.generic-netlist/1" {
        return Err(GenericNetlistError(format!(
            "unsupported schema '{}', expected 'klt.synth.generic-netlist/1'",
            netlist.schema
        )));
    }
    for cell in &netlist.cells {
        if !GENERIC_TYPES.contains(&cell.cell_type.as_str()) {
            return Err(GenericNetlistError(format!(
                "cell '{}' has type '{}' outside the v1 generic vocabulary",
                cell.name, cell.cell_type
            )));
        }
    }
    Ok(netlist)
}

#[cfg(test)]
mod tests {
    use super::*;

    const ADDER4: &str = r#"
{
  "schema": "klt.synth.generic-netlist/1",
  "top": "adder4",
  "ports": [
    { "name": "a", "direction": "input", "bits": ["a[1]", "a[0]"] },
    { "name": "sum", "direction": "output", "bits": ["sum[1]", "sum[0]"] }
  ],
  "cells": [
    { "name": "_g0_", "type": "$xor2", "connections": { "A": "a[0]", "B": "a[1]", "Y": "sum[0]" } }
  ]
}
"#;

    #[test]
    fn parses_worked_example() {
        let n = parse(ADDER4).unwrap();
        assert_eq!(n.top, "adder4");
        assert_eq!(n.ports.len(), 2);
        assert_eq!(n.cells.len(), 1);
        assert_eq!(n.cells[0].cell_type, "$xor2");
    }

    #[test]
    fn rejects_unknown_cell_type() {
        let bad = ADDER4.replace("$xor2", "$notreal");
        let err = parse(&bad).unwrap_err();
        assert!(err.0.contains("outside the v1 generic vocabulary"));
    }

    #[test]
    fn rejects_wrong_schema() {
        let bad = ADDER4.replace("klt.synth.generic-netlist/1", "klt.synth.generic-netlist/2");
        let err = parse(&bad).unwrap_err();
        assert!(err.0.contains("unsupported schema"));
    }
}
