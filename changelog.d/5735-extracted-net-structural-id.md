- **`klt extract` reports `nets[].structural_id`, a stable handle for
  anonymous nets** (#5735, 2026-11-17): every `nets[]` entry now carries the
  net's numbering-independent structural identity — the `$anon<...>` token
  `netlist_normalized_sha256` (#3407) and `--check --rerun` already derive
  from the net's sorted `<device>.<terminal>` attachment list, now also
  emitted per net. A testbench probing an unlabeled internal node can pin
  its measurement to that token and join an older report to a re-extraction
  on it, instead of re-deriving the node from device cards after every run
  (a renumbered `$N` probe still *resolves* — to a plausible but wrong
  node). `null` for a named net and for an anonymous net with zero device
  terminals. `--check --rerun` strips the field alongside `net_id` (it is
  content class, but a pre-field committed report lacks the key — stripping
  is backward compatibility), so `netlist_normalized_sha256` is
  byte-identical across the field's introduction and reports committed
  before it still verify `match`. `parasitics.nets[]` is untouched.
  Additive; no `schema_version` bump.
