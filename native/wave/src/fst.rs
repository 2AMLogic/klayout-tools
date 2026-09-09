// Ported and adapted from boldaxolotl/booley, crates/bwave/src/fst.rs
// (commit 0c3b4cd7b66a0f793de605ce58e82dc1bc0ac892,
// https://github.com/boldaxolotl/booley).
// Copyright (c) the boldaxolotl/booley contributors.
// Licensed under the Apache License, Version 2.0; see /NOTICE.
//
// Modifications from upstream:
// - Only the FST-read path is ported (`FstBacking`, `load_fst`, and the
//   value/window-read primitives they need). Upstream's `fst.rs` also holds
//   the VCD-streaming-to-FST *build* path (`ChunkParser`, `FstBuildHandler`,
//   `parse_bytes_parallel`, the byte-oriented VCD scanner used for
//   attribution benchmarks) -- none of that is ported here. `klt wave`
//   splits build and query into separate verbs/issues (see
//   docs/design/waveform-query-contract-spike.md section 10's own
//   `index.rs`/`extract.rs` disposition for the same reasoning); this
//   issue (#1599, Phase 2a) is query-only, so no VCD ingestion or FST
//   writing code is needed here.
// - `FxHashMap`/`FxHashSet` (the `rustc-hash` crate) replaced with
//   `std::collections::HashMap` to avoid an extra dependency not otherwise
//   needed by this crate; behavior is unchanged (the upstream crate uses
//   the faster hasher purely for throughput on much larger multi-GB
//   traces than this crate's own read paths are sized for today).
// - `crate::parser`/`crate::vcd_chunk`/`crate::profile` imports removed
//   (VCD-build-path-only, not ported).
// - Cross-module callers renamed to this crate's own module names
//   (`crate::cache`, `crate::format`, `crate::signal`).
// - One nested `if` collapsed into its parent condition
//   (`clippy::collapsible_if`, mechanical/behavior-preserving) to satisfy
//   this crate's own `cargo clippy -- -D warnings` CI gate.

//! FST-backed implementation of the `ColumnCache` read interface.
//!
//! Loads a plain FST waveform (produced by a `klt wave build` step, or
//! dumped natively by a simulator) and presents it through the
//! `ColumnCache` surface the query-op layer (`crate::query`) consumes. The
//! disk format is pure FST -- no sidecars, no custom attributes -- so every
//! trace this crate reads opens directly in any standard FST viewer.
//!
//! Reader library: `fst-reader` (not `wellen` -- `wellen` merges bit-blasted
//! vars like `bus[0]`/`bus[1]` into synthetic vectors with no opt-out, which
//! breaks signal-name parity with the raw FST/VCD view; matches the upstream
//! `bwave` project's own documented choice for the same reason).

use std::collections::HashMap;
use std::fs::File;
use std::io::BufReader;
use std::path::Path;
use std::sync::Mutex;

use fst_reader::{
    FstFilter, FstHierarchyEntry, FstReader, FstSignalHandle, FstSignalValue, FstVarType,
};

use crate::cache::{CachedSignal, ClockEntry, ColumnCache};
use crate::format::format_value;

/// f64 read from an FST frame slot that was never written: fst-writer fills
/// frames with ASCII 'x' bytes, so a real signal with no change before the
/// first time step reads back as this exact bit pattern. We drop it.
const REAL_FRAME_GARBAGE_BITS: u64 = u64::from_le_bytes([b'x'; 8]);

/// Read-side backing for one open FST file: the reader itself plus decode
/// memoization for the handful of signals the loader probes at load time
/// (clock/reset candidates) and for windowed multi-signal prefetches.
#[allow(clippy::type_complexity)]
pub struct FstBacking {
    reader: Mutex<FstReader<BufReader<File>>>,
    /// Per signal index (parallel to `ColumnCache::signals`).
    handles: Vec<usize>, // FstSignalHandle indices (handle is not Clone)
    is_real: Vec<bool>,
    /// Real-ness indexed by FST handle (hot-path lookup for bulk decodes).
    is_real_by_handle: Vec<bool>,
    /// Decoded full streams for the handful of signals the loader probed
    /// (clock/reset candidates), keyed by FST handle index so aliases share
    /// one entry. Deliberately NOT a general cache: memoizing every stream a
    /// query touches would hold multi-GB of decoded strings on
    /// high-activity traces.
    memo: Mutex<HashMap<usize, std::sync::Arc<Vec<(u64, String)>>>>,
    /// Prefix streams (all changes in `[0, max_tick]`) bulk-decoded by
    /// `prefetch_to`; serves every windowed read with `tick_max <= max_tick`
    /// without another file pass.
    prefix: Mutex<Option<PrefixMemo>>,
}

