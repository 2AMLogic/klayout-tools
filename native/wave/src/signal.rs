// Partial port from boldaxolotl/booley, crates/bwave/src/signal.rs
// (commit 0c3b4cd7b66a0f793de605ce58e82dc1bc0ac892,
// https://github.com/boldaxolotl/booley).
// Copyright (c) the boldaxolotl/booley contributors.
// Licensed under the Apache License, Version 2.0; see /NOTICE.
//
// Modifications from upstream: only the `SignalMeta` struct is ported --
// `parser.rs`'s `try_parse_header`/`parse_streaming` (also ported, this
// crate) need it as their signal-declaration record. Upstream's glob
// matching / scope-prefix utilities in this same source file are NOT
// ported here: `klt wave build` addresses signals by an exact
// (post-flattening) hierarchical dotted-path string, matching the
// `signals` allow-list shape docs/design/waveform-query-contract-spike.md
// section 4 contracts -- no glob support in the `build` request. Issue
// #1599 (Phase 2a, the query engine) is expected to port the remainder of
// this file into this same module if/when a query op needs glob matching.

//! Signal metadata for a VCD signal declaration.

/// Metadata for a single VCD signal declaration.
#[derive(Debug, Clone)]
pub struct SignalMeta {
    /// Full hierarchical name (e.g. "tb.dut.data[7:0]")
    pub name: String,
    /// VCD identifier code (short ASCII string like "!" or "#")
    pub id: String,
    /// Bit width
    pub width: u32,
    /// Variable type from $var (wire, reg, etc.)
    pub var_type: String,
}
