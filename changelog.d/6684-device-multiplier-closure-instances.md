- **`klt sim` checks device multipliers inside included DUT files**
  (#6684, 2026-11-19). An `X` instance with `mult=6` and no `m=` can now
  produce a `drive_1x` warning when it appears in an included subcircuit.
  A finding from an include carries a `file` field to identify its source.
  Inside an include, the body of a subcircuit that itself declares `mult`
  is a device definition and is not scanned, so a vendored model library's
  internal devices stay quiet. A call resolves to the subcircuit its own
  enclosing scope makes visible, so two included DUTs with private
  same-named helpers are each checked against their own. The top-level
  netlist retains the #5779 findings and disclosures. See [Device multiplier warnings](https://github.com/2AMLogic/klayout-tools/blob/main/docs/cli/sim.md)
  and the [JSON contract](https://github.com/2AMLogic/klayout-tools/blob/main/docs/json-contract.md).