struct PrefixMemo {
    max_tick: u64,
    by_handle: HashMap<usize, Vec<(u64, String)>>,
}

impl FstBacking {
    #[inline]
    fn handle_is_real(&self, handle_idx: usize) -> bool {
        self.is_real_by_handle
            .get(handle_idx)
            .copied()
            .unwrap_or(false)
    }

    /// Bulk-decode the `[0, max_tick]` prefix of many signals in ONE file
    /// pass (each fst-reader pass re-decodes the time table and block
    /// metadata, so per-signal passes dominate point-query latency). Called
    /// by windowed query entry points (`sample`/`wave`/`diff`) with their
    /// matched signal set; the pass early-terminates past `max_tick`, so
    /// memory is bounded by the window, not the trace.
    pub(crate) fn prefetch_to(&self, sig_indices: &[usize], max_tick: u64) {
        if sig_indices.len() <= 1 {
            return; // a single signal costs the same either way
        }
        {
            let prefix = self.prefix.lock().unwrap();
            if let Some(p) = prefix.as_ref() {
                if p.max_tick >= max_tick
                    && sig_indices
                        .iter()
                        .all(|&i| p.by_handle.contains_key(&self.handles[i]))
                {
                    return;
                }
            }
        }
        let mut need: Vec<usize> = sig_indices.iter().map(|&i| self.handles[i]).collect();
        need.sort_unstable();
        need.dedup();
        let handles: Vec<FstSignalHandle> = need
            .iter()
            .map(|&h| FstSignalHandle::from_index(h))
            .collect();
        let mut by_handle: HashMap<usize, Vec<(u64, String)>> = HashMap::new();
        {
            let mut reader = self.reader.lock().unwrap();
            let _ = reader.read_signals(
                &FstFilter::new(0, max_tick, handles),
                |time, h, value| -> Result<(), ()> {
                    if time <= max_tick {
                        let hi = h.get_index();
                        if let Some(v) = canon_value(&value, self.handle_is_real(hi)) {
                            by_handle.entry(hi).or_default().push((time, v));
                        }
                    }
                    Ok(())
                },
            );
        }
        for (&h, list) in by_handle.iter_mut() {
            strip_real_frame_garbage(list, self.handle_is_real(h));
        }
        // signals with no changes in the window still need an entry so the
        // hit-check above and read_range know they were covered
        for h in need {
            by_handle.entry(h).or_default();
        }
        *self.prefix.lock().unwrap() = Some(PrefixMemo {
            max_tick,
            by_handle,
        });
    }

    /// All transitions of one signal, in canonical value-string form.
    /// Loader-probed signals (clock/reset) are served from the memo; other
    /// signals decode directly per call.
    pub(crate) fn read_all(&self, sig_idx: usize) -> Vec<(u64, String)> {
        let h = self.handles[sig_idx];
        if let Some(hit) = self.memo.lock().unwrap().get(&h) {
            return hit.as_ref().clone();
        }
        let is_real = self.is_real[sig_idx];
        let mut out: Vec<(u64, String)> = Vec::new();
        {
            let mut reader = self.reader.lock().unwrap();
            let _ = reader.read_signals(
                &FstFilter::filter_signals(vec![FstSignalHandle::from_index(h)]),
                |time, _h, value| -> Result<(), ()> {
                    if let Some(v) = canon_value(&value, is_real) {
                        out.push((time, v));
                    }
                    Ok(())
                },
            );
        }
        strip_real_frame_garbage(&mut out, is_real);
        out
    }

