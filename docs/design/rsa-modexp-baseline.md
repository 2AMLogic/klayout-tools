# RSA modexp baseline — moved to `2AMLogic/sky130-modexp`

This document used to record Phase 1 of Epic `2AMLogic/marketing#56` (the
"first *digital* canary" — GCD/RSA modexp, gate-count minimized, against a
published external number): the chosen design (`modexp.v`), its provenance,
the measured sky130 cell-count baseline (**682 cells** at `WIDTH=16`,
`sky130_fd_sc_hd`/`tt_025C_1v80`), and the stop-and-reassess analysis
against Qwen3.8-Max's published 8,298-cell pre-optimization figure (issue
`2AMLogic/klayout-tools#486`, PR #488).

Per the epic-level operator ruling (2026-08-04) — the 682-cell baseline is
accepted and no artificial ~8k-cell starting design is being manufactured —
the canary's design content, and this record, moved to the dedicated
[`2AMLogic/sky130-modexp`](https://github.com/2AMLogic/sky130-modexp) repo
(`sky130-modexp#2`, PR #4). The canonical, up-to-date version of this
document now lives at
[`sky130-modexp/docs/baseline.md`](https://github.com/2AMLogic/sky130-modexp/blob/main/docs/baseline.md).

This repo (`klayout-tools`) keeps only what it needs as tool fixtures/examples,
independent of the canary:

- `examples/functional-verification/gcd.v`, `test_gcd.py`, and
  `tests/corpus/place_and_route/gcd.gds.gz` — the pre-existing,
  minimal-iterative-subtractor GCD fixture used throughout Epic #391;
  documented in `tests/corpus/README.md`.
- `examples/functional-verification/modexp.v`, `test_modexp.py`, and
  `request-modexp.json` — retained as a `klt functional-verification`
  example fixture (not the canary's design-of-record, which now lives in
  `sky130-modexp`).

Phase 2 (optimization) and any further design-record updates happen in
`sky130-modexp`, not here.
