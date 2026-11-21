# sta_electrical_checks fixture (issue #3621, AC4 of #3005)

A routed DEF + SPEF whose `electrical_checks` result is cross-checked against
a hand-run OpenSTA `report_check_types`.

- `top.def` -- the six-flop-and-clock-buffer post-CTS DEF from
  `../sta_clock_propagation/captures.json` (`defs.buffered`).
- `top.spef` -- the same SPEF with one added net, `q1`, carrying 6 pF of
  lumped capacitance on the `ff1/Q` driver (`dfrtp_1`). That deliberately
  exceeds the Liberty `max_capacitance` and `max_transition` of the pin, so
  the fast corner has a nonzero violation count that does not depend on
  hunting for a naturally violating design. The synthetic RC is not a
  proprietary or extracted-from-PDK value; the cells and Liberty are the
  open sky130A `sky130_fd_sc_hd` ones.
- `request.json` -- `run_sta` request at corner `ff_n40C_1v95`. The Liberty is
  resolved from the sky130A install (`PDK_ROOT`), not vendored.

## Hand-run reference

Tool: OpenROAD `26Q3-1510-g6cb3f2b704`. Script, run as
`openroad -no_init -exit hand.tcl` from the repository root:

```tcl
read_liberty $PDK_ROOT/sky130A/libs.ref/sky130_fd_sc_hd/lib/sky130_fd_sc_hd__ff_n40C_1v95.lib
read_lef $PDK_ROOT/sky130A/libs.ref/sky130_fd_sc_hd/techlef/sky130_fd_sc_hd__nom.tlef
read_lef $PDK_ROOT/sky130A/libs.ref/sky130_fd_sc_hd/lef/sky130_fd_sc_hd.lef
read_def tests/fixtures/sta_electrical_checks/top.def
read_spef tests/fixtures/sta_electrical_checks/top.spef
set_cmd_units -time ns -capacitance pF
report_check_types -max_slew -max_capacitance -violators -digits 5
```

Recorded output (`(VIOLATED)` rows):

```
max slew
ff1/Q       1.50568   38.70499  -37.19931 (VIOLATED)
max capacitance
ff1/Q       0.23072    6.00000   -5.76928 (VIOLATED)
```

So the expected `violation_count` is **1** for `max_transition` and **1** for
`max_capacitance`. (`worst_excess` differs slightly in `klt sta` because its
session also applies the request's clock and input constraints; only the
counts are asserted.)
