//! The timing graph, and block-based max-delay propagation over it.
//!
//! # The delay model, stated exactly
//!
//! This engine reproduces OpenSTA's default *parasitics-free* NLDM
//! calculation. That model is not obvious from the liberty format alone --
//! three of its choices were pinned down by matching OpenSTA numerically on a
//! real path before a line of this file was written (see `../README.md`,
//! "Pinning the delay model down"):
//!
//! 1. **Net load capacitance is edge-dependent.** The capacitance a driver
//!    sees for a *falling* output is the sum of its sinks' `fall_capacitance`
//!    (not `capacitance`); for a rising output, their `rise_capacitance`.
//! 2. **Table lookups extrapolate linearly**, they do not clamp. With an
//!    ideal clock every CLK->Q arc is evaluated at an input transition of
//!    0.0, below every sky130 table's first index.
//! 3. **Slew is propagated block-based and merged worst-case per edge**,
//!    independently of which arc supplies the maximum arrival. Arc delays are
//!    then computed from the *merged* input slew, not from the slew along the
//!    path that won the arrival race.
//!
//! # What is deliberately not modelled
//!
//! No parasitics (zero net delay, no wire capacitance/resistance), no
//! wire-load model, no CCS, no derating, no on-chip variation, no crosstalk,
//! no multi-corner, no case analysis or constant propagation, no
//! false/multicycle paths, no clock network propagation (the clock is ideal:
//! every register clock pin is at time 0 with slew 0). Each of these is a
//! real gap relative to signoff STA, and `../README.md` lists them as the
//! work a "Go" verdict would have implied.

use std::collections::HashMap;

use crate::liberty::{ArcKind, Direction, LibCell, Library, Sense, TimingArc};
use crate::netlist::{Netlist, PortDir};

/// Transition index: `FALL` and `RISE` are used as array subscripts
/// throughout, so every vertex carries an independent rise and fall value.
pub const FALL: usize = 0;
pub const RISE: usize = 1;

const NEG_INF: f64 = f64::NEG_INFINITY;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VertexKind {
    /// A top-level port of the design.
    Port(PortDir),
    /// `instances[inst]`'s pin, an input of the cell.
    CellInput { inst: usize },
    /// `instances[inst]`'s pin, an output of the cell.
    CellOutput { inst: usize },
}

#[derive(Debug, Clone)]
pub struct Vertex {
    /// `inst/PIN`, or the port name for a top-level port.
    pub name: String,
    /// The cell type, for reporting (`""` for a port).
    pub cell: String,
    pub pin: String,
    pub kind: VertexKind,
    pub net: usize,
    /// Load capacitance driven by this vertex, per output edge. Zero unless
    /// the vertex is a net driver.
    pub load_cap: [f64; 2],
}

#[derive(Debug, Clone, Copy)]
enum EdgeKind {
    /// A cell timing arc; `arc` indexes `Graph::arcs`.
    Cell { arc: usize },
    /// A net connection: zero delay, slew passes through unchanged.
    Net,
}

#[derive(Debug, Clone, Copy)]
struct Edge {
    from: usize,
    to: usize,
    kind: EdgeKind,
}

/// A resolved cell timing arc: a borrow of the library's own arc, flattened
/// out of the `cell -> pin -> timing` nesting so propagation is a flat index.
#[derive(Clone, Copy)]
struct ResolvedArc<'a> {
    sense: Sense,
    kind: ArcKind,
    arc: &'a TimingArc,
}

/// One reported timing-path node.
#[derive(Debug, Clone)]
pub struct PathNode {
    pub pin: String,
    pub cell: String,
    pub rise: bool,
    pub delay: f64,
    pub arrival: f64,
    pub slew: f64,
    pub load_cap: f64,
}

/// One reported register-to-register path.
#[derive(Debug, Clone)]
pub struct Path {
    pub startpoint: String,
    pub startpoint_cell: String,
    pub endpoint: String,
    pub endpoint_cell: String,
    /// Data arrival time at the endpoint, in library time units.
    pub arrival: f64,
    /// Library setup time consumed at the endpoint (positive = required time
    /// is pulled in), or `None` when the endpoint has no setup arc.
    pub setup: Option<f64>,
    /// `period - setup - arrival` when a period was supplied.
    pub slack: Option<f64>,
    pub nodes: Vec<PathNode>,
}

