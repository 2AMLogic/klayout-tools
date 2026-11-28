//! `klt wave build` orchestration: request/response JSON shapes and the
//! top-level `run` entry point `main.rs` calls. Written fresh -- the
//! request/response contract itself
//! (`docs/design/waveform-query-contract-spike.md` section 4) is a `klt`-
//! native shape, not a port of any upstream `bwave` CLI surface (decision
//! record section 10, `main.rs`/`output.rs` rows: "written fresh").

use std::fs::{self, File};
use std::io::{BufReader, Read};
use std::path::{Path, PathBuf};

use rustc_hash::FxHashSet;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::build_fst;
use crate::build_vcd;
use crate::cycle::Edge;
use crate::fst_convert::timescale_from_exponent;
use crate::store_writer::{DeclSignal, StoreWriter};

/// `(exit_code, message)` -- exit code `1` for an application-level
/// failure, `2` for a usage error, matching `klt-techmap`'s own
/// `main.rs` convention.
pub type BuildError = (u8, String);

#[derive(Deserialize)]
struct Request {
    #[serde(default)]
    #[allow(dead_code)]
    schema: Option<String>,
    trace: TraceReq,
    #[serde(default)]
    clock: Option<ClockReq>,
    #[serde(default)]
    reset: Option<ResetReq>,
    #[serde(default)]
    signals: Option<Vec<String>>,
    store: StoreReq,
}

#[derive(Deserialize)]
struct TraceReq {
    path: String,
    #[serde(default)]
    format: Option<String>,
}

#[derive(Deserialize)]
struct ClockReq {
    signal: String,
    #[serde(default)]
    edge: Option<String>,
}

#[derive(Deserialize)]
struct ResetReq {
    signal: String,
    #[serde(default)]
    active: Option<String>,
}

#[derive(Deserialize)]
struct StoreReq {
    path: String,
}

#[derive(Serialize, Debug)]
pub struct Response {
    pub schema_version: u32,
    pub trace: TraceResp,
    pub store: StoreResp,
    pub clock: Option<ClockResp>,
    pub reset: Option<ResetResp>,
    pub timescale: TimescaleResp,
    pub time_range: TimeRangeResp,
    pub signal_count: usize,
    pub value_change_count: u64,
    pub provenance: Provenance,
}

/// Shared `provenance` block (`docs/json-contract.md`), with `pdk`/`deck`/
/// `klayout_version` always `null` per contract section 2 -- `klt wave`
/// resolves neither a PDK nor a rule deck, and never invokes the
/// `klayout`/`pya` engine. `klt_version` here is this crate's own
/// `CARGO_PKG_VERSION` (matching issue #1599's `query` verb's own
/// `main.rs` convention); a future Python wrapper (issue #1601) may
/// override it with the installed `klayout-tools` package's own version,
/// the same way `klayout_tools/techmap.py` post-processes `klt-techmap`'s
/// response.
#[derive(Serialize, Debug)]
pub struct Provenance {
    pub klt_version: String,
    pub klayout_version: Option<String>,
    pub pdk: Option<String>,
    pub deck: Option<String>,
    pub input: InputRef,
}

/// `provenance.input` -- for `klt wave build`, this is the **trace** file's
/// hash (contract section 4: "`input.content_hash` is the trace file's
/// hash"), distinct from `store.content_hash` above (the built artifact's
/// own hash).
#[derive(Serialize, Debug)]
pub struct InputRef {
    pub content_hash: String,
}

#[derive(Serialize, Debug)]
pub struct TraceResp {
    pub path: String,
    pub format: String,
    pub size_bytes: u64,
}

#[derive(Serialize, Debug)]
pub struct StoreResp {
    pub path: String,
    pub size_bytes: u64,
    pub content_hash: String,
}

#[derive(Serialize, Debug)]
pub struct ClockResp {
    pub signal: String,
    pub edge: String,
    pub period_ns: Option<f64>,
}

#[derive(Serialize, Debug)]
pub struct ResetResp {
    pub signal: String,
    pub active: String,
    pub release: Option<TimePoint>,
}

#[derive(Serialize, Debug)]
pub struct TimescaleResp {
    pub unit: String,
    pub value: i64,
}

#[derive(Serialize, Debug)]
pub struct TimeRangeResp {
    pub from: TimePoint,
    pub to: TimePoint,
}

