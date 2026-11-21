- **`klt extract --engine klayout-runset` can carry measured device
  parameters onto its `X` calls** (#5513, 2026-11-17): the new repeatable
  `--device-param CLASS:PARAM=NAME[:SUFFIX]` (request field `device_params`)
  appends `NAME=<value><SUFFIX>` to every call of `CLASS`, where `<value>` is
  the measured KLayout parameter, unscaled. The caller states the name and the
  SPICE suffix; there is no built-in per-PDK table. Before, every call was
  parameter-free, so a MOS or resistor drawn at a non-default size simulated at
  its subcircuit's default (an `sg13_hv_nmos` measured at 1.0 µm × 1.0 µm ran at
  0.35 µm × 0.34 µm). An unknown `PARAM` for its class is an error (exit 1), and
  the flag is refused under the default engine. `warnings[]` and the netlist
  header now name only what is *still* uncarried: a class with every parameter
  declared gets no dropped-parameter warning. The report gains a top-level
  `device_params` echo, **absent** (not `null`) without the flag. Additive; with
  no `--device-param` the netlist, header, `warnings[]` and report are
  byte-identical to before. `docs/cli/extract.md` also corrects the implication
  that the class terminal order always matches the subcircuit's pin order
  (true for `pnpMPA`, false for MOS: `S G D B` against `d g s b`), and gives
  the MOS recipe that cross-declares `AS`/`AD`/`PS`/`PD`. No
  `schema_version` change.
