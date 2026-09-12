//! `klt-wave-native` -- the native waveform build/query engine backing
//! `klt wave build` / `klt wave query`
//! (`docs/design/waveform-query-contract-spike.md`).
//!
//! This issue (#1600, Epic #1585 Phase 2b) implements the `build` side:
//! ingesting a VCD or FST trace into an indexed FST store. The `query`
//! side (issue #1599, Phase 2a) is implemented separately, reading the
//! store this crate's `build` module produces -- see `store_writer.rs`
//! and `docs/cli/wave.md` for the on-disk store shape the two share.

// `parser.rs` is ported verbatim from upstream (see its own file header) --
// these two lints fire on upstream's own style choices, not this crate's;
// suppressed here rather than "fixed" in a file whose whole point is to
// match the ported source exactly.
#![allow(clippy::manual_pattern_char_comparison, clippy::unwrap_or_default)]

pub mod build;
pub mod build_fst;
pub mod build_vcd;
pub mod cycle;
pub mod fst_convert;
pub mod parser;
pub mod signal;
pub mod store_writer;
pub mod verify;
