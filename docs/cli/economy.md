# `klt economy`

Quantitative layout-density report (issue #1012): utilization (per cell and
for the top), a whitespace map (a coarse grid plus the largest exact empty
regions), bounding-box tightness/aspect-ratio/dead-margins, and an optional
area-budget/reference-area check — the numbers backend for judging **silicon
economy** (agent-produced layouts are correct-but-sprawling by default, and
area is unit cost) before it gates anything.

```
klt economy <file> [--top <cell>] [--grid-cols N] [--grid-rows N]
                    [--max-empty-regions N] [--budget-um2 <area>]
                    [--reference-area-um2 <area>] [--format text|json]
```

- `<file>` — path to a GDSII (`.gds`) or OASIS (`.oas`) file. KLayout
  auto-detects the stream format on read; the extension is not authoritative.
- `--top` — top cell to report on when the stream has more than one
  (required in that case; optional otherwise), matching `klt stats --top`.
- `--grid-cols`/`--grid-rows` — whitespace-grid resolution (default `4x4`, a
  quadrant-scale overview). Independent of the fixed, finer grid the
  `dead_margins_um` computation uses internally (see below).
- `--max-empty-regions` — cap on how many of the largest exact empty regions
  to report (default `10`). `empty_region_count` always reports the full
  count before capping.
- `--budget-um2` — an area budget in square micrometres. When given, adds a
  `budget` block reporting PASS/FAIL against the top cell's bbox area with
  the margin. Absent a budget, the top-level `bbox_area_um2`/`bbox_area_mm2`
  fields already report the absolute area, so a budget can be set later.
- `--reference-area-um2` — a comparable hand-designed reference's area in
  square micrometres (e.g. a PDK example cell, a published block). When
  given, adds a `reference` block reporting the ratio (`N.NNx the reference
  area`).
- `--format` — `text` (default, a human-readable summary + per-cell table) or
  `json`.

The command runs fully headless via KLayout's batch database API
(`klayout.db`) — no GUI, no Qt — and is safe to run in CI.

## JSON schema (the contract)

**JSON is the API.** Human-readable text output is a courtesy; the JSON
schema below is the stable contract. Per the project's rules, **breaking
(renaming, removing, or retyping) a field is a breaking change**. New fields
may be added without breaking the contract, so consumers should ignore
unknown fields. See [`docs/json-contract.md`](../json-contract.md) for the
envelope shared across all `klt` commands (`schema_version`, error shape,
exit codes).

```json
{
  "schema_version": 1,
  "file": "bandgap_core_routed.gds",
  "top": "bandgap_core_routed",
  "dbu_um": 0.001,
  "bbox_um": {"left": -10.0, "bottom": -10.0, "right": 318.2, "top": 215.44,
              "width": 328.2, "height": 225.44},
  "bbox_area_um2": 73989.408,
  "bbox_area_mm2": 0.073989408,
  "aspect_ratio": 1.4558197303051813,
  "drawn_area_um2": 28153.02002,
  "utilization": 0.38050067950266614,
  "tight_bbox_um": {"left": -10.0, "bottom": -10.0, "right": 318.2, "top": 215.44,
                     "width": 328.2, "height": 225.44},
  "tight_bbox_area_um2": 73989.408,
  "bbox_tightness": 1.0,
  "dead_margins_um": {"left": 41.025, "right": 41.025, "bottom": 37.573, "top": 0.0},
  "whitespace_area_um2": 45836.38798,
  "whitespace_fraction": 0.6194993204973338,
  "whitespace_grid": {
    "cols": 4,
    "rows": 4,
    "cells": [
      {"row": 0, "col": 0,
       "bbox_um": {"left": -10.0, "bottom": -10.0, "right": 72.05, "top": 46.36,
                   "width": 82.05, "height": 56.36},
       "covered_fraction": 0.21993651415618842,
       "whitespace_fraction": 0.7800634858438116}
    ]
  },
  "largest_empty_regions": [
    {"bbox_um": {"left": -8.0, "bottom": -8.0, "right": 316.2, "top": 213.44,
                 "width": 324.2, "height": 221.44},
     "area_um2": 26087.11535,
     "fill_fraction": 0.36337661521981185}
  ],
  "empty_region_count": 298,
  "cells": [
    {"name": "bjt_array",
     "bbox_um": {"left": -2.87, "bottom": -0.97, "right": 10.07, "top": 2.73,
                 "width": 12.94, "height": 3.7},
     "bbox_area_um2": 47.878,
     "drawn_area_um2": 35.71,
     "utilization": 0.7458540456994862}
  ],
  "row_utilization": null,
  "annotation_layers_excluded": [],
  "provenance": {
    "klt_version": "0.2.0",
    "klayout_version": "0.30.10",
    "pdk": null,
    "deck": null,
    "input": {"content_hash": "sha256:f2ff80..."}
  }
}
```

