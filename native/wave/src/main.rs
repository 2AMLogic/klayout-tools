//! `klt-wave` -- the standalone CLI backing `klt wave build` / `klt wave
//! query` (2AMLogic/klayout-tools, Epic #1585). Not (yet) a `klt`
//! subcommand's own in-process implementation -- a Python wrapper module
//! invoking this binary as a subprocess (mirroring `native/techmap`'s own
//! `klt-techmap <request.json>` pattern) is issue #1601's job (Epic #1585
//! Phase 2c), not this one's.
//!
//! Usage: `klt-wave build <request.json> [--format text|json]` -- a
//! `klt.wave_build.request/1` document
//! (`docs/design/waveform-query-contract-spike.md` section 4). Prints the
//! full `klt.wave_build.response/1` JSON (including `provenance`, using
//! this crate's own `CARGO_PKG_VERSION` as `klt_version` --  a Python
//! wrapper layer, once #1601 lands, may override that field with the
//! installed `klayout-tools` package's own version instead, the same way
//! `klayout_tools/techmap.py` post-processes `klt-techmap`'s own response)
//! to stdout on success. `--format text` (the default) prints a short
//! human-readable summary instead.
//!
//! `klt-wave query <request.json>` is not yet implemented by this issue
//! -- see issue #1599 (Epic #1585 Phase 2a), which adds it to this same
//! binary.
//!
//! Exit codes (contract section 4's table, `build`'s own -- `query`'s
//! table additionally defines exit `3`, not relevant to this binary
//! until #1599 lands):
//!   0 -- store built successfully.
//!   1 -- failed to build (unreadable/malformed trace, unresolvable
//!        `trace.format`, a named `clock.signal`/`reset.signal`/
//!        `signals` entry absent from the trace, empty `signals`, an
//!        unwritable `store.path`).
//!   2 -- usage error (missing argument, bad `--format` value, or an
//!        unreadable/malformed request file).

use std::env;
use std::process::ExitCode;

use klt_wave_native::build;

#[derive(Debug)]
struct Args {
    mode: String,
    request_path: std::path::PathBuf,
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
                request_path = Some(std::path::PathBuf::from(other));
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
            .map(|r| format!("{}ns (cycle {})", r.time_ns, r.cycle.map_or(0, |c| c)))
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
        "query" => {
            eprintln!(
                "error: klt-wave query is not yet implemented in this build \
                 (see issue #1599, Epic #1585 Phase 2a)"
            );
            ExitCode::from(1)
        }
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
