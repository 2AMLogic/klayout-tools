//! Shared FST-store accumulator fed by either ingestion frontend (VCD via
//! `build_vcd.rs`, FST-passthrough via `build_fst.rs`). Written fresh for
//! this crate: neither upstream `booley` module has an equivalent shape
//! that is source-format-agnostic (its own `FstBuildHandler`, in
//! `fst.rs`, is VCD-only and tied to booley's vendored parallel
//! `fst-writer` fork -- see `fst_convert.rs`'s header for why that struct
//! itself was not ported). Uses the small ported helpers in
//! `fst_convert.rs` (`var_type_of`, `canon_bits_into`, `transition_scopes`)
//! plus this crate's own `cycle.rs` clock/reset tracking.

use std::collections::HashMap;
use std::fs::File;
use std::io::BufWriter;
use std::path::Path;

use crate::cycle::{ClockTracker, Edge, ResetTracker};
use crate::fst_convert::{canon_bits_into, transition_scopes, var_type_of};

/// The concrete FST writer type every store uses -- `fst_writer::open_fst`
/// only opens a file by path (it is not generic over an arbitrary
/// `Write + Seek`, see `writer.rs::FstHeaderWriter::open`), so
/// `StoreWriter` is not generic either.
type Body = fst_writer::FstBodyWriter<BufWriter<File>>;

/// One signal being written to the output store.
struct Group {
    fst_id: fst_writer::FstSignalId,
    width: u32,
    is_real: bool,
}

/// Accumulates value-change events from a source (VCD text or an existing
/// FST file) into a new FST store, while tracking the declared clock/reset
/// signals' edges and the overall value-change/time-range statistics
/// `klt wave build`'s response reports.
pub struct StoreWriter {
    body: Body,
    groups: Vec<Group>,
    /// VCD-id (or, for an FST source, a decimal string of the source
    /// handle) -> group index. A signal declared more than once under the
    /// same id (VCD aliasing) maps every alias to the same group.
    id_to_group: HashMap<String, usize>,
    clock_group: Option<usize>,
    clock_edge: Edge,
    clock_tracker: ClockTracker,
    reset_group: Option<usize>,
    reset_active_low: bool,
    reset_tracker: ResetTracker,
    ticks_to_ns: f64,
    value_change_count: u64,
    min_tick_seen: Option<u64>,
    max_tick_seen: Option<u64>,
    current_tick: Option<u64>,
    scratch: Vec<u8>,
}

/// One signal to declare in the output store's hierarchy, in declaration
/// order. `id` is the source-format-native identifier `record_change`
/// callers key changes on (a VCD id string, or an FST handle rendered as
/// a decimal string).
pub struct DeclSignal<'a> {
    pub id: &'a str,
    pub full_name: &'a str,
    pub width: u32,
    pub var_type: &'a str,
    pub is_real: bool,
}

impl StoreWriter {
    /// Create a new store at `out_path`, declare `signals` (already
    /// filtered to the request's `signals` allow-list, in source
    /// declaration order), and identify the clock/reset signals (by the
    /// same `id` values used in `signals`) that `record_change` should
    /// track.
    #[allow(clippy::too_many_arguments)]
    pub fn create(
        out_path: &Path,
        signals: &[DeclSignal],
        timescale_exponent: i8,
        ticks_to_ns: f64,
        clock_id: Option<&str>,
        clock_edge: Edge,
        reset_id: Option<&str>,
        reset_active_low: bool,
    ) -> Result<Self, String> {
        let info = fst_writer::FstInfo {
            start_time: 0,
            timescale_exponent,
            version: format!("klt-wave {}", env!("CARGO_PKG_VERSION")),
            date: String::new(),
            file_type: fst_writer::FstFileType::Verilog,
        };
        let mut hw = fst_writer::open_fst(out_path, &info)
            .map_err(|e| format!("cannot open store '{}': {e}", out_path.display()))?;

        let mut groups: Vec<Group> = Vec::new();
        let mut id_to_group: HashMap<String, usize> = HashMap::new();
        let mut current_scope: Vec<String> = Vec::new();
        let mut clock_group = None;
        let mut reset_group = None;

        for sig in signals {
            let mut parts: Vec<&str> = sig.full_name.split('.').collect();
            let var_name = parts.pop().unwrap_or(sig.full_name);
            transition_scopes(&mut hw, &mut current_scope, &parts)?;

            if let Some(&existing) = id_to_group.get(sig.id) {
                // Alias: same source id declared again under a different
                // name -- register the FST var as an alias of the same
                // signal, matching upstream's own duplicate-id handling.
                hw.var(
                    var_name,
                    if sig.is_real {
                        fst_writer::FstSignalType::real()
                    } else {
                        fst_writer::FstSignalType::bit_vec(sig.width)
                    },
                    var_type_of(sig.var_type),
                    fst_writer::FstVarDirection::Implicit,
                    Some(groups[existing].fst_id),
                )
                .map_err(|e| format!("fst var '{}': {e}", sig.full_name))?;
                continue;
            }

            let signal_tpe = if sig.is_real {
                fst_writer::FstSignalType::real()
            } else {
                fst_writer::FstSignalType::bit_vec(sig.width)
            };
            let fst_id = hw
                .var(
                    var_name,
                    signal_tpe,
                    var_type_of(sig.var_type),
                    fst_writer::FstVarDirection::Implicit,
                    None,
                )
                .map_err(|e| format!("fst var '{}': {e}", sig.full_name))?;

            let group_idx = groups.len();
            groups.push(Group {
                fst_id,
                width: sig.width,
                is_real: sig.is_real,
            });
            id_to_group.insert(sig.id.to_string(), group_idx);

            if Some(sig.id) == clock_id {
                clock_group = Some(group_idx);
            }
            if Some(sig.id) == reset_id {
                reset_group = Some(group_idx);
            }
        }
        transition_scopes(&mut hw, &mut current_scope, &[])?;

        let body = hw.finish().map_err(|e| format!("fst header finish: {e}"))?;

        Ok(StoreWriter {
            body,
            groups,
            id_to_group,
            clock_group,
            clock_edge,
            clock_tracker: ClockTracker::default(),
            reset_group,
            reset_active_low,
            reset_tracker: ResetTracker::default(),
            ticks_to_ns,
            value_change_count: 0,
            min_tick_seen: None,
            max_tick_seen: None,
            current_tick: None,
            scratch: Vec::new(),
        })
    }

