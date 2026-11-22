- **`klt extract` no longer spends ~95% of its wall time re-walking every
  active island on marker-flavoured decks** (#5893, 2026-11-17): three
  Python-level scans around KLayout's `LayoutToNetlist` pass intersected
  whole-chip unmerged regions once per active island — the MOS-capacitor
  claim scan (inside `device_extraction`), the unsupported-device
  diagnostic's flavour-island re-scan, and the per-gate flavour split's
  gate build. Each now computes its whole-chip region once (merged, and
  restricted to islands that can actually claim), and the split returns a
  wholly-inside-the-marker island without touching gate geometry. The new
  `timing.stages.unmodeled_marker_diagnostic` complements the existing
  `unsupported_devices` stage so both diagnostic costs are visible.
  Measured on the 166 KB ttgf0p2 `tt_um_MATTHIAS_M_PAL_TOP_WRAPPER` block
  (33,582 devices): 2,262.7 s → 83.6 s total, netlist byte-identical; the
  2x2 `tt_um_2048_vga_game` (66,459 devices) went from a 300 s timeout to
  112.0 s on the same host. The extra timing stage is additive and keeps the
  current `schema_version`.
