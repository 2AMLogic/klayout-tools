This block's `output/layout.json` is a real, layout-derived record
(issue #896) — not a placeholder.

It is ingested from the public
[`2AMLogic/gf180-bandgap`](https://github.com/2AMLogic/gf180-bandgap) canary
repo by [`../../scripts/ingest-canary.py`](../../scripts/ingest-canary.py)
(issue #62) — a bandgap voltage reference on gf180mcu, designed end-to-end
by AI agents against a RATIFIED target spec, with a real, verified block
layout: `status: "ok"`, with `layer_count`/`cell_count`/`instance_count`
and a `renders.overview` thumbnail derived from the source repo's own
committed GDS at `layout/bandgap_top/bandgap_top.gds` (`source.path` in
`layout.json` records the exact file). Per that repo's own README, this
layout is DRC-clean (0 violations) and LVS-matching against the schematic
netlist on both comparators (`klt lvs` and an independent `netgen`
cross-check); post-layout parasitic-extracted re-verification has run and
currently fails the output-reference/temperature-coefficient spec rows
(attributed to the drawn PNP array's effective dVBE ratio, tracked as an
open item in that repo — not a `klayout-tools`/ingest defect).
`layout_file` is staged into `output/bandgap_top.gds` and `downloadable`
is `true`. The ratified target-spec table (`spec_summary`, from the source
repo's `README.md`) and real `ngspice` PVT sweep results from its `sim/`
evidence trail (`signals`, sourced from that repo's own simulation
records, not a fresh `klt sim` run against this repo) are both still
present alongside the layout data. See [`../README.md`](../README.md) for
the field-level documentation and
[`../../scripts/ingest-canary.py`](../../scripts/ingest-canary.py)'s
module docstring for how each field is derived.

Re-running `python scripts/ingest-canary.py --repo 2AMLogic/gf180-bandgap`
refreshes this file from the source repo's current state (still gated on
that repo staying public) — as its layout advances, this is the repeatable
way to pick it up; no separate refresh mechanism exists or is needed.

## `schematic.svg` (issue #1122)

`schematic.svg` is a headless `xschem --svg` export of
`design/bandgap_core.sch` in
[`2AMLogic/gf180-bandgap`](https://github.com/2AMLogic/gf180-bandgap) @
`04c59f1103504e824fdcb224eaa7e070ef8e2b82`, post-processed to the site's
theme convention (`currentColor` ink, no baked dark-canvas background,
cropped `viewBox`) — full provenance and the post-processing diff are
recorded in the SVG's own leading `<!-- ... -->` comment. `bandgap_core.sch`
(the actual Brokaw-core topology) was chosen over the literal hierarchy
root `design/bandgap_top.sch`, which is a pure black-box interconnect of
`bandgap_core`/`bandgap_amp`/`bandgap_startup` with no devices of its own —
per issue #1122's "top-level schematic is the MVP, sub-blocks can come
later" decision, the informative core cell is the more useful "top-level"
diagram to ship first.

Device-count spot-check against `design/bandgap_core.sch` at that commit:
17 non-pin/non-comment `C {...}` instances — `M1`-`M5`, `MC1`-`MC4`, `MCB`,
`MNB` (`pfet_03v3`/`nfet_03v3` mirrors/cascodes/tail), `Q1`-`Q3`
(`pnp_05p00x05p00`/`pnp_10p00x10p00`), `R1`, `R2` (`ppolyf_u`), and `XTRIM`
(`bandgap_trim.sym`, drawn as a black box — its internals are a separate
sub-block, out of scope per the decision above) — matches the diagram
exactly.
