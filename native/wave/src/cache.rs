// Ported and adapted from boldaxolotl/booley, crates/bwave/src/cache.rs
// (commit 0c3b4cd7b66a0f793de605ce58e82dc1bc0ac892,
// https://github.com/boldaxolotl/booley).
// Copyright (c) the boldaxolotl/booley contributors.
// Licensed under the Apache License, Version 2.0; see /NOTICE.
//
// Modifications from upstream:
// - Only the `ColumnCache` read-path struct/impl and `minimal_xz` are
//   ported. Upstream's `cache.rs` also holds the ten `*_from_cache` query
//   functions that implement bwave's own CLI surface (they print directly
//   to stdout/stderr in bwave's own text/JSON output shapes, call
//   `std::process::exit`, and read a `crate::ExtractConfig` this crate does
//   not have) -- those are not ported verbatim. `crate::query` in this
//   crate implements the equivalent query vocabulary
//   (docs/design/waveform-query-contract-spike.md section 5) as fresh code
//   returning typed results for klt's own JSON envelope, informed by (but
//   not copied from) those functions' algorithms.
// - `match_signals`/`detect_clock_from_pattern` no longer call
//   `std::process::exit`/`eprintln!` on a bad pattern; they return
//   `Result<_, String>` instead, since exit-code selection is this crate's
//   own CLI layer's job (`src/main.rs`), not the read-path library's.
// - The `rayon`/`serde::Serialize` imports and the CLI-facing
//   `no_signals_in_store_message`/`report_unmatched_patterns` helpers are
//   dropped (unused by the ported subset).

//! Query-facing read layer over the FST waveform store.
//!
//! `ColumnCache` presents header metadata (sim range, timescale, clock
//! table) plus transition-read primitives; `crate::query` implements the
//! `klt wave query` op vocabulary on top of it. The on-disk format is plain
//! FST (see `crate::fst`); this module holds no format knowledge of its own.

use std::path::Path;

use crate::signal::{compile_patterns, match_signal};

/// Clock entry for the multi-clock table (re-derived at FST load).
#[derive(Debug, Clone)]
pub struct ClockEntry {
    pub period: u64,
    pub first_rise: u64,
    pub id: String,
}

/// Reduce a stored text value to its minimal VCD form. Simulators disagree
/// on how much left-extension padding they dump; normalizing at decode keeps
/// query output independent of dump dialect. Two rules, both
/// semantics-preserving under the IEEE 1364 left-extension:
/// - a leading run of the same x/z char collapses to one
///   ("xxxx01" == "x01")
/// - leading 0-fill padding on a bit-text value drops ("0001z" == "01z" ==
///   "1z"), keeping one '0' when the first significant char is x/z --
///   "0z1" and "z1" extend differently, so that zero is load-bearing.
pub(crate) fn minimal_xz(mut s: String) -> String {
    let b = s.as_bytes();
    if b.len() <= 1 {
        return s;
    }
    let first = b[0].to_ascii_lowercase();
    if matches!(first, b'x' | b'z') {
        let c = b[0];
        let mut run = 0;
        while run + 1 < b.len() && b[run + 1] == c {
            run += 1;
        }
        if run > 0 {
            s.drain(..run);
        }
        return s;
    }
    if first == b'0' {
        // Only pure bit-text values (this path also stores reals and other
        // tokens, where a leading zero is not padding).
        let is_bit_text = b
            .iter()
            .all(|&c| matches!(c.to_ascii_lowercase(), b'0' | b'1' | b'x' | b'z'))
            && b.iter()
                .any(|&c| matches!(c.to_ascii_lowercase(), b'x' | b'z'));
        if is_bit_text {
            let mut start = 0;
            while start + 1 < b.len() && b[start] == b'0' {
                start += 1;
            }
            if matches!(b[start].to_ascii_lowercase(), b'x' | b'z') && start > 0 {
                start -= 1; // keep one zero: the fill char is significant
            }
            if start > 0 {
                s.drain(..start);
            }
        }
    }
    s
}

/// Directory entry for one signal in the store.
#[derive(Debug, Clone)]
pub struct CachedSignal {
    pub name: String,
    pub width: u32,
    pub var_type: String,
    /// Alias-group key (the FST handle index): aliases of the same
    /// underlying signal share it, and queries dedup alias groups by it.
    pub group_id: u64,
}

/// Query-facing view of an FST waveform store: header metadata plus read
/// primitives, all backed by `crate::fst::FstBacking`.
pub struct ColumnCache {
    pub sim_start_tick: u64,
    pub sim_end_tick: u64,
    pub ticks_to_ns: f64,
    pub clock_period_ticks: u64,
    pub first_rise_tick: u64,
    pub timescale_str: String,
    pub clock_id: String,
    pub clock_before_reset_at_deassert: bool,
    pub clock_table: Vec<ClockEntry>,
    pub signals: Vec<CachedSignal>,
    fst: crate::fst::FstBacking,
}

impl ColumnCache {
    /// Load a waveform store. Delegates to the FST loader; returns `None`
    /// if the file is missing or not a readable FST.
    pub fn load_from_file(store_path: &Path) -> Option<ColumnCache> {
        crate::fst::load_fst(store_path)
    }

    /// Distinct signals in the store, deduplicated by `group_id`.
    pub fn unique_signal_count(&self) -> usize {
        let mut groups = std::collections::HashSet::new();
        self.signals
            .iter()
            .filter(|s| groups.insert(s.group_id))
            .count()
    }