#[derive(Serialize, Debug, Clone, Copy)]
pub struct TimePoint {
    pub time_ns: f64,
    pub cycle: Option<u64>,
}

fn resolve(base_dir: &Path, path_str: &str) -> PathBuf {
    let p = Path::new(path_str);
    if p.is_absolute() {
        p.to_path_buf()
    } else {
        base_dir.join(p)
    }
}

fn infer_format(path: &Path, given: Option<&str>) -> Result<String, BuildError> {
    if let Some(f) = given {
        return match f {
            "vcd" | "fst" => Ok(f.to_string()),
            other => Err((1, format!("trace.format '{other}' is not 'vcd' or 'fst'"))),
        };
    }
    match path.extension().and_then(|e| e.to_str()) {
        Some(ext) if ext.eq_ignore_ascii_case("vcd") => Ok("vcd".to_string()),
        Some(ext) if ext.eq_ignore_ascii_case("fst") => Ok("fst".to_string()),
        _ => Err((
            1,
            format!(
                "trace.format was not given and could not be inferred from '{}': \
                 expected a .vcd or .fst extension",
                path.display()
            ),
        )),
    }
}

fn sha256_file(path: &Path) -> Result<String, BuildError> {
    let mut file = File::open(path).map_err(|e| {
        (
            1,
            format!("cannot read '{}' for hashing: {e}", path.display()),
        )
    })?;
    let mut hasher = Sha256::new();
    let mut buf = [0u8; 1 << 20];
    loop {
        let n = file.read(&mut buf).map_err(|e| {
            (
                1,
                format!("cannot read '{}' for hashing: {e}", path.display()),
            )
        })?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    // sha2 0.11's digest output no longer implements `LowerHex`.
    let hex: String = hasher
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    Ok(format!("sha256:{hex}"))
}

fn cycle_of(tick: u64, anchor: Option<u64>, period: Option<u64>) -> Option<u64> {
    let anchor = anchor?;
    let period = period?;
    if tick < anchor || period == 0 {
        return None;
    }
    Some((tick - anchor) / period)
}

pub fn run(request_path: &Path) -> Result<Response, BuildError> {
    let request_text = fs::read_to_string(request_path).map_err(|e| {
        (
            2,
            format!("failed to read request '{}': {e}", request_path.display()),
        )
    })?;
    let req: Request = serde_json::from_str(&request_text)
        .map_err(|e| (2, format!("malformed request JSON: {e}")))?;

    let base_dir = request_path.parent().unwrap_or_else(|| Path::new("."));
    let trace_path = resolve(base_dir, &req.trace.path);
    let format = infer_format(&trace_path, req.trace.format.as_deref())?;

    if let Some(signals) = req.signals.as_ref() {
        if signals.is_empty() {
            return Err((
                1,
                "request.signals is a non-null empty array -- an empty allow-list can \
                 never usefully answer any query, refusing to build a store from it"
                    .to_string(),
            ));
        }
    }

    let clock_edge = match req.clock.as_ref().and_then(|c| c.edge.as_deref()) {
        None => Edge::Rising,
        Some(s) => Edge::parse(s)
            .ok_or_else(|| (1, format!("clock.edge '{s}' is not 'rising' or 'falling'")))?,
    };
    let reset_active_low = match req.reset.as_ref().and_then(|r| r.active.as_deref()) {
        None | Some("low") => true,
        Some("high") => false,
        Some(other) => return Err((1, format!("reset.active '{other}' is not 'low' or 'high'"))),
    };

    let trace_meta = fs::metadata(&trace_path).map_err(|e| {
        (
            1,
            format!("cannot read trace '{}': {e}", trace_path.display()),
        )
    })?;
    let trace_size_bytes = trace_meta.len();

    let store_path = resolve(base_dir, &req.store.path);
    if let Some(parent) = store_path.parent() {
        if !parent.as_os_str().is_empty() {
            fs::create_dir_all(parent).map_err(|e| {
                (
                    1,
                    format!("cannot create store directory '{}': {e}", parent.display()),
                )
            })?;
        }
    }

    let options = BuildOptions {
        store_path: &store_path,
        allow_list: req.signals.as_deref(),
        clock_signal: req.clock.as_ref().map(|c| c.signal.as_str()),
        clock_edge,
        reset_signal: req.reset.as_ref().map(|r| r.signal.as_str()),
        reset_active_low,
    };
    let BuildSummary {
        signal_count,
        value_change_count,
        timescale,
        min_tick,
        max_tick,
        ticks_to_ns,
        clock_period_ticks,
        anchor_tick,
    } = if format == "vcd" {
        build_from_vcd(&trace_path, &options)?
    } else {
        build_from_fst(&trace_path, &options)?
    };

    let store_meta = fs::metadata(&store_path).map_err(|e| {
        (
            1,
            format!("cannot stat built store '{}': {e}", store_path.display()),
        )
    })?;
    let store_content_hash = sha256_file(&store_path)?;
    let trace_content_hash = sha256_file(&trace_path)?;

    // `anchor_tick` is the resolved cycle-0 anchor: the first clock edge
    // at/after reset release (when a reset was declared and its release
    // was actually observed in this trace), or the clock's own first
    // edge otherwise (no reset declared, or one declared but never
    // observed releasing -- a benign degrade, not an error; see
    // docs/cli/wave.md's "Cycle-0 anchoring" section).
    let clock_resp = req.clock.as_ref().map(|c| ClockResp {
        signal: c.signal.clone(),
        edge: match clock_edge {
            Edge::Rising => "rising".to_string(),
            Edge::Falling => "falling".to_string(),
        },
        period_ns: clock_period_ticks.map(|p| p as f64 * ticks_to_ns),
    });

    let reset_resp = req.reset.as_ref().map(|r| ResetResp {
        signal: r.signal.clone(),
        active: if reset_active_low {
            "low".to_string()
        } else {
            "high".to_string()
        },
        release: anchor_tick.map(|tick| TimePoint {
            time_ns: tick as f64 * ticks_to_ns,
            cycle: Some(0),
        }),
    });

    let from_cycle = min_tick.and_then(|t| cycle_of(t, anchor_tick, clock_period_ticks));
    let to_cycle = max_tick.and_then(|t| cycle_of(t, anchor_tick, clock_period_ticks));

    Ok(Response {
        schema_version: 1,
        trace: TraceResp {
            path: req.trace.path.clone(),
            format,
            size_bytes: trace_size_bytes,
        },
        store: StoreResp {
            path: req.store.path.clone(),
            size_bytes: store_meta.len(),
            content_hash: store_content_hash,
        },
        clock: clock_resp,
        reset: reset_resp,
        timescale,
        time_range: TimeRangeResp {
            from: TimePoint {
                time_ns: min_tick.map(|t| t as f64 * ticks_to_ns).unwrap_or(0.0),
                cycle: from_cycle,
            },
            to: TimePoint {
                time_ns: max_tick.map(|t| t as f64 * ticks_to_ns).unwrap_or(0.0),
                cycle: to_cycle,
            },
        },
        signal_count,
        value_change_count,
        provenance: Provenance {
            klt_version: env!("CARGO_PKG_VERSION").to_string(),
            klayout_version: None,
            pdk: None,
            deck: None,
            input: InputRef {
                content_hash: trace_content_hash,
            },
        },
    })
}

struct BuildOptions<'a> {
    store_path: &'a Path,
    allow_list: Option<&'a [String]>,
    clock_signal: Option<&'a str>,
    clock_edge: Edge,
    reset_signal: Option<&'a str>,
    reset_active_low: bool,
}

