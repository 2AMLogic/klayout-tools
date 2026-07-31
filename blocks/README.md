# Gallery blocks

Per-block data for the klayout-tools.org gallery (Epic #13). Each immediate
subdirectory is a "block" (a layout the gallery shows); the site's data
loader (`site/src/data/loadLayouts.ts`) discovers blocks here and reads each
one's `<slug>/output/layout.json`.

## Provisional bootstrap (until #61 lands)

`layout.json`'s schema is defined by issue #61 ("Gallery: per-layout metrics
extractor"), which had not landed when the loader in #59 was built. Per
#59's acceptance criteria, this directory is bootstrapped instead from the
[#4 test corpus](../tests/corpus/README.md) —
[`../scripts/bootstrap-gallery-blocks.py`](../scripts/bootstrap-gallery-blocks.py)
runs `klt layers` / `klt cells` against each corpus GDS file and writes
`<slug>/output/layout.json`.

Once #61 lands with the real `klt` metrics extractor:

1. Regenerate this directory with that command instead.
2. Delete `scripts/bootstrap-gallery-blocks.py`.
3. Revisit whether `layout.json` should stay checked in (as it does today)
   or move to a gitignored, locally-generated artifact — #61 owns that
   decision.

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

`gf180mcu_fd_sc_mcu9t5v0__clkinv_1` is **intentionally** left without an
`output/layout.json` (the directory exists, the file does not) — it
demonstrates the loader's `no_artifacts` handling on a real, non-synthetic
block directory rather than only in unit tests.

## License note

`layout.json` metrics here are derived from the #4 corpus GDS files, which
are redistributed under the upstream repositories' Apache License 2.0 terms
(see [`../tests/corpus/README.md`](../tests/corpus/README.md) for full
provenance). The generated `layout.json` files themselves are original
output of this repository's tooling and carry this repository's MIT
license.
