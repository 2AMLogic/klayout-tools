//! Timing-graph construction and critical-path propagation.
//!
//! Rise and fall are tracked **separately** per net (edge-aware
//! propagation): each net carries an independent rise arrival/slew and
//! fall arrival/slew, and each arc's `timing_sense`
//! (`positive_unate`/`negative_unate`/`non_unate`) determines which input
//! edge(s) can produce which output edge, exactly as a real NLDM-based STA
//! engine does. An earlier version of this spike collapsed rise/fall into
//! a single `max(cell_rise, cell_fall)` value per hop; that was measured
//! (see `README.md`) to overstate delay by ~35-40% on the `gcd` corpus
//! design because it double-counts the worse of two unrelated tables at
//! every hop instead of following the one polarity that is actually
//! active -- switching to edge-aware propagation is what closes that gap.
//!
//! **Remaining documented simplifications** (see `README.md` for the full
//! list and measured impact):
//!
//! 1. **No wire delay / no parasitics** -- a net's arrival equals its
//!    driver pin's arrival exactly (matches ABC's own `WireLoad = "none"`
//!    mode, per `docs/design/synthesize-qor-improvements-survey.md` §1.4).
//! 2. **A uniform boundary condition**: every primary input net (including
//!    the clock net, since no `create_clock` is modeled -- this mirrors
//!    OpenSTA's own `-unconstrained` mode used for the oracle comparison)
//!    gets the same caller-supplied input transition on both edges; every
//!    primary output net gets the same caller-supplied extra load. No SDC,
//!    no per-pin overrides.
//! 3. **Register data-input pins are identified by the literal name `"D"`**
//!    -- correct for every sky130_fd_sc_hd sequential cell this spike's
//!    corpus uses (`dfrtp_1`), not a general liberty-driven classification.
//! 4. **Async set/reset arcs are not modeled** -- only `combinational`,
//!    `rising_edge`, and `falling_edge` timing arcs drive propagation;
//!    `recovery`/`removal`/`clear`/`preset`/setup/hold arcs are parsed
//!    (`liberty.rs`) but never consulted here.

use std::collections::{HashMap, VecDeque};
use std::fmt;