struct BuildSummary {
    signal_count: usize,
    value_change_count: u64,
    timescale: TimescaleResp,
    min_tick: Option<u64>,
    max_tick: Option<u64>,
    ticks_to_ns: f64,
    clock_period_ticks: Option<u64>,
    anchor_tick: Option<u64>,
}

fn build_from_vcd(
    trace_path: &Path,
    options: &BuildOptions<'_>,
) -> Result<BuildSummary, BuildError> {
    let file = File::open(trace_path).map_err(|e| {
        (
            1,
            format!("cannot open VCD trace '{}': {e}", trace_path.display()),
        )
    })?;
    let mut reader = BufReader::new(file);
    let (header, summary) = build_vcd::parse_header(&mut reader).map_err(|e| (1, e))?;
    let signals = header.signals.iter().map(|s| DeclSignal {
        id: &s.id,
        full_name: &s.name,
        width: s.width,
        var_type: &s.var_type,
        is_real: matches!(s.var_type.as_str(), "real" | "realtime"),
    });
    let exponent = crate::fst_convert::timescale_to_exponent(&summary.timescale_str);
    let (mut store, watched) =
        prepare_store(options, "VCD", signals, exponent, summary.ticks_to_ns)?;
    build_vcd::stream_body(&mut reader, &watched, &mut store);
    finish_store(store, options.store_path, exponent, summary.ticks_to_ns)
}