With `--budget-um2`/`--reference-area-um2`, `budget`/`reference` blocks are
additionally present (see "Budget and reference blocks" below).

### Top-level fields

| Field                        | Type                    | Description |
| ----------------------------- | ----------------------- | ----------- |
| `schema_version`              | integer                 | Version of this command's JSON shape (starts at `1`). |
| `file`                        | string                  | The input path exactly as provided on the command line. |
| `top`                         | string                  | Name of the top cell the report is scoped to. |
| `dbu_um`                      | number (float)          | Database unit in micrometres. |
| `bbox_um`                     | object                  | The top cell's bounding box (hierarchy-inclusive), in micrometres — `{left, bottom, right, top, width, height}`. |
| `bbox_area_um2`                | number (float)          | `bbox_um`'s area, in square micrometres. |
| `bbox_area_mm2`                | number (float)          | Same, in square millimetres — a budget-setting reference for larger blocks. |
| `aspect_ratio`                 | number (float) \| null  | `bbox_um.width / bbox_um.height`; `null` if height is `0`. |
| `drawn_area_um2`               | number (float)          | Merged (non-overlap-double-counted), non-annotation-layer drawn area, hierarchy-flattened under `top`. |
| `utilization`                  | number (float)          | `drawn_area_um2 / bbox_area_um2`; `0.0` if `bbox_area_um2` is `0`. |
| `tight_bbox_um`                 | object \| null           | The minimal enclosing rectangle of the drawn (non-annotation) geometry — `null` only when the top cell draws no non-annotation geometry at all. |
| `tight_bbox_area_um2`           | number (float)          | `tight_bbox_um`'s area. |
| `bbox_tightness`                | number (float)          | `tight_bbox_area_um2 / bbox_area_um2` — `1.0` means drawn geometry already touches every `bbox` edge somewhere (see "Bounding-box tightness vs. dead margins" below). |
| `dead_margins_um`               | object                   | `{left, right, bottom, top}` — grid-band-walked dead-margin width per edge (see below). |
| `whitespace_area_um2`           | number (float)          | `bbox_area_um2 - drawn_area_um2`. |
| `whitespace_fraction`           | number (float)          | `1.0 - utilization`. |
| `whitespace_grid`               | object                   | `{cols, rows, cells}` — see below. |
| `largest_empty_regions`         | array\<object\>          | The largest disjoint empty regions, capped at `--max-empty-regions`, largest-area first. See below. |
| `empty_region_count`            | integer                  | Total disjoint empty-region count before capping. |
| `cells`                         | array\<object\>          | Per-cell local (non-flattened) utilization — see below. |
| `row_utilization`               | object \| null           | Best-effort std-cell-row utilization, or `null` when the top cell's placement doesn't look row-based. See below. |
| `annotation_layers_excluded`    | array\<string\>          | `"layer/datatype"` strings excluded from every drawn-area computation (reserved 990-999 annotation range; see `klt layers`). |
| `provenance`                    | object                   | The shared provenance block (`klt_version`, `klayout_version`, `pdk`, `deck`, `input`) — see [`docs/json-contract.md`](../json-contract.md). `pdk`/`deck` are always `null` (this command resolves neither); `input.content_hash` is the input file's own SHA-256. |
| `budget`                        | object (optional)        | Present only with `--budget-um2`. See below. |
| `reference`                     | object (optional)        | Present only with `--reference-area-um2`. See below. |

### `whitespace_grid`

