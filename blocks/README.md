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

## Design-pipeline worked example (Epic #105 Phase 3)

`sky130-ota-5t` is an eighth block, added by issue #130: a sky130 5T OTA
driven through the staged agent design pipeline
([`docs/design/design-pipeline.md`](../docs/design/design-pipeline.md)) from
proposal through schematic-level simulation. It is outside the #4 test
corpus (it has no source GDS at all — layout generation, S7, has no `klt`
verb yet, #104), so like `clkinv_1` above it deliberately has no
`output/layout.json`, explained in its own
[`sky130-ota-5t/NOTE.md`](sky130-ota-5t/NOTE.md). The full run — every
pipeline artifact, the stage-by-stage status, and a provisional pre-layout
signoff comparison — lives at
[`../examples/design-pipeline/README.md`](../examples/design-pipeline/README.md).

## Canary blocks (issue #62)

Two more blocks — `gf180-bandgap` and `sky130-bandgap` — are ingested from
real, public canary block repos (2AM Logic's own bandgap voltage-reference
designs, built end to end by AI agents against the open gf180mcu/sky130
PDKs), not the #4 demo corpus and not the design-pipeline worked example
above. Per operator decision 2026-07-31, these real blocks are first-class
gallery content, not just purpose-built demos; simple demo blocks remain a
welcome supplement, not the focus.

[`../scripts/ingest-canary.py`](../scripts/ingest-canary.py) does the work:

```
python scripts/ingest-canary.py --repo 2AMLogic/gf180-bandgap
python scripts/ingest-canary.py --repo 2AMLogic/sky130-bandgap
```

It clones (or fetches+checks out, if already cached under the gitignored
`.cache/canary-repos/`) the given repo at its default branch's current
`HEAD`, resolves and pins the exact commit sha as `layout.json`'s
`source.ref`, and writes `blocks/<slug>/output/layout.json`.

**Public-repo gate (fail-closed).** Before touching the filesystem, the
script calls `gh api repos/<repo> --jq .visibility` and refuses to proceed
unless the answer is exactly `"public"` — a private repo, an inaccessible
repo, an API error, or `gh` not being installed/authenticated are all
treated identically (refuse, write nothing). There is no allowlist to keep
in sync with the still-private canaries (`gf180-trng`, `gf180-sar-adc`,
`gf180-pll`, `gf180-temp-por`, `gf180-ldo`) — the API call is the source of
truth at ingest time, every run, so a repo that flips to public later just
starts working the next time this script runs against it.

**Sim-evidence cards for pre-layout blocks.** Both current canaries are at
schematic/sim stage — no GDS exists yet, so there is nothing for `klt
layers`/`klt cells` to derive `layer_count`/`cell_count`/`renders` from.
Rather than a `no_artifacts` stub, their `layout.json` carries `status: "in
design — simulation evidence"` (the honest status chip — see issue #140)
plus two fields not part of `klt layout-metrics`'s own contract:

- `spec_summary` — the source repo's `README.md` "Target specification"
  table, parsed verbatim (`{status_note, rows: [{parameter, target,
  stretch?, corner_binding?}, ...]}`). `gf180-bandgap`'s table is ratified;
  `sky130-bandgap`'s is still draft — `status_note` carries that state
  through to the detail page.
- `signals` — reuses `layout.json`'s `signals` object shape (issue #99),
  but populated from the *source repo's own* `sim/` evidence trail (its
  append-only `records/*.md`, and `suite/summaries/*.md` when the repo has
  full-loop testbenches for its ratified spec rows) rather than from a
  fresh `klt sim` run this repo performs — these are the canary repo's own
  simulation results. An experiment whose latest record has no extractable
  `**Overall: PASS/FAIL/ERROR**` verdict (pure device-characterization
  "recorded, not claimed" data) is excluded, since there is no honest
  pass/fail/error value for it.

Every canary block also carries `source: {repo, ref, path}` provenance and
`downloadable: true` (the public-repo gate having just confirmed the repo
is public) — see `site/src/data/types.ts`'s `LayoutSource`/
`LayoutSpecSummary` for the field-level TypeScript contract, and
`scripts/ingest-canary.py`'s module docstring for the full derivation
(including the markdown-parsing helpers for the two different `sim/`
evidence record shapes the two current canaries happen to use).

When a canary's layout eventually lands, re-running the same command finds
the GDS (under `layout/`, `gds/`, or the repo root) and upgrades the block
to a normal `"ok"`/`"partial"` metrics record — same slug, same card. This
upgrade path exists in the script today but is not exercised by either
current canary (both pre-layout); see `tests/test_ingest_canary.py`.

## License note

`layout.json` metrics here are derived from the #4 corpus GDS files, which
are redistributed under the upstream repositories' Apache License 2.0 terms
(see [`../tests/corpus/README.md`](../tests/corpus/README.md) for full
provenance). The generated `layout.json` files themselves are original
output of this repository's tooling and carry this repository's MIT
license.

The two canary blocks' `layout.json` content is derived from
`2AMLogic/gf180-bandgap` and `2AMLogic/sky130-bandgap`, both Apache License
2.0 (see each repo's own `LICENSE`) — the same public-repo gate that
ingests them also confirms, at ingest time, that they are public.
