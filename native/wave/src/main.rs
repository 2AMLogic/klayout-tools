//! `klt-wave` -- the standalone CLI backing `klt wave build` / `klt wave
//! query` (2AMLogic/klayout-tools, Epic #1585). Not (yet) a `klt`
//! subcommand's own in-process implementation -- a Python wrapper module
//! invoking this binary as a subprocess (mirroring `native/techmap`'s own
//! `klt-techmap <request.json>` pattern, `klayout_tools.techmap.run_techmap`)
//! is issue #1601's job (Epic #1585 Phase 2c), not this crate's, matching
//! `native/techmap/src/main.rs`'s own documented precedent.
//!
//! Usage:
//!
//! - `klt-wave build <request.json> [--format text|json]` -- a
//!   `klt.wave_build.request/1` document
//!   (`docs/design/waveform-query-contract-spike.md` section 4). Prints the
//!   full `klt.wave_build.response/1` JSON (including `provenance`, using
//!   this crate's own `CARGO_PKG_VERSION` as `klt_version` -- a Python
//!   wrapper layer, once #1601 lands, may override that field with the
//!   installed `klayout-tools` package's own version instead, the same way
//!   `klayout_tools/techmap.py` post-processes `klt-techmap`'s own response)
//!   to stdout on success. `--format text` (the default) prints a short
//!   human-readable summary instead.
//! - `klt-wave query <request.json> [--format text|json]` -- a
//!   `klt.wave_query.request/1` document (contract section 5). Prints the
//!   `klt.wave_query.response/1` JSON to stdout on success.
//!
//! Exit codes (contract sections 4 and 5):
//!   0 -- `build`: store built successfully. `query`: every op ran and
//!        every declared predicate (if any) is satisfied.
//!   1 -- `build`: failed to build (unreadable/malformed trace,
//!        unresolvable `trace.format`, a named `clock.signal`/
//!        `reset.signal`/`signals` entry absent from the trace, empty
//!        `signals`, an unwritable `store.path`). `query`: failed to run
//!        (unreadable/malformed request file, bad request, unreadable/
//!        corrupt store, an op naming a signal absent from the store, cycle
//!        addressing against a store with no clock declared, a predicate on
//!        an op that does not define one, or an empty `ops` array).
//!   2 -- usage error (missing argument, unknown mode, bad `--format`
//!        value; for `build`, also an unreadable/malformed request file).
//!   3 -- `query` only: every op ran successfully, but at least one
//!        declared predicate was not satisfied.

use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use sha2::{Digest, Sha256};

use klt_wave_native::build;
use klt_wave_native::cache::ColumnCache;
use klt_wave_native::query;
use klt_wave_native::request::{InputRef, Provenance, QueryRequest, QueryResponse, StoreRef};

#[derive(Debug)]
struct Args {
    mode: String,
    request_path: PathBuf,
    format: String,
}

fn parse_args(argv: &[String]) -> Result<Args, (u8, String)> {
    if argv.len() < 3 {
        return Err((
            2,
            "usage: <build|query> <request.json> [--format text|json]".to_string(),
        ));
    }
    let mode = argv[1].clone();
    let mut request_path = None;
    let mut format = "text".to_string();
    let mut i = 2;
    while i < argv.len() {
        match argv[i].as_str() {
            "--format" => {
                i += 1;
                let v = argv
                    .get(i)
                    .ok_or_else(|| (2, "missing value for --format".to_string()))?;
                if v != "text" && v != "json" {
                    return Err((
                        2,
                        format!("invalid --format value '{v}': expected text or json"),
                    ));
                }
                format = v.clone();
            }
            other if other.starts_with("--") => {
                return Err((2, format!("unknown flag '{other}'")));
            }
            other => {
                if request_path.is_some() {
                    return Err((2, format!("unexpected argument '{other}'")));
                }
                request_path = Some(PathBuf::from(other));
            }
        }
        i += 1;
    }
    let request_path =
        request_path.ok_or_else(|| (2, "missing <request.json> argument".to_string()))?;
    Ok(Args {
        mode,
        request_path,
        format,
    })
}

