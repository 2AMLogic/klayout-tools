- **`klt sim` names device instances whose vendor `mult` and acting `m=`
  disagree** (#5779, 2026-11-17): sky130's device subcircuits declare a
  vendor `mult` parameter and never forward it to the inner primitive as a
  device multiplier, so a netlist carrying `mult=6` and no `m=` simulates a
  **1x** device with no signal from any step of the flow. After the corners
  run, `klt sim` lints the netlist's instance cards against the device
  subcircuit texts of the scanned `.include` closure (text-driven — a body
  `m=` referencing `mult` is forwarding; a mismatch expression like
  `sqrt(l*w*mult)` is not) and reports three verdicts: `mult=N` (N>1) with
  no `m=` → finding `drive_1x` (simulates 1x); `m=M` with `mult` absent or
  unequal → finding `drive_mismatch_disagreement` (drive and mismatch
  de-rating disagree); `mult=N m=N` → clean. Findings are **warning-only**
  in a new additive `environment.device_multiplier` (`findings`, plus
  `note` when the closure cannot verify an instance, `unevaluated` for
  non-numeric `m=`/`mult=` values, `not_scanned` for unread closure parts):
  the exit code and every existing field are unchanged. The wording never
  suggests dropping either form — `mult` de-rates mismatch in a Monte Carlo
  run (`sqrt(l*w*mult)` in the sigma denominator), so removing it would
  overstate mismatch by `sqrt(N)` per device. Non-numeric values and
  subcircuits absent from the closure are disclosed, never guessed. See
  [`docs/cli/sim.md`](https://github.com/2AMLogic/klayout-tools/blob/main/docs/cli/sim.md)'s "Device multiplier warnings" and
  [`docs/json-contract.md`](https://github.com/2AMLogic/klayout-tools/blob/main/docs/json-contract.md).
