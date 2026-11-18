- **Added** (#5632, 2026-11-18): `klt extract --abstract-cells --abstract-cell-lef` now
  warns when the LEF-merged substrate-role pins' net — the deck's
  synthesized substrate global (`vsubs`) — carries no other connection in
  the emitted netlist (no device terminal, no other subcircuit pin): the
  tap cell is itself abstracted and its LEF declares no body pins, so the
  net is an isolated node and a PG-pin `klt lvs` reference cannot match.
  The `warnings[]` entry names the net, the instance/cell counts that
  reached it, and the two fixes (a `hints.layout_short_nets` tie in the
  downstream compare, or a drawn-and-labelled substrate tie in the cells).
  It does not fire when the net carries real connectivity or no
  substrate-role pin was merged. Warning-only: `status` and exit codes are
  unchanged; `warnings[]` is not part of the versioned payload shape, so
  **no `schema_version` bump**.
