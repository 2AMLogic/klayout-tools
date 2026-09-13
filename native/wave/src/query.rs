//! The `klt wave query` op vocabulary (contract section 5), written fresh
//! against `docs/design/waveform-query-contract-spike.md` -- not ported
//! from `boldaxolotl/booley`. bwave's own `*_from_cache` query functions
//! (`crate::cache`'s upstream, see that module's own header) print directly
//! to stdout/stderr in bwave's own CLI shapes and read a `bwave`-specific
//! `ExtractConfig`; this module implements the *equivalent query
//! vocabulary* (value-at-time, first/last match, count-between,
//! sample-on-edge, stuck detection, two-run diff, bounded wave dump)
//! against `crate::cache::ColumnCache`, returning typed results for klt's
//! own JSON envelope. Algorithms here are informed by bwave's design (the
//! same read primitives, the same edge/level-match semantics) but the code
//! is original.

use std::path::Path;

use serde_json::{json, Map, Value};

use crate::cache::ColumnCache;
use crate::format::values_match;
use crate::request::{DurationReq, MatchReq, OpReq, TimePointReq, TimePointResp, WindowReq};

/// Result of running one op: the JSON object for `results[]`, plus whether
/// it declared a `predicate` and, if so, whether that predicate was
/// satisfied -- contract section 5's "Predicates and exit code 3".
pub struct OpOutcome {
    pub result: Value,
    pub satisfied: Option<bool>,
}

// -- time-point / window / duration resolution ---------------------------

/// Resolve a request time-point to an absolute FST tick.
///
/// Cycle `0` is `cache.sim_start_tick` -- the first active edge of the
/// primary clock at or after reset release, exactly as re-derived at load
/// time (`crate::fst::load_fst`); contract section 3's own definition of
/// cycle `0`. A `from`/`to` window with no time-point given instead
/// defaults to `cache.trace_start_tick`/`cache.sim_end_tick` -- the true
/// first/last tick in the trace -- since contract section 5 defines an
/// omitted `window` as covering "the whole trace", a distinct concept from
/// the post-reset cycle-0 anchor (pre-reset activity must still be visible
/// to a query with no explicit window).
fn resolve_tick(cache: &ColumnCache, point: &TimePointReq) -> Result<u64, String> {
    match (point.time_ns, point.cycle) {
        (Some(_), Some(_)) => Err("time-point carries both time_ns and cycle".to_string()),
        (None, None) => Err("time-point must specify time_ns or cycle".to_string()),
        (Some(ns), None) => {
            if cache.ticks_to_ns <= 0.0 {
                return Err("store has no resolvable timescale".to_string());
            }
            let ticks = (ns / cache.ticks_to_ns).round();
            if ticks < 0.0 {
                return Err(format!(
                    "time_ns {ns} resolves before the start of the trace"
                ));
            }
            Ok(ticks as u64)
        }
        (None, Some(cycle)) => {
            if cache.clock_period_ticks == 0 {
                return Err("cycle addressing requires a clock declared at build time".to_string());
            }
            let tick =
                cache.sim_start_tick as i128 + (cycle as i128) * (cache.clock_period_ticks as i128);
            if tick < 0 {
                return Err(format!(
                    "cycle {cycle} resolves before the start of the trace"
                ));
            }
            Ok(tick as u64)
        }
    }
}

/// Echo a resolved tick back as both time forms (contract section 3).
fn point_response(cache: &ColumnCache, tick: u64) -> TimePointResp {
    let time_ns = tick as f64 * cache.ticks_to_ns;
    let cycle = if cache.clock_period_ticks > 0 && tick >= cache.sim_start_tick {
        Some(((tick - cache.sim_start_tick) / cache.clock_period_ticks) as i64)
    } else {
        None
    };
    TimePointResp { time_ns, cycle }
}

