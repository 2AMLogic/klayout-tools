//! `klt-sta` — the spike's CLI.
//!
//! ```text
//! klt-sta --liberty <lib> --netlist <v> --top <name> [--period <ns>]
//!         [--paths <n>] [--repeat <k>] [--format json|text]
//! ```
//!
//! `--repeat <k>` re-runs graph construction and propagation `k` times inside
//! one process, reporting the per-iteration mean. That is the number issue
//! #809 actually turns on: it is the cost a Phase-3 optimization loop would
//! pay per candidate netlist with the liberty already parsed and resident,
//! and it is what the "wrapped OpenSTA" alternative must be compared against
//! (see `tests/corpus/sta/compare.py`, which measures the same quantity on
//! the OpenSTA side by keeping one `sta` process alive across iterations).

use std::process::ExitCode;
use std::time::Instant;

use klt_sta_native::graph::Graph;
use klt_sta_native::liberty::parse_library;
use klt_sta_native::netlist::parse_netlist;
use serde::Serialize;

#[derive(Serialize)]
struct NodeOut {
    pin: String,
    cell: String,
    edge: &'static str,
    delay_ns: f64,
    arrival_ns: f64,
    slew_ns: f64,
    load_cap_pf: f64,
}

#[derive(Serialize)]
struct PathOut {
    startpoint: String,
    startpoint_cell: String,
    endpoint: String,
    endpoint_cell: String,
    arrival_ns: f64,
    #[serde(skip_serializing_if = "Option::is_none")]
    library_setup_ns: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    slack_ns: Option<f64>,
    nodes: Vec<NodeOut>,
}

#[derive(Serialize)]
struct TimingsOut {
    liberty_parse_ms: f64,
    netlist_parse_ms: f64,
    /// Mean over `--repeat` iterations of graph build + propagate + report.
    analysis_ms: f64,
    analysis_iterations: usize,
    /// Per-iteration wall-clock, so a caller can discard the warm-up
    /// iteration exactly as it does for the OpenSTA side rather than
    /// comparing a warmed-up mean against a cold one.
    analysis_ms_per_iteration: Vec<f64>,
    total_ms: f64,
}

#[derive(Serialize)]
struct Report {
    engine: &'static str,
    design: String,
    liberty: String,
    cells: usize,
    library_cells: usize,
    registers: usize,
    graph_vertices: usize,
    graph_edges: usize,
    endpoints_reached: usize,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    unknown_cells: Vec<String>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    combinational_loop_pins: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    critical_path_ns: Option<f64>,
    paths: Vec<PathOut>,
    timings: TimingsOut,
}

struct Args {
    liberty: String,
    netlist: String,
    top: String,
    period: Option<f64>,
    paths: usize,
    repeat: usize,
    json: bool,
}

fn usage() -> String {
    "usage: klt-sta --liberty <file.lib> --netlist <file.v> --top <module> \
     [--period <ns>] [--paths <n>] [--repeat <k>] [--format json|text]"
        .to_string()
}

fn parse_args() -> Result<Args, String> {
    let mut a = Args {
        liberty: String::new(),
        netlist: String::new(),
        top: String::new(),
        period: None,
        paths: 1,
        repeat: 1,
        json: false,
    };
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let mut i = 0;
    while i < argv.len() {
        let need = |i: usize| -> Result<String, String> {
            argv.get(i + 1)
                .cloned()
                .ok_or_else(|| format!("{} needs a value\n{}", argv[i], usage()))
        };
        match argv[i].as_str() {
            "--liberty" => {
                a.liberty = need(i)?;
                i += 2;
            }
            "--netlist" => {
                a.netlist = need(i)?;
                i += 2;
            }
            "--top" => {
                a.top = need(i)?;
                i += 2;
            }
            "--period" => {
                a.period = Some(need(i)?.parse().map_err(|_| "--period must be a number")?);
                i += 2;
            }
            "--paths" => {
                a.paths = need(i)?.parse().map_err(|_| "--paths must be an integer")?;
                i += 2;
            }
            "--repeat" => {
                a.repeat = need(i)?
                    .parse()
                    .map_err(|_| "--repeat must be an integer")?;
                i += 2;
            }
            "--format" => {
                a.json = need(i)? == "json";
                i += 2;
            }
            "-h" | "--help" => return Err(usage()),
            other => return Err(format!("unknown argument `{other}`\n{}", usage())),
        }
    }
    if a.liberty.is_empty() || a.netlist.is_empty() || a.top.is_empty() {
        return Err(format!(
            "--liberty, --netlist and --top are required\n{}",
            usage()
        ));
    }
    if a.repeat == 0 {
        return Err("--repeat must be at least 1".to_string());
    }
    Ok(a)
}

fn main() -> ExitCode {
    let args = match parse_args() {
        Ok(a) => a,
        Err(e) => {
            eprintln!("{e}");
            return ExitCode::from(2);
        }
    };
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("klt-sta: {e}");
            ExitCode::FAILURE
        }
    }
}

fn ms(d: std::time::Duration) -> f64 {
    d.as_secs_f64() * 1e3
}

