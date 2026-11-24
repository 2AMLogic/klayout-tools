- **`klt array-check` abutment: correct arrays no longer report false
  violations; the declared pitch is honoured and placements are checked per
  lane** (#5698, 2026-11-17): the abutment mode projected *every* placement
  of the unit cell onto one axis and tested each consecutive pair's gap
  against zero, so a unit cell placed on a 2-D grid reported spurious
  full-cell-width overlaps between stacked rows, and `--pitch P` was
  satisfiable only when `P` equalled the unit cell's extent along
  `--direction` — a mirror-abutted seam (`pitch < width`) or a spaced array
  (`pitch > width`) reported a false `gap`/`overlap` on every seam plus a
  false `length`, and raising `--tolerance` past a whole cell width to
  suppress the noise would have masked real defects of the same size.
  Placements are now grouped into lanes by their perpendicular extent and
  each lane is checked independently: `--count` is the per-lane population,
  the expected seam gap is derived from the declared pitch (`pitch - width`),
  `--tolerance` bounds the deviation from that expectation, and the lane
  extent is `(count - 1) x pitch + width` (the old `count x pitch` was right
  only when `pitch == width` masked the off-by-one-cell). Abutment violations
  carry an additive `lane` index and the record an additive `lanes` count; no
  `schema_version` bump. Arrays that previously reported `violated` on a
  geometrically perfect layout now report `clean`, and `kind` for `gap` /
  `overlap` names the deviation direction from the expected seam gap rather
  than a physical intersection. When pitch equals the cell’s extent, `expected_um`
  stays zero and `measured_um` keeps the positive gap or overlap magnitude
  and its existing description. For a nonzero expected seam gap,
  `measured_um` and `expected_um` are the signed actual and design gaps.
  Their difference is the deviation, and the description says how much
  wider or narrower the seam is than expected.