// ---------------------------------------------------------------------------
// `query` (issue #1599, Phase 2a)
// ---------------------------------------------------------------------------

fn resolve(base_dir: &Path, path_str: &str) -> PathBuf {
    let p = Path::new(path_str);
    if p.is_absolute() {
        p.to_path_buf()
    } else {
        base_dir.join(p)
    }
}

fn sha256_hex(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("sha256:{:x}", hasher.finalize())
}

/// Returns `(response, exit_code)` on a run that at least executed to
/// completion (`exit_code` is `0` or `3`); an `Err` is a run that failed to
/// execute at all (exit `1`, per contract section 5's partial-failure
/// design: one bad op fails the whole batch, no envelope).
fn run_query(args: &Args) -> Result<(QueryResponse, u8), (u8, String)> {
    let request_text = fs::read_to_string(&args.request_path).map_err(|e| {
        (
            1,
            format!(
                "failed to read request '{}': {e}",
                args.request_path.display()
            ),
        )
    })?;
    let req: QueryRequest = serde_json::from_str(&request_text)
        .map_err(|e| (1, format!("malformed request JSON: {e}")))?;

    if req.ops.is_empty() {
        return Err((1, "ops must be non-empty".to_string()));
    }

    let request_dir = args
        .request_path
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from("."));
    let store_path = resolve(&request_dir, &req.store);

    let store_bytes = fs::read(&store_path).map_err(|e| {
        (
            1,
            format!("failed to read store '{}': {e}", store_path.display()),
        )
    })?;
    let store_hash = sha256_hex(&store_bytes);

    let cache = ColumnCache::load_from_file(&store_path).ok_or_else(|| {
        (
            1,
            format!("'{}' is not a readable FST store", store_path.display()),
        )
    })?;

    let mut results = Vec::with_capacity(req.ops.len());
    let mut all_satisfied = true;
    for op in &req.ops {
        let outcome = query::run_op(&cache, &request_dir, op).map_err(|e| (1, e))?;
        if let Some(false) = outcome.satisfied {
            all_satisfied = false;
        }
        results.push(outcome.result);
    }

    let status = if all_satisfied { "ok" } else { "unsatisfied" };
    let exit_code = if all_satisfied { 0 } else { 3 };

    let response = QueryResponse {
        schema_version: 1,
        store: StoreRef {
            path: req.store.clone(),
            content_hash: store_hash.clone(),
        },
        status: status.to_string(),
        results,
        provenance: Provenance {
            klt_version: env!("CARGO_PKG_VERSION").to_string(),
            klayout_version: None,
            pdk: None,
            deck: None,
            input: InputRef {
                content_hash: store_hash,
            },
        },
    };
    Ok((response, exit_code))
}

fn print_query_text(resp: &QueryResponse) {
    println!("store: {} (status: {})", resp.store.path, resp.status);
    for result in &resp.results {
        let op = result.get("op").and_then(|v| v.as_str()).unwrap_or("?");
        let signal = result.get("signal").and_then(|v| v.as_str());
        let satisfied = result.get("satisfied").and_then(|v| v.as_bool());
        let mut line = format!("  {op}");
        if let Some(s) = signal {
            line.push_str(&format!(" {s}"));
        }
        for key in ["value", "found", "count", "stuck", "diverges", "truncated"] {
            if let Some(v) = result.get(key) {
                line.push_str(&format!(" {key}={v}"));
            }
        }
        if let Some(sat) = satisfied {
            line.push_str(if sat {
                " [satisfied]"
            } else {
                " [unsatisfied]"
            });
        }
        println!("{line}");
    }
}

// ---------------------------------------------------------------------------
// `build` (issue #1600, Phase 2b)
// ---------------------------------------------------------------------------

