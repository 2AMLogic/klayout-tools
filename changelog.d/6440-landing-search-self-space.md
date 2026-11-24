- **`klt gen-compose` label-derived-port landing search (`ports_from_labels`
  `access`) rejects a sub-spacing gap to the port net's own separate shapes
  (#6440, 2026-11-24)**: the sibling of #5608's notch test. A pad landing
  between two *non-touching* shapes of its own net (e.g. met2 islands
  0.0..2.75 and 3.25..10.0 um, port at x=3.0) leaves a gap to each that the
  deck's net-agnostic `met*.space.*` rule flags, but it is an inter-polygon
  gap, so `self_notch` (`notch_check`, intra-polygon) never saw it and
  `clearance` skips the net's own metal. Every pad layer is now also checked
  against the non-touching polygons within its `space` rule
  (`self_space`, a new category in the no-legal-landing error's per-category
  counts); a shape the pad overlaps is the intended landing and stays legal.
  The check is pad-relative and windowed, so a gap that already exists
  elsewhere on the layer rejects no candidate. `compose()`'s own #1520
  `_pad_self_notch_violation_um` is unchanged -- the search is intentionally
  stricter than that late check. `schema_version` is unchanged.