fn point_response_json(cache: &ColumnCache, tick: u64) -> Value {
    let p = point_response(cache, tick);
    json!({ "time_ns": p.time_ns, "cycle": p.cycle })
}

fn resolve_window(cache: &ColumnCache, window: &WindowReq) -> Result<(u64, u64), String> {
    let from_tick = match &window.from {
        Some(p) => resolve_tick(cache, p)?,
        None => cache.trace_start_tick,
    };
    let to_tick = match &window.to {
        Some(p) => resolve_tick(cache, p)?,
        None => cache.sim_end_tick,
    };
    if from_tick > to_tick {
        return Err(format!(
            "window.from ({from_tick}) is after window.to ({to_tick})"
        ));
    }
    Ok((from_tick, to_tick))
}

fn resolve_duration_ticks(cache: &ColumnCache, d: &DurationReq) -> Result<u64, String> {
    match (d.cycles, d.time_ns) {
        (Some(_), Some(_)) => Err("min_span carries both cycles and time_ns".to_string()),
        (None, None) => Err("min_span must specify cycles or time_ns".to_string()),
        (Some(cycles), None) => {
            if cache.clock_period_ticks == 0 {
                return Err("min_span.cycles requires a clock declared at build time".to_string());
            }
            if cycles < 0 {
                return Err("min_span.cycles must be non-negative".to_string());
            }
            Ok(cycles as u64 * cache.clock_period_ticks)
        }
        (None, Some(ns)) => {
            if cache.ticks_to_ns <= 0.0 {
                return Err("store has no resolvable timescale".to_string());
            }
            if ns < 0.0 {
                return Err("min_span.time_ns must be non-negative".to_string());
            }
            Ok((ns / cache.ticks_to_ns).round() as u64)
        }
    }
}

// -- level/edge matching ---------------------------------------------------

fn edge_matches(prev: &str, cur: &str, kind: &str) -> Result<bool, String> {
    match kind {
        "rising" => Ok(prev == "0" && cur == "1"),
        "falling" => Ok(prev == "1" && cur == "0"),
        "any" => Ok(prev != cur),
        other => Err(format!(
            "unknown edge kind '{other}' (expected rising / falling / any)"
        )),
    }
}

/// Ticks within `[from_tick, to_tick]` where `signal` starts matching
/// `match_` -- one entry per maximal run for a level match, one entry per
/// qualifying edge for an edge match. Shared by `find` and `count`.
fn match_ticks(
    cache: &ColumnCache,
    sig_idx: usize,
    match_: &MatchReq,
    from_tick: u64,
    to_tick: u64,
) -> Result<Vec<u64>, String> {
    let (before, changes) = cache.read_transitions_range(sig_idx, from_tick, to_tick);
    let mut hits = Vec::new();
    let mut cur = before.unwrap_or_else(|| "x".to_string());

    if let Some(target) = &match_.value {
        if values_match(&cur, target) {
            hits.push(from_tick);
        }
        for (t, v) in &changes {
            if values_match(v, target) && !values_match(&cur, target) {
                hits.push(*t);
            }
            cur = v.clone();
        }
    } else if let Some(edge) = &match_.edge {
        for (t, v) in &changes {
            if edge_matches(&cur, v, edge)? {
                hits.push(*t);
            }
            cur = v.clone();
        }
    } else {
        return Err("match must specify value or edge".to_string());
    }
    Ok(hits)
}

// -- predicate evaluation ---------------------------------------------------

fn obj(v: &Value) -> Result<&Map<String, Value>, String> {
    v.as_object()
        .ok_or_else(|| "predicate must be a JSON object".to_string())
}

// -- ops ---------------------------------------------------------------

