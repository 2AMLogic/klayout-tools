//! FST-passthrough ingestion frontend for `klt wave build`. Written fresh
//! (not a port -- `booley`'s `bwave` has no equivalent "build a store from
//! an already-FST trace" path; it treats FST input as directly queryable
//! without a build step at all, an option this contract's two-verb
//! `build`-then-`query` split does not offer). Per the issue body and
//! `docs/design/waveform-query-survey.md` section 6.4: an FST-format
//! trace (e.g. from Verilator with `--trace-fst`) builds directly, with
//! no VCD-parse step -- this module is that direct path, re-encoding the
//! source FST's declared (and `signals`-allow-list-filtered) signals into
//! this crate's own store via the same `store_writer::StoreWriter`
//! accumulator `build_vcd.rs` feeds, so both frontends produce an
//! identically-shaped store.

use std::fs::File;
use std::io::BufReader;

use fst_reader::{FstFilter, FstHierarchyEntry, FstReader, FstSignalValue, FstVarType};
use rustc_hash::FxHashSet;

use crate::store_writer::StoreWriter;

pub struct FstSourceSignal {
    pub id: String,
    pub full_name: String,
    pub width: u32,
    pub var_type: FstVarType,
    pub is_real: bool,
}

pub struct FstSource {
    reader: FstReader<BufReader<File>>,
    pub signals: Vec<FstSourceSignal>,
    pub timescale_exponent: i8,
    pub ticks_to_ns: f64,
}

pub fn var_type_string(tpe: FstVarType) -> &'static str {
    // Mirrors `fst_convert::var_type_of`'s own keyword set, in reverse --
    // `StoreWriter::create` re-maps this string back through
    // `var_type_of` so both ingestion frontends (VCD and FST) share one
    // `DeclSignal` shape. See that module's header for why the round trip
    // through a string (rather than passing `FstVarType` straight
    // through) was chosen: `signal.rs::SignalMeta` (the VCD side's own
    // declared-signal record, ported from upstream) already carries
    // `var_type` as a VCD keyword string, so this keeps `DeclSignal`'s
    // `var_type` field a single shared type across both frontends.
    match tpe {
        FstVarType::Reg => "reg",
        FstVarType::Integer => "integer",
        FstVarType::Parameter => "parameter",
        FstVarType::Real => "real",
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

/// Open an FST trace and read its hierarchy, producing the same kind of
/// flattened, dot-separated signal names (`tb.dut.clk`) the VCD frontend
/// produces from `$scope`/`$var` -- so the request's `clock.signal`/
/// `reset.signal`/`signals` fields resolve identically regardless of
/// trace format.
pub fn open(path: &std::path::Path) -> Result<FstSource, String> {
    let file =
        File::open(path).map_err(|e| format!("cannot open FST trace '{}': {e}", path.display()))?;
    let mut reader = FstReader::open_and_read_time_table(BufReader::new(file))
        .map_err(|e| format!("not a valid FST file '{}': {e:?}", path.display()))?;

    let header = reader.get_header();
    let mut signals = Vec::new();
    let mut scope: Vec<String> = Vec::new();
    reader
        .read_hierarchy(|entry| match entry {
            FstHierarchyEntry::Scope { name, .. } => scope.push(name),
            FstHierarchyEntry::UpScope => {
                scope.pop();
            }
            FstHierarchyEntry::Var {
                tpe,
                name,
                length,
                handle,
                ..
            } => {
                let full_name = if scope.is_empty() {
                    name
                } else {
                    format!("{}.{}", scope.join("."), name)
                };
                signals.push(FstSourceSignal {
                    id: handle.get_index().to_string(),
                    full_name,
                    width: length,
                    var_type: tpe,
                    is_real: tpe.is_real(),
                });
            }
            _ => {}
        })
        .map_err(|e| format!("failed to read FST hierarchy: {e:?}"))?;

    // ns-per-tick from the FST's own power-of-ten timescale exponent
    // (e.g. -9 => 1ns/tick), matching `fst_convert::timescale_to_exponent`
    // used on the VCD-write side.
    let ticks_to_ns = 10f64.powi(header.timescale_exponent as i32 + 9);

    Ok(FstSource {
        reader,
        signals,
        timescale_exponent: header.timescale_exponent,
        ticks_to_ns,
    })
}

/// Filtered declared signals, preserving hierarchy declaration order.
pub fn filter_signals<'a>(
    all: &'a [FstSourceSignal],
    allow_list: Option<&[String]>,
) -> Vec<&'a FstSourceSignal> {
    match allow_list {
        None => all.iter().collect(),
        Some(names) => {
            let wanted: FxHashSet<&str> = names.iter().map(|s| s.as_str()).collect();
            all.iter()
                .filter(|s| wanted.contains(s.full_name.as_str()))
                .collect()
        }
    }
}

/// Stream every recorded value change for the (already handle-filtered)
/// declared signals into `store`.
pub fn stream_body(
    source: &mut FstSource,
    watched_ids: &FxHashSet<String>,
    store: &mut StoreWriter,
) -> Result<(), String> {
    let include: Vec<_> = source
        .signals
        .iter()
        .filter(|s| watched_ids.contains(&s.id))
        .map(|s| fst_reader::FstSignalHandle::from_index(s.id.parse::<usize>().unwrap_or(0)))
        .collect();
    let filter = FstFilter::filter_signals(include);

    let mut real_buf = String::new();
    source
        .reader
        // fst-reader's `read_signals` callback passes the *resolved
        // absolute tick* as its first argument (see `io.rs`'s
        // `callback(start_time, handle, value)`), not an index into
        // `get_time_table()` -- despite the parameter commonly being
        // named `time_idx` in examples elsewhere. Use it directly.
        .read_signals(&filter, |tick, handle, value| {
            store.advance_time(tick)?;
            let id = handle.get_index().to_string();
            if !store.is_declared(&id) {
                return Ok::<(), String>(());
            }
            match value {
                FstSignalValue::String(bits) => {
                    let lower: Vec<u8> = bits.iter().map(|b| b.to_ascii_lowercase()).collect();
                    store.record_change(&id, &lower);
                }
                FstSignalValue::Real(v) => {
                    real_buf.clear();
                    real_buf.push_str(&v.to_string());
                    store.record_change(&id, real_buf.as_bytes());
                }
            }
            Ok(())
        })
        .map_err(|e| format!("failed to read FST value changes: {e:?}"))?;

    Ok(())
}
