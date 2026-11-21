- **`klt gen-compose`'s new `routing.minimal_ladder` sizes a via-drop ladder's
  intermediate pads at their own deck floors** (#6058, 2026-11-19): a
  multi-hop ladder draws a landing pad on every plane it crosses, each at
  least the fixed contact-derived 0.42 µm baseline even where the resolved
  deck's own rules for that `(landing, via)` pair allow less. With
  `routing.minimal_ladder: true` (default `false`), every *intermediate* hop
  pad — the levels between the backbone's plane and the pin's own layer —
  resolves to its per-pair floors (minimum area, minimum width, plain
  enclosure, and two-adjacent-edges enclosure), dropping the baseline; the
  two terminal pads per endpoint keep today's sizing. On sky130A a met1
  intermediate pad over via1 shrinks from 0.42 µm to 0.32 µm
  (`met1.enclosing_adjacent.via.1`), enough to route a neighbouring net past
  the ladder where the default pads violate `met1.space.1`, with the full
  curated deck still clean on the composed GDS. The route-vs-route collision
  checks measure the same shrunk footprints drawing inserts; a pair whose
  deck declares no enclosure rule keeps the baseline (never shrunk on
  missing information). `nets[].legs[].via_drops[]` entries gain an additive
  `terminal_landing_layers` field naming the subset of each hop's
  `landing_layers` that are terminal planes. The flag cannot be combined
  with `routing.landing_pad_size_um` (application error naming both fields),
  and a request with it omitted or `false` composes byte-for-byte today's
  output.
