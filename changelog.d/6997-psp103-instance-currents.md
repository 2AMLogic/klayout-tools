- **`klt sim`: documented, verified recipe for a nested PSP103 device's
  current** (#6997, 2026-11-22): `docs/cli/sim.md` now gives the OSDI
  instance-vector form — `@n.<subckt path>.<inner element>[ids|ide]` listed in
  `options.save_vectors` and read back by a `measurements[].expr` — for
  operating-point and DC analyses, with the measured values, how to derive the
  inner element name from the PDK wrapper subcircuit, and why PSP103 has no
  `[id]`. A new live regression test (`tests/test_sim_osdi_instance_op.py`,
  skipped without ngspice and a compiled `psp103.osdi`) cross-checks the device
  current against an independent supply-current measurement in both analyses.
  Documentation and tests only; no JSON fields or behavior changed.
