This block deliberately has no `output/layout.json`.

It is the sky130 5T OTA worked example driven through the staged agent
design pipeline (Epic #105 Phase 3, issue #130) — see
[`../../examples/design-pipeline/README.md`](../../examples/design-pipeline/README.md)
for the full stage-by-stage run (proposal through S10, at both the
schematic level and post-extraction).

Layout generation (S7) now runs — `examples/design-pipeline/ota_5t_top.gds`
is a real `klt gen` + `klt gen-compose` output (Epic #153 phase 4, issue
#164) — but that composed cell has not been wired into the gallery content
pipeline, so there is still no `layout.json` here. Deriving one via
`klt layout-metrics` (plus the render/thumbnail steps the loader expects) is
follow-on gallery work. The gallery's data loader
(`site/src/data/loadLayouts.ts`) represents this block honestly with
`status: "no_artifacts"` rather than fabricating layout data, following the
existing precedent at
[`../gf180mcu_fd_sc_mcu9t5v0__clkinv_1/NOTE.md`](../gf180mcu_fd_sc_mcu9t5v0__clkinv_1/NOTE.md)
— see [`../README.md`](../README.md).
