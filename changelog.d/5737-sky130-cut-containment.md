- **sky130 now reports a fully orphan via/contact cut** (#5737, 2026-11-18). Eight
  single-population `enclosing` rules opt in to `require_containment`
  (`li1.enclosing.licon1.1`, `met1.enclosing.mcon.1`, `met1.`/`met2.enclosing.via.1`,
  `met2.`/`met3.enclosing.via2.1`, `met4.enclosing.via3.1`, `met5.enclosing.via4.1`),
  so a cut sitting entirely off its conductor -- previously `clean` because the
  enclosure term only sees cuts that touch the metal -- now reports on its rule id.
  The seven multi-population sides (`diff.`/`tap.`/`poly.enclosing.licon.1`,
  `met3.`/`capm.enclosing.via3.1`, `met4.`/`capm2.enclosing.via4.1`) keep the legacy
  behaviour. A `licon1` with no `li1` above it is now itself reported, so a bare
  contact placed at a `klt gen` gate port needs an `li1` landing (0.08 um enclosure
  clears every generator case; generator output itself is unchanged and still
  clean). The off-grid rule block moved to `decks/sky130_offgrid.py` (re-exported
  from `decks/sky130.py`). No rule id, `DrcRule` field or schema change.
