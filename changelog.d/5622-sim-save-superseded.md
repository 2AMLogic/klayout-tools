- **`klt sim` warns when the generated `save all` supersedes a body `.save`
  while waveforms are on, and reports `artifacts.raw_bytes`** (#5622,
  2026-11-17): the default `save_mode: "all"` silently widened a netlist body's
  `.save v(a) v(b)` to every node of the design, turning a 4.7 MB rawfile into
  7.9 GB with nothing in the report. A request-level `save_all_supersedes`
  warning (also in `--dry-run`) now names both facts and the remedies
  (`save_mode: "netlist"`, `save_vectors`); it fires only for a top-level
  `.save` with `save_mode` `"all"`, no `save_vectors` and `options.waveforms`
  on. `corners[].artifacts.raw_bytes` is added when a rawfile is kept. Additive;
  no emitted deck changes and the default `save_mode` is unchanged.
