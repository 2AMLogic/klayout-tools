- **`klt lvs --engine klayout-runset` never reports `match` beside a runset
  failure, the runset's strict top-port findings are first-class, and the
  mirrored-`inductor2` terminal-order defect is named** (#6903, 2026-11-21).
  A PDK runset may check more than its compare: IHP sg13g2's runs
  `compare && flag_missing_ports` by default (klt passes no
  `ignore_top_ports_mismatch`), and `flag_missing_ports` logs
  `Port mismatch '<layout>' vs. '<reference>'` entries into the `.lvsdb`
  cross-reference's log block — on a compare whose pairs are all `Match`.
  Those findings now appear verbatim as error-severity `port.name_mismatch`
  entries (`provenance.runset.xref_log` records the log,
  `strict_top_port_mode` the mode), and while any stands, `status` is never
  `match` — previously such a run read `status: "match"` next to the
  runset's own `Netlists don't match`. A mirrored, correctly wired
  `inductor2` additionally gets a warning `topology.known_limitation` entry
  (`mirrored-inductor2-terminal-order`): the runset orders the winding
  terminals by port x position, so the mirror extracts terminal-reversed;
  `klt signoff` renders the named reason `runset_inductor_terminal_order`
  for items 4 and 11 instead of a bare `check_failed`/`lvs_did_not_pass`,
  and never grades such a report `met`.
