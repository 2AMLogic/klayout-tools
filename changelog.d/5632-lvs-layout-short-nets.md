- **Added** (#5632, 2026-11-18): `klt lvs` accepts `hints.layout_short_nets` — the
  layout-side dual of `hints.short_nets`: `{"nets": [["vsubs", "VGND"], ...]}`
  merges each named group of **layout** nets on the layout's selected top
  circuit into one net (`Circuit.join_nets()`) before the comparer is built,
  and before `options.flatten_layout`, the `options.combine_devices*` steps
  and the gate-level power-only prune, so the tie survives all of them. Its
  reason to exist is the abstract-`--abstract-cell-lef` substrate case
  (#3749/#4571): the deck's synthesized substrate global (`vsubs`) is an
  isolated node when the tap cell is itself abstracted, so a PG-pin
  `gate-level-verilog` reference that ties body pins to the rails cannot
  match without the caller asserting the tie. Every applied group emits one
  new additive `mismatches[].category`, `hints.layout_short_nets_applied`
  (always `severity: "warning"`, `side: "layout"`), disclosing that the tie
  was asserted by the request, not read from the layout — a request whose
  only finding is this entry reports `status: "match"` with a nonzero
  `mismatch_count`. A net name that does not resolve, a malformed entry, a
  non-object value, or an object naming no `nets` key is an application
  error (exit 1), never a silent no-op; `"engine": "klayout"` only. Absent
  by default: a request without the key behaves byte-identically to before.
  Both changes are additive per `docs/json-contract.md` — a new optional
  request key and a new warning category — so **no `schema_version` bump**.