fn op_value(
    cache: &ColumnCache,
    signal: &str,
    at: &TimePointReq,
    predicate: &Option<Value>,
) -> Result<OpOutcome, String> {
    let sig_idx = cache.match_one_signal(signal)?;
    let tick = resolve_tick(cache, at)?;
    let value = cache.value_at_tick_direct(sig_idx, tick);
    let mut result = Map::new();
    result.insert("op".into(), json!("value"));
    result.insert("signal".into(), json!(signal));
    result.insert("at".into(), point_response_json(cache, tick));
    result.insert("value".into(), json!(value));

    let satisfied = match predicate {
        None => None,
        Some(p) => {
            let p = obj(p)?;
            let equals = p
                .get("equals")
                .and_then(Value::as_str)
                .ok_or_else(|| "value predicate requires 'equals'".to_string())?;
            Some(values_match(&value, equals))
        }
    };
    if let Some(s) = satisfied {
        result.insert("satisfied".into(), json!(s));
    }
    Ok(OpOutcome {
        result: Value::Object(result),
        satisfied,
    })
}

fn op_find(
    cache: &ColumnCache,
    signal: &str,
    match_: &MatchReq,
    occurrence: &str,
    window: &WindowReq,
    predicate: &Option<Value>,
) -> Result<OpOutcome, String> {
    let sig_idx = cache.match_one_signal(signal)?;
    let (from_tick, to_tick) = resolve_window(cache, window)?;
    let hits = match_ticks(cache, sig_idx, match_, from_tick, to_tick)?;
    let found_tick = match occurrence {
        "first" => hits.first().copied(),
        "last" => hits.last().copied(),
        other => {
            return Err(format!(
                "unknown occurrence '{other}' (expected first / last)"
            ))
        }
    };

    let mut result = Map::new();
    result.insert("op".into(), json!("find"));
    result.insert("signal".into(), json!(signal));
    result.insert("found".into(), json!(found_tick.is_some()));
    result.insert(
        "at".into(),
        match found_tick {
            Some(t) => point_response_json(cache, t),
            None => Value::Null,
        },
    );

    let satisfied = match predicate {
        None => None,
        Some(p) => {
            let p = obj(p)?;
            let must_find = p.get("must_find").and_then(Value::as_bool).unwrap_or(true);
            let mut ok = if must_find {
                found_tick.is_some()
            } else {
                true
            };
            if let Some(t) = found_tick {
                let at = point_response(cache, t);
                if let Some(max_cycle) = p.get("max_cycle").and_then(Value::as_i64) {
                    ok = ok && matches!(at.cycle, Some(c) if c <= max_cycle);
                }
                if let Some(max_time_ns) = p.get("max_time_ns").and_then(Value::as_f64) {
                    ok = ok && at.time_ns <= max_time_ns;
                }
            } else if p.get("max_cycle").is_some() || p.get("max_time_ns").is_some() {
                ok = false;
            }
            Some(ok)
        }
    };
    if let Some(s) = satisfied {
        result.insert("satisfied".into(), json!(s));
    }
    Ok(OpOutcome {
        result: Value::Object(result),
        satisfied,
    })
}

fn op_count(
    cache: &ColumnCache,
    signal: &str,
    match_: &MatchReq,
    window: &WindowReq,
    predicate: &Option<Value>,
) -> Result<OpOutcome, String> {
    let sig_idx = cache.match_one_signal(signal)?;
    let (from_tick, to_tick) = resolve_window(cache, window)?;
    let hits = match_ticks(cache, sig_idx, match_, from_tick, to_tick)?;
    let count = hits.len();

    let mut result = Map::new();
    result.insert("op".into(), json!("count"));
    result.insert("signal".into(), json!(signal));
    result.insert("count".into(), json!(count));

    let satisfied = match predicate {
        None => None,
        Some(p) => {
            let p = obj(p)?;
            let mut ok = true;
            if let Some(min) = p.get("min").and_then(Value::as_i64) {
                ok = ok && (count as i64) >= min;
            }
            if let Some(max) = p.get("max").and_then(Value::as_i64) {
                ok = ok && (count as i64) <= max;
            }
            Some(ok)
        }
    };
    if let Some(s) = satisfied {
        result.insert("satisfied".into(), json!(s));
    }
    Ok(OpOutcome {
        result: Value::Object(result),
        satisfied,
    })
}

