# Gallery blocks

Per-block data for the klayout-tools.org gallery (Epic #13). Each immediate
subdirectory is a "block" (a layout the gallery shows); the site's data
loader (`site/src/data/loadLayouts.ts`) discovers blocks here and reads each
one's `<slug>/output/layout.json`.

## Regenerating

`layout.json`'s schema is the contract emitted by `klt layout-metrics`
(issue #61, "Gallery: per-layout metrics extractor") — see
[`../docs/cli/layout-metrics.md`](../docs/cli/layout-metrics.md) and
`src/klayout_tools/layout_metrics.py` for the authoritative shape (the
always-present fields are `schema_version`, `generated_at`, `slug`, `name`,
`status`). This directory is generated from the [#4 test
corpus](../tests/corpus/README.md) by
[`../scripts/regen-gallery-blocks.py`](../scripts/regen-gallery-blocks.py),
which materializes each corpus GDS into its block directory, renders
per-layer PNGs with `klt render` into `<slug>/output/renders/`, and emits
`<slug>/output/layout.json` with `klt layout-metrics` — the same path a
real (non-corpus) block will use. Each block's `meta.json` (curated by
hand, read by the extractor, never overwritten by the regen script)
carries the display `name`/`description` overrides.

Remaining follow-up: revisit whether `layout.json`, the renders, and the
materialized GDS should stay checked in (as they do today) or move to
gitignored, locally-generated artifacts.

## Contents

Seven blocks, one per GDS file in the #4 test corpus:

| Slug | PDK | Source |
| --- | --- | --- |
| `sky130_fd_sc_hd__inv_1` | sky130 | `tests/corpus/sky130/sky130_fd_sc_hd__inv_1.gds` |
| `sky130_fd_sc_hd__nand2_2` | sky130 | `tests/corpus/sky130/sky130_fd_sc_hd__nand2_2.gds` |
| `sky130_fd_sc_hd__buf_4` | sky130 | `tests/corpus/sky130/sky130_fd_sc_hd__buf_4.gds` |
| `sky130_fd_sc_hd__dfxtp_2` | sky130 | `tests/corpus/sky130/sky130_fd_sc_hd__dfxtp_2.gds` |
| `gf180mcu_fd_sc_mcu9t5v0__and2_1` | gf180mcu | `tests/corpus/gf180mcu/gf180mcu_fd_sc_mcu9t5v0__and2_1.gds` |
| `gf180mcu_fd_sc_mcu9t5v0__dffnq_1` | gf180mcu | `tests/corpus/gf180mcu/gf180mcu_fd_sc_mcu9t5v0__dffnq_1.gds` |
| `gf180mcu_fd_sc_mcu9t5v0__clkinv_1` | gf180mcu | `tests/corpus/gf180mcu/gf180mcu_fd_sc_mcu9t5v0__clkinv_1.gds` |

(The earlier bootstrap left `gf180mcu_fd_sc_mcu9t5v0__clkinv_1` without
artifacts as a live demo of the loader's `no_artifacts` path; now that the
gallery is public-facing, all blocks ship full artifacts and that path is
exercised in unit tests only.)

## License note

`layout.json` metrics here are derived from the #4 corpus GDS files, which
are redistributed under the upstream repositories' Apache License 2.0 terms
(see [`../tests/corpus/README.md`](../tests/corpus/README.md) for full
provenance). The generated `layout.json` files themselves are original
output of this repository's tooling and carry this repository's MIT
license.