    /// Whether `id` is one of the declared (allow-listed) signals -- the
    /// ingestion frontend should skip dispatching changes for ids that
    /// fail this check.
    pub fn is_declared(&self, id: &str) -> bool {
        self.id_to_group.contains_key(id)
    }

    /// Advance to a new absolute time (in trace ticks); must be called
    /// before `record_change` for that time, and with non-decreasing
    /// `tick` values (a straggling/backdated tick is coalesced into the
    /// current one, matching the ported VCD parser's own tolerance for
    /// non-monotonic timestamps).
    pub fn advance_time(&mut self, tick: u64) -> Result<(), String> {
        if self.current_tick.is_some_and(|cur| tick <= cur) {
            return Ok(());
        }
        self.body
            .time_change(tick)
            .map_err(|e| format!("fst time_change: {e}"))?;
        self.current_tick = Some(tick);
        self.min_tick_seen.get_or_insert(tick);
        self.max_tick_seen = Some(tick);
        Ok(())
    }

    /// Record a value change for `id` at the current time (set by the
    /// most recent `advance_time`). `bits` is the raw VCD-style bit text
    /// (already lowercase-normalized 0/1/x/z per bit, MSB-first) for a
    /// bit-vector signal, or a decimal real-value string for a real
    /// signal. A change for an id that was never declared (filtered out
    /// by the `signals` allow-list) is silently ignored by the caller via
    /// `is_declared` -- this method assumes `id` is declared.
    pub fn record_change(&mut self, id: &str, bits: &[u8]) {
        let Some(&group_idx) = self.id_to_group.get(id) else {
            return;
        };
        let tick = self.current_tick.unwrap_or(0);
        let group = &self.groups[group_idx];
        let fst_id = group.fst_id;

        if group.is_real {
            let text = std::str::from_utf8(bits).unwrap_or("nan");
            let value: f64 = text.parse().unwrap_or(f64::NAN);
            let le = value.to_le_bytes();
            let _ = self.body.signal_change(fst_id, &le);
        } else {
            let width = group.width as usize;
            if bits.len() == width {
                let _ = self.body.signal_change(fst_id, bits);
            } else {
                canon_bits_into(bits, width, &mut self.scratch);
                let scratch = std::mem::take(&mut self.scratch);
                let _ = self.body.signal_change(fst_id, &scratch);
                self.scratch = scratch;
            }
        }
        self.value_change_count += 1;

        if Some(group_idx) == self.clock_group {
            if let Some(&value) = bits.last() {
                self.clock_tracker.observe(self.clock_edge, value, tick);
            }
        }
        if Some(group_idx) == self.reset_group {
            if let Some(&value) = bits.last() {
                self.reset_tracker
                    .observe(self.reset_active_low, value, tick);
            }
        }
    }

    pub fn value_change_count(&self) -> u64 {
        self.value_change_count
    }

    pub fn time_range_ticks(&self) -> (Option<u64>, Option<u64>) {
        (self.min_tick_seen, self.max_tick_seen)
    }

    pub fn ticks_to_ns(&self, tick: u64) -> f64 {
        tick as f64 * self.ticks_to_ns
    }

    pub fn clock_tracker(&self) -> &ClockTracker {
        &self.clock_tracker
    }

    pub fn reset_tracker(&self) -> &ResetTracker {
        &self.reset_tracker
    }

    pub fn signal_count(&self) -> usize {
        self.groups.len()
    }

    /// Finish writing the store: flushes the final value-change section
    /// and updates the FST header in place (seeking back into `W`).
    /// `FstBodyWriter::finish` does not hand the underlying writer back,
    /// so a caller that needs to measure/hash the finished file (as
    /// `build.rs` does) reopens it by path afterward rather than reusing
    /// this method's `W`.
    pub fn finish(self) -> Result<(), String> {
        self.body.finish().map_err(|e| format!("fst finish: {e}"))
    }
}