    /// Transitions of one signal within `[tick_min, tick_max]` plus the last
    /// value before the range. Served from the prefix memo when covered;
    /// otherwise one windowed file pass from time 0 (section-frame semantics
    /// can never hide a value that precedes the window) with early
    /// termination past `tick_max`.
    pub(crate) fn read_range(
        &self,
        sig_idx: usize,
        tick_min: u64,
        tick_max: u64,
    ) -> (Option<String>, Vec<(u64, String)>) {
        let h = self.handles[sig_idx];
        {
            let prefix = self.prefix.lock().unwrap();
            if let Some(p) = prefix.as_ref() {
                if p.max_tick >= tick_max {
                    if let Some(stream) = p.by_handle.get(&h) {
                        return split_window(stream, tick_min, tick_max);
                    }
                }
            }
        }
        if let Some(hit) = self.memo.lock().unwrap().get(&h) {
            return split_window(hit, tick_min, tick_max);
        }
        let is_real = self.is_real[sig_idx];
        let mut all: Vec<(u64, String)> = Vec::new();
        {
            let mut reader = self.reader.lock().unwrap();
            let _ = reader.read_signals(
                &FstFilter::new(0, tick_max, vec![FstSignalHandle::from_index(h)]),
                |time, _h, value| -> Result<(), ()> {
                    if time <= tick_max {
                        if let Some(v) = canon_value(&value, is_real) {
                            all.push((time, v));
                        }
                    }
                    Ok(())
                },
            );
        }
        strip_real_frame_garbage(&mut all, is_real);
        split_window(&all, tick_min, tick_max)
    }

    /// One batched pass over several signals (used for clock/reset
    /// re-derivation at load). Returns raw transition lists per handle index.
    fn read_many(
        reader: &mut FstReader<BufReader<File>>,
        handle_indices: &[usize],
    ) -> HashMap<usize, Vec<(u64, String)>> {
        let mut by_handle: HashMap<usize, Vec<(u64, String)>> = HashMap::new();
        if handle_indices.is_empty() {
            return by_handle;
        }
        let handles: Vec<FstSignalHandle> = handle_indices
            .iter()
            .map(|&i| FstSignalHandle::from_index(i))
            .collect();
        let _ = reader.read_signals(
            &FstFilter::filter_signals(handles),
            |time, h, value| -> Result<(), ()> {
                if let Some(v) = canon_value(&value, false) {
                    by_handle.entry(h.get_index()).or_default().push((time, v));
                }
                Ok(())
            },
        );
        by_handle
    }
}

/// Convert an FST value to the canonical stored-string form: 1-char values
/// as-is, multi-bit with x/z kept as bit text, pure-binary converted to
/// leading-zero-stripped uppercase hex.
fn canon_value(value: &FstSignalValue, is_real: bool) -> Option<String> {
    match value {
        FstSignalValue::String(bytes) => {
            // FST stores bit strings at full declared width; the minimal
            // form strips implied left-extension padding so query output is
            // independent of dump dialect.
            let s = std::str::from_utf8(bytes).ok()?;
            Some(crate::cache::minimal_xz(format_value(s)))
        }
        FstSignalValue::Real(r) => {
            if is_real && r.to_bits() == REAL_FRAME_GARBAGE_BITS {
                // frame slot for a never-initialized real -- no genuine value
                return Some(String::new()); // marker, stripped below
            }
            // Debug formatting keeps a decimal point ("100.0", not "100"),
            // matching the raw VCD real tokens this format stores. Never
            // run reals through format_value -- a value like 100 would be
            // mistaken for binary and hexified.
            Some(format!("{r:?}"))
        }
    }
}

/// Remove the frame-garbage marker produced by `canon_value` for reals.
fn strip_real_frame_garbage(list: &mut Vec<(u64, String)>, is_real: bool) {
    if is_real {
        list.retain(|(_, v)| !v.is_empty());
    }
}

/// Split a `[0, ..]` change stream into (last value before `tick_min`,
/// changes within `[tick_min, tick_max]`).
fn split_window(
    stream: &[(u64, String)],
    tick_min: u64,
    tick_max: u64,
) -> (Option<String>, Vec<(u64, String)>) {
    let mut before: Option<String> = None;
    let mut in_range = Vec::new();
    for (t, v) in stream {
        if *t < tick_min {
            before = Some(v.clone());
        } else if *t <= tick_max {
            in_range.push((*t, v.clone()));
        } else {
            break;
        }
    }
    (before, in_range)
}

// ---------------------------------------------------------------- loading

struct VarEntry {
    name: String,
    width: u32,
    var_type: String,
    handle_idx: usize,
    is_real: bool,
}

