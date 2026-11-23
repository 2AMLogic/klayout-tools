- **`klt gen-compose` now reports composed-frame mirrors on every
  `blocks[].derived_ports[]` entry** (#5609, 2026-11-17): the entry's
  `label_um` and `landing.x_um`/`y_um` were the only per-block coordinates
  still in the **source cell's own** frame — un-oriented, un-translated —
  so a caller driving its own router or via-drop from them placed geometry
  at the wrong coordinates for any block not placed at the origin. Each
  entry now additionally carries `label_composed_um: {x, y}` and, when
  `landing` is non-null, `landing.x_composed_um`/`y_composed_um`, oriented
  by the block's `orientation` and translated by its `offset_um` (the same
  transform as `bbox_um` and the composed `ports[]`). The source-frame
  fields keep their exact values, so no JSON field changes meaning or moves
  — additive only, no `schema_version` bump.
