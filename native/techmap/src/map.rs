//! The core technology-mapping algorithm: walks a
//! [`genlist::GenericNetlist`], picks a Liberty cell (via
//! [`celllib::best_comb`]/[`celllib::best_dff`]) for each generic-netlist
//! node, and emits a flat list of mapped standard-cell instances.
//!
//! **`$dff` realisation, including "cell chains":** the generic `$dff`'s
//! optional `RST`/`EN` pins (`docs/design/synth-techmap-stage-contract.md`
//! section 2.2) don't always have a single matching liberty cell --
//! `sky130_fd_sc_hd` has no single cell combining async-clear *and*
//! clock-enable, for example (`native/techmap/README.md` documents the
//! measured cell inventory). Rather than fail those designs, an `EN` input
//! is always realised the same structural way, whether or not `RST` is
//! also present: a `$mux2(select=EN, A=<Q feedback>, B=D)` feeding a plain
//! (or reset) D flip-flop's `D` pin -- the classic "hold via feedback mux"
//! construction, and exactly the "cell (or cell chain)" language issue
//! #874 itself uses. An active-low reset/enable pin on the chosen liberty
//! cell is bridged with an inserted `$not`-realising inverter, so this
//! stage's own generic-vocabulary polarity convention (active-high) never
//! leaks a library-specific inversion into the caller.

use std::collections::{BTreeMap, HashMap, HashSet};

use crate::celllib::CellLibrary;
use crate::genlist::{GenericCell, GenericNetlist};

#[derive(Debug, Clone)]
pub struct MappedInstance {
    pub instance_name: String,
    pub cell_type: String,
    /// liberty pin name -> net name
    pub connections: HashMap<String, String>,
}

#[derive(Debug)]
pub struct MapResult {
    pub instances: Vec<MappedInstance>,
    pub instance_count: usize,
    pub area_um2: f64,
    pub instance_counts_by_type: BTreeMap<String, usize>,
}

#[derive(Debug)]
pub enum MapError {
    /// A generic cell has no realisable liberty equivalent at all -- exit
    /// code `1` per the contract's "Exit codes" table (not a QoR question,
    /// a hard failure).
    Unrealizable {
        cell: String,
        detail: String,
    },
    MissingConnection {
        cell: String,
        pin: String,
    },
}

impl std::fmt::Display for MapError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            MapError::Unrealizable { cell, detail } => {
                write!(f, "cell '{cell}': {detail}")
            }
            MapError::MissingConnection { cell, pin } => {
                write!(f, "cell '{cell}': missing required connection '{pin}'")
            }
        }
    }
}

/// The reserved pseudo-net names the interim on-ramp converter
/// (`tests/corpus/techmap/`) emits for a tie-hi/tie-lo constant net --
/// `celllib::TieCandidate`'s own docs explain why this is the answer
/// contract section 8 asked #874 to propose.
const CONST0: &str = "$const0";
const CONST1: &str = "$const1";

struct Mapper<'a> {
    lib: &'a CellLibrary,
    delay_weight: f64,
    instances: Vec<MappedInstance>,
    area_um2: f64,
    counts: BTreeMap<String, usize>,
    fresh_counter: usize,
    /// Memoises the single shared tie-hi/tie-lo instance's output net, so
    /// every `$const0`/`$const1` reference in the design shares one
    /// instance rather than instantiating a fresh tie cell per fanout.
    const_nets: HashMap<&'static str, String>,
}

impl<'a> Mapper<'a> {
    fn fresh_net(&mut self, hint: &str) -> String {
        self.fresh_counter += 1;
        format!("__techmap_{hint}_{}", self.fresh_counter)
    }

    fn record(
        &mut self,
        instance_name: String,
        cell_type: String,
        connections: HashMap<String, String>,
    ) {
        let area = self
            .lib
            .comb
            .values()
            .flatten()
            .find(|c| c.cell_name == cell_type)
            .map(|c| c.area_um2)
            .or_else(|| {
                self.lib
                    .dff_plain
                    .iter()
                    .chain(self.lib.dff_reset.iter())
                    .find(|c| c.cell_name == cell_type)
                    .map(|c| c.area_um2)
            })
            .or_else(|| {
                self.lib
                    .tie_hi
                    .iter()
                    .chain(self.lib.tie_lo.iter())
                    .find(|c| c.cell_name == cell_type)
                    .map(|c| c.area_um2)
            })
            .unwrap_or(0.0);
        self.area_um2 += area;
        *self.counts.entry(cell_type.clone()).or_insert(0) += 1;
        self.instances.push(MappedInstance {
            instance_name,
            cell_type,
            connections,
        });
    }