use crate::liberty::{ArcKind, Direction, Library, TimingSense};
use crate::netlist::Module;
use crate::nldm;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Edge {
    Rise,
    Fall,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RootKind {
    PrimaryInput,
    Register,
}

#[derive(Debug, Clone)]
pub struct DrivingHop {
    pub instance: String,
    pub cell_type: String,
    pub output_pin: String,
    pub output_edge: Edge,
    pub related_pin: String,
    pub related_net: String,
    pub related_edge: Edge,
    pub is_sequential: bool,
}

#[derive(Debug, Clone)]
struct EdgeArrival {
    arrival_ns: f64,
    slew_ns: f64,
    root: RootKind,
    prev: Option<DrivingHop>,
}

#[derive(Debug, Clone, Default)]
struct NetTiming {
    rise: Option<EdgeArrival>,
    fall: Option<EdgeArrival>,
}

impl NetTiming {
    fn get(&self, edge: Edge) -> Option<&EdgeArrival> {
        match edge {
            Edge::Rise => self.rise.as_ref(),
            Edge::Fall => self.fall.as_ref(),
        }
    }

    fn is_resolved(&self) -> bool {
        self.rise.is_some() && self.fall.is_some()
    }
}

#[derive(Debug)]
pub struct StaError(pub String);

impl fmt::Display for StaError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

pub struct StaConfig {
    pub input_transition_ns: f64,
    pub output_load_pf: f64,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct PathHopReport {
    pub point: String,
    pub cell: Option<String>,
    pub edge: String,
    pub arrival_ns: f64,
    pub slew_ns: f64,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct PathReport {
    pub startpoint: String,
    pub startpoint_kind: String,
    pub endpoint: String,
    pub endpoint_kind: String,
    pub delay_ns: f64,
    pub hops: Vec<PathHopReport>,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct StaResult {
    pub top: String,
    pub num_nets: usize,
    pub num_cells: usize,
    pub worst_path: PathReport,
    /// The worst path whose startpoint is a register (clk-to-Q launch) and
    /// whose endpoint is a register's `D` pin -- `None` for a purely
    /// combinational design (e.g. this spike's `mult8` corpus design).
    pub worst_reg_to_reg_path: Option<PathReport>,
}

fn pin_direction(lib: &Library, cell_type: &str, pin: &str) -> Option<Direction> {
    lib.cells.get(cell_type)?.pins.get(pin).map(|p| p.direction)
}

fn pin_capacitance_pf(lib: &Library, cell_type: &str, pin: &str) -> f64 {
    lib.cells
        .get(cell_type)
        .and_then(|c| c.pins.get(pin))
        .map(|p| p.capacitance_pf * lib.cap_unit_to_pf)
        .unwrap_or(0.0)
}

fn is_sequential_cell(lib: &Library, cell_type: &str) -> bool {
    lib.cells.get(cell_type).is_some_and(|c| {
        c.pins.values().any(|p| {
            p.direction == Direction::Output
                && p.arcs.iter().any(|a| {
                    matches!(
                        a.kind,
                        Some(ArcKind::RisingEdge) | Some(ArcKind::FallingEdge)
                    )
                })
        })
    })
}

/// Which input edge(s) can produce a given output edge, per `timing_sense`.
fn input_edges_for_output(sense: TimingSense, output_edge: Edge) -> &'static [Edge] {
    match (sense, output_edge) {
        (TimingSense::Positive, Edge::Rise) => &[Edge::Rise],
        (TimingSense::Positive, Edge::Fall) => &[Edge::Fall],
        (TimingSense::Negative, Edge::Rise) => &[Edge::Fall],
        (TimingSense::Negative, Edge::Fall) => &[Edge::Rise],
        (TimingSense::NonUnate, _) => &[Edge::Rise, Edge::Fall],
    }
}

/// Run the critical-path analysis. Returns the globally worst path (any
/// startpoint type to any endpoint type) and, if the design has registers,
/// the worst pure register-to-register path.
pub fn analyze(module: &Module, lib: &Library, cfg: &StaConfig) -> Result<StaResult, StaError> {
    // --- Build net_sinks / net capacitance -------------------------------
    let mut net_sinks: HashMap<String, Vec<(usize, String)>> = HashMap::new();
    let mut net_driver: HashMap<String, (usize, String)> = HashMap::new();

    for (idx, cell) in module.cells.iter().enumerate() {
        for (pin, net) in &cell.connections {
            match pin_direction(lib, &cell.cell_type, pin) {
                Some(Direction::Output) => {
                    if let Some((prev_idx, prev_pin)) =
                        net_driver.insert(net.clone(), (idx, pin.clone()))
                    {
                        return Err(StaError(format!(
                            "net '{net}' has multiple drivers: {}.{} and {}.{}",
                            module.cells[prev_idx].instance_name, prev_pin, cell.instance_name, pin
                        )));
                    }
                }
                _ => {
                    net_sinks
                        .entry(net.clone())
                        .or_default()
                        .push((idx, pin.clone()));
                }
            }
        }
    }

    let output_ports: std::collections::HashSet<&str> = module.output_nets().into_iter().collect();
    let input_ports: std::collections::HashSet<&str> = module.input_nets().into_iter().collect();

    let net_load_pf = |net: &str| -> f64 {
        let mut load = 0.0;
        if let Some(sinks) = net_sinks.get(net) {
            for (idx, pin) in sinks {
                load += pin_capacitance_pf(lib, &module.cells[*idx].cell_type, pin);
            }
        }
        if output_ports.contains(net) {
            load += cfg.output_load_pf;
        }
        load
    };

    // --- Seed primary inputs (both edges, same boundary condition) -------
    let mut timing: HashMap<String, NetTiming> = HashMap::new();
    for net in &input_ports {
        let seed = EdgeArrival {
            arrival_ns: 0.0,
            slew_ns: cfg.input_transition_ns,
            root: RootKind::PrimaryInput,
            prev: None,
        };
        timing.insert(
            (*net).to_string(),
            NetTiming {
                rise: Some(seed.clone()),
                fall: Some(seed),
            },
        );
    }

    // --- Build pin-level dependency graph (Kahn's algorithm at
    //     output-pin granularity; a node is "ready" once every net its
    //     valid arcs reference is fully resolved on both edges). ---------
    struct OutputNode {
        instance_idx: usize,
        pin_name: String,
    }

    let mut nodes: Vec<OutputNode> = Vec::new();
    let mut net_to_dependents: HashMap<String, Vec<usize>> = HashMap::new();
    let mut remaining: Vec<usize> = Vec::new();

    for (idx, cell) in module.cells.iter().enumerate() {
        let Some(lib_cell) = lib.cells.get(&cell.cell_type) else {
            return Err(StaError(format!(
                "cell type '{}' (instance '{}') not found in liberty",
                cell.cell_type, cell.instance_name
            )));
        };
        for lib_pin in lib_cell.pins.values() {
            if lib_pin.direction != Direction::Output {
                continue;
            }
            let mut needed_nets: Vec<String> = Vec::new();
            for arc in &lib_pin.arcs {
                if !matches!(
                    arc.kind,
                    Some(ArcKind::Combinational)
                        | Some(ArcKind::RisingEdge)
                        | Some(ArcKind::FallingEdge)
                ) {
                    continue;
                }
                if let Some(related_net) = cell.connections.get(&arc.related_pin) {
                    if !needed_nets.contains(related_net) {
                        needed_nets.push(related_net.clone());
                    }
                }
            }
            if needed_nets.is_empty() {
                continue;
            }
            let node_idx = nodes.len();
            for net in &needed_nets {
                net_to_dependents
                    .entry(net.clone())
                    .or_default()
                    .push(node_idx);
            }
            remaining.push(needed_nets.len());
            nodes.push(OutputNode {
                instance_idx: idx,
                pin_name: lib_pin.name.clone(),
            });
        }
    }

    let mut queue: VecDeque<String> = VecDeque::new();
    for net in timing.keys() {
        queue.push_back(net.clone());
    }

    let mut ready_queue: VecDeque<usize> = VecDeque::new();
    let mut visited_dep_once: HashMap<usize, std::collections::HashSet<String>> = HashMap::new();

    while let Some(net) = queue.pop_front() {
        if let Some(dependents) = net_to_dependents.get(&net) {
            for &node_idx in dependents {
                let seen = visited_dep_once.entry(node_idx).or_default();
                if seen.insert(net.clone()) {
                    remaining[node_idx] -= 1;
                    if remaining[node_idx] == 0 {
                        ready_queue.push_back(node_idx);
                    }
                }
            }
        }

        while let Some(node_idx) = ready_queue.pop_front() {
            let node = &nodes[node_idx];
            let cell = &module.cells[node.instance_idx];
            let lib_cell = &lib.cells[&cell.cell_type];
            let lib_pin = &lib_cell.pins[&node.pin_name];
            let Some(output_net) = cell.connections.get(&node.pin_name).cloned() else {
                continue;
            };
            let load = net_load_pf(&output_net);

            let mut result_edges: [Option<EdgeArrival>; 2] = [None, None];
            for (i, output_edge) in [Edge::Rise, Edge::Fall].into_iter().enumerate() {
                let mut best_arrival = f64::NEG_INFINITY;
                let mut best_slew = 0.0;
                let mut best_hop: Option<DrivingHop> = None;

                for arc in &lib_pin.arcs {
                    let kind = match arc.kind {
                        Some(
                            k @ (ArcKind::Combinational
                            | ArcKind::RisingEdge
                            | ArcKind::FallingEdge),
                        ) => k,
                        _ => continue,
                    };
                    let Some(related_net) = cell.connections.get(&arc.related_pin) else {
                        continue;
                    };
                    let Some(related_timing) = timing.get(related_net) else {
                        continue;
                    };

                    let (delay_table, transition_table) = match output_edge {
                        Edge::Rise => (&arc.cell_rise, &arc.rise_transition),
                        Edge::Fall => (&arc.cell_fall, &arc.fall_transition),
                    };
                    let Some(delay_table) = delay_table else {
                        continue;
                    };

                    for &input_edge in input_edges_for_output(arc.sense, output_edge) {
                        let Some(related_arrival) = related_timing.get(input_edge) else {
                            continue;
                        };
                        let delay = nldm::interpolate(delay_table, related_arrival.slew_ns, load);
                        let transition = transition_table
                            .as_ref()
                            .map(|t| nldm::interpolate(t, related_arrival.slew_ns, load))
                            .unwrap_or(0.0);
                        let candidate_arrival = related_arrival.arrival_ns + delay;
                        if candidate_arrival > best_arrival {
                            best_arrival = candidate_arrival;
                            best_slew = transition;
                            best_hop = Some(DrivingHop {
                                instance: cell.instance_name.clone(),
                                cell_type: cell.cell_type.clone(),
                                output_pin: node.pin_name.clone(),
                                output_edge,
                                related_pin: arc.related_pin.clone(),
                                related_net: related_net.clone(),
                                related_edge: input_edge,
                                is_sequential: matches!(
                                    kind,
                                    ArcKind::RisingEdge | ArcKind::FallingEdge
                                ),
                            });
                        }
                    }
                }

                if let Some(hop) = best_hop {
                    let root = if hop.is_sequential {
                        RootKind::Register
                    } else {
                        timing
                            .get(&hop.related_net)
                            .and_then(|t| t.get(hop.related_edge))
                            .map(|a| a.root)
                            .unwrap_or(RootKind::PrimaryInput)
                    };
                    result_edges[i] = Some(EdgeArrival {
                        arrival_ns: best_arrival,
                        slew_ns: best_slew,
                        root,
                        prev: Some(hop),
                    });
                }
            }

            let [rise, fall] = result_edges;
            if rise.is_none() && fall.is_none() {
                continue;
            }
            timing.insert(output_net.clone(), NetTiming { rise, fall });
            queue.push_back(output_net);
        }
    }

    if timing.len() < input_ports.len() {
        return Err(StaError(
            "no primary inputs resolved -- empty design?".to_string(),
        ));
    }

    // --- Reconstruct a path ending at `(end_net, end_edge)` ----------------
    let build_path = |end_net: &str,
                      end_edge: Edge,
                      endpoint_label: String,
                      endpoint_kind: &str|
     -> PathReport {
        let mut forward: Vec<PathHopReport> = Vec::new();
        let mut cur = end_net.to_string();
        let mut cur_edge = end_edge;
        let startpoint;
        let startpoint_kind;
        loop {
            let rec = timing[&cur]
                .get(cur_edge)
                .expect("resolved net/edge must exist");
            let edge_label = match cur_edge {
                Edge::Rise => "rise",
                Edge::Fall => "fall",
            };
            match &rec.prev {
                None => {
                    forward.push(PathHopReport {
                        point: format!("{cur} (in)"),
                        cell: None,
                        edge: edge_label.to_string(),
                        arrival_ns: rec.arrival_ns,
                        slew_ns: rec.slew_ns,
                    });
                    startpoint = cur.clone();
                    startpoint_kind = "input port".to_string();
                    break;
                }
                Some(hop) => {
                    forward.push(PathHopReport {
                        point: format!("{}/{}", hop.instance, hop.output_pin),
                        cell: Some(hop.cell_type.clone()),
                        edge: edge_label.to_string(),
                        arrival_ns: rec.arrival_ns,
                        slew_ns: rec.slew_ns,
                    });
                    if hop.is_sequential {
                        startpoint = format!("{}/{}", hop.instance, hop.output_pin);
                        startpoint_kind = "flip-flop (rising edge)".to_string();
                        break;
                    }
                    cur_edge = hop.related_edge;
                    cur = hop.related_net.clone();
                }
            }
        }
        forward.reverse();
        let delay_ns = timing[end_net].get(end_edge).unwrap().arrival_ns;
        PathReport {
            startpoint,
            startpoint_kind,
            endpoint: endpoint_label,
            endpoint_kind: endpoint_kind.to_string(),
            delay_ns,
            hops: forward,
        }
    };

    // --- Global worst path (any endpoint, either edge) ----------------------
    // `timing` is a HashMap, whose iteration order is randomized per process;
    // on a bit-sliced datapath (e.g. `gcd`) many parallel D-pin paths land on
    // the exact same worst delay, so iterating it directly picked an
    // arbitrary tied endpoint from run to run of the identical binary. Walk a
    // sorted `Vec` of net names instead, and break ties deterministically:
    // prefer a genuine STA endpoint (a register D pin, then a primary output)
    // over an arbitrary internal net, then fall back to net name.
    let register_d_nets: std::collections::HashSet<&str> = module
        .cells
        .iter()
        .filter(|cell| is_sequential_cell(lib, &cell.cell_type))
        .filter_map(|cell| cell.connections.get("D"))
        .map(|s| s.as_str())
        .collect();
    let endpoint_rank = |net: &str| -> u8 {
        if register_d_nets.contains(net) {
            0
        } else if output_ports.contains(net) {
            1
        } else {
            2
        }
    };
    let mut sorted_nets: Vec<&str> = timing.keys().map(|s| s.as_str()).collect();
    sorted_nets.sort_unstable();

    let mut worst: Option<(&str, Edge, f64)> = None;
    for net in sorted_nets {
        let t = timing.get(net).expect("net taken from timing.keys()");
        for (edge, rec) in [(Edge::Rise, &t.rise), (Edge::Fall, &t.fall)] {
            if let Some(rec) = rec {
                let is_new_worst = match worst {
                    None => true,
                    Some((cur_net, _, cur_delay)) => {
                        rec.arrival_ns > cur_delay
                            || (rec.arrival_ns == cur_delay
                                && endpoint_rank(net) < endpoint_rank(cur_net))
                    }
                };
                if is_new_worst {
                    worst = Some((net, edge, rec.arrival_ns));
                }
            }
        }
    }
    let (worst_net, worst_edge, _) =
        worst.ok_or_else(|| StaError("no nets analyzed".to_string()))?;
    let endpoint_kind = if output_ports.contains(worst_net) {
        "output port"
    } else {
        "internal net"
    };
    let worst_path = build_path(worst_net, worst_edge, worst_net.to_string(), endpoint_kind);

    // --- Worst register-to-register path -----------------------------------
    let mut worst_reg_to_reg: Option<(String, Edge, f64)> = None;
    for cell in &module.cells {
        if !is_sequential_cell(lib, &cell.cell_type) {
            continue;
        }
        if let Some(d_net) = cell.connections.get("D") {
            if let Some(t) = timing.get(d_net) {
                for (edge, rec) in [(Edge::Rise, &t.rise), (Edge::Fall, &t.fall)] {
                    if let Some(rec) = rec {
                        if rec.root == RootKind::Register {
                            let better = worst_reg_to_reg
                                .as_ref()
                                .is_none_or(|(_, _, best)| rec.arrival_ns > *best);
                            if better {
                                worst_reg_to_reg = Some((d_net.clone(), edge, rec.arrival_ns));
                            }
                        }
                    }
                }
            }
        }
    }
    let worst_reg_to_reg_path = worst_reg_to_reg
        .map(|(net, edge, _)| build_path(&net, edge, net.clone(), "flip-flop (D, setup)"));

    Ok(StaResult {
        top: module.name.clone(),
        num_nets: timing.values().filter(|t| t.is_resolved()).count(),
        num_cells: module.cells.len(),
        worst_path,
        worst_reg_to_reg_path,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::liberty::{Cell, Library, Pin, Table2D, TimingArcTables};
    use crate::netlist::{CellInstance, Port, PortDirection};

    /// A 1x1 NLDM table that always interpolates to `delay_ns` regardless of
    /// input slew / output load -- see `nldm::tests::single_row_or_column_table`
    /// for why a single-point axis pins the result to the sole table entry.
    fn flat_table(delay_ns: f64) -> Table2D {
        Table2D {
            index1: vec![0.0],
            index2: vec![0.0],
            values: vec![vec![delay_ns]],
        }
    }

    /// Builds a synthetic library with two combinational cell types:
    /// - `BUF`: `A` (input) -> `Y` (output), a plain combinational arc whose
    ///   rise/fall delay is exactly `delay_ns` regardless of slew/load (via
    ///   `flat_table`), and zero pin capacitance everywhere so every net's
    ///   `net_load_pf` is identical (no capacitance-driven skew between the
    ///   two BUF instances below).
    /// - `DFF`: `D` (input, unused for propagation), `Q` (output) with a
    ///   `rising_edge` arc on `CLK` -- present only so `is_sequential_cell`
    ///   classifies `DFF` as a register and `D`'s net is recognized as a
    ///   register D-pin net by `analyze`'s tie-break (`register_d_nets`).
    fn synthetic_library(delay_ns: f64) -> Library {
        let mut cells = HashMap::new();

        let mut buf_pins = HashMap::new();
        buf_pins.insert(
            "A".to_string(),
            Pin {
                name: "A".to_string(),
                direction: Direction::Input,
                capacitance_pf: 0.0,
                arcs: Vec::new(),
            },
        );
        buf_pins.insert(
            "Y".to_string(),
            Pin {
                name: "Y".to_string(),
                direction: Direction::Output,
                capacitance_pf: 0.0,
                arcs: vec![TimingArcTables {
                    related_pin: "A".to_string(),
                    kind: Some(ArcKind::Combinational),
                    sense: TimingSense::Positive,
                    cell_rise: Some(flat_table(delay_ns)),
                    cell_fall: Some(flat_table(delay_ns)),
                    rise_transition: None,
                    fall_transition: None,
                }],
            },
        );
        cells.insert(
            "BUF".to_string(),
            Cell {
                name: "BUF".to_string(),
                pins: buf_pins,
            },
        );

        let mut dff_pins = HashMap::new();
        dff_pins.insert(
            "D".to_string(),
            Pin {
                name: "D".to_string(),
                direction: Direction::Input,
                capacitance_pf: 0.0,
                arcs: Vec::new(),
            },
        );
        dff_pins.insert(
            "Q".to_string(),
            Pin {
                name: "Q".to_string(),
                direction: Direction::Output,
                capacitance_pf: 0.0,
                arcs: vec![TimingArcTables {
                    related_pin: "CLK".to_string(),
                    kind: Some(ArcKind::RisingEdge),
                    sense: TimingSense::NonUnate,
                    cell_rise: Some(flat_table(delay_ns)),
                    cell_fall: Some(flat_table(delay_ns)),
                    rise_transition: None,
                    fall_transition: None,
                }],
            },
        );
        cells.insert(
            "DFF".to_string(),
            Cell {
                name: "DFF".to_string(),
                pins: dff_pins,
            },
        );

        Library {
            name: "test_lib".to_string(),
            time_unit_to_ns: 1.0,
            cap_unit_to_pf: 1.0,
            cells,
        }
    }

    fn conn(pairs: &[(&str, &str)]) -> HashMap<String, String> {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect()
    }

    /// A module with two independent, equal-delay combinational chains from
    /// primary inputs that tie for the global worst arrival:
    /// `inA -> buf1/Y -> n_internal` (an internal net with no sequential
    /// sink -- endpoint rank "internal net") and
    /// `inB -> buf2/Y -> net_d -> dff1/D` (a register D-pin net -- endpoint
    /// rank "register D pin"). Both `BUF` instances use identical flat delay
    /// tables and zero pin capacitance, so their computed arrivals are
    /// bit-for-bit equal -- a genuine tie, not an approximation. `cell_order`
    /// lets the caller permute instantiation order to confirm the result
    /// does not depend on it.
    fn tied_module(cell_order: [usize; 3]) -> Module {
        let all_cells = [
            CellInstance {
                instance_name: "buf1".to_string(),
                cell_type: "BUF".to_string(),
                connections: conn(&[("A", "inA"), ("Y", "n_internal")]),
            },
            CellInstance {
                instance_name: "buf2".to_string(),
                cell_type: "BUF".to_string(),
                connections: conn(&[("A", "inB"), ("Y", "net_d")]),
            },
            CellInstance {
                instance_name: "dff1".to_string(),
                cell_type: "DFF".to_string(),
                connections: conn(&[("D", "net_d"), ("Q", "q_out")]),
            },
        ];
        let cells = cell_order.iter().map(|&i| all_cells[i].clone()).collect();

        Module {
            name: "tied".to_string(),
            ports: vec![
                Port {
                    name: "inA".to_string(),
                    direction: PortDirection::Input,
                    nets: vec!["inA".to_string()],
                },
                Port {
                    name: "inB".to_string(),
                    direction: PortDirection::Input,
                    nets: vec!["inB".to_string()],
                },
            ],
            cells,
        }
    }

    /// Regression test for the non-determinism PR #830 fixed: `analyze`'s
    /// global worst-path selection used to iterate a `HashMap<String,
    /// NetTiming>` directly, so a tie between an internal net and a register
    /// D-pin net (both landing on the exact same worst delay, as on a
    /// bit-sliced datapath like `gcd`) picked whichever the HashMap's
    /// randomized-per-instance iteration order happened to visit first. The
    /// fix walks a sorted `Vec` of net names and breaks ties by endpoint kind
    /// (register D pin > primary output > internal net) then net name, so
    /// the register D-pin net (`net_d`) must win every time regardless of
    /// (a) how many times `analyze` is called -- `std::collections::HashMap`
    /// uses a fresh randomized hash state per instance, so repeated calls
    /// resample the very iteration-order randomness the bug depended on --
    /// and (b) the order the tied cells were declared in the netlist.
    #[test]
    fn worst_path_tie_break_is_deterministic() {
        let lib = synthetic_library(1.0);
        let cfg = StaConfig {
            input_transition_ns: 0.1,
            output_load_pf: 0.0,
        };

        for cell_order in [[0, 1, 2], [1, 0, 2], [2, 0, 1], [2, 1, 0]] {
            for _ in 0..25 {
                let module = tied_module(cell_order);
                let result = analyze(&module, &lib, &cfg).expect("analyze should succeed");
                assert_eq!(
                    result.worst_path.endpoint, "net_d",
                    "worst_path must deterministically pick the register D-pin net \
                     over the tied internal net (cell_order={cell_order:?})"
                );
                assert_eq!(result.worst_path.delay_ns, 1.0);
            }
        }
    }
}
