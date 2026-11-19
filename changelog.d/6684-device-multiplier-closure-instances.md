- **`klt sim`'s device-multiplier lint now scans the whole `.include`
  closure, not just the top-level netlist** (#6684, 2026-11-19): the lint
  (#5779) paired the *netlist's* instance cards with the *closure's*
  subcircuit texts, so an `X` card with `mult=6` and no `m=` that lived in
  an `.include`d DUT file was never checked — a hand-written or
  externally-produced hierarchical deck simulated 1x with no signal. The
  lint now reads instance cards from the netlist and every file of the
  same scanned `.include` closure (cards inside `.subckt` bodies included
  — a hierarchical DUT is mostly `X` cards in subcircuit bodies), and a
  finding for a closure-file card carries an additive `file` field with
  that file's repo-relative scanned-closure label (`X1` is legal in two
  different `.subckt` scopes); findings for the netlist's own cards keep
  the exact #5779 shape, and the netlist itself — always the first staged
  closure file — is linted once, as top-level, never double-counted. The
  `note`/`unevaluated` disclosures stay scoped to the netlist's own
  cards, so an included library's parametric internals (sky130's
  `res_xhigh_po_*` wrappers pass `mult={mult}` on to the
  declaring-not-forwarding `res_xhigh_po__base`) cannot add noise:
  measured before/after on a real sky130A install, `environment.
  device_multiplier` is byte-identical (absent or unresolved-closure note
  unchanged) on a full-corner deck, a small nfet-corner deck with and
  without a finding, and the resistor-models noise shape. Still
  warning-only, still no `schema_version` bump. See
  [`docs/cli/sim.md`](https://github.com/2AMLogic/klayout-tools/blob/main/docs/cli/sim.md)'s
  "Device multiplier warnings" and
  [`docs/json-contract.md`](https://github.com/2AMLogic/klayout-tools/blob/main/docs/json-contract.md).
