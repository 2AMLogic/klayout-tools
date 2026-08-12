//! `klt-sta-native` — the gate-level static-timing spike for issue #809.
//!
//! **This crate is a spike artifact, not a shipped component.** It is not
//! wired into `klt synthesize`, has no Python binding, and should not grow
//! one until a follow-on reverses the verdict recorded in `README.md`.
//!
//! Three modules, in dependency order:
//!
//! * [`liberty`] — an NLDM-subset liberty reader (cells, pins, timing arcs,
//!   lookup tables).
//! * [`netlist`] — a structural-Verilog reader for `write_verilog`'s mapped
//!   gate netlist.
//! * [`graph`] — the timing graph and block-based max-delay propagation,
//!   whose delay model is documented arc-by-arc in that module's header.

pub mod graph;
pub mod liberty;
pub mod netlist;
