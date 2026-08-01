This block's `output/layout.json` is a **sim-evidence card**, not a
layout-derived record.

It is ingested from the public
[`2AMLogic/gf180-bandgap`](https://github.com/2AMLogic/gf180-bandgap) canary
repo by [`../../scripts/ingest-canary.py`](../../scripts/ingest-canary.py)
(issue #62) — a bandgap voltage reference on gf180mcu, designed end-to-end
by AI agents, currently at schematic/sim stage with no GDS yet. `status` is
`"in design — simulation evidence"` rather than `"ok"`/`"partial"`/
`"no_artifacts"`: there is no layout to derive `layer_count`/`cell_count`/
`renders` from, but there *is* real content — a ratified target-spec table
(`spec_summary`, from the source repo's `README.md`) and real `ngspice` PVT
sweep results from its `sim/` evidence trail (`signals`, sourced from that
repo's own simulation records, not a fresh `klt sim` run against this
repo). See [`../README.md`](../README.md) for the field-level documentation
and [`../../scripts/ingest-canary.py`](../../scripts/ingest-canary.py)'s
module docstring for how each field is derived.

Re-running `python scripts/ingest-canary.py --repo 2AMLogic/gf180-bandgap`
refreshes this file from the source repo's current state (still gated on
that repo staying public); once its layout lands, the same command upgrades
this entry to a normal `"ok"`/`"partial"` metrics record.
