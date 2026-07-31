This block deliberately has no `output/layout.json`.

It is the sky130 5T OTA worked example driven through the staged agent
design pipeline (Epic #105 Phase 3, issue #130) — see
[`../../examples/design-pipeline/README.md`](../../examples/design-pipeline/README.md)
for the full stage-by-stage run (proposal through S10 schematic-level
simulation, both AC and operating-point corner sweeps passing 20/20).
Layout generation (S7) has no `klt` verb yet (#104), so no GDS exists for
this block and there is nothing for `klt layout-metrics` to derive
`layout.json` from. The gallery's data loader
(`site/src/data/loadLayouts.ts`) represents this block honestly with
`status: "no_artifacts"` rather than fabricating layout data, following the
existing precedent at
[`../gf180mcu_fd_sc_mcu9t5v0__clkinv_1/NOTE.md`](../gf180mcu_fd_sc_mcu9t5v0__clkinv_1/NOTE.md)
— see [`../README.md`](../README.md).