    /// Construct a cache over an open FST backing (see `crate::fst::load_fst`).
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn new_fst_backed(
        signals: Vec<CachedSignal>,
        sim_start_tick: u64,
        sim_end_tick: u64,
        ticks_to_ns: f64,
        clock_period_ticks: u64,
        first_rise_tick: u64,
        timescale_str: String,
        clock_id: String,
        clock_before_reset_at_deassert: bool,
        clock_table: Vec<ClockEntry>,
        backing: crate::fst::FstBacking,
    ) -> ColumnCache {
        ColumnCache {
            sim_start_tick,
            sim_end_tick,
            ticks_to_ns,
            clock_period_ticks,
            first_rise_tick,
            timescale_str,
            clock_id,
            clock_before_reset_at_deassert,
            clock_table,
            signals,
            fst: backing,
        }
    }

    /// Detect a clock period from a cached signal's transitions. Finds the
    /// signal matching `pattern`, reads its rising edges, and returns
    /// `(period, first_rise, name)`.
    pub fn detect_clock_from_pattern(&self, pattern: &str) -> Result<(u64, u64, String), String> {
        let matchers = compile_patterns(&[pattern.to_string()])
            .map_err(|e| format!("invalid clock pattern: {e}"))?;

        let mut candidates: Vec<(usize, &str)> = self
            .signals
            .iter()
            .enumerate()
            .filter(|(_, s)| s.width == 1 && match_signal(&s.name, &matchers))
            .map(|(i, s)| (i, s.name.as_str()))
            .collect();

        if candidates.is_empty() {
            return Err(format!("no 1-bit signal matches clock pattern '{pattern}'"));
        }

        candidates.sort_by(|a, b| {
            let da = a.1.matches('.').count();
            let db = b.1.matches('.').count();
            da.cmp(&db).then(a.1.cmp(b.1))
        });

        let (sig_idx, clock_name) = candidates[0];
        let transitions = self.read_transitions(sig_idx);

        let mut rising_ticks: Vec<u64> = Vec::new();
        let mut prev_val = "x";
        for (tick, val) in &transitions {
            if (val == "1" || val == "01") && (prev_val == "0" || prev_val == "00") {
                rising_ticks.push(*tick);
            }
            prev_val = val;
        }

        if rising_ticks.len() < 2 {
            return Err(format!(
                "clock '{clock_name}' has fewer than 2 rising edges -- cannot determine period"
            ));
        }

        let first_rise = rising_ticks[0];
        let period = rising_ticks[1] - rising_ticks[0];
        if period == 0 {
            return Err(format!(
                "clock '{clock_name}' has zero-width period (edges at same tick)"
            ));
        }

        Ok((period, first_rise, clock_name.to_string()))
    }

    /// Bulk-read hint from windowed query entry points: decode the
    /// `[0, tick_max]` prefix of all `sig_indices` in ONE FST pass before
    /// the per-signal range reads start.
    pub fn prefetch_window(&self, sig_indices: &[usize], tick_max: u64) {
        self.fst.prefetch_to(sig_indices, tick_max);
    }

    /// Read all transitions for a signal by index.
    pub fn read_transitions(&self, sig_idx: usize) -> Vec<(u64, String)> {
        self.fst.read_all(sig_idx)
    }

    /// Read transitions in a tick range.
    /// Returns (before_value, transitions_in_range).
    /// `before_value` is the last value at or before `tick_min` (None if no
    /// transition before range).
    pub fn read_transitions_range(
        &self,
        sig_idx: usize,
        tick_min: u64,
        tick_max: u64,
    ) -> (Option<String>, Vec<(u64, String)>) {
        self.fst.read_range(sig_idx, tick_min, tick_max)
    }

    /// Get the value of a signal at a specific tick.
    pub fn value_at_tick_direct(&self, sig_idx: usize, tick: u64) -> String {
        let (before, transitions) = self.read_transitions_range(sig_idx, tick, tick);
        transitions
            .last()
            .map(|(_, v)| v.clone())
            .or(before)
            .unwrap_or_else(|| "x".to_string())
    }

    /// Find cached signal indices matching glob patterns (see
    /// `crate::signal::compile_patterns`). Deduplicates by alias group,
    /// keeping the LAST matching alias per group.
    pub fn match_signals(&self, patterns: &[String]) -> Result<Vec<usize>, String> {
        let matchers = compile_patterns(patterns)?;
        let mut group_pos: std::collections::HashMap<u64, usize> = std::collections::HashMap::new();
        let mut results: Vec<usize> = Vec::new();
        for (i, s) in self.signals.iter().enumerate() {
            if match_signal(&s.name, &matchers) {
                if let Some(&p) = group_pos.get(&s.group_id) {
                    results[p] = i; // overwrite with later alias
                } else {
                    group_pos.insert(s.group_id, results.len());
                    results.push(i);
                }
            }
        }
        Ok(results)
    }

    /// Find the single cached signal index matching `pattern`, erroring on
    /// zero or more than one distinct alias-group match -- every op in
    /// `docs/design/waveform-query-contract-spike.md` section 5 names
    /// exactly one signal per op.
    pub fn match_one_signal(&self, pattern: &str) -> Result<usize, String> {
        let matches = self.match_signals(std::slice::from_ref(&pattern.to_string()))?;
        match matches.len() {
            0 => Err(format!("no signal in store matches '{pattern}'")),
            1 => Ok(matches[0]),
            n => Err(format!(
                "signal pattern '{pattern}' is ambiguous: matched {n} distinct signals"
            )),
        }
    }
}
