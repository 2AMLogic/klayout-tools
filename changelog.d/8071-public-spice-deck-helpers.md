- **The SPICE deck input helpers shared by `klt sim` and `--op-lint` are public** (#8071).
  `corner_lib_lines` (re-exported from `klayout_tools.sim`), `resolve_netlist_include`,
  `scale_supply_waveforms` and `supply_alter_lines` drop their leading underscore, and the
  four matching legacy private-symbol grants leave `import-boundaries.json`.
