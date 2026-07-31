This block deliberately has no `output/layout.json`.

It exists in the corpus (`tests/corpus/gf180mcu/gf180mcu_fd_sc_mcu9t5v0__clkinv_1.gds`)
but `scripts/bootstrap-gallery-blocks.py` skips it on purpose, so the
gallery's layout data loader (`site/src/data/loadLayouts.ts`) has a real,
non-synthetic block to represent with `status: "no_artifacts"` — see
[`../README.md`](../README.md).
