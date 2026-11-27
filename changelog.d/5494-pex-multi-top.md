- **`klt pex --top` is repeatable: one graded run can extract every drawn
  cell a testbench instantiates** (#5494, 2026-11-13): `klt pex` extracted
  exactly one `--top` per run, so a link bench instantiating two or more
  cells that are *separate tops in the layout* (driver, declared channel,
  receiver) had no single `--top` below the full array and no
  both-ends-extracted leg could be produced at all — the post-layout row
  ([`docs/design-evidence-tiers.md`](docs/design-evidence-tiers.md) item 7)
  stayed a declared gap rather than a measured number, since two one-ended
  deltas do not compose into the both-ends one. `--top A --top B` now
  extracts each top on its own (one `run_extract` call per top, the caller's
  flags unchanged) and concatenates the results into the one `-o` netlist,
  which therefore defines a `.SUBCKT` per top; the testbench's single DUT
  `.include` is re-pointed at that one file, so the exactly-one-`.include`
  rule (#1030, #2871) is untouched and the testbench's own unmodified `X...`
  lines bind each cell on both legs. Each top is extracted independently —
  **no parasitics are modelled between tops**, exactly as two separate
  single-top runs would behave. The schematic DUT must declare a `.SUBCKT`
  for every `--top` (a missing one is an exit-`1` error naming the cell,
  raised before any simulation), and a repeated cell name is a usage error.
  The three static pre-flight diagnostics (`pin_count_mismatch`,
  `flat_dut_mismatch`, `pin_order_mismatch`) now run once per top against the
  combined netlist, each scoped to that top's own `.SUBCKT` header.
  **Additive JSON: one new top-level field, `tops`** — `null` on a zero- or
  one-`--top` run (every other field unchanged there), otherwise one block
  per `--top` in `--top` order carrying `{top, netlist, extraction,
  body_bias, pin_count_mismatch, flat_dut_mismatch, pin_order_mismatch,
  provenance}`, each in the same shape as the top-level field of that name.
  The existing singular fields keep describing the *combined* netlist the
  extracted leg simulated, reduced so they never read cleaner than the worst
  top: `extraction.device_count`/`net_count` and the
  `parasitic_integrity` counters are sums (its `nets[]` concatenated in
  `--top` order), `body_bias.status` is `"unbiased"` if **any** top's is with
  every top's entries concatenated (so `klt signoff`'s verbatim item-7
  citation keeps the disclosure), `extraction.unsupported_devices` (#5477's
  device-coverage disclosure, computed per extracted cell rather than echoed
  from a flag) is concatenated in `--top` order so a clean first top cannot
  hide a flagged second one, `extraction.critical_nets` is passed through
  verbatim when every top echoes the same `--critical-net` request and is
  their union in `--top` order when `--critical-net-pattern` /
  `--critical-net-all` resolved a different set against each top, and each
  mismatch field is the first firing top's block. `--flatten-schematic`, `--device-only-leg`, `--tie-net`,
  `--tie-declared-pins`, `--schematic-top`, `--engine klayout-runset`,
  `--mom-rlc-net`, `--measure-command`,
  `--extract-only`, `--submit-only`/`--attach`, `--resume` and `--dry-run`
  (the last tracked for support in #5511) are each a usage error with two or
  more tops (`error.code: "pex_multi_top_unsupported"`), raised before any
  extraction — refusals, not
  designs. Each curated extraction must be flat; every other flag passes through to every top unchanged, `--pins`
  as the union of the tops' pin names. No existing field is added to, renamed
  or removed; no `schema_version` bump. See
  [`docs/cli/pex.md`](docs/cli/pex.md)'s "Several drawn tops in one run".
