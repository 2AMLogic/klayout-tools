- **`klt extract` severs SB.10 enclosure bands in time linear-ish in
  recognised resistor bodies** (#6493, 2026-11-18): `_sever_terminal_enclosure_bands`
  (`src/klayout_tools/extract_passive_regions.py`) now judges each recognised
  body's cut against the terminal-layer conductor clipped to that body's ring
  grown by one more enclosure width — exactly the reach of
  `outer.sized(enc_dbu)` into the band, by symmetry of `sized`'s structuring
  element — instead of running a full-layer subtraction and a full-layer
  sizing per component. The cuts are geometrically identical (regression-gated
  by an empty-symmetric-difference equivalence test against the previous
  full-layer loop, `tests/test_extract_gf180mcu_sab_resistor.py`), while the
  helper-level cost drops from quadratic to ~flat per body: median 5-repeat
  timings on synthetic separated halo+lead structures (Python 3.14.9, KLayout
  0.30.12, `scripts/sever_enclosure_bench.py`) went from 3.24 s total at 500
  bodies (6.48 ms/body, rising ~46x from the 1-body figure) to 0.029 s
  (0.058 ms/body, flat within noise) — ~104x at 500 bodies, ~50x at 200. No
  schema, CLI, deck, or connectivity change; behaviour for every existing
  fixture (ordinary, wide-lead, exact-body, flavour, shared-halo) is
  byte-identical.