`{cols, rows, cells}` — `bbox_um` partitioned into `cols` x `rows` equal
cells (row `0` is the bottom row), each reporting its own covered/whitespace
fraction: `{row, col, bbox_um, covered_fraction, whitespace_fraction}`.
`covered_fraction` is that cell's drawn-area fraction (`0.0`-`1.0`,
`0.0` for a cell with zero cell area); `whitespace_fraction` is `1.0 -
covered_fraction`. This is the "where is the wasted area, by quadrant/grid"
view the issue asks for — coarse and configurable (`--grid-cols`/
`--grid-rows`), meant to be scanned at a glance rather than read
sub-cell-precisely (use `largest_empty_regions` for exact geometry).

### `largest_empty_regions`

Each entry is one disjoint connected component of `bbox_um` minus the merged
drawn geometry (an exact `kdb.Region` boolean subtraction, not a
rasterization): `{bbox_um, area_um2, fill_fraction}`.

- `bbox_um` — the region's own bounding rectangle (its "position + size").
- `area_um2` — the region's **true** area (not its bbox's area) — may be
  smaller than `bbox_um`'s own area for an irregular (non-rectangular) gap.
- `fill_fraction` — `area_um2 / bbox_um's own area` — `1.0` for a perfect
  rectangle, lower for an L-shaped/irregular gap. A low `fill_fraction`
  means `bbox_um` should be read as "where this gap roughly is", not
  "this exact rectangle is empty".

Sorted largest-`area_um2`-first; capped at `--max-empty-regions` (default
`10`). A sparse design's single largest empty region is often the
background swath connecting several smaller gaps around islands of drawn
content — in that case its `bbox_um` spans nearly the whole design and
`fill_fraction` is well below `1.0`, correctly signalling "this isn't one
clean rectangle to fill".

### `cells`

Per-library-cell utilization using **only each cell's own local geometry**
(not flattened/recursive — the same convention `klt cells`' `shapes` field
uses), over that cell's own local drawn extent (not its hierarchy-inclusive
`bbox()`). This answers "which specific library cell is sparse", distinct
from the top-level `utilization` (the whole assembled design's density).
Cells with no local drawn geometry at all (pure assembly/wrapper cells) are
omitted — utilization is undefined for a cell that draws nothing itself.
Sorted by name.

Each entry: `{name, bbox_um, bbox_area_um2, drawn_area_um2, utilization}` —
`bbox_um`/`bbox_area_um2` here are the cell's own local drawn region's
bounding box (not the full-hierarchy `cell.bbox()`), so `utilization` is
always in `(0.0, 1.0]`, never diluted by unrelated sub-instance geometry.

### `row_utilization`

Best-effort std-cell-row utilization inferred purely from the top cell's
direct instance placement geometry — there is no DEF `ROW` record available
at the bare-GDS stage this command operates at. `null` when the top cell's
placement doesn't look row-based (too few direct instances, or no detected
row passes the consistency/legality checks below) — omission, not a
fabricated zero.

When present: `{row_count, mean_utilization, rows}`, where each `rows[]`
entry is `{bbox_um, instance_count, row_area_um2, placed_area_um2,
utilization}` (`utilization = placed_area_um2 / row_area_um2`, area-weighted
into `mean_utilization` across all detected rows).

Detection heuristic (see `economy.py`'s `_row_utilization` docstring for the
full algorithm): direct instances are clustered by placed bottom-y
coordinate within half the layout-wide modal instance height of each other;
a cluster is only reported as a row when at least 2 instances share it, at
least 60% of them match the modal height, and — the key legality check — the
cluster's placed footprint area does not exceed its row's own bounding area
(a real std-cell row never overlaps itself by construction; a cluster that
would need `utilization > 1.0` to describe it is not a genuine row and is
silently discarded rather than reported as an impossible density).

### Budget and reference blocks

With `--budget-um2 <area>`, `budget` is added:

```json
"budget": {
  "budget_um2": 50000.0,
  "actual_um2": 73989.408,
  "margin_um2": -23989.408,
  "margin_fraction": -0.4797881600,
  "status": "fail"
}
```

`actual_um2` is the top cell's `bbox_area_um2`; `status` is `"pass"` when
`actual_um2 <= budget_um2`, else `"fail"`; `margin_um2` is
`budget_um2 - actual_um2` (negative when over budget).