fn var_type_str(tpe: FstVarType) -> &'static str {
    match tpe {
        FstVarType::Wire => "wire",
        FstVarType::Reg => "reg",
        FstVarType::Integer => "integer",
        FstVarType::Parameter => "parameter",
        FstVarType::Real | FstVarType::RealParameter | FstVarType::ShortReal => "real",
        FstVarType::RealTime => "realtime",
        FstVarType::Time => "time",
        FstVarType::Logic => "logic",
        FstVarType::Bit => "bit",
        FstVarType::Supply0 => "supply0",
        FstVarType::Supply1 => "supply1",
        FstVarType::Tri => "tri",
        FstVarType::TriAnd => "triand",
        FstVarType::TriOr => "trior",
        FstVarType::TriReg => "trireg",
        FstVarType::Tri0 => "tri0",
        FstVarType::Tri1 => "tri1",
        FstVarType::Wand => "wand",
        FstVarType::Wor => "wor",
        FstVarType::Event => "event",
        FstVarType::Port => "port",
        FstVarType::Int => "int",
        _ => "wire",
    }
}

/// Join the VCD token form "name [7:0]" / "name [3]" into "name[7:0]".
/// GTKWave-family writers (Verilator's embedded fstapi) keep the two-token
/// VCD form in the FST hierarchy; joining them here keeps FST-loaded
/// directory names consistent with plain-VCD-sourced ones. Only a
/// digits/colon bracket group after a single space is joined -- escaped
/// identifiers with other embedded spaces pass through untouched.
fn join_bit_range(name: String) -> String {
    if name.ends_with(']') {
        if let Some(pos) = name.rfind(" [") {
            let inner = &name[pos + 2..name.len() - 1];
            if !inner.is_empty() && inner.bytes().all(|b| b.is_ascii_digit() || b == b':') {
                let mut out = String::with_capacity(name.len() - 1);
                out.push_str(&name[..pos]);
                out.push_str(&name[pos + 1..]);
                return out;
            }
        }
    }
    name
}

/// Render an FST timescale exponent back to a VCD-style timescale string.
/// exponent -9 -> "1ns", -10 -> "100ps", -11 -> "10ps".
fn timescale_str_from_exponent(exp: i8) -> String {
    let units: [(i8, &str); 6] = [
        (0, "s"),
        (-3, "ms"),
        (-6, "us"),
        (-9, "ns"),
        (-12, "ps"),
        (-15, "fs"),
    ];
    for (u, name) in units {
        let delta = exp as i32 - u as i32;
        if (0..=2).contains(&delta) {
            let factor = 10_i32.pow(delta as u32);
            return format!("{factor}{name}");
        }
    }
    // out of the VCD range -- fall back to 1ns semantics
    "1ns".to_string()
}

