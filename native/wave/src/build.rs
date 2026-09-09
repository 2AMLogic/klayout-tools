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
    Ok(format!("sha256:{:x}", hasher.finalize()))
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

    let (
        signal_count,
        value_change_count,
        timescale_unit,
        timescale_value,
        min_tick,
        max_tick,
        ticks_to_ns,
        clock_period_ticks,
        anchor_tick,
        _reset_release_tick,
    ) = if format == "vcd" {
        build_from_vcd(
            &trace_path,
            &store_path,
            req.signals.as_deref(),
            req.clock.as_ref().map(|c| c.signal.as_str()),
            clock_edge,
            req.reset.as_ref().map(|r| r.signal.as_str()),
            reset_active_low,
        )?
    } else {
        build_from_fst(
            &trace_path,
            &store_path,
            req.signals.as_deref(),
            req.clock.as_ref().map(|c| c.signal.as_str()),
            clock_edge,
            req.reset.as_ref().map(|r| r.signal.as_str()),
            reset_active_low,
        )?
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
    // docs/cli/wave.md's "Cycle-0 anchoring" section). Computed by
    // `build_from_vcd`/`build_from_fst` via `resolve_anchor` below, using
    // `ClockTracker::first_edge_at_or_after`.
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
        timescale: TimescaleResp {
            unit: timescale_unit,
            value: timescale_value,
        },
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

#[allow(clippy::type_complexity)]
fn build_from_vcd(
    trace_path: &Path,
    store_path: &Path,
    allow_list: Option<&[String]>,
    clock_signal: Option<&str>,
    clock_edge: Edge,
    reset_signal: Option<&str>,
    reset_active_low: bool,
) -> Result<
    (
        usize,
        u64,
        String,
        i64,
        Option<u64>,
        Option<u64>,
        f64,
        Option<u64>,
        Option<u64>,
        Option<u64>,
    ),
    BuildError,
> {
    let file = File::open(trace_path).map_err(|e| {
        (
            1,
            format!("cannot open VCD trace '{}': {e}", trace_path.display()),
        )
    })?;
    let mut reader = BufReader::new(file);
    let (header, summary) = build_vcd::parse_header(&mut reader).map_err(|e| (1, e))?;

    if header.signals.is_empty() {
        return Err((
            1,
            "input VCD declares no signals -- refusing to build an unqueryable store".to_string(),
        ));
    }

    let filtered = build_vcd::filter_signals(&header.signals, allow_list);
    if let Some(list) = allow_list {
        if filtered.len() != list.len() {
            return Err((
                1,
                "request.signals names a signal not declared in the trace".to_string(),
            ));
        }
    }
    if filtered.is_empty() {
        return Err((1, "no signals to index after filtering".to_string()));
    }

    let clock_id = resolve_signal_id(
        clock_signal,
        &filtered,
        |s| s.name.as_str(),
        |s| s.id.as_str(),
    )?;
    let reset_id = resolve_signal_id(
        reset_signal,
        &filtered,
        |s| s.name.as_str(),
        |s| s.id.as_str(),
    )?;

    let decls: Vec<DeclSignal> = filtered
        .iter()
        .map(|s| {
            let is_real = s.var_type == "real" || s.var_type == "realtime";
            DeclSignal {
                id: s.id.as_str(),
                full_name: s.name.as_str(),
                width: s.width,
                var_type: s.var_type.as_str(),
                is_real,
            }
        })
        .collect();

    let timescale_exponent = crate::fst_convert::timescale_to_exponent(&summary.timescale_str);
    let mut store = StoreWriter::create(
        store_path,
        &decls,
        timescale_exponent,
        summary.ticks_to_ns,
        clock_id.as_deref(),
        clock_edge,
        reset_id.as_deref(),
        reset_active_low,
    )
    .map_err(|e| (1, e))?;

    let watched_ids: FxHashSet<String> = filtered.iter().map(|s| s.id.clone()).collect();
    build_vcd::stream_body(&mut reader, &watched_ids, &mut store);

    let signal_count = store.signal_count();
    let value_change_count = store.value_change_count();
    let (min_tick, max_tick) = store.time_range_ticks();
    let clock_period_ticks = store.clock_tracker().period_ticks();
    let reset_release_tick = store.reset_tracker().release_tick();
    let anchor = resolve_anchor(store.clock_tracker(), reset_release_tick);
    store.finish().map_err(|e| (1, e))?;
    crate::verify::verify_store(store_path, max_tick).map_err(|e| (1, e))?;

    let (unit, value) = timescale_from_exponent(timescale_exponent);
    Ok((
        signal_count,
        value_change_count,
        unit.to_string(),
        value,
        min_tick,
        max_tick,
        summary.ticks_to_ns,
        clock_period_ticks,
        anchor,
        reset_release_tick,
    ))
}

#[allow(clippy::type_complexity)]
fn build_from_fst(
    trace_path: &Path,
    store_path: &Path,
    allow_list: Option<&[String]>,
    clock_signal: Option<&str>,
    clock_edge: Edge,
    reset_signal: Option<&str>,
    reset_active_low: bool,
) -> Result<
    (
        usize,
        u64,
        String,
        i64,
        Option<u64>,
        Option<u64>,
        f64,
        Option<u64>,
        Option<u64>,
        Option<u64>,
    ),
    BuildError,
> {
    let mut source = build_fst::open(trace_path).map_err(|e| (1, e))?;
    if source.signals.is_empty() {
        return Err((
            1,
            "input FST declares no signals -- refusing to build an unqueryable store".to_string(),
        ));
    }
    let filtered = build_fst::filter_signals(&source.signals, allow_list);
    if let Some(list) = allow_list {
        if filtered.len() != list.len() {
            return Err((
                1,
                "request.signals names a signal not declared in the trace".to_string(),
            ));
        }
    }
    if filtered.is_empty() {
        return Err((1, "no signals to index after filtering".to_string()));
    }

    let clock_id = resolve_signal_id(
        clock_signal,
        &filtered,
        |s| s.full_name.as_str(),
        |s| s.id.as_str(),
    )?;
    let reset_id = resolve_signal_id(
        reset_signal,
        &filtered,
        |s| s.full_name.as_str(),
        |s| s.id.as_str(),
    )?;

    let decls: Vec<DeclSignal> = filtered
        .iter()
        .map(|s| DeclSignal {
            id: s.id.as_str(),
            full_name: s.full_name.as_str(),
            width: s.width,
            var_type: build_fst::var_type_string(s.var_type),
            is_real: s.is_real,
        })
        .collect();

    let mut store = StoreWriter::create(
        store_path,
        &decls,
        source.timescale_exponent,
        source.ticks_to_ns,
        clock_id.as_deref(),
        clock_edge,
        reset_id.as_deref(),
        reset_active_low,
    )
    .map_err(|e| (1, e))?;

    let watched_ids: FxHashSet<String> = filtered.iter().map(|s| s.id.clone()).collect();
    build_fst::stream_body(&mut source, &watched_ids, &mut store).map_err(|e| (1, e))?;

    let signal_count = store.signal_count();
    let value_change_count = store.value_change_count();
    let (min_tick, max_tick) = store.time_range_ticks();
    let clock_period_ticks = store.clock_tracker().period_ticks();
    let reset_release_tick = store.reset_tracker().release_tick();
    let anchor = resolve_anchor(store.clock_tracker(), reset_release_tick);
    let ticks_to_ns = source.ticks_to_ns;
    let timescale_exponent = source.timescale_exponent;
    store.finish().map_err(|e| (1, e))?;
    crate::verify::verify_store(store_path, max_tick).map_err(|e| (1, e))?;

    let (unit, value) = timescale_from_exponent(timescale_exponent);
    Ok((
        signal_count,
        value_change_count,
        unit.to_string(),
        value,
        min_tick,
        max_tick,
        ticks_to_ns,
        clock_period_ticks,
        anchor,
        reset_release_tick,
    ))
}

fn resolve_anchor(
    clock: &crate::cycle::ClockTracker,
    reset_release_tick: Option<u64>,
) -> Option<u64> {
    match reset_release_tick {
        Some(release) => clock.first_edge_at_or_after(release),
        None => clock.first_edge_tick(),
    }
}

fn resolve_signal_id<T>(
    wanted_name: Option<&str>,
    declared: &[&T],
    name_of: impl Fn(&T) -> &str,
    id_of: impl Fn(&T) -> &str,
) -> Result<Option<String>, BuildError> {
    let Some(name) = wanted_name else {
        return Ok(None);
    };
    declared
        .iter()
        .find(|s| name_of(s) == name)
        .map(|s| Some(id_of(s).to_string()))
        .ok_or_else(|| {
            (
                1,
                format!("signal '{name}' is named in the request but absent from the trace"),
            )
        })
}