fn op_sample(
    cache: &ColumnCache,
    signals: &[String],
    edge_of: &str,
    edge: &str,
    window: &WindowReq,
    predicate: &Option<Value>,
) -> Result<OpOutcome, String> {
    if predicate.is_some() {
        return Err("'sample' does not define a predicate".to_string());
    }
    let edge_idx = cache.match_one_signal(edge_of)?;
    let (from_tick, to_tick) = resolve_window(cache, window)?;

    let mut edge_ticks: Vec<u64> = Vec::new();
    let (before, changes) = cache.read_transitions_range(edge_idx, from_tick, to_tick);
    let mut cur = before.unwrap_or_else(|| "x".to_string());
    for (t, v) in &changes {
        let hit = match edge {
            "rising" => cur == "0" && v == "1",
            "falling" => cur == "1" && v == "0",
            "both" => cur != *v,
            other => {
                return Err(format!(
                    "unknown sample edge '{other}' (expected rising / falling / both)"
                ))
            }
        };
        if hit {
            edge_ticks.push(*t);
        }
        cur = v.clone();
    }

    let sig_indices: Vec<usize> = signals
        .iter()
        .map(|s| cache.match_one_signal(s))
        .collect::<Result<_, _>>()?;
    cache.prefetch_window(&sig_indices, to_tick);

    let samples: Vec<Value> = edge_ticks
        .iter()
        .map(|&t| {
            let mut values = Map::new();
            for (name, &idx) in signals.iter().zip(sig_indices.iter()) {
                values.insert(name.clone(), json!(cache.value_at_tick_direct(idx, t)));
            }
            json!({ "at": point_response_json(cache, t), "values": Value::Object(values) })
        })
        .collect();

    let mut result = Map::new();
    result.insert("op".into(), json!("sample"));
    result.insert("signals".into(), json!(signals));
    result.insert("samples".into(), Value::Array(samples));
    Ok(OpOutcome {
        result: Value::Object(result),
        satisfied: None,
    })
}

fn op_stuck(
    cache: &ColumnCache,
    signal: &str,
    window: &WindowReq,
    min_span: &DurationReq,
    predicate: &Option<Value>,
) -> Result<OpOutcome, String> {
    let sig_idx = cache.match_one_signal(signal)?;
    let (from_tick, to_tick) = resolve_window(cache, window)?;
    let min_span_ticks = resolve_duration_ticks(cache, min_span)?;

    let (before, changes) = cache.read_transitions_range(sig_idx, from_tick, to_tick);
    // Build (value, start_tick) runs bounded by [from_tick, to_tick].
    let mut runs: Vec<(String, u64, u64)> = Vec::new(); // (value, start, end)
    let mut cur_val = before.unwrap_or_else(|| "x".to_string());
    let mut cur_start = from_tick;
    for (t, v) in &changes {
        if *t > cur_start {
            runs.push((cur_val.clone(), cur_start, *t));
        }
        cur_val = v.clone();
        cur_start = *t;
    }
    runs.push((cur_val, cur_start, to_tick));

    let longest = runs.iter().max_by_key(|(_, s, e)| e.saturating_sub(*s));

    let (stuck, value, span) = match longest {
        Some((v, s, e)) => {
            let span_ticks = e.saturating_sub(*s);
            (
                span_ticks >= min_span_ticks,
                Some(v.clone()),
                Some(
                    json!({ "from": point_response_json(cache, *s), "to": point_response_json(cache, *e) }),
                ),
            )
        }
        None => (false, None, None),
    };

    let mut result = Map::new();
    result.insert("op".into(), json!("stuck"));
    result.insert("signal".into(), json!(signal));
    result.insert("stuck".into(), json!(stuck));
    result.insert(
        "value".into(),
        value.map(|v| json!(v)).unwrap_or(Value::Null),
    );
    result.insert("span".into(), span.unwrap_or(Value::Null));

    let satisfied = match predicate {
        None => None,
        Some(p) => {
            let p = obj(p)?;
            let expect = p
                .get("expect")
                .and_then(Value::as_bool)
                .ok_or_else(|| "stuck predicate requires 'expect'".to_string())?;
            Some(expect == stuck)
        }
    };
    if let Some(s) = satisfied {
        result.insert("satisfied".into(), json!(s));
    }
    Ok(OpOutcome {
        result: Value::Object(result),
        satisfied,
    })
}