With `--reference-area-um2 <area>`, `reference` is added:

```json
"reference": {
  "reference_area_um2": 40000.0,
  "actual_area_um2": 73989.408,
  "ratio": 1.8497352
}
```

`ratio` is `actual_area_um2 / reference_area_um2` — "this design is `ratio`x
the reference area". A crude comparison hook (issue #1012's item 5): the
caller is responsible for sourcing a comparable reference area (a PDK
example cell, a published block) — this command does not look one up.

## Bounding-box tightness vs. dead margins

`bbox_tightness` (`tight_bbox_area_um2 / bbox_area_um2`) and
`dead_margins_um` both describe "how much of `bbox_um` is unused", but
answer different questions and can legitimately disagree:

- `bbox_tightness` is **exact and literal**: it compares `bbox_um` to the
  minimal enclosing rectangle of the merged drawn geometry. Almost every
  real layout carries *some* edge-touching feature (a seal ring, a power
  rail stripe, a single alignment mark, a guard ring) — so `bbox_tightness`
  is very often exactly `1.0` (geometry touches every edge somewhere), even
  when most of the interior near an edge is empty. That is not a bug — it
  is the literal answer to "does anything touch the edge".
- `dead_margins_um` answers the more actionable question "how far can I
  push each edge in before hitting *substantial* content" — walked over a
  fixed, finer internal grid (8x6, independent of `--grid-cols`/
  `--grid-rows`), growing a margin band from each edge inward while that
  band's *mean* covered fraction stays below 20%. A single thin
  edge-hugging feature does not zero out the margin the way the naive
  `bbox` vs. `tight_bbox` subtraction would.

Both are reported because they're both useful: `bbox_tightness` is a cheap
one-number tightness signal; `dead_margins_um` names which edge(s) actually
have room to pull geometry inward, and by how much.

## Determinism

Areas are computed via `klayout.db` `Region` boolean/merge operations over
the input's own geometry; the same input always produces the same output
(deterministic engine, no RNG). `cells` is sorted by name;
`largest_empty_regions` by descending `area_um2`; `rows[]` within
`row_utilization` by ascending `bbox_um.bottom`.

## Text format

The default `text` format prints a summary, the largest empty regions,
`row_utilization`/`budget`/`reference` (when present), and a per-cell
utilization table. It is intended for human eyes and its exact layout is
**not** part of the contract — parse the JSON instead.

```
$ klt economy blocks/sky130-bandgap/output/bandgap_core_routed.gds
file: blocks/sky130-bandgap/output/bandgap_core_routed.gds
top: bandgap_core_routed
dbu_um: 0.001
bbox_um: (-10, -10) - (318.2, 215.44)  328.2 x 225.44  (73989.408 um2 / 0.073989 mm2)
aspect_ratio: 1.455819730305181
utilization: 0.3805
bbox_tightness: 1.0000
dead_margins_um: left=41.025 right=41.025 bottom=37.573 top=0.000
whitespace: 45836.388 um2 (0.6195 fraction)
empty_regions: showing 10 of 298
  (-8, -8) - (316.2, 213.44)  area=26087.115 um2  fill=0.363
  ...

cell                                drawn_area_um2  bbox_area_um2  utilization
----------------------------------  --------------  -------------  -----------
bjt_array                                   35.710         47.878       0.7459
...
```

## Exit codes and errors

| Exit code | Meaning |
| --------- | ------- |
| `0`       | Success — report written to stdout. |
| `1`       | The file is missing, unreadable, not a recognisable layout, `--top` names a cell absent from the stream, the layout has more than one top cell and `--top` was not given, the layout has no cells, the resolved top cell has no geometry at all, or `--grid-cols`/`--grid-rows`/`--max-empty-regions` is not positive. |
| `2`       | Usage error (missing argument, bad `--format` value) — from argparse. |

On error, a concise message is written to **stderr** and nothing is written
to stdout. No Python traceback is printed.

- `--format text` (default): a plain-text line prefixed `klt economy:`.
- `--format json`: the documented JSON error envelope (see
  [`docs/json-contract.md`](../json-contract.md)):

  ```json
  { "schema_version": 1, "error": { "command": "economy", "message": "file not found: missing.gds" } }
  ```
