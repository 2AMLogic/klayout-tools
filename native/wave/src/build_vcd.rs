//! VCD ingestion frontend for `klt wave build`: drives the ported
//! `parser.rs` (`VcdHandler`, `try_parse_header`, `parse_streaming`) and
//! feeds each event into a `store_writer::StoreWriter`. Written fresh
//! (not a port) -- it is the adapter between the ported upstream parser
//! and this crate's own store-writer accumulator, analogous in *purpose*
//! to upstream's `fst.rs::FstBuildHandler` but not in implementation (see
//! `fst_convert.rs`'s header for why that struct's own code was not
//! ported).

use std::io::BufRead;
use std::ops::ControlFlow;

use rustc_hash::FxHashSet;

use crate::parser::{parse_streaming, try_parse_header, VcdHandler, VcdHeader};
use crate::signal::SignalMeta;
use crate::store_writer::StoreWriter;

pub struct VcdSummary {
    pub timescale_str: String,
    pub ticks_to_ns: f64,
}

struct Handler<'a> {
    store: &'a mut StoreWriter,
}

impl VcdHandler for Handler<'_> {
    fn on_timestamp(&mut self, _time: u64) -> ControlFlow<()> {
        ControlFlow::Continue(())
    }

    fn on_time_update(&mut self, time: u64, _byte_offset: u64) {
        let _ = self.store.advance_time(time);
    }

    fn on_scalar(&mut self, id: &str, value: u8) {
        if self.store.is_declared(id) {
            let lower = value.to_ascii_lowercase();
            self.store.record_change(id, &[lower]);
        }
    }

    fn on_vector(&mut self, id: &str, bits: &str) {
        if self.store.is_declared(id) {
            let lower = bits.to_ascii_lowercase();
            self.store.record_change(id, lower.as_bytes());
        }
    }
}

/// Parse `reader`'s VCD header, returning it plus a timescale summary --
/// the caller uses `header.signals` to resolve the request's `signals`
/// allow-list and `clock.signal`/`reset.signal` before constructing the
/// `StoreWriter` and calling `stream_body`.
pub fn parse_header(reader: &mut impl BufRead) -> Result<(VcdHeader, VcdSummary), String> {
    let header =
        try_parse_header(reader).map_err(|e| format!("failed to parse VCD header: {e}"))?;
    let summary = VcdSummary {
        timescale_str: header.timescale_str.clone(),
        ticks_to_ns: header.ticks_to_ns,
    };
    Ok((header, summary))
}

/// Stream the VCD body (everything after `$enddefinitions`) into `store`.
/// `watched_ids` is the set of VCD ids the request's `signals` allow-list
/// resolved to (every declared id when the request gave no allow-list).
pub fn stream_body(
    reader: &mut impl BufRead,
    watched_ids: &FxHashSet<String>,
    store: &mut StoreWriter,
) {
    let mut handler = Handler { store };
    parse_streaming(reader, watched_ids, &mut handler);
}

/// Signals from a parsed header, filtered to `allow_list` (`None` = every
/// declared signal), preserving VCD declaration order (including
/// duplicate ids -- `StoreWriter::create` handles those as FST aliases).
pub fn filter_signals<'a>(
    all: &'a [SignalMeta],
    allow_list: Option<&[String]>,
) -> Vec<&'a SignalMeta> {
    match allow_list {
        None => all.iter().collect(),
        Some(names) => {
            let wanted: FxHashSet<&str> = names.iter().map(|s| s.as_str()).collect();
            all.iter()
                .filter(|s| wanted.contains(s.name.as_str()))
                .collect()
        }
    }
}
