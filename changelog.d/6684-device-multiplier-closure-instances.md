- **`klt sim` checks device multipliers inside included DUT files**
  (#6684, 2026-11-19). An `X` instance with `mult=6` and no `m=` can now
  produce a `drive_1x` warning when it appears in an included subcircuit.
  A finding from an include carries a `file` field to identify its source.
  Calls and definitions inherit the scope of their include site. Calls in
  parameterized DUTs are checked, including calls to sky130 primitives.
  Included calls between sky130 primitive-library members are omitted.
  The top-level netlist retains the #5779 findings and disclosures. See [Device multiplier warnings](https://github.com/2AMLogic/klayout-tools/blob/main/docs/cli/sim.md)
  and the [JSON contract](https://github.com/2AMLogic/klayout-tools/blob/main/docs/json-contract.md).
