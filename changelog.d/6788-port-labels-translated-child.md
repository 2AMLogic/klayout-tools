- **`gen-compose` `ports_from_labels` lands correctly on a pin pad that lives
  in a translated child cell** (#6788, 2026-11-19): the label's net was
  resolved by a hierarchical connectivity probe, so a net that exists only
  inside a child cell came back in that child's own frame while the label and
  the clearance obstacles are in the macro's. On gf180mcuA the search then
  either returned a landing *off the pad* (`(1.21, 1.21)` µm for a pad
  centred at `(4, 3)`) or, with disconnected metal near that spot, rejected
  every candidate for clearance (`7225 candidate(s) rejected`); the same
  geometry flattened was always right. Connectivity now runs flat, so each
  occurrence of a child cell is its own net in the macro's frame and a
  hierarchical macro gives the same landing as its flattened copy. One
  consequence: the same label text on two *disconnected* instances of one
  child cell is now reported as ambiguous (two nets), exactly as it already was
  for flat geometry, instead of silently resolving to the first instance. No
  field is added, removed or retyped; no `schema_version` bump. The original
  host incident in #6572 is not attributed by this change (its GDS was never
  available).
