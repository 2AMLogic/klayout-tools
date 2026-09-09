//! `klt-wave-native` -- FST waveform query engine library for `klt wave
//! query` (2AMLogic/klayout-tools, issue #1599 -- Epic #1585 Phase 2a).
//!
//! `fst`/`cache`/`signal`/`format` are ported and adapted from
//! `boldaxolotl/booley` (Apache-2.0) -- see each module's own file header
//! and `/NOTICE` at the repository root. `request`/`query` are written
//! fresh against `docs/design/waveform-query-contract-spike.md`.

pub mod cache;
pub mod format;
pub mod fst;
pub mod query;
pub mod request;
pub mod signal;
