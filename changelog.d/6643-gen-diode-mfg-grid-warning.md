- **`klt gen diode` now warns when `width_um`/`length_um` is off the
  manufacturing grid** (#6643, 2026-11-18): an off-grid size such as
  `width_um=2.003` drew `*.ongrid.*`-dirty geometry (`klt drc` →
  `violations`: `diff.ongrid.1`, `li1.ongrid.1`, `licon1.ongrid.1`, ...) with
  an empty `warnings[]`, while `mos_array`, `res_array` and `diff_pair` already
  warned for the same mistake. `warnings[]` now carries one `manufacturing
  grid` entry per off-grid edge, naming the grid (0.005 µm on sky130) and the
  nearest representable values below/above, read from the curated deck's own
  `ongrid` rule via the shared `_mfg_grid_warnings` helper. **Additive, but
  visible on sky130:** a request that previously returned `warnings: []` for an
  off-grid size now returns a non-empty list. Advisory only — the geometry is
  drawn exactly as requested and `klt drc` stays the authority; on-grid sizes
  are unchanged.
