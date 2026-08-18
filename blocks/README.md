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

## Renders (issue #651/#942, epic #650 "gallery visuals")

All 6 corpus blocks with a `layout.json` (plus the two canary blocks below)
carry a `renders` field: the gallery-thumbnail `"overview"` composite `klt
render` produces (`src/klayout_tools/render.py`), one entry per non-empty
layer, and — for the canary blocks only — a zoomed `"center_crop"`
composite, all attached by `scripts/_gallery_common.py`'s shared
`attach_overview_render` helper. Per-layer keys are labeled with that PDK's
curated `(layer, datatype) -> name` table
(`klayout_tools.decks.get_layer_names`, e.g. `"poly.drawing"`,
`"Metal2"`) when known, else a `"layer_<n>_<n>"` fallback — never a bare
`"67_20"`-style filename stem (issue #942; previously only the fixed
`{"overview": ...}` entry was recorded, "Option A" from #651, discarding
the per-layer PNGs `klt render` always wrote alongside it). `.gitignore`
now un-ignores those per-layer PNGs and each block's `renders/center_crop/
overview.png`, not just the composite — still only a handful of
~23-47 KB PNGs per block. `clkinv_1` has no `renders` field for the same
reason it has no `layout.json` at all (see above).

## Schematic diagrams (issue #1121)

A block may optionally carry a `schematic` field (staged the same
`output/`-relative-path way as `renders`/`layout_file`/
`signals.corners[].waveform`): `output/schematic.svg` plus a required
`provenance` caption, referenced from `layout.json` as
`schematic.{path,provenance}`. `site/scripts/copy-renders.mjs` stages it
alongside every other artifact; `DetailPage.tsx` renders it in a
"Schematic" section above the layer renders when present (omitted entirely
otherwise). See [`../site/README.md`](../site/README.md#schematic-section-issue-1121)
for the full staging convention, including the theme rule diagrams must
follow (a single self-contained mid-tone palette, no `prefers-color-scheme`
— the file is loaded via `<img>`, so it cannot inherit the host page's CSS,
and the OS-tracked media query doesn't match this always-dark site's own
theme). `sky130_fd_sc_hd__inv_1` carries
the seed diagram — a hand-drawn 2-transistor CMOS inverter topology; the
content-production issues for analog (xschem export, #1122) and
standard-cell (PDK-netlist-derived, #1123) diagrams stage into this same
convention.

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

**Both wave-1 canaries now have a real, committed layout** (issue #896):
`sky130-bandgap`'s routed core (`layout/bandgap-core/reports/LATEST` →
`bandgap_core_routed.gds`, LVS-clean as of DR-007) and `gf180-bandgap`'s
DRC-clean/LVS-matching top block (`layout/bandgap_top/bandgap_top.gds`).
`ingest-canary.py` finds each repo's own layout GDS (see "GDS detection"
below), stages it into `blocks/<slug>/output/`, and derives
`layer_count`/`cell_count`/`instance_count` via the same `klt
layers`/`klt cells` library calls `klt layout-metrics` uses — `status`
becomes `"ok"` (or `"partial"` if either extractor call fails) instead of
the pre-layout status below, and `renders` (overview + per-layer + a zoomed
`center_crop`, issue #942) is attached the same way the #4 corpus blocks
get theirs, plus the crop (see "Renders" above). Layer labels come from
`infer_layer_names()`'s slug-prefix PDK guess (`gf180-bandgap` ->
gf180mcu, `sky130-bandgap` -> sky130), since a canary repo carries no
explicit `pdk` field. Both blocks are `downloadable: true`.

**GDS detection.** Canary repos don't share one fixed layout-output
convention, so `find_layout_gds()` tries, under each of `layout/`, `gds/`,
and the repo root: (1) a `reports/LATEST` pointer file naming the current
timestamped run directory (`sky130-bandgap`'s convention — the run
directory often holds several sub-block GDS exports alongside the
assembled block, e.g. `amp_input_pair.gds`, so a `*_routed*.gds` match,
excluding `*_inner*`, is preferred when present); (2) failing that, a
bounded recursive search (3 levels deep, skipping `reports/`, `fixtures/`,
`drc/`, `lvs/`, `renders/`, `output/` subtrees) for the flatter
`layout/<block>/<block>.gds` shape `gf180-bandgap` uses. **Refreshing a
canary's render as its layout advances is just re-running the same
command** — no separate refresh mechanism exists or is needed; see
`scripts/ingest-canary.py`'s module docstring for the full detection logic
and `tests/test_ingest_canary.py::test_full_ingest_real_public_canary_with_layout`
for the real-network regression coverage of both repos' current shapes.

**Sim-evidence cards for pre-layout blocks.** A canary with no GDS
findable this way (or a future wave-1+ canary that hasn't reached layout
yet) gets `layout.json` with `status: "in design — simulation evidence"`
(the honest status chip — see issue #140) instead of `"ok"`/`"partial"`/
`"no_artifacts"`. Two extra fields, not part of `klt layout-metrics`'s own
contract, are populated either way (pre- or post-layout — both current
canaries' `layout.json` still carries both alongside their real
`layer_count`/`renders`):

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

When a canary's layout advances further (a new routed run, a design
change), re-running the same command re-resolves `find_layout_gds()`
against the repo's current `HEAD`, restages the GDS, re-renders the
overview thumbnail, and rewrites `layout.json` in place — same slug, same
card, no hand-editing required.

## Field data (issue #958, Epic #840 Phase 3a)

A block that has a committed layout can also carry
`<slug>/output/<slug>.em-export.json` — real
[geode-fem](https://github.com/rjwalters/geode-fem) electrostatic field and
coupling-capacitance results **for that block's own geometry**, conforming to
[`../docs/schemas/em-site-export.schema.json`](../docs/schemas/em-site-export.schema.json).
Unlike the standalone benchmark exports under `../examples/em/`, these are an
artifact of the gallery project itself, a sibling of its `layout.json` and its
`signals/` waveforms. Two blocks ship one today:

| Slug | Structure solved |
| --- | --- |
| `sky130-bandgap` | The bandgap core's differential branch bundle — `TAIL`, two unlabeled neighbours, `D2`, `D1` — running 182.5 µm in parallel on `met1` |
| `gf180-bandgap` | The reference-output net `vref` packed against five unlabeled `Metal1` neighbours at drawn minimum spacing, running 94.5 µm |

Everything in the artifact traces back to the committed GDS: conductor widths
and spacings are the drawn ones, the parallel-run length is measured from the
layout, the conductor names are the GDS's own label texts, and
`provenance.geometry` pins the exact file, its sha256 and the commit that
introduced it (`tests/test_em_block_exports.py` re-checks that hash against the
file on disk, so an export cannot quietly outlive the layout it describes).

Generation is reproducible from a committed script,
[`../examples/em/block_coupling/generate.py`](../examples/em/block_coupling/generate.py) —
**that directory's [`README.md`](../examples/em/block_coupling/README.md) is the
recipe for adding the next block**, including the `--dry-run` bundle search
that needs no solver checkout. Nothing on the site renders these yet; the
detail-page field panel is Phase 3b.

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
