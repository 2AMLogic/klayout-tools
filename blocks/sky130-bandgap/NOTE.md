This block's `output/layout.json` is a real, layout-derived record
(issue #896) — not a placeholder.

It is ingested from the public
[`2AMLogic/sky130-bandgap`](https://github.com/2AMLogic/sky130-bandgap)
canary repo by [`../../scripts/ingest-canary.py`](../../scripts/ingest-canary.py)
(issue #62) — a bandgap voltage reference on sky130, designed end-to-end by
AI agents, still with a DRAFT (not yet ratified) target spec, but with a
real routed core layout: `status: "ok"`, with `layer_count`/`cell_count`/
`instance_count` and a `renders.overview` thumbnail derived from the
source repo's own committed, routed GDS —
`layout/bandgap-core/reports/LATEST` → `bandgap_core_routed.gds` (the
current run as of `source.ref`; `source.path` in `layout.json` records the
exact file). That run's `klt lvs` verdict is clean (`status=match,
mismatch_count=0`; area is over the ratified budget pending DR-007,
proposed not ratified — see that repo's own record for the full
acceptance-criteria scoreboard). `layout_file` is staged into
`output/bandgap_core_routed.gds` and `downloadable` is `true`. The draft
target-spec table (`spec_summary`, from the source repo's `README.md`) and
real `ngspice` device-characterization results from its `sim/` evidence
trail (`signals`, sourced from that repo's own simulation records, not a
fresh `klt sim` run against this repo) are both still present alongside
the layout data. See [`../README.md`](../README.md) for the field-level
documentation and
[`../../scripts/ingest-canary.py`](../../scripts/ingest-canary.py)'s
module docstring for how each field is derived.

Re-running `python scripts/ingest-canary.py --repo 2AMLogic/sky130-bandgap`
refreshes this file from the source repo's current state (still gated on
that repo staying public) — as its layout advances (a new routed run, a
design change), this is the repeatable way to pick it up; no separate
refresh mechanism exists or is needed.

## `schematic.svg` (issue #1122)

`schematic.svg` is a headless `xschem --svg` export of this repo's
design-of-record analog schematic, `design/bandgap_core.sch` in
[`2AMLogic/sky130-bandgap`](https://github.com/2AMLogic/sky130-bandgap)
@ `31d8167d37d021ed0c54681f39d058770f29f0db`, post-processed to the site's
theme convention (`currentColor` ink, no baked dark-canvas background,
cropped `viewBox`) — full provenance and the post-processing diff are
recorded in the SVG's own leading `<!-- ... -->` comment. Only decorative,
non-electrical content was removed for the export (the two embedded
`code_shown.sym` parameter/documentation text blocks and the sheet header
comment); every symbol, wire, and label is unchanged from the source file.

Device-count spot-check against `design/bandgap_core.sch` at that commit: 8
non-pin/non-comment `C {...}` instances — `MPOUT`, `MPAMP`
(`sky130_fd_pr__pfet_g5v0d10v5`), `R2A`, `R2B`, `R1`
(`sky130_fd_pr__res_high_po`), `Q1`, `Q2` (`sky130_fd_pr__pnp_05v5_*`), and
`XAMP` (`design/error_amp.sym`, drawn as a black box — its internals are a
separate sub-block, out of scope per issue #1122's "top-level schematic is
the MVP" decision) — matches the diagram exactly.
