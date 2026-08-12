//! `klt-statime-native` -- a native-Rust liberty/NLDM gate-level static
//! timing spike for issue #809 (Epic #704 Phase 1c). See `README.md` for
//! the go/no-go verdict and measured comparison against OpenSTA.

pub mod liberty;
pub mod netlist;
pub mod nldm;
pub mod sta;