    /// Resolve a net name pulled from a generic cell's `connections`:
    /// `$const0`/`$const1` lazily instantiate (once) the cheapest tie-lo/
    /// tie-hi candidate and return its output net; any other net name
    /// passes through unchanged.
    fn resolve_net(&mut self, requesting_cell: &str, net: &str) -> Result<String, MapError> {
        let (key, candidates, label) = match net {
            CONST0 => (CONST0, &self.lib.tie_lo, "$const0"),
            CONST1 => (CONST1, &self.lib.tie_hi, "$const1"),
            _ => return Ok(net.to_string()),
        };
        if let Some(existing) = self.const_nets.get(key) {
            return Ok(existing.clone());
        }
        let best = candidates
            .first()
            .cloned()
            .ok_or_else(|| MapError::Unrealizable {
                cell: requesting_cell.to_string(),
                detail: format!("no liberty tie cell realises '{label}'"),
            })?;
        let inst_name = format!("__techmap_tie_{}", key.trim_start_matches('$'));
        let mut connections = HashMap::new();
        connections.insert(best.pin_name.clone(), inst_name.clone());
        self.record(inst_name.clone(), best.cell_name.clone(), connections);
        self.const_nets.insert(key, inst_name.clone());
        Ok(inst_name)
    }

    fn map_comb(&mut self, gtype: &'static str, cell: &GenericCell) -> Result<(), MapError> {
        let candidates = self
            .lib
            .comb
            .get(gtype)
            .map(|v| v.as_slice())
            .unwrap_or(&[]);
        let best = crate::celllib::best_comb(candidates, self.delay_weight)
            .ok_or_else(|| MapError::Unrealizable {
                cell: cell.name.clone(),
                detail: format!("no liberty cell realises '{gtype}'"),
            })?
            .clone();
        let mut connections = HashMap::new();
        for (generic_pin, liberty_pin) in &best.pin_map {
            let net = cell
                .connections
                .get(*generic_pin)
                .ok_or_else(|| MapError::MissingConnection {
                    cell: cell.name.clone(),
                    pin: generic_pin.to_string(),
                })?
                .clone();
            let net = self.resolve_net(&cell.name, &net)?;
            connections.insert(liberty_pin.clone(), net);
        }
        self.record(cell.name.clone(), best.cell_name.clone(), connections);
        Ok(())
    }

    /// Instantiate a `$mux2`-realising cell wired as `Y = sel ? b : a`,
    /// returning the fresh net it drives.
    fn insert_mux_chain(
        &mut self,
        base_name: &str,
        a: &str,
        b: &str,
        sel: &str,
    ) -> Result<String, MapError> {
        let candidates = self
            .lib
            .comb
            .get("$mux2")
            .map(|v| v.as_slice())
            .unwrap_or(&[]);
        let best = crate::celllib::best_comb(candidates, self.delay_weight)
            .ok_or_else(|| MapError::Unrealizable {
                cell: base_name.to_string(),
                detail: "no liberty cell realises '$mux2' (needed for a clock-enable $dff chain)"
                    .to_string(),
            })?
            .clone();
        let out_net = self.fresh_net(&format!("{base_name}_en_fb"));
        let mut connections = HashMap::new();
        connections.insert(best.pin_map["A"].clone(), a.to_string());
        connections.insert(best.pin_map["B"].clone(), b.to_string());
        connections.insert(best.pin_map["S"].clone(), sel.to_string());
        connections.insert(best.pin_map["Y"].clone(), out_net.clone());
        let inst_name = format!("{base_name}_enmux");
        self.record(inst_name, best.cell_name, connections);
        Ok(out_net)
    }

    /// Instantiate a `$not`-realising inverter, returning the fresh net it
    /// drives.
    fn insert_inverter(&mut self, base_name: &str, a: &str) -> Result<String, MapError> {
        let candidates = self
            .lib
            .comb
            .get("$not")
            .map(|v| v.as_slice())
            .unwrap_or(&[]);
        let best = crate::celllib::best_comb(candidates, self.delay_weight)
            .ok_or_else(|| MapError::Unrealizable {
                cell: base_name.to_string(),
                detail: "no liberty cell realises '$not' (needed to bridge an active-low reset polarity)".to_string(),
            })?
            .clone();
        let out_net = self.fresh_net(&format!("{base_name}_rstinv"));
        let mut connections = HashMap::new();
        connections.insert(best.pin_map["A"].clone(), a.to_string());
        connections.insert(best.pin_map["Y"].clone(), out_net.clone());
        let inst_name = format!("{base_name}_rstinv");
        self.record(inst_name, best.cell_name, connections);
        Ok(out_net)
    }

