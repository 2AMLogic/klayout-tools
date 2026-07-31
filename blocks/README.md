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
block directory rather than only in unit tests. This predates and is
unrelated to the `signals` field below — `clkinv_1` still gets a real,
checksum-verified vendored netlist and a real `klt sim` sweep (its
`output/sim/signals.json` exists on disk and is exercised by
`tests/test_gallery_signals.py`), it is just never attached to a
`layout.json` that doesn't exist.

## Signals (issue #99)

All 6 blocks with a `layout.json` (every block above except
`clkinv_1`) carry a `signals` field — real, transistor-level `klt sim` PVT
sweep results against vendored standard-cell netlists (checksum-verified,
[`../scripts/fetch-cell-netlists.sh`](../scripts/fetch-cell-netlists.sh)),
not a synthetic placeholder. See
[`../docs/cli/layout-metrics.md`](../docs/cli/layout-metrics.md#signals-object)
for the field's schema and
[`../scripts/gallery_signals.py`](../scripts/gallery_signals.py)'s module
docstring for the full pipeline, including the one documented device
substitution the 3 gf180mcu cells require (their vendored `nfet_05v0`/
`pfet_05v0` device references have no matching bin in the currently
published open gf180mcu primitive-model repo).

Each block's `output/signals/<corner_id>.json` files are the waveform
artifacts issue #100's detail-page viewer fetches — real ngspice transient
output for that block's 3 nominal corners (nominal supply, 27 °C, one per
process corner), referenced from `layout.json` as
`signals.corners[].waveform` (a path relative to `output/`, staged into the
built site by `site/scripts/copy-renders.mjs`). They supersede the
hand-built `sky130_fd_sc_hd__inv_1` fixture #100 shipped as a placeholder
while this pipeline was in flight; every value in them is now simulator
output. Only the nominal corners are staged — the remaining 12 PVT corners
keep their measurements in `layout.json` but their multi-megabyte rawfiles
are not committed.

## License note

`layout.json` metrics here are derived from the #4 corpus GDS files, which
are redistributed under the upstream repositories' Apache License 2.0 terms
(see [`../tests/corpus/README.md`](../tests/corpus/README.md) for full
provenance). The generated `layout.json` files themselves are original
output of this repository's tooling and carry this repository's MIT
license.