pub struct Graph<'a> {
    lib: &'a Library,
    pub vertices: Vec<Vertex>,
    edges: Vec<Edge>,
    arcs: Vec<ResolvedArc<'a>>,
    /// `true` for register clock pins: analysis startpoints, held at arrival
    /// 0 and slew 0 by ideal-clock semantics.
    is_clock_startpoint: Vec<bool>,
    /// Incoming edge indices per vertex.
    incoming: Vec<Vec<usize>>,
    /// Register clock pins: the startpoints of a reg-to-reg analysis.
    pub clock_pins: Vec<usize>,
    /// Register data pins with a setup arc: `(vertex, setup arc)`.
    pub data_pins: Vec<(usize, &'a TimingArc)>,
    /// Vertices dropped from the topological order because they sit on a
    /// combinational cycle.
    pub cyclic_vertices: Vec<String>,
    pub unknown_cells: Vec<String>,
    order: Vec<usize>,
}

/// Result of a propagation run.
pub struct Timing {
    pub slew: Vec<[f64; 2]>,
    pub arrival: Vec<[f64; 2]>,
    /// `(vertex, transition, edge)` that produced the maximum arrival.
    prev: Vec<[Option<(usize, usize, usize)>; 2]>,
}

impl<'a> Graph<'a> {
    /// Build the timing graph for `nl` against `lib`.
    pub fn build(lib: &'a Library, nl: &Netlist) -> Graph<'a> {
        let mut vertices: Vec<Vertex> = Vec::new();
        let mut net_ids: HashMap<String, usize> = HashMap::new();
        let mut net_drivers: Vec<Vec<usize>> = Vec::new();
        let mut net_sinks: Vec<Vec<usize>> = Vec::new();
        let mut unknown_cells: Vec<String> = Vec::new();

        let net_of = |name: &str,
                      net_ids: &mut HashMap<String, usize>,
                      drivers: &mut Vec<Vec<usize>>,
                      sinks: &mut Vec<Vec<usize>>| {
            let canon = nl.canonical(name).to_string();
            *net_ids.entry(canon).or_insert_with(|| {
                drivers.push(Vec::new());
                sinks.push(Vec::new());
                drivers.len() - 1
            })
        };

        // Top-level ports. An `input` port *drives* its net; an `output` port
        // is a sink (and, with no `set_load`, contributes zero capacitance --
        // matching OpenSTA with no SDC output load).
        for (name, dir) in &nl.ports {
            let net = net_of(name, &mut net_ids, &mut net_drivers, &mut net_sinks);
            let vid = vertices.len();
            vertices.push(Vertex {
                name: name.clone(),
                cell: String::new(),
                pin: name.clone(),
                kind: VertexKind::Port(*dir),
                net,
                load_cap: [0.0, 0.0],
            });
            match dir {
                PortDir::Input => net_drivers[net].push(vid),
                _ => net_sinks[net].push(vid),
            }
        }

        // Cell pins.
        let mut inst_cells: Vec<Option<&LibCell>> = Vec::with_capacity(nl.instances.len());
        for (ii, inst) in nl.instances.iter().enumerate() {
            let cell = lib.cells.get(&inst.cell);
            if cell.is_none() && !unknown_cells.contains(&inst.cell) {
                unknown_cells.push(inst.cell.clone());
            }
            inst_cells.push(cell);
            let Some(cell) = cell else { continue };
            for (formal, actual) in &inst.conns {
                let Some(lp) = cell.pin(formal) else { continue };
                if matches!(lp.direction, Direction::Internal) {
                    continue;
                }
                let net = net_of(actual, &mut net_ids, &mut net_drivers, &mut net_sinks);
                let vid = vertices.len();
                let is_out = matches!(lp.direction, Direction::Output);
                vertices.push(Vertex {
                    name: format!("{}/{}", inst.name, formal),
                    cell: inst.cell.clone(),
                    pin: formal.clone(),
                    kind: if is_out {
                        VertexKind::CellOutput { inst: ii }
                    } else {
                        VertexKind::CellInput { inst: ii }
                    },
                    net,
                    load_cap: [0.0, 0.0],
                });
                if is_out {
                    net_drivers[net].push(vid);
                } else {
                    net_sinks[net].push(vid);
                }
            }
        }

        // Net load capacitance, per output edge.
        for (net, sinks) in net_sinks.iter().enumerate() {
            let mut cap = [0.0f64; 2];
            for &s in sinks {
                let v = &vertices[s];
                if let VertexKind::CellInput { inst } = v.kind {
                    if let Some(cell) = inst_cells[inst] {
                        if let Some(lp) = cell.pin(&v.pin) {
                            cap[FALL] += lp.load_cap(false);
                            cap[RISE] += lp.load_cap(true);
                        }
                    }
                }
            }
            for &d in &net_drivers[net] {
                vertices[d].load_cap = cap;
            }
        }

        // Edges.
        let mut edges: Vec<Edge> = Vec::new();
        let mut arcs: Vec<ResolvedArc> = Vec::new();
        let mut clock_pins: Vec<usize> = Vec::new();
        let mut data_pins: Vec<(usize, &'a TimingArc)> = Vec::new();

        // Index vertices by (instance, pin) for arc wiring.
        let mut pin_vertex: HashMap<(usize, &str), usize> = HashMap::new();
        for (vid, v) in vertices.iter().enumerate() {
            match v.kind {
                VertexKind::CellInput { inst } | VertexKind::CellOutput { inst } => {
                    pin_vertex.insert((inst, v.pin.as_str()), vid);
                }
                VertexKind::Port(_) => {}
            }
        }
        for (ii, maybe_cell) in inst_cells.iter().enumerate() {
            let Some(cell) = maybe_cell else { continue };
            for lp in &cell.pins {
                let Some(&to) = pin_vertex.get(&(ii, lp.name.as_str())) else {
                    continue;
                };
                for arc in &lp.arcs {
                    let Some(&from) = pin_vertex.get(&(ii, arc.related_pin.as_str())) else {
                        continue;
                    };
                    match arc.kind {
                        ArcKind::Combinational | ArcKind::Async | ArcKind::Edge { .. } => {
                            if arc.cell_rise.is_empty() && arc.cell_fall.is_empty() {
                                continue;
                            }
                            arcs.push(ResolvedArc {
                                sense: arc.sense,
                                kind: arc.kind,
                                arc,
                            });
                            edges.push(Edge {
                                from,
                                to,
                                kind: EdgeKind::Cell {
                                    arc: arcs.len() - 1,
                                },
                            });
                            if matches!(arc.kind, ArcKind::Edge { .. })
                                && !clock_pins.contains(&from)
                            {
                                clock_pins.push(from);
                            }
                        }
                        ArcKind::Setup { .. } => data_pins.push((to, arc)),
                        ArcKind::Other => {}
                    }
                }
            }
        }
        // Net edges: driver -> every sink.
        for net in 0..net_drivers.len() {
            for &d in &net_drivers[net] {
                for &s in &net_sinks[net] {
                    edges.push(Edge {
                        from: d,
                        to: s,
                        kind: EdgeKind::Net,
                    });
                }
            }
        }

        let n = vertices.len();
        let mut incoming = vec![Vec::new(); n];
        let mut outgoing = vec![Vec::new(); n];
        for (ei, e) in edges.iter().enumerate() {
            incoming[e.to].push(ei);
            outgoing[e.from].push(ei);
        }

        // Kahn topological order. Anything left over sits on a combinational
        // cycle and is reported, never silently analysed.
        let mut indeg: Vec<usize> = incoming.iter().map(|v| v.len()).collect();
        let mut queue: Vec<usize> = (0..n).filter(|&i| indeg[i] == 0).collect();
        let mut order = Vec::with_capacity(n);
        let mut head = 0;
        while head < queue.len() {
            let v = queue[head];
            head += 1;
            order.push(v);
            for &ei in &outgoing[v] {
                let t = edges[ei].to;
                indeg[t] -= 1;
                if indeg[t] == 0 {
                    queue.push(t);
                }
            }
        }
        let cyclic_vertices: Vec<String> = if order.len() == n {
            Vec::new()
        } else {
            let mut seen = vec![false; n];
            for &v in &order {
                seen[v] = true;
            }
            (0..n)
                .filter(|&i| !seen[i])
                .map(|i| vertices[i].name.clone())
                .collect()
        };

        clock_pins.sort_unstable();
        let mut is_clock_startpoint = vec![false; n];
        for &c in &clock_pins {
            is_clock_startpoint[c] = true;
        }
        Graph {
            lib,
            vertices,
            edges,
            arcs,
            is_clock_startpoint,
            incoming,
            clock_pins,
            data_pins,
            cyclic_vertices,
            unknown_cells,
            order,
        }
    }

    pub fn edge_count(&self) -> usize {
        self.edges.len()
    }

    fn arc_eval(&self, arc: usize, out_rise: bool, in_slew: f64, cap: f64) -> Option<(f64, f64)> {
        let a = self.arcs[arc].arc;
        let (d, t) = if out_rise {
            (&a.cell_rise, &a.rise_transition)
        } else {
            (&a.cell_fall, &a.fall_transition)
        };
        if d.is_empty() {
            return None;
        }
        Some((d.eval(in_slew, cap), t.eval(in_slew, cap)))
    }

    /// Propagate arrivals from register clock pins, and slews along the arcs
    /// that carry those arrivals.
    ///
    /// **Slew propagation is arrival-gated, and that is not an
    /// approximation** -- it is what reproduces OpenSTA. The first version of
    /// this engine merged slew from *every* incoming arc regardless of
    /// arrival, which is the textbook block-based rule; on `gcd` that made
    /// the launching flip-flop's `Q` slew `0.080163 ns` (the value its
    /// asynchronous `RESET_B -> Q` `clear` arc produces at zero input slew)
    /// where OpenSTA reports `0.073654 ns` (the `CLK -> Q` value), and the
    /// 6.5 ps error compounded down the path. `rst_n` is an unconstrained
    /// primary input: it launches no clock edge, so the `clear` arc carries
    /// no arrival, and OpenSTA does not let it set `Q`'s slew.
    ///
    /// Consequence, stated plainly: outside the register-launched cone every
    /// vertex keeps slew `0`. That is correct for the register-to-register
    /// question this spike asks and wrong for an input-to-register one, which
    /// would need `set_input_transition`/`set_input_delay` modelling this
    /// engine does not have.
    pub fn propagate(&self) -> Timing {
        let n = self.vertices.len();
        let mut slew = vec![[0.0f64; 2]; n];
        let mut arrival = vec![[NEG_INF; 2]; n];
        let mut prev: Vec<[Option<(usize, usize, usize)>; 2]> = vec![[None, None]; n];

        for &c in &self.clock_pins {
            arrival[c] = [0.0, 0.0];
        }

        for &v in &self.order {
            // Ideal clock: a register clock pin is held at arrival 0 / slew 0
            // regardless of what the clock net delivers, which is what
            // OpenSTA reports as "clock network delay (ideal)".
            if self.is_clock_startpoint[v] {
                continue;
            }
            let cap = self.vertices[v].load_cap;
            for &ei in &self.incoming[v] {
                let e = self.edges[ei];
                match e.kind {
                    EdgeKind::Net => {
                        for t in [FALL, RISE] {
                            if arrival[e.from][t] == NEG_INF {
                                continue;
                            }
                            if slew[e.from][t] > slew[v][t] {
                                slew[v][t] = slew[e.from][t];
                            }
                            if arrival[e.from][t] > arrival[v][t] {
                                arrival[v][t] = arrival[e.from][t];
                                prev[v][t] = Some((e.from, t, ei));
                            }
                        }
                    }
                    EdgeKind::Cell { arc } => {
                        let sense = self.arcs[arc].sense;
                        let is_edge_arc = matches!(self.arcs[arc].kind, ArcKind::Edge { .. });
                        for out_t in [FALL, RISE] {
                            for in_t in [FALL, RISE] {
                                // Arrival-gated: an arc whose input carries no
                                // arrival contributes neither arrival nor
                                // slew. See this method's doc comment.
                                let a = arrival[e.from][in_t];
                                if a == NEG_INF {
                                    continue;
                                }
                                if !is_edge_arc && !sense.allows(in_t == RISE, out_t == RISE) {
                                    continue;
                                }
                                // A `rising_edge` arc is launched by the
                                // clock's rising edge only.
                                if is_edge_arc {
                                    let want_rise = matches!(
                                        self.arcs[arc].kind,
                                        ArcKind::Edge { rising: true }
                                    );
                                    if (in_t == RISE) != want_rise {
                                        continue;
                                    }
                                }
                                let Some((d, out_slew)) = self.arc_eval(
                                    arc,
                                    out_t == RISE,
                                    slew[e.from][in_t],
                                    cap[out_t],
                                ) else {
                                    continue;
                                };
                                if out_slew > slew[v][out_t] {
                                    slew[v][out_t] = out_slew;
                                }
                                if a + d > arrival[v][out_t] {
                                    arrival[v][out_t] = a + d;
                                    prev[v][out_t] = Some((e.from, in_t, ei));
                                }
                            }
                        }
                    }
                }
            }
        }
        Timing {
            slew,
            arrival,
            prev,
        }
    }

    /// Recover the maximum-arrival path terminating at `(vertex, transition)`.
    fn trace(&self, t: &Timing, vertex: usize, tr: usize) -> Vec<PathNode> {
        let mut chain: Vec<(usize, usize)> = vec![(vertex, tr)];
        let mut cur = (vertex, tr);
        while let Some((pv, pt, _)) = t.prev[cur.0][cur.1] {
            chain.push((pv, pt));
            cur = (pv, pt);
            if chain.len() > self.vertices.len() + 2 {
                break;
            }
        }
        chain.reverse();
        let mut nodes = Vec::with_capacity(chain.len());
        let mut last_arrival = 0.0;
        for (i, &(v, tt)) in chain.iter().enumerate() {
            let arrival = t.arrival[v][tt];
            nodes.push(PathNode {
                pin: self.vertices[v].name.clone(),
                cell: self.vertices[v].cell.clone(),
                rise: tt == RISE,
                delay: if i == 0 { 0.0 } else { arrival - last_arrival },
                arrival,
                slew: t.slew[v][tt],
                load_cap: self.vertices[v].load_cap[tt],
            });
            last_arrival = arrival;
        }
        nodes
    }

    /// Setup time consumed at a register data pin, from its constraint table.
    fn setup_time(
        &self,
        arc: &TimingArc,
        clk_slew: f64,
        data_slew: f64,
        data_rise: bool,
    ) -> Option<f64> {
        let lut = if data_rise {
            &arc.rise_constraint
        } else {
            &arc.fall_constraint
        };
        if lut.is_empty() {
            return None;
        }
        Some(lut.eval(clk_slew, data_slew))
    }

    /// Report the worst `count` register-to-register paths, one per endpoint,
    /// worst first -- OpenSTA's `-group_path_count <count>
    /// -endpoint_path_count 1` shape.
    pub fn report(&self, t: &Timing, count: usize, period: Option<f64>) -> Vec<Path> {
        let mut ends: Vec<(usize, usize, f64)> = Vec::new();
        for (vid, _arc) in &self.data_pins {
            let (mut best_t, mut best) = (FALL, NEG_INF);
            for tr in [FALL, RISE] {
                if t.arrival[*vid][tr] > best {
                    best = t.arrival[*vid][tr];
                    best_t = tr;
                }
            }
            if best > NEG_INF {
                ends.push((*vid, best_t, best));
            }
        }
        ends.sort_by(|a, b| b.2.partial_cmp(&a.2).unwrap_or(std::cmp::Ordering::Equal));
        ends.truncate(count);

        let scale = self.lib.time_unit_ns;
        ends.iter()
            .map(|&(vid, tr, arrival)| {
                let arc = self
                    .data_pins
                    .iter()
                    .find(|(v, _)| *v == vid)
                    .map(|(_, a)| *a)
                    .expect("endpoint arc");
                let setup = self.setup_time(arc, 0.0, t.slew[vid][tr], tr == RISE);
                let nodes = self.trace(t, vid, tr);
                let (sp, spc) = nodes
                    .first()
                    .map(|n| (n.pin.clone(), n.cell.clone()))
                    .unwrap_or_default();
                let slack = match (period, setup) {
                    (Some(p), Some(s)) => Some((p - s - arrival) * scale),
                    (Some(p), None) => Some((p - arrival) * scale),
                    _ => None,
                };
                Path {
                    startpoint: sp,
                    startpoint_cell: spc,
                    endpoint: self.vertices[vid].name.clone(),
                    endpoint_cell: self.vertices[vid].cell.clone(),
                    arrival: arrival * scale,
                    setup: setup.map(|s| s * scale),
                    slack,
                    nodes: nodes
                        .into_iter()
                        .map(|mut n| {
                            n.delay *= scale;
                            n.arrival *= scale;
                            n.slew *= scale;
                            n.load_cap *= self.lib.cap_unit_pf;
                            n
                        })
                        .collect(),
                }
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::liberty::parse_library;

    const LIB: &str = include_str!("../tests/tiny.lib");

    fn build<'a>(lib: &'a Library, src: &str, top: &str) -> Graph<'a> {
        let nl = crate::netlist::parse_netlist(src, top).expect("netlist");
        Graph::build(lib, &nl)
    }

    #[test]
    fn identifies_clock_pins_and_data_pins() {
        let lib = parse_library(LIB);
        let src = r#"
            module t(clk, d, q);
              input clk; input d; output q; wire n;
              DFF r0 (.CLK(clk), .D(d), .Q(n));
              INV  i0 (.A(n), .Y(q));
            endmodule"#;
        let g = build(&lib, src, "t");
        assert_eq!(g.clock_pins.len(), 1);
        assert_eq!(g.vertices[g.clock_pins[0]].name, "r0/CLK");
        assert_eq!(g.data_pins.len(), 1);
        assert_eq!(g.vertices[g.data_pins[0].0].name, "r0/D");
        assert!(g.cyclic_vertices.is_empty());
        assert!(g.unknown_cells.is_empty());
    }

    #[test]
    fn reports_a_register_to_register_path_and_not_an_input_to_register_one() {
        let lib = parse_library(LIB);
        // r0 -> inv -> r1 is reg-to-reg; the primary input `d` also reaches
        // r1's D pin through a second inverter, and must NOT be reported.
        let src = r#"
            module t(clk, d, q);
              input clk; input d; output q;
              wire n0, n1, n2;
              DFF r0 (.CLK(clk), .D(d), .Q(n0));
              INV i0 (.A(n0), .Y(n1));
              DFF r1 (.CLK(clk), .D(n1), .Q(q));
            endmodule"#;
        let g = build(&lib, src, "t");
        let t = g.propagate();
        let paths = g.report(&t, 10, Some(10.0));
        assert_eq!(paths.len(), 1, "only the reg-launched endpoint is reported");
        let p = &paths[0];
        assert_eq!(p.startpoint, "r0/CLK");
        assert_eq!(p.endpoint, "r1/D");
        // r0/D is only reachable from the primary input `d`, which launches
        // no clock edge, so it has no arrival and is not reported.
        assert!(paths.iter().all(|p| p.endpoint != "r0/D"));
        assert!(p.arrival > 0.0);
        assert_eq!(p.nodes.first().map(|n| n.pin.as_str()), Some("r0/CLK"));
        assert_eq!(p.nodes.last().map(|n| n.pin.as_str()), Some("r1/D"));
        assert!(p.setup.is_some());
        assert!(p.slack.unwrap() < 10.0);
    }

    #[test]
    fn arrival_equals_the_sum_of_the_arc_delays_on_the_path() {
        let lib = parse_library(LIB);
        let src = r#"
            module t(clk, q);
              input clk; output q;
              wire n0, n1;
              DFF r0 (.CLK(clk), .D(n1), .Q(n0));
              INV i0 (.A(n0), .Y(n1));
              DFF r1 (.CLK(clk), .D(n1), .Q(q));
            endmodule"#;
        let g = build(&lib, src, "t");
        let t = g.propagate();
        let paths = g.report(&t, 1, None);
        let p = &paths[0];
        let sum: f64 = p.nodes.iter().map(|n| n.delay).sum();
        assert!((sum - p.arrival).abs() < 1e-12, "{sum} vs {}", p.arrival);
    }

    #[test]
    fn detects_a_combinational_cycle_instead_of_looping_forever() {
        let lib = parse_library(LIB);
        let src = r#"
            module t(y);
              output y; wire a, b;
              INV i0 (.A(a), .Y(b));
              INV i1 (.A(b), .Y(a));
              INV i2 (.A(b), .Y(y));
            endmodule"#;
        let g = build(&lib, src, "t");
        assert!(!g.cyclic_vertices.is_empty());
    }

    #[test]
    fn records_cells_missing_from_the_library() {
        let lib = parse_library(LIB);
        let src = "module t(y); output y; NOT_A_REAL_CELL i (.A(y)); endmodule";
        let g = build(&lib, src, "t");
        assert_eq!(g.unknown_cells, vec!["NOT_A_REAL_CELL".to_string()]);
    }

    #[test]
    fn load_capacitance_is_edge_dependent_and_sums_over_fanout() {
        let lib = parse_library(LIB);
        let src = r#"
            module t(clk, q0, q1);
              input clk; output q0; output q1; wire n;
              DFF r0 (.CLK(clk), .D(n), .Q(n));
              INV i0 (.A(n), .Y(q0));
              INV i1 (.A(n), .Y(q1));
            endmodule"#;
        let g = build(&lib, src, "t");
        let q = g.vertices.iter().find(|v| v.name == "r0/Q").expect("r0/Q");
        // Two INV `A` pins plus r0's own `D` pin (no edge caps -> falls back
        // to `capacitance`).
        assert!((q.load_cap[RISE] - (0.0021 * 2.0 + 0.002)).abs() < 1e-12);
        assert!((q.load_cap[FALL] - (0.0019 * 2.0 + 0.002)).abs() < 1e-12);
        assert!(q.load_cap[RISE] != q.load_cap[FALL]);
    }
}