/// Load a plain FST file behind the `ColumnCache` interface.
/// Returns `None` if the file is missing or not a readable FST.
pub fn load_fst(path: &Path) -> Option<ColumnCache> {
    let file = File::open(path).ok()?;
    let mut reader = FstReader::open(BufReader::new(file)).ok()?;
    let header = reader.get_header();

    // -- signal directory from the raw FST hierarchy -----------------------
    let mut vars: Vec<VarEntry> = Vec::new();
    let mut scope_stack: Vec<String> = Vec::new();
    reader
        .read_hierarchy(|entry| match entry {
            FstHierarchyEntry::Scope { name, .. } => scope_stack.push(name),
            FstHierarchyEntry::UpScope => {
                scope_stack.pop();
            }
            FstHierarchyEntry::Var {
                tpe,
                name,
                length,
                handle,
                is_alias,
                ..
            } => {
                let name = join_bit_range(name);
                let full = if scope_stack.is_empty() {
                    name
                } else {
                    format!("{}.{}", scope_stack.join("."), name)
                };
                let is_real = matches!(
                    tpe,
                    FstVarType::Real
                        | FstVarType::RealTime
                        | FstVarType::RealParameter
                        | FstVarType::ShortReal
                );
                // reals report their byte length; a real is declared as 64-bit
                let width = if is_real {
                    64
                } else if length == 0 {
                    1
                } else {
                    length
                };
                let mut var_type = var_type_str(tpe).to_string();
                let mut width = width;
                let mut is_real = is_real;
                // Alias groups are keyed by FST handle and keep only the
                // FIRST declaration's metadata for the whole group (an alias
                // with a different declared type, e.g. `reg` in tb and
                // `wire` in dut, shows the first type for both entries).
                if is_alias {
                    if let Some(first) = vars.iter().find(|v| v.handle_idx == handle.get_index()) {
                        var_type = first.var_type.clone();
                        width = first.width;
                        is_real = first.is_real;
                    }
                }
                vars.push(VarEntry {
                    name: full,
                    width,
                    var_type,
                    handle_idx: handle.get_index(),
                    is_real,
                });
            }
            _ => {}
        })
        .ok()?;

    let signals: Vec<CachedSignal> = vars
        .iter()
        .map(|v| CachedSignal {
            name: v.name.clone(),
            width: v.width,
            var_type: v.var_type.clone(),
            // aliases share the FST handle; queries dedup alias groups by
            // group_id, so expose the handle index there.
            group_id: v.handle_idx as u64,
        })
        .collect();

    // -- clock + reset re-derivation ---------------------------------------
    // Candidates are 1-bit signals whose bracket-stripped name contains
    // "clk" (resp. "rst"), sorted by (scope depth, name).
    let mut clock_candidates: Vec<usize> = Vec::new();
    let mut reset_candidates: Vec<usize> = Vec::new();
    for (i, v) in vars.iter().enumerate() {
        if v.width == 1 && !v.is_real {
            let stripped = v.name.split('[').next().unwrap_or(&v.name).to_lowercase();
            if stripped.contains("clk") {
                clock_candidates.push(i);
            }
            if stripped.contains("rst") {
                reset_candidates.push(i);
            }
        }
    }
    let by_depth_then_name = |a: &usize, b: &usize| {
        let da = vars[*a].name.matches('.').count();
        let db = vars[*b].name.matches('.').count();
        da.cmp(&db).then(vars[*a].name.cmp(&vars[*b].name))
    };
    clock_candidates.sort_by(by_depth_then_name);
    reset_candidates.sort_by(by_depth_then_name);

    // one batched read for every candidate signal
    let mut probe_handles: Vec<usize> = Vec::new();
    for &i in clock_candidates.iter().chain(reset_candidates.iter()) {
        probe_handles.push(vars[i].handle_idx);
    }
    probe_handles.sort_unstable();
    probe_handles.dedup();
    let probe_data = FstBacking::read_many(&mut reader, &probe_handles);
    let empty: Vec<(u64, String)> = Vec::new();
    let trans_of = |var_idx: usize| -> &Vec<(u64, String)> {
        probe_data.get(&vars[var_idx].handle_idx).unwrap_or(&empty)
    };

    // first two rising edges per clock candidate -> (period, first_rise)
    let rises = |transitions: &[(u64, String)]| -> (Option<u64>, Option<u64>) {
        let mut prev = "x";
        let (mut first, mut second) = (None, None);
        for (t, v) in transitions {
            if v == "1" && prev == "0" {
                if first.is_none() {
                    first = Some(*t);
                } else if second.is_none() {
                    second = Some(*t);
                    break;
                }
            }
            prev = v;
        }
        (first, second)
    };

    // clock table, deduplicated by (period, phase)
    let mut clock_table: Vec<ClockEntry> = Vec::new();
    let mut primary_clock: Option<(u64, u64, String)> = None; // period, first_rise, name
    for &ci in &clock_candidates {
        let (first, second) = rises(trans_of(ci));
        if primary_clock.is_none() {
            if let (Some(f), Some(s)) = (first, second) {
                if s > f {
                    primary_clock = Some((s - f, f, vars[ci].name.clone()));
                }
            }
        }
        if let (Some(f), Some(s)) = (first, second) {
            if s > f {
                let period = s - f;
                let phase = f % period;
                let dup = clock_table
                    .iter()
                    .any(|e| e.period == period && e.first_rise % e.period == phase);
                if !dup {
                    clock_table.push(ClockEntry {
                        period,
                        first_rise: f,
                        id: vars[ci].name.clone(),
                    });
                }
            }
        }
    }
    // Note: primary clock = first candidate *with a valid period*. A
    // never-toggling first candidate stores no clock meta at all, matching
    // primary_clock = None here unless a later candidate toggles.

    // reset: first candidate; active-low from the leaf name
    let mut reset_deassert_tick: Option<u64> = None;
    let mut reset_transitions: Option<&Vec<(u64, String)>> = None;
    let mut reset_active_low = false;
    if let Some(&ri) = reset_candidates.first() {
        let leaf = vars[ri]
            .name
            .split('[')
            .next()
            .unwrap_or(&vars[ri].name)
            .split('.')
            .next_back()
            .unwrap_or(&vars[ri].name)
            .to_lowercase();
        reset_active_low = leaf.ends_with('n') || leaf.contains("_n");
        let rt = trans_of(ri);
        reset_transitions = Some(rt);
        // reset asserted-state starts true; the deassert tick is the first
        // 0/1 transition observed in the deasserted state
        for (t, v) in rt {
            let asserted = if reset_active_low { v == "0" } else { v == "1" };
            if (v == "0" || v == "1") && !asserted {
                reset_deassert_tick = Some(*t);
                break;
            }
        }
    }
    // reset asserted-state after all events at `tick`: last 0/1 value at or
    // before tick, initial state = asserted
    let reset_asserted_after = |tick: u64| -> bool {
        match reset_transitions {
            None => false, // no reset signal -- count every edge
            Some(rt) => {
                let mut asserted = true;
                for (t, v) in rt {
                    if *t > tick {
                        break;
                    }
                    if v == "0" || v == "1" {
                        asserted = if reset_active_low { v == "0" } else { v == "1" };
                    }
                }
                asserted
            }
        }
    };

    // clock-before-reset flag (approximation): FST cannot represent VCD line
    // order within one timestamp, so this is approximated as "the clock has
    // a transition at exactly the deassert tick".
    let mut clock_before_reset_at_deassert = false;
    if let (Some(dt), Some(&ci)) = (reset_deassert_tick, clock_candidates.first()) {
        clock_before_reset_at_deassert = trans_of(ci).iter().any(|(t, _)| *t == dt);
    }

    // sim range: end = last time in the file; start = first counted cycle
    // (first primary-clock rising edge with reset inactive after that tick),
    // falling back to the header start time. `trace_start_tick` retains the
    // header's true start separately -- it is the default `window.from`
    // (contract section 5: an omitted window covers "the whole trace"),
    // which `sim_start_tick` below no longer represents once it is rebased
    // to the cycle-0 anchor.
    let sim_end_tick = header.end_time;
    let trace_start_tick = header.start_time;
    let mut sim_start_tick = header.start_time;
    if let Some(&ci) = clock_candidates.first() {
        let mut prev = "x";
        for (t, v) in trans_of(ci) {
            if v == "1" && prev == "0" && !reset_asserted_after(*t) {
                sim_start_tick = *t;
                break;
            }
            prev = v;
        }
    }

    let (clock_period_ticks, first_rise_tick, clock_id) = match primary_clock {
        Some((p, f, name)) => (p, f, name),
        None => (0, 0, String::new()),
    };

    let ticks_to_ns = 10f64.powi(header.timescale_exponent as i32 + 9);
    let timescale_str = timescale_str_from_exponent(header.timescale_exponent);

    let mut is_real_by_handle =
        vec![false; vars.iter().map(|v| v.handle_idx + 1).max().unwrap_or(0)];
    for v in &vars {
        if v.is_real {
            is_real_by_handle[v.handle_idx] = true;
        }
    }
    // Seed the decode memo with the clock/reset probe results -- sync-mode
    // queries re-read those signals immediately (reset rebasing) and must
    // not pay a second file pass for them.
    let mut memo: HashMap<usize, std::sync::Arc<Vec<(u64, String)>>> = HashMap::new();
    for (h, list) in probe_data {
        memo.insert(h, std::sync::Arc::new(list));
    }
    let backing = FstBacking {
        reader: Mutex::new(reader),
        handles: vars.iter().map(|v| v.handle_idx).collect(),
        is_real: vars.iter().map(|v| v.is_real).collect(),
        is_real_by_handle,
        memo: Mutex::new(memo),
        prefix: Mutex::new(None),
    };

    Some(ColumnCache::new_fst_backed(
        signals,
        sim_start_tick,
        trace_start_tick,
        sim_end_tick,
        ticks_to_ns,
        clock_period_ticks,
        first_rise_tick,
        timescale_str,
        clock_id,
        clock_before_reset_at_deassert,
        clock_table,
        backing,
    ))
}
