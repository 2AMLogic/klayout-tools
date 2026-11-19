- **`klt extract` severs SB.10 enclosure bands in time linear-ish in
  recognised resistor bodies** (#6493, 2026-11-18): `_sever_terminal_enclosure_bands`
  (`src/klayout_tools/extract_passive_regions.py`) now judges each recognised
  body's cut against the **whole** terminal-layer conductor polygons
  interacting with that body's ring grown by two enclosure widths — a window
  that provably contains every polygon whose `outer.sized(enc_dbu)` dilation
  can reach the band (`sized` offsets by the square metric, whose
  convex-corner reach is at most sqrt(2)x the offset) — instead of running a
  full-layer subtraction and a full-layer sizing per component. Selecting
  whole polygons rather than clipping them keeps the polygon decomposition
  the full-layer loop sizes, so the cuts are geometrically identical for
  conductor at any angle, 45-degree routing included (#6688 review: the
  earlier clip-to-`ring.sized(enc_dbu)` window could leave a corner patch
  that bridged two conductor components the reference keeps apart).
  Regression-gated by an empty-symmetric-difference equivalence test against
  the previous full-layer loop over randomised Manhattan, 45-degree and
  off-axis staircase fixtures
  (`tests/test_extract_sever_enclosure_local_window.py`),
  while the helper-level cost stays ~flat per body: median 5-repeat timings
  on synthetic separated halo+lead structures (Python 3.14.9, KLayout
  0.30.12, `scripts/sever_enclosure_bench.py`) went from 2.83 s total at 500
  bodies (5.66 ms/body, rising ~80x from the 1-body figure) to 0.025 s
  (0.050 ms/body, flat within noise) — ~113x at 500 bodies, ~48x at 200. No
  schema, CLI, deck, or connectivity change; behaviour for every existing
  fixture (ordinary, wide-lead, exact-body, flavour, shared-halo) is
  unchanged.
