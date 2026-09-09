//! `klt-wave` -- the standalone CLI for issue #1599's FST waveform query
//! engine crate (2AMLogic/klayout-tools, Epic #1585 Phase 2a). Not (yet) a
//! `klt` subcommand -- wiring `klt wave query`/`klt wave build` into the
//! Python `klt` CLI (a subprocess call, mirroring
//! `klayout_tools.techmap.run_techmap`'s own invocation of `klt-techmap`)
//! is left to a follow-on issue, matching `native/techmap/src/main.rs`'s
//! own documented precedent.
//!
//! Usage: `klt-wave query <request.json> [--format text|json]` -- a
//! `klt.wave_query.request/1` document
//! (docs/design/waveform-query-contract-spike.md section 5). Prints the
//! `klt.wave_query.response/1` JSON to stdout on success.
//!
//! Exit codes (contract section 5):
//!   0 -- every op ran and every declared predicate (if any) is satisfied.
//!   1 -- failed to run (bad request, unreadable/corrupt store, an op
//!        naming a signal absent from the store, cycle addressing against a
//!        store with no clock declared, a predicate on an op that does not
//!        define one, or an empty `ops` array).
//!   2 -- usage error (missing argument / unreadable request file / bad
//!        `--format` value).
//!   3 -- every op ran successfully, but at least one declared predicate
//!        was not satisfied.

use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use sha2::{Digest, Sha256};

use klt_wave_native::cache::ColumnCache;
use klt_wave_native::query;
use klt_wave_native::request::{InputRef, Provenance, QueryRequest, QueryResponse, StoreRef};

struct Args {
    request_path: PathBuf,
    format: String,
}

fn program_name() -> String {
    env::args().next().unwrap_or_else(|| "klt-wave".to_string())
}

fn parse_args() -> Result<Args, (u8, String)> {
    let argv: Vec<String> = env::args().collect();
    if argv.len() < 2 || argv[1] != "query" {
        return Err((
            2,
            format!(
                "usage: {} query <request.json> [--format text|json]",
                program_name()
            ),
        ));
    }
    let mut request_path: Option<PathBuf> = None;
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
        request_path,
        format,
    })
}

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
fn run(args: &Args) -> Result<(QueryResponse, u8), (u8, String)> {
    let request_text = fs::read_to_string(&args.request_path).map_err(|e| {
        (
            2,
            format!(
                "failed to read request '{}': {e}",
                args.request_path.display()
            ),
        )
    })?;
    let req: QueryRequest = serde_json::from_str(&request_text)
        .map_err(|e| (2, format!("malformed request JSON: {e}")))?;

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

fn print_text(resp: &QueryResponse) {
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

fn main() -> ExitCode {
    let args = match parse_args() {
        Ok(a) => a,
        Err((code, msg)) => {
            eprintln!("{}: {msg}", program_name());
            return ExitCode::from(code);
        }
    };
    match run(&args) {
        Ok((resp, code)) => {
            if args.format == "json" {
                println!("{}", serde_json::to_string_pretty(&resp).unwrap());
            } else {
                print_text(&resp);
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
                eprintln!("{}: {msg}", program_name());
            }
            ExitCode::from(code)
        }
    }
}