fn print_build_text(resp: &build::Response) {
    println!(
        "store: {} ({} bytes, {})",
        resp.store.path, resp.store.size_bytes, resp.store.content_hash
    );
    println!(
        "trace: {} ({}, {} bytes)",
        resp.trace.path, resp.trace.format, resp.trace.size_bytes
    );
    if let Some(clock) = &resp.clock {
        let period = clock
            .period_ns
            .map(|p| format!("{p}ns"))
            .unwrap_or_else(|| "unknown".to_string());
        println!("clock: {} ({}, period {period})", clock.signal, clock.edge);
    }
    if let Some(reset) = &resp.reset {
        let release = reset
            .release
            .map(|r| format!("{}ns (cycle {})", r.time_ns, r.cycle.unwrap_or(0)))
            .unwrap_or_else(|| "not observed".to_string());
        println!(
            "reset: {} ({}, release {release})",
            reset.signal, reset.active
        );
    }
    println!(
        "timescale: {} {}",
        resp.timescale.value, resp.timescale.unit
    );
    println!(
        "time range: {}ns .. {}ns",
        resp.time_range.from.time_ns, resp.time_range.to.time_ns
    );
    println!(
        "signals: {}, value changes: {}",
        resp.signal_count, resp.value_change_count
    );
}

fn main() -> ExitCode {
    let argv: Vec<String> = env::args().collect();
    let program = argv.first().map(|s| s.as_str()).unwrap_or("klt-wave");

    let args = match parse_args(&argv) {
        Ok(a) => a,
        Err((code, msg)) => {
            eprintln!("{program}: {msg}");
            return ExitCode::from(code);
        }
    };

    match args.mode.as_str() {
        "build" => match build::run(&args.request_path) {
            Ok(response) => {
                if args.format == "json" {
                    println!("{}", serde_json::to_string_pretty(&response).unwrap());
                } else {
                    print_build_text(&response);
                }
                ExitCode::SUCCESS
            }
            Err((code, msg)) => {
                if args.format == "json" {
                    let err = serde_json::json!({
                        "schema_version": 1,
                        "error": { "command": "wave build", "message": msg },
                    });
                    eprintln!("{}", serde_json::to_string_pretty(&err).unwrap());
                } else {
                    eprintln!("error: {msg}");
                }
                ExitCode::from(code)
            }
        },
        "query" => match run_query(&args) {
            Ok((resp, code)) => {
                if args.format == "json" {
                    println!("{}", serde_json::to_string_pretty(&resp).unwrap());
                } else {
                    print_query_text(&resp);
                }
                ExitCode::from(code)
            }
            Err((code, msg)) => {
                if args.format == "json" {
                    let err = serde_json::json!({
                        "schema_version": 1,
                        "error": { "command": "wave query", "message": msg },
                    });
                    eprintln!("{}", serde_json::to_string_pretty(&err).unwrap());
                } else {
                    eprintln!("{program}: {msg}");
                }
                ExitCode::from(code)
            }
        },
        other => {
            eprintln!(
                "{program}: unknown mode '{other}' (usage: {program} <build|query> \
                 <request.json> [--format text|json])"
            );
            ExitCode::from(2)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;

    #[test]
    fn parse_args_defaults_format_to_text() {
        let argv = vec![
            "klt-wave".to_string(),
            "build".to_string(),
            "req.json".to_string(),
        ];
        let args = parse_args(&argv).unwrap();
        assert_eq!(args.mode, "build");
        assert_eq!(args.format, "text");
        assert_eq!(args.request_path, Path::new("req.json"));
    }

    #[test]
    fn parse_args_accepts_format_json() {
        let argv = vec![
            "klt-wave".to_string(),
            "build".to_string(),
            "req.json".to_string(),
            "--format".to_string(),
            "json".to_string(),
        ];
        let args = parse_args(&argv).unwrap();
        assert_eq!(args.format, "json");
    }

    #[test]
    fn parse_args_rejects_bad_format_value() {
        let argv = vec![
            "klt-wave".to_string(),
            "build".to_string(),
            "req.json".to_string(),
            "--format".to_string(),
            "xml".to_string(),
        ];
        let err = parse_args(&argv).unwrap_err();
        assert_eq!(err.0, 2);
    }

    #[test]
    fn parse_args_requires_request_path() {
        let argv = vec!["klt-wave".to_string(), "build".to_string()];
        let err = parse_args(&argv).unwrap_err();
        assert_eq!(err.0, 2);
    }
}
