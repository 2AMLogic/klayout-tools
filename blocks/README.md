# Gallery blocks

Per-block data for the klayout-tools.org gallery (Epic #13). Each immediate
subdirectory is a "block" (a layout the gallery shows); the site's data
loader (`site/src/data/loadLayouts.ts`) discovers blocks here and reads each
one's `<slug>/output/layout.json`.

## Bootstrap data

`layout.json`'s schema is the contract emitted by `klt layout-metrics`
(issue #61, "Gallery: per-layout metrics extractor") — see
[`../docs/cli/layout-metrics.md`](../docs/cli/layout-metrics.md) and
`src/klayout_tools/layout_metrics.py` for the authoritative shape (the
always-present fields are `schema_version`, `generated_at`, `slug`, `name`,
`status`). This directory is bootstrapped from the [#4 test
corpus](../tests/corpus/README.md) —
[`../scripts/bootstrap-gallery-blocks.py`](../scripts/bootstrap-gallery-blocks.py)
runs `klt layers` / `klt cells` against each corpus GDS file and writes
`<slug>/output/layout.json` in that same shape (it omits the optional
`layout_file`/`drc` fields, which require running the full extractor against
a materialized block directory).

Follow-ups for a future pass (owned by #61's extractor):

1. Regenerate this directory with `klt layout-metrics` directly (which also
   populates `layout_file` and, with `--deck`, `drc`).
2. Retire `scripts/bootstrap-gallery-blocks.py` once that regeneration path
   is wired up.
3. Revisit whether `layout.json` should stay checked in (as it does today)
   or move to a gitignored, locally-generated artifact.

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

## Signals fixture (provisional, pending #99)

`sky130_fd_sc_hd__inv_1/output/layout.json`'s `signals` section and its
`output/signals/*.json` waveform artifacts are a **hand-built fixture**
added by issue #100 (Epic #90 Phase 2, the waveform viewer) to exercise the
end-to-end detail-page/staging path against real gallery data, since #99
("Gallery: signals pipeline") had not landed real `klt sim` output at the
time. The shape mirrors `klt sim`'s own JSON contract (`docs/cli/sim.md`)
as closely as possible — see the field docs on `Layout.signals` in
`site/src/data/types.ts` — but the actual sample values are synthetic (a
plausible RC-edge inverter transient, not simulator output). Re-running
`scripts/bootstrap-gallery-blocks.py` will drop this fixture (it doesn't
know about `signals`); once #99 lands, regenerate this block for real and
this fixture note can be deleted.

## License note

`layout.json` metrics here are derived from the #4 corpus GDS files, which
are redistributed under the upstream repositories' Apache License 2.0 terms
(see [`../tests/corpus/README.md`](../tests/corpus/README.md) for full
provenance). The generated `layout.json` files themselves are original
output of this repository's tooling and carry this repository's MIT
license.