    fn map_dff(&mut self, cell: &GenericCell) -> Result<(), MapError> {
        let get = |pin: &str| -> Option<String> { cell.connections.get(pin).cloned() };
        let d_net = get("D").ok_or_else(|| MapError::MissingConnection {
            cell: cell.name.clone(),
            pin: "D".to_string(),
        })?;
        let clk_net = get("CLK").ok_or_else(|| MapError::MissingConnection {
            cell: cell.name.clone(),
            pin: "CLK".to_string(),
        })?;
        let q_net = get("Q").ok_or_else(|| MapError::MissingConnection {
            cell: cell.name.clone(),
            pin: "Q".to_string(),
        })?;
        let rst_net = get("RST");
        let en_net = get("EN");

        let d_net = self.resolve_net(&cell.name, &d_net)?;
        let rst_net = rst_net
            .map(|n| self.resolve_net(&cell.name, &n))
            .transpose()?;
        let en_net = en_net
            .map(|n| self.resolve_net(&cell.name, &n))
            .transpose()?;

        // Resolve the effective D input: if EN is present, insert a
        // feedback mux (Y = EN ? D : Q) ahead of the flop, whichever
        // family is ultimately selected below.
        let effective_d = match &en_net {
            None => d_net.clone(),
            Some(en) => self.insert_mux_chain(&cell.name, &q_net, &d_net, en)?,
        };

        match rst_net {
            None => {
                let candidates = self.lib.dff_plain.clone();
                let best = crate::celllib::best_dff(&candidates, self.delay_weight)
                    .ok_or_else(|| MapError::Unrealizable {
                        cell: cell.name.clone(),
                        detail: "no liberty cell realises a plain '$dff'".to_string(),
                    })?
                    .clone();
                let mut connections = HashMap::new();
                connections.insert(best.d_pin.clone(), effective_d);
                connections.insert(best.clk_pin.clone(), clk_net);
                connections.insert(best.q_pin.clone(), q_net);
                self.record(cell.name.clone(), best.cell_name, connections);
            }
            Some(rst) => {
                let candidates = self.lib.dff_reset.clone();
                let best = crate::celllib::best_dff(&candidates, self.delay_weight)
                    .ok_or_else(|| MapError::Unrealizable {
                        cell: cell.name.clone(),
                        detail: "no liberty cell realises a '$dff' with async reset".to_string(),
                    })?
                    .clone();
                let (rst_pin, active_low) = best
                    .reset
                    .clone()
                    .expect("dff_reset candidates always carry a reset pin");
                let rst_net_final = if active_low {
                    self.insert_inverter(&cell.name, &rst)?
                } else {
                    rst
                };
                let mut connections = HashMap::new();
                connections.insert(best.d_pin.clone(), effective_d);
                connections.insert(best.clk_pin.clone(), clk_net);
                connections.insert(best.q_pin.clone(), q_net);
                connections.insert(rst_pin, rst_net_final);
                self.record(cell.name.clone(), best.cell_name, connections);
            }
        }
        Ok(())
    }
}

pub fn map_design(
    netlist: &GenericNetlist,
    lib: &CellLibrary,
    delay_weight: f64,
) -> Result<MapResult, MapError> {
    let mut m = Mapper {
        lib,
        delay_weight,
        instances: Vec::new(),
        area_um2: 0.0,
        counts: BTreeMap::new(),
        fresh_counter: 0,
        const_nets: HashMap::new(),
    };
    for cell in &netlist.cells {
        match cell.cell_type.as_str() {
            "$dff" => m.map_dff(cell)?,
            "$not" | "$buf" | "$and2" | "$nand2" | "$or2" | "$nor2" | "$xor2" | "$xnor2"
            | "$mux2" => m.map_comb(leak_gtype(&cell.cell_type), cell)?,
            other => {
                return Err(MapError::Unrealizable {
                    cell: cell.name.clone(),
                    detail: format!("'{other}' is outside the v1 generic vocabulary"),
                })
            }
        }
    }
    let instance_count = m.instances.len();
    Ok(MapResult {
        instances: m.instances,
        instance_count,
        area_um2: m.area_um2,
        instance_counts_by_type: m.counts,
    })
}