fn build_from_fst(
    trace_path: &Path,
    options: &BuildOptions<'_>,
) -> Result<BuildSummary, BuildError> {
    let mut source = build_fst::open(trace_path).map_err(|e| (1, e))?;
    let signals = source.signals.iter().map(|s| DeclSignal {
        id: &s.id,
        full_name: &s.full_name,
        width: s.width,
        var_type: crate::fst_convert::var_type_str(s.var_type),
        is_real: s.is_real,
    });
    let exponent = source.timescale_exponent;
    let ticks_to_ns = source.ticks_to_ns;
    let (mut store, watched) = prepare_store(options, "FST", signals, exponent, ticks_to_ns)?;
    build_fst::stream_body(&mut source, &watched, &mut store).map_err(|e| (1, e))?;
    finish_store(store, options.store_path, exponent, ticks_to_ns)
}

fn prepare_store<'a>(
    options: &BuildOptions<'_>,
    format: &str,
    signals: impl ExactSizeIterator<Item = DeclSignal<'a>>,
    exponent: i8,
    ticks_to_ns: f64,
) -> Result<(StoreWriter, FxHashSet<String>), BuildError> {
    if signals.len() == 0 {
        return Err((
            1,
            format!("input {format} declares no signals -- refusing to build an unqueryable store"),
        ));
    }

    // Preserve declaration order and aliases even when the allow-list is reordered.
    let declared: Vec<_> = match options.allow_list {
        Some(names) => {
            let wanted: FxHashSet<&str> = names.iter().map(String::as_str).collect();
            let selected: Vec<_> = signals.filter(|s| wanted.contains(s.full_name)).collect();
            if selected.len() != names.len() {
                return Err((
                    1,
                    "request.signals names a signal not declared in the trace".to_string(),
                ));
            }
            selected
        }
        None => signals.collect(),
    };
    if declared.is_empty() {
        return Err((1, "no signals to index after filtering".to_string()));
    }

    let clock_id = resolve_signal_id(options.clock_signal, &declared)?;
    let reset_id = resolve_signal_id(options.reset_signal, &declared)?;
    let store = StoreWriter::create(
        options.store_path,
        &declared,
        exponent,
        ticks_to_ns,
        clock_id,
        options.clock_edge,
        reset_id,
        options.reset_active_low,
    )
    .map_err(|e| (1, e))?;
    let watched = declared.iter().map(|s| s.id.to_owned()).collect();
    Ok((store, watched))
}

fn finish_store(
    store: StoreWriter,
    store_path: &Path,
    exponent: i8,
    ticks_to_ns: f64,
) -> Result<BuildSummary, BuildError> {
    let (min_tick, max_tick) = store.time_range_ticks();
    let (unit, value) = timescale_from_exponent(exponent);
    let clock = store.clock_tracker();
    let anchor_tick = match store.reset_tracker().release_tick() {
        Some(release) => clock.first_edge_at_or_after(release),
        None => clock.first_edge_tick(),
    };
    let summary = BuildSummary {
        signal_count: store.signal_count(),
        value_change_count: store.value_change_count(),
        timescale: TimescaleResp {
            unit: unit.to_string(),
            value,
        },
        min_tick,
        max_tick,
        ticks_to_ns,
        clock_period_ticks: clock.period_ticks(),
        anchor_tick,
    };
    store.finish().map_err(|e| (1, e))?;
    crate::verify::verify_store(store_path, max_tick).map_err(|e| (1, e))?;
    Ok(summary)
}

fn resolve_signal_id<'a>(
    wanted_name: Option<&str>,
    declared: &[DeclSignal<'a>],
) -> Result<Option<&'a str>, BuildError> {
    let Some(name) = wanted_name else {
        return Ok(None);
    };
    declared
        .iter()
        .find(|s| s.full_name == name)
        .map(|s| Some(s.id))
        .ok_or_else(|| {
            (
                1,
                format!("signal '{name}' is named in the request but absent from the trace"),
            )
        })
}
