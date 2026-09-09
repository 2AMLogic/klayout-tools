//! `klt-wave-native` -- the native waveform build/query engine backing
//! `klt wave build` / `klt wave query`
//! (`docs/design/waveform-query-contract-spike.md`, Epic #1585).
//!
//! The `query` side (issue #1599, Phase 2a): `fst`/`cache`/`signal`/
//! `format` are ported and adapted from `boldaxolotl/booley` (Apache-2.0)
//! -- see each module's own file header and `/NOTICE` at the repository
//! root. `request`/`query` are written fresh against the contract.
//!
//! The `build` side (issue #1600, Phase 2b): ingesting a VCD or FST trace
//! into an indexed FST store. `parser` is ported verbatim and
//! `fst_convert` adapted from the same upstream; `build`/`build_vcd`/
//! `build_fst`/`cycle`/`store_writer`/`verify` are written fresh -- see
//! `store_writer.rs` for the on-disk store shape the two sides share.

// `parser.rs` is ported verbatim from upstream (see its own file header) --
// these two lints fire on upstream's own style choices, not this crate's;
// suppressed here rather than "fixed" in a file whose whole point is to
// match the ported source exactly.
#![allow(clippy::manual_pattern_char_comparison, clippy::unwrap_or_default)]

pub mod build;
pub mod build_fst;
pub mod build_vcd;
pub mod cache;
pub mod cycle;
pub mod format;
pub mod fst;
pub mod fst_convert;
pub mod parser;
pub mod query;
pub mod request;
pub mod signal;
pub mod store_writer;
pub mod verify;
