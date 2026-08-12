//! `klt-techmap` -- the standalone CLI for issue #874's technology-mapping
//! crate. Not (yet) a `klt` subcommand -- per
//! `docs/design/synth-techmap-stage-contract.md` section 1, wiring this
//! into `klt synthesize`/a future `klt synth` is left to a follow-on issue;
//! #875 wires **the equivalence gate** in from the Python side
//! (`klayout_tools.techmap.run_techmap`, `--verify-equivalence`), invoking
//! this same binary as a subprocess (mirroring `native/statime`'s own
//! `klt-statime critical-path` CLI at the equivalent point in that issue)
//! rather than duplicating the mapping algorithm in Python. This binary
//! also backs the corpus-benchmark harness
//! (`tests/corpus/techmap/compare.py`).
//!
//! Usage: `klt-techmap <request.json>` -- a `klt.synth.techmap.request/1`
//! document (section 6). Prints the `klt.synth.techmap.response/1` JSON to
//! stdout on success -- including, since #875, the additive
//! `generic_netlist_verilog_path` field (the pre-mapping generic netlist
//! re-emitted as behavioral Verilog, `verilog::write_generic`) alongside
//! `mapped_netlist_path`, so a caller has both sides ready to hand to `klt
//! equiv` without a separate conversion step.
//!
//! Exit codes (contract section 6):
//!   0 -- mapping succeeded.
//!   1 -- failed to map (malformed generic netlist, an unrealised generic
//!        cell, unreadable/unparseable liberty).
//!   2 -- usage error (missing argument / unreadable request file).

use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use klt_techmap_native::{celllib, genlist, liberty, map, request, verilog};

fn resolve(base_dir: &Path, path_str: &str) -> PathBuf {
    let p = Path::new(path_str);
    if p.is_absolute() {
        p.to_path_buf()
    } else {
        base_dir.join(p)
    }
}

fn run() -> Result<request::Response, (u8, String)> {
    let args: Vec<String> = env::args().collect();
    if args.len() != 2 {
        return Err((
            2,
            format!(
                "usage: {} <request.json>",
                args.first().map(|s| s.as_str()).unwrap_or("klt-techmap")
            ),
        ));
    }
    let request_path = Path::new(&args[1]);
    let request_text = fs::read_to_string(request_path).map_err(|e| {
        (
            2,
            format!("failed to read request '{}': {e}", request_path.display()),
        )
    })?;
    let req: request::Request = serde_json::from_str(&request_text)
        .map_err(|e| (2, format!("malformed request JSON: {e}")))?;

    let base_dir = request_path.parent().unwrap_or_else(|| Path::new("."));
    let netlist_path = resolve(base_dir, &req.generic_netlist);
    let liberty_path = resolve(base_dir, &req.liberty);

    let netlist_text = fs::read_to_string(&netlist_path).map_err(|e| {
        (
            1,
            format!(
                "failed to read generic netlist '{}': {e}",
                netlist_path.display()
            ),
        )
    })?;
    let netlist = genlist::parse(&netlist_text).map_err(|e| (1, format!("{e}")))?;

    let liberty_text = fs::read_to_string(&liberty_path).map_err(|e| {
        (
            1,
            format!("failed to read liberty '{}': {e}", liberty_path.display()),
        )
    })?;
    let lib = liberty::parse(&liberty_text).map_err(|e| (1, format!("{e}")))?;
    let cell_lib = celllib::build(&lib);

    // A simple, stated v1 heuristic: no delay target -> pure area
    // minimisation; a delay target present -> bias cell selection toward
    // lower intrinsic delay. See celllib::best_comb's own docs for why
    // this is not a full STA-driven score.
    const TECHMAP_DELAY_WEIGHT: f64 = 50.0;
    let delay_weight = if req.constraints.clock_period_ns.is_some() {
        TECHMAP_DELAY_WEIGHT
    } else {
        0.0
    };

    let mapped =
        map::map_design(&netlist, &cell_lib, delay_weight).map_err(|e| (1, format!("{e}")))?;

    let out_dir = netlist_path.parent().unwrap_or_else(|| Path::new("."));
    let out_path = out_dir.join(format!("{}_mapped.v", netlist.top));
    let verilog_text =
        verilog::write(&netlist, &mapped.instances).map_err(|e| (1, format!("{e}")))?;
    fs::write(&out_path, verilog_text).map_err(|e| {
        (
            1,
            format!(
                "failed to write mapped netlist '{}': {e}",
                out_path.display()
            ),
        )
    })?;
    let out_path_abs = fs::canonicalize(&out_path).unwrap_or(out_path);

    // Issue #875: the same generic netlist, re-emitted as self-contained
    // behavioral Verilog -- `klt equiv`'s own `gold` side against
    // `mapped_netlist_path`'s `gate` side.
    let generic_out_path = out_dir.join(format!("{}_generic.v", netlist.top));
    let generic_verilog_text = verilog::write_generic(&netlist).map_err(|e| (1, format!("{e}")))?;
    fs::write(&generic_out_path, generic_verilog_text).map_err(|e| {
        (
            1,
            format!(
                "failed to write generic-netlist Verilog '{}': {e}",
                generic_out_path.display()
            ),
        )
    })?;
    let generic_out_path_abs = fs::canonicalize(&generic_out_path).unwrap_or(generic_out_path);

    Ok(request::Response {
        schema_version: 1,
        top: netlist.top.clone(),
        status: "ok".to_string(),
        instance_count: mapped.instance_count,
        area_um2: mapped.area_um2,
        instance_counts_by_type: mapped.instance_counts_by_type,
        timing: None,
        mapped_netlist_path: out_path_abs.display().to_string(),
        generic_netlist_verilog_path: generic_out_path_abs.display().to_string(),
    })
}

fn main() -> ExitCode {
    match run() {
        Ok(resp) => {
            println!("{}", serde_json::to_string_pretty(&resp).unwrap());
            ExitCode::SUCCESS
        }
        Err((code, msg)) => {
            eprintln!("error: {msg}");
            ExitCode::from(code)
        }
    }
}
