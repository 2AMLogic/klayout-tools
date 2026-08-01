This block's `output/layout.json` is a **sim-evidence card**, not a
layout-derived record.

It is ingested from the public
[`2AMLogic/sky130-bandgap`](https://github.com/2AMLogic/sky130-bandgap)
canary repo by [`../../scripts/ingest-canary.py`](../../scripts/ingest-canary.py)
(issue #62) — a bandgap voltage reference on sky130, designed end-to-end by
AI agents, currently at device-characterization stage with no GDS yet and a
still-DRAFT (not yet ratified) target spec. `status` is `"in design —
simulation evidence"` rather than `"ok"`/`"partial"`/`"no_artifacts"`:
there is no layout to derive `layer_count`/`cell_count`/`renders` from, but
there *is* real content — the draft target-spec table (`spec_summary`, from
the source repo's `README.md`) and real `ngspice` device-characterization
results from its `sim/` evidence trail (`signals`, sourced from that
repo's own simulation records, not a fresh `klt sim` run against this
repo). See [`../README.md`](../README.md) for the field-level documentation
and [`../../scripts/ingest-canary.py`](../../scripts/ingest-canary.py)'s
module docstring for how each field is derived.

Re-running `python scripts/ingest-canary.py --repo 2AMLogic/sky130-bandgap`
refreshes this file from the source repo's current state (still gated on
that repo staying public); once its layout lands, the same command upgrades
this entry to a normal `"ok"`/`"partial"` metrics record.