fn run(args: &Args) -> Result<(), String> {
    let t0 = Instant::now();

    let lib_text = std::fs::read_to_string(&args.liberty)
        .map_err(|e| format!("cannot read liberty `{}`: {e}", args.liberty))?;
    let t_lib = Instant::now();
    let lib = parse_library(&lib_text);
    let liberty_parse_ms = ms(t_lib.elapsed());
    if lib.cells.is_empty() {
        return Err(format!("`{}` yielded no cells", args.liberty));
    }

    let nl_text = std::fs::read_to_string(&args.netlist)
        .map_err(|e| format!("cannot read netlist `{}`: {e}", args.netlist))?;
    let t_nl = Instant::now();
    let nl = parse_netlist(&nl_text, &args.top).map_err(|e| e.to_string())?;
    let netlist_parse_ms = ms(t_nl.elapsed());

    // Repeat the part a Phase-3 optimization loop would repeat: rebuild the
    // graph from the (notionally changed) netlist and re-propagate. The
    // liberty stays parsed and resident, exactly as it would in-process.
    let t_an = Instant::now();
    let mut per_iteration = Vec::with_capacity(args.repeat);
    let mut last = None;
    for _ in 0..args.repeat {
        let t_i = Instant::now();
        let g = Graph::build(&lib, &nl);
        let t = g.propagate();
        let paths = g.report(&t, args.paths.max(1), args.period);
        per_iteration.push(ms(t_i.elapsed()));
        last = Some((
            g.vertices.len(),
            g.edge_count(),
            g.clock_pins.len(),
            g.cyclic_vertices.clone(),
            g.unknown_cells.clone(),
            g.data_pins
                .iter()
                .filter(|(v, _)| {
                    t.arrival[*v][0] > f64::NEG_INFINITY || t.arrival[*v][1] > f64::NEG_INFINITY
                })
                .count(),
            paths,
        ));
    }
    let analysis_ms = ms(t_an.elapsed()) / args.repeat as f64;
    let (nv, ne, nregs, cyclic, unknown, reached, paths) = last.expect("at least one iteration");

    let report = Report {
        engine: "klt-sta-native",
        design: args.top.clone(),
        liberty: lib.name.clone(),
        cells: nl.instances.len(),
        library_cells: lib.cells.len(),
        registers: nregs,
        graph_vertices: nv,
        graph_edges: ne,
        endpoints_reached: reached,
        unknown_cells: unknown,
        combinational_loop_pins: cyclic,
        critical_path_ns: paths.first().map(|p| p.arrival),
        paths: paths
            .iter()
            .map(|p| PathOut {
                startpoint: p.startpoint.clone(),
                startpoint_cell: p.startpoint_cell.clone(),
                endpoint: p.endpoint.clone(),
                endpoint_cell: p.endpoint_cell.clone(),
                arrival_ns: p.arrival,
                library_setup_ns: p.setup,
                slack_ns: p.slack,
                nodes: p
                    .nodes
                    .iter()
                    .map(|n| NodeOut {
                        pin: n.pin.clone(),
                        cell: n.cell.clone(),
                        edge: if n.rise { "rise" } else { "fall" },
                        delay_ns: n.delay,
                        arrival_ns: n.arrival,
                        slew_ns: n.slew,
                        load_cap_pf: n.load_cap,
                    })
                    .collect(),
            })
            .collect(),
        timings: TimingsOut {
            liberty_parse_ms,
            netlist_parse_ms,
            analysis_ms,
            analysis_iterations: args.repeat,
            analysis_ms_per_iteration: per_iteration,
            total_ms: ms(t0.elapsed()),
        },
    };

    if args.json {
        println!(
            "{}",
            serde_json::to_string_pretty(&report).map_err(|e| e.to_string())?
        );
    } else {
        print_text(&report);
    }
    Ok(())
}

fn print_text(r: &Report) {
    println!(
        "{} — {} cells, {} registers, {} graph vertices / {} edges",
        r.design, r.cells, r.registers, r.graph_vertices, r.graph_edges
    );
    if !r.unknown_cells.is_empty() {
        println!(
            "  WARNING: cells absent from the liberty: {:?}",
            r.unknown_cells
        );
    }
    if !r.combinational_loop_pins.is_empty() {
        println!(
            "  WARNING: {} pins on combinational loops (excluded)",
            r.combinational_loop_pins.len()
        );
    }
    for p in &r.paths {
        println!();
        println!("Startpoint: {} ({})", p.startpoint, p.startpoint_cell);
        println!("Endpoint:   {} ({})", p.endpoint, p.endpoint_cell);
        println!(
            "{:>10} {:>11} {:>11} {:>11}   Description",
            "Cap", "Slew", "Delay", "Time"
        );
        for n in &p.nodes {
            println!(
                "{:>10.6} {:>11.6} {:>11.6} {:>11.6} {} {} ({})",
                n.load_cap_pf,
                n.slew_ns,
                n.delay_ns,
                n.arrival_ns,
                if n.edge == "rise" { "^" } else { "v" },
                n.pin,
                n.cell
            );
        }
        println!("{:>46.6}   data arrival time", p.arrival_ns);
        if let Some(s) = p.library_setup_ns {
            println!("{s:>46.6}   library setup time");
        }
        if let Some(s) = p.slack_ns {
            println!("{s:>46.6}   slack");
        }
    }
    println!();
    println!(
        "timing: liberty {:.1} ms, netlist {:.1} ms, analysis {:.3} ms (mean of {}), total {:.1} ms",
        r.timings.liberty_parse_ms,
        r.timings.netlist_parse_ms,
        r.timings.analysis_ms,
        r.timings.analysis_iterations,
        r.timings.total_ms
    );
}
