//! `klt-legalize-native` -- issue #784's native-Rust Abacus-style standard-
//! cell row legalizer spike (Epic #700 Phase 1). See `abacus.rs` for the
//! algorithm, `def.rs`/`lef.rs` for the minimal DEF/LEF I/O, and this
//! crate's own `README.md` for the spike's measured go/no-go result.

pub mod abacus;
pub mod def;
pub mod lef;
pub mod metrics;
