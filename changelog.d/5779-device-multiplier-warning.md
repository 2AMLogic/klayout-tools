- **`klt sim` names device instances whose vendor `mult` and acting `m=`
  disagree** (#5779, 2026-11-17): sky130's device subcircuits declare a
  vendor `mult` parameter and never forward it to the inner primitive as a
  device multiplier, so a netlist carrying `mult=6` and no `m=` simulates a
  **1x** device with no signal from any step of the flow. After the corners
  run, `klt sim` lints the netlist's instance cards against the device
  subcircuit texts of the scanned `.include` closure (text-driven — a body
  `m=` referencing `mult` is forwarding; a mismatch expression like
  `sqrt(l*w*mult)` is not) and reports three verdicts: `mult=N` (N>1) with
  no `m=` → finding `drive_1x` (simulates 1x); `m=M` with `mult` absent (M≠1) or
  unequal → finding `drive_mismatch_disagreement` (the drive and vendor
  multiplier settings differ); `mult=N m=N` → clean. Findings are **warning-only**
  in a new additive `environment.device_multiplier` (`findings`, plus
  `note` when the closure cannot verify an instance, `unevaluated` for
  non-numeric `m=`/`mult=` values, `not_scanned` for unread closure parts):
  the exit code and every existing field are unchanged. Keep `m=N` and
  `mult=N` for an N-times device. The effect of `mult` on Monte Carlo
  mismatch depends on the model. For example, the sky130 `nfet_01v8`
  uses `sqrt(l*w*mult)` in its mismatch sigma denominator. Non-numeric
  values and subcircuits absent from the closure are disclosed, never guessed. See
  [`docs/cli/sim.md`](https://github.com/2AMLogic/klayout-tools/blob/main/docs/cli/sim.md)'s "Device multiplier warnings" and
  [`docs/json-contract.md`](https://github.com/2AMLogic/klayout-tools/blob/main/docs/json-contract.md).
