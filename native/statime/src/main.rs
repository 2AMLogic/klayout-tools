//! CLI for the STA spike: parse a liberty file + a Yosys-mapped structural
//! netlist and report the worst critical path(s).
//!
//! ```text
//! klt-statime critical-path <netlist.v> <liberty.lib> --top <name> \
//!     [--input-transition-ns 0.05] [--output-load-pf 0.03] [--json out.json]
//! ```

use std::fs;
use std::process::ExitCode;
use std::time::Instant;

use klt_statime_native::{liberty, netlist, sta};

struct Args {
    netlist_path: String,
    liberty_path: String,
    top: String,
    input_transition_ns: f64,
    output_load_pf: f64,
    json_out: Option<String>,
}

fn parse_args() -> Result<Args, String> {
    let raw: Vec<String> = std::env::args().collect();
    if raw.len() < 2 || raw[1] != "critical-path" {
        return Err("usage: klt-statime critical-path <netlist.v> <liberty.lib> --top <name> [--input-transition-ns N] [--output-load-pf N] [--json path]".to_string());
    }
    let mut positional = Vec::new();
    let mut top = None;
    let mut input_transition_ns = 0.05;
    let mut output_load_pf = 0.03;
    let mut json_out = None;
    let mut i = 2;
    while i < raw.len() {
        match raw[i].as_str() {
            "--top" => {
                top = raw.get(i + 1).cloned();
                i += 2;
            }
            "--input-transition-ns" => {
                input_transition_ns = raw.get(i + 1).and_then(|s| s.parse().ok()).unwrap_or(0.05);
                i += 2;
            }
            "--output-load-pf" => {
                output_load_pf = raw.get(i + 1).and_then(|s| s.parse().ok()).unwrap_or(0.03);
                i += 2;
            }
            "--json" => {
                json_out = raw.get(i + 1).cloned();
                i += 2;
            }
            other => {
                positional.push(other.to_string());
                i += 1;
            }
        }
    }
    if positional.len() != 2 {
        return Err("expected exactly two positional args: <netlist.v> <liberty.lib>".to_string());
    }
    let top = top.ok_or("--top <module> is required")?;
    Ok(Args {
        netlist_path: positional[0].clone(),
        liberty_path: positional[1].clone(),
        top,
        input_transition_ns,
        output_load_pf,
        json_out,
    })
}

fn run() -> Result<(), String> {
    let args = parse_args()?;

    let t0 = Instant::now();
    let liberty_src =
        fs::read_to_string(&args.liberty_path).map_err(|e| format!("read liberty: {e}"))?;
    let lib = liberty::parse(&liberty_src).map_err(|e| e.to_string())?;
    let liberty_parse_ms = t0.elapsed().as_secs_f64() * 1000.0;

    let t1 = Instant::now();
    let netlist_src =
        fs::read_to_string(&args.netlist_path).map_err(|e| format!("read netlist: {e}"))?;
    let module = netlist::parse(&netlist_src, &args.top).map_err(|e| e.to_string())?;
    let netlist_parse_ms = t1.elapsed().as_secs_f64() * 1000.0;

    let t2 = Instant::now();
    let cfg = sta::StaConfig {
        input_transition_ns: args.input_transition_ns,
        output_load_pf: args.output_load_pf,
    };
    let result = sta::analyze(&module, &lib, &cfg).map_err(|e| e.to_string())?;
    let analyze_ms = t2.elapsed().as_secs_f64() * 1000.0;

    println!("top: {}", result.top);
    println!("cells: {}  nets: {}", result.num_cells, result.num_nets);
    println!(
        "timings: liberty_parse={:.2}ms netlist_parse={:.2}ms analyze={:.2}ms total={:.2}ms",
        liberty_parse_ms,
        netlist_parse_ms,
        analyze_ms,
        liberty_parse_ms + netlist_parse_ms + analyze_ms
    );
    println!();
    println!(
        "worst path (any): {} -> {}  delay={:.4} ns ({:.1} ps)",
        result.worst_path.startpoint,
        result.worst_path.endpoint,
        result.worst_path.delay_ns,
        result.worst_path.delay_ns * 1000.0
    );
    if let Some(r2r) = &result.worst_reg_to_reg_path {
        println!(
            "worst path (reg->reg): {} -> {}  delay={:.4} ns ({:.1} ps)",
            r2r.startpoint,
            r2r.endpoint,
            r2r.delay_ns,
            r2r.delay_ns * 1000.0
        );
    } else {
        println!("worst path (reg->reg): none (no registers in design)");
    }

    if let Some(path) = &args.json_out {
        #[derive(serde::Serialize)]
        struct Output<'a> {
            result: &'a sta::StaResult,
            timings_ms: TimingsMs,
        }
        #[derive(serde::Serialize)]
        struct TimingsMs {
            liberty_parse: f64,
            netlist_parse: f64,
            analyze: f64,
            total: f64,
        }
        let out = Output {
            result: &result,
            timings_ms: TimingsMs {
                liberty_parse: liberty_parse_ms,
                netlist_parse: netlist_parse_ms,
                analyze: analyze_ms,
                total: liberty_parse_ms + netlist_parse_ms + analyze_ms,
            },
        };
        let json = serde_json::to_string_pretty(&out).map_err(|e| e.to_string())?;
        fs::write(path, json).map_err(|e| format!("write json: {e}"))?;
    }

    Ok(())
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("error: {e}");
            ExitCode::FAILURE
        }
    }
}