/// The generic-type strings are a fixed, known-`'static` set
/// (`genlist::GENERIC_TYPES`); this turns the owned `String` on a
/// [`GenericCell`] back into the `'static` key `celllib::CellLibrary::comb`
/// is keyed by, without allocating a lookup table for ten entries.
fn leak_gtype(s: &str) -> &'static str {
    match s {
        "$not" => "$not",
        "$buf" => "$buf",
        "$and2" => "$and2",
        "$nand2" => "$nand2",
        "$or2" => "$or2",
        "$nor2" => "$nor2",
        "$xor2" => "$xor2",
        "$xnor2" => "$xnor2",
        "$mux2" => "$mux2",
        _ => unreachable!("caller already matched on the same set"),
    }
}

/// All net names referenced by any mapped instance's connections.
pub fn referenced_nets(instances: &[MappedInstance]) -> HashSet<String> {
    let mut out = HashSet::new();
    for inst in instances {
        for net in inst.connections.values() {
            out.insert(net.clone());
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{celllib, genlist, liberty};

    const MINI_LIB: &str = r#"
library ("test_lib") {
    time_unit : "1ns";
    capacitive_load_unit(1.0, "pf");

    cell ("AND2") {
        area : 6.0;
        pin ("A") { direction : "input"; }
        pin ("B") { direction : "input"; }
        pin ("X") { direction : "output"; function : "(A&B)"; }
    }
    cell ("MUX2") {
        area : 8.0;
        pin ("A0") { direction : "input"; }
        pin ("A1") { direction : "input"; }
        pin ("S") { direction : "input"; }
        pin ("X") { direction : "output"; function : "(A0&!S) | (A1&S)"; }
    }
    cell ("NOT1") {
        area : 2.0;
        pin ("A") { direction : "input"; }
        pin ("Y") { direction : "output"; function : "(!A)"; }
    }
    cell ("DFXTP") {
        area : 15.0;
        ff ("IQ", "IQN") { clocked_on : "CLK"; next_state : "D"; }
        pin ("CLK") { direction : "input"; }
        pin ("D") { direction : "input"; }
        pin ("Q") {
            direction : "output";
            timing () { related_pin : "CLK"; timing_type : "rising_edge"; }
        }
    }
    cell ("DFRTP") {
        area : 18.0;
        ff ("IQ", "IQN") { clocked_on : "CLK"; next_state : "D"; clear : "!RESET_B"; }
        pin ("CLK") { direction : "input"; }
        pin ("D") { direction : "input"; }
        pin ("RESET_B") { direction : "input"; }
        pin ("Q") {
            direction : "output";
            timing () { related_pin : "CLK"; timing_type : "rising_edge"; }
        }
    }
    cell ("CONB") {
        area : 3.75;
        pin ("HI") { direction : "output"; function : "1"; }
        pin ("LO") { direction : "output"; function : "0"; }
    }
}
"#;

    fn lib() -> celllib::CellLibrary {
        celllib::build(&liberty::parse(MINI_LIB).unwrap())
    }

    fn cell(name: &str, ty: &str, conns: &[(&str, &str)]) -> genlist::GenericCell {
        genlist::GenericCell {
            name: name.to_string(),
            cell_type: ty.to_string(),
            connections: conns
                .iter()
                .map(|(k, v)| (k.to_string(), v.to_string()))
                .collect(),
        }
    }

    fn netlist(cells: Vec<genlist::GenericCell>) -> genlist::GenericNetlist {
        genlist::GenericNetlist {
            schema: "klt.synth.generic-netlist/1".to_string(),
            top: "t".to_string(),
            ports: vec![],
            cells,
        }
    }

    #[test]
    fn maps_plain_and2() {
        let n = netlist(vec![cell(
            "g0",
            "$and2",
            &[("A", "a"), ("B", "b"), ("Y", "y")],
        )]);
        let res = map_design(&n, &lib(), 0.0).unwrap();
        assert_eq!(res.instance_count, 1);
        assert_eq!(res.instances[0].cell_type, "AND2");
        assert_eq!(res.instances[0].connections.get("A").unwrap(), "a");
        assert_eq!(res.instances[0].connections.get("X").unwrap(), "y");
    }

    #[test]
    fn maps_plain_dff() {
        let n = netlist(vec![cell(
            "r0",
            "$dff",
            &[("D", "d"), ("CLK", "clk"), ("Q", "q")],
        )]);
        let res = map_design(&n, &lib(), 0.0).unwrap();
        assert_eq!(res.instance_count, 1);
        assert_eq!(res.instances[0].cell_type, "DFXTP");
    }

    #[test]
    fn maps_dff_with_reset_and_inserts_inverter_for_active_low_pin() {
        let n = netlist(vec![cell(
            "r0",
            "$dff",
            &[("D", "d"), ("CLK", "clk"), ("Q", "q"), ("RST", "rst")],
        )]);
        let res = map_design(&n, &lib(), 0.0).unwrap();
        // One DFRTP instance + one inverter bridging active-high RST to the
        // active-low RESET_B pin.
        assert_eq!(res.instance_count, 2);
        assert_eq!(*res.instance_counts_by_type.get("DFRTP").unwrap(), 1);
        assert_eq!(*res.instance_counts_by_type.get("NOT1").unwrap(), 1);
        let dff = res
            .instances
            .iter()
            .find(|i| i.cell_type == "DFRTP")
            .unwrap();
        let inv = res
            .instances
            .iter()
            .find(|i| i.cell_type == "NOT1")
            .unwrap();
        assert_eq!(inv.connections.get("A").unwrap(), "rst");
        assert_eq!(
            dff.connections.get("RESET_B").unwrap(),
            inv.connections.get("Y").unwrap()
        );
    }

    #[test]
    fn maps_dff_with_enable_via_mux_feedback_chain() {
        let n = netlist(vec![cell(
            "r0",
            "$dff",
            &[("D", "d"), ("CLK", "clk"), ("Q", "q"), ("EN", "en")],
        )]);
        let res = map_design(&n, &lib(), 0.0).unwrap();
        assert_eq!(res.instance_count, 2);
        assert_eq!(*res.instance_counts_by_type.get("DFXTP").unwrap(), 1);
        assert_eq!(*res.instance_counts_by_type.get("MUX2").unwrap(), 1);
        let mux = res
            .instances
            .iter()
            .find(|i| i.cell_type == "MUX2")
            .unwrap();
        // A = Q feedback, B = D, S = EN
        assert_eq!(mux.connections.get("A0").unwrap(), "q");
        assert_eq!(mux.connections.get("A1").unwrap(), "d");
        assert_eq!(mux.connections.get("S").unwrap(), "en");
        let dff = res
            .instances
            .iter()
            .find(|i| i.cell_type == "DFXTP")
            .unwrap();
        assert_eq!(
            dff.connections.get("D").unwrap(),
            mux.connections.get("X").unwrap()
        );
    }

    #[test]
    fn maps_dff_with_reset_and_enable_via_chain_plus_reset_family() {
        let n = netlist(vec![cell(
            "r0",
            "$dff",
            &[
                ("D", "d"),
                ("CLK", "clk"),
                ("Q", "q"),
                ("RST", "rst"),
                ("EN", "en"),
            ],
        )]);
        let res = map_design(&n, &lib(), 0.0).unwrap();
        // MUX2 (enable) + inverter (reset polarity) + DFRTP.
        assert_eq!(res.instance_count, 3);
        assert_eq!(*res.instance_counts_by_type.get("DFRTP").unwrap(), 1);
        assert_eq!(*res.instance_counts_by_type.get("MUX2").unwrap(), 1);
        assert_eq!(*res.instance_counts_by_type.get("NOT1").unwrap(), 1);
    }

    #[test]
    fn resolves_const_nets_to_a_single_shared_tie_instance() {
        // Two $and2 cells both reference $const1 on their B pin -- both
        // should resolve to the *same* tie-hi instance's output net, not
        // two separate tie cells.
        let n = netlist(vec![
            cell("g0", "$and2", &[("A", "a"), ("B", "$const1"), ("Y", "y0")]),
            cell("g1", "$and2", &[("A", "a"), ("B", "$const1"), ("Y", "y1")]),
        ]);
        let res = map_design(&n, &lib(), 0.0).unwrap();
        // 2 AND2 instances + exactly 1 shared tie-hi instance.
        assert_eq!(res.instance_count, 3);
        assert_eq!(*res.instance_counts_by_type.get("CONB").unwrap(), 1);
        let g0 = res
            .instances
            .iter()
            .find(|i| i.instance_name == "g0")
            .unwrap();
        let g1 = res
            .instances
            .iter()
            .find(|i| i.instance_name == "g1")
            .unwrap();
        assert_eq!(
            g0.connections.get("B").unwrap(),
            g1.connections.get("B").unwrap()
        );
    }

    #[test]
    fn unrealizable_cell_type_is_an_error() {
        // No $xor2 candidate in MINI_LIB.
        let n = netlist(vec![cell(
            "g0",
            "$xor2",
            &[("A", "a"), ("B", "b"), ("Y", "y")],
        )]);
        let err = map_design(&n, &lib(), 0.0).unwrap_err();
        assert!(matches!(err, MapError::Unrealizable { .. }));
    }
}
