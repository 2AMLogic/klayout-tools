- **`klt sim`'s device-multiplier lint reads the subcircuit's declared
  `mult` default instead of assuming 1** (#6812, 2026-11-19): on a
  declaring-not-forwarding subcircuit, an instance that omits `mult=` now
  compares its `m=` against the header's declared default, matching how
  ngspice resolves the parameter (confirmed on ngspice 47 against a header
  `mult=4`: `m=4` with no `mult=` is the clean pair, `m=1` the
  disagreement). Before, the lint hard-coded an effective `mult` of 1,
  flagging the first as a false positive and missing the second as a false
  negative on any subcircuit whose default is not unity. No supported PDK
  ships one today (all 55 declaring subckts in sky130A/B declare `mult=1`;
  gf180mcuD declares none), so current closures lint exactly as before. An
  explicit instance `mult=` still wins over the header, a nonnumeric
  declared default (an `{expression}`) keeps the unity assumption, and the
  finding kinds, fields and raw instance echo are unchanged.
  `docs/cli/sim.md`'s "Device multiplier warnings" rule table now states
  the omitted-`mult=` cases in terms of the declared default.