fn op_diff(
    cache: &ColumnCache,
    request_dir: &Path,
    signal: &str,
    other_store: &str,
    window: &WindowReq,
    predicate: &Option<Value>,
) -> Result<OpOutcome, String> {
    let other_path = if Path::new(other_store).is_absolute() {
        Path::new(other_store).to_path_buf()
    } else {
        request_dir.join(other_store)
    };
    let other = ColumnCache::load_from_file(&other_path)
        .ok_or_else(|| format!("failed to read other_store '{}'", other_path.display()))?;

    let sig_idx = cache.match_one_signal(signal)?;
    let other_sig_idx = other.match_one_signal(signal)?;

    let (from_tick, to_tick) = resolve_window(cache, window)?;
    let (other_from_tick, other_to_tick) = resolve_window(&other, window)?;

    let (before_a, changes_a) = cache.read_transitions_range(sig_idx, from_tick, to_tick);
    let (before_b, changes_b) =
        other.read_transitions_range(other_sig_idx, other_from_tick, other_to_tick);

    // Merge both change streams on absolute simulation time (each store's
    // own timescale, per contract section 5's "applied identically to both
    // stores' own time axes"), tracking each side's current value.
    let mut events: Vec<(f64, u8, String)> = Vec::new(); // (time_ns, side, value)
    for (t, v) in &changes_a {
        events.push((*t as f64 * cache.ticks_to_ns, 0, v.clone()));
    }
    for (t, v) in &changes_b {
        events.push((*t as f64 * other.ticks_to_ns, 1, v.clone()));
    }
    events.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap());

    let mut cur_a = before_a.unwrap_or_else(|| "x".to_string());
    let mut cur_b = before_b.unwrap_or_else(|| "x".to_string());
    let mut was_diff = cur_a != cur_b;
    let mut first_divergence: Option<f64> = if was_diff {
        Some(from_tick as f64 * cache.ticks_to_ns)
    } else {
        None
    };
    // `distance` counts *transitions into* a divergent state (one event per
    // maximal divergent run), not every individual value-change while
    // diverged -- events at the identical simulation instant on both sides
    // (e.g. comparing a store to itself) are applied together before the
    // divergence check runs, so two stores whose signal always changes in
    // lockstep never register a spurious blip between the two sides'
    // same-instant updates.
    let mut distance: u64 = 0;
    let mut i = 0;
    while i < events.len() {
        let t = events[i].0;
        let mut j = i;
        while j < events.len() && events[j].0 == t {
            if events[j].1 == 0 {
                cur_a = events[j].2.clone();
            } else {
                cur_b = events[j].2.clone();
            }
            j += 1;
        }
        let now_diff = cur_a != cur_b;
        if now_diff && !was_diff {
            distance += 1;
            if first_divergence.is_none() {
                first_divergence = Some(t);
            }
        }
        was_diff = now_diff;
        i = j;
    }
    let diverges = first_divergence.is_some();

    let mut result = Map::new();
    result.insert("op".into(), json!("diff"));
    result.insert("signal".into(), json!(signal));
    result.insert("diverges".into(), json!(diverges));
    result.insert(
        "at".into(),
        match first_divergence {
            Some(ns) => {
                let tick = (ns / cache.ticks_to_ns).round().max(0.0) as u64;
                point_response_json(cache, tick)
            }
            None => Value::Null,
        },
    );
    result.insert("distance".into(), json!(distance));

    let satisfied = match predicate {
        None => None,
        Some(p) => {
            let p = obj(p)?;
            let expect = p
                .get("expect_diverges")
                .and_then(Value::as_bool)
                .ok_or_else(|| "diff predicate requires 'expect_diverges'".to_string())?;
            Some(expect == diverges)
        }
    };
    if let Some(s) = satisfied {
        result.insert("satisfied".into(), json!(s));
    }
    Ok(OpOutcome {
        result: Value::Object(result),
        satisfied,
    })
}

