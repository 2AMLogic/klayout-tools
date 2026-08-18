This block has no *layout* artifacts, and until issue #1122 had no
`output/layout.json` at all.

It is the sky130 5T OTA worked example driven through the staged agent
design pipeline (Epic #105 Phase 3, issue #130) — see
[`../../examples/design-pipeline/README.md`](../../examples/design-pipeline/README.md)
for the full stage-by-stage run (proposal through S10 schematic-level
simulation, both AC and operating-point corner sweeps passing 20/20).
Layout generation (S7) has no `klt` verb yet (#104), so no GDS exists for
this block and there is nothing for `klt layout-metrics` to derive a full
`layout.json` from — the gallery's data loader
(`site/src/data/loadLayouts.ts`) would otherwise represent this block
honestly with a synthesized `status: "no_artifacts"` stub rather than
fabricating layout data, following the existing precedent at
[`../gf180mcu_fd_sc_mcu9t5v0__clkinv_1/NOTE.md`](../gf180mcu_fd_sc_mcu9t5v0__clkinv_1/NOTE.md)
— see [`../README.md`](../README.md).

## `schematic.svg` (issue #1122)

Issue #1122 adds a **minimal, hand-written** `output/layout.json` —
`schema_version`/`generated_at`/`slug`/`name`/`description`, still
`status: "no_artifacts"` (no layer/cell/instance counts, no `renders`, no
fabricated layout data — the honesty precedent above is unchanged) — solely
to carry a `schematic` field, since `loadLayout()` only reads `schematic`
from a real `layout.json` and synthesizes a bare stub (no optional fields
at all) when the file is absent. This is the one block in this repo where
`layout.json`'s presence means "has a schematic diagram", not "has a
layout".

Unlike the two bandgap canary blocks, this block has no xschem-authored
design-of-record — its S6 schematic stage is the SPICE netlist
[`../../examples/design-pipeline/ota_5t.spice`](../../examples/design-pipeline/ota_5t.spice)
(`2AMLogic/klayout-tools` @ `7c8f43a58be51b50085302d32179895c7c0f97f3`).
`schematic.svg` is therefore a **redrawn** (hand-authored, in xschem)
diagram, not an export — a 1:1 device-for-device transcription of that
netlist, per issue #1122's "acceptable to stage a redrawn version, provided
the provenance line says so" guidance. Full provenance is recorded in the
SVG's own leading `<!-- ... -->` comment and in `layout.json`'s
`schematic.provenance` caption; per the site's staging convention (issue
#1121), the SVG is self-contained (an inline `<style>` block switching
palettes via `@media (prefers-color-scheme: dark)`), since it is loaded via
a plain `<img>` tag and cannot inherit the host page's CSS.

Device-count spot-check against `ota_5t.spice` at that commit: 6
transistors (`M1`/`M2` input pair, `M3`/`M4` mirror load, `M5` tail, `M5b`
diode bias reference) + 1 ideal bias current source (`Iref`) in the
diagram, matching every `X`-line device and current source in the netlist.
The AC-analysis-only testbench scaffolding in that file (`Vdd`/`Vcm`/`Vin`
supplies, the `CL` load cap, and the `Lfb`/`Cfb` DC-feedback network used
only to bias the open-loop-gain measurement) is intentionally omitted —
it is test fixture, not part of the OTA circuit itself.
