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