fn op_wave(
    cache: &ColumnCache,
    signal: &str,
    window: &WindowReq,
    max_entries: usize,
    predicate: &Option<Value>,
) -> Result<OpOutcome, String> {
    if predicate.is_some() {
        return Err("'wave' does not define a predicate".to_string());
    }
    let sig_idx = cache.match_one_signal(signal)?;
    let (from_tick, to_tick) = resolve_window(cache, window)?;
    let (before, changes) = cache.read_transitions_range(sig_idx, from_tick, to_tick);

    // Run-length-encode: consecutive identical values collapse to one entry.
    let mut runs: Vec<(String, u64)> = Vec::new(); // (value, start_tick)
    runs.push((before.unwrap_or_else(|| "x".to_string()), from_tick));
    for (t, v) in &changes {
        match runs.last_mut() {
            // A change recorded exactly at the current run's start tick
            // (e.g. tick 0, when `before` is None because nothing precedes
            // the trace) defines that run's value rather than a
            // zero-duration transition into it.
            Some((rv, rt)) if *rt == *t => *rv = v.clone(),
            Some((rv, _)) if rv == v => {}
            _ => runs.push((v.clone(), *t)),
        }
    }

    let truncated = runs.len() > max_entries;
    let shown = if truncated {
        &runs[..max_entries]
    } else {
        &runs[..]
    };

    let entries: Vec<Value> = shown
        .iter()
        .enumerate()
        .map(|(i, (value, start))| {
            let to = shown
                .get(i + 1)
                .map(|(_, next)| point_response_json(cache, *next));
            json!({
                "from": point_response_json(cache, *start),
                "to": to,
                "value": value,
            })
        })
        .collect();

    let mut result = Map::new();
    result.insert("op".into(), json!("wave"));
    result.insert("signal".into(), json!(signal));
    result.insert("entries".into(), Value::Array(entries));
    result.insert("truncated".into(), json!(truncated));
    Ok(OpOutcome {
        result: Value::Object(result),
        satisfied: None,
    })
}

/// Run one op from a `klt wave query` request batch.
pub fn run_op(cache: &ColumnCache, request_dir: &Path, op: &OpReq) -> Result<OpOutcome, String> {
    match op {
        OpReq::Value {
            signal,
            at,
            predicate,
        } => op_value(cache, signal, at, predicate),
        OpReq::Find {
            signal,
            match_,
            occurrence,
            window,
            predicate,
        } => op_find(cache, signal, match_, occurrence, window, predicate),
        OpReq::Count {
            signal,
            match_,
            window,
            predicate,
        } => op_count(cache, signal, match_, window, predicate),
        OpReq::Sample {
            signals,
            edge_of,
            edge,
            window,
            predicate,
        } => op_sample(cache, signals, edge_of, edge, window, predicate),
        OpReq::Stuck {
            signal,
            window,
            min_span,
            predicate,
        } => op_stuck(cache, signal, window, min_span, predicate),
        OpReq::Diff {
            signal,
            other_store,
            window,
            predicate,
        } => op_diff(cache, request_dir, signal, other_store, window, predicate),
        OpReq::Wave {
            signal,
            window,
            max_entries,
            predicate,
        } => op_wave(cache, signal, window, *max_entries, predicate),
    }
}
