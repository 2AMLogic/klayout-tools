# `klt diff`

Compare two GDSII or OASIS layout streams and report whether they hold the
same geometry, labels and hierarchy. Built for regenerate-and-compare
(`--check`) gates: DRC/LVS status cannot tell "same layout" from "different
layout that also passes", and a raw byte hash trips on stream timestamps —
and, across a `klt` upgrade, on record order within a cell. See
[`docs/json-contract.md`](../json-contract.md#stream-stability-contract-layout-stream-bytes-are-not-stable-across-klt-versions)
→ "Stream-stability contract" for why a byte-compare gate is within-version
only, and why this verb is the supported cross-upgrade gate.

```
klt diff <file_a> <file_b> [--top <cell>] [--max-items N] [--flattened] [--format text|json]
```

- `--top` — compare only this cell's hierarchy (the cell plus everything it
  calls). The cell must exist in **both** streams, else exit `1`. Without it,
  every cell in each stream is compared.
- `--max-items` — cap on every listed difference list (default `50`). The
  `*_count` fields always carry the true totals; `truncated` is `true` when any
  list was cut.
- `--flattened` — compare the flattened geometry instead of the cell-by-cell
  content (see [Flattened mode](#flattened-mode---flattened)).

## Exit codes

| Code | Meaning                                                              |
| ---- | -------------------------------------------------------------------- |
| `0`  | Equivalent under the normalization below.                            |
| `3`  | Ran fine, layouts differ (payload on stdout), like `klt drc` findings. |
| `1`  | Unreadable file, unknown `--top`, or invalid `--max-items` (error on stderr). |

`1` is reserved for application errors by
[`docs/json-contract.md`](../json-contract.md), so a `--check` script can tell
"layouts differ" (`3`) from "file unreadable" (`1`).

## Normalization (what is and is not compared)

**Ignored:** stream timestamps (BGNLIB/BGNSTR), order of shapes and instances
within a cell, trailing record padding, the library name, user properties,
text size/font/alignment.

**Never ignored:** shape coordinates in database units, path width and end
extensions (and round-ends flag), text string/position/rotation/mirror,
instance transform (displacement, angle, mirror, magnification) and array
parameters, the database unit, cell names, layer/datatype numbers.

This is the default mode's contract. **`--flattened` is the only mode that
ignores cell names and hierarchy** — the default never does.

Duplicates count: two identical boxes differ from one. Polygons are compared
after rotating each contour to start at its smallest point. A rectangular
boundary and the same rectangle as a polygon are the stream reader's call
(KLayout reads rectangular boundaries as boxes), so both streams are read
the same way.

## JSON output

```json
{
  "schema_version": 1,
  "top": null,
  "status": "different",
  "equivalent": false,
  "stream_identical": false,
  "a": {"file": "a.gds", "stream_sha256": "...", "canonical_hash": "...", "dbu_um": 0.001, "cell_count": 2},
  "b": {"file": "b.gds", "stream_sha256": "...", "canonical_hash": "...", "dbu_um": 0.001, "cell_count": 2},
  "dbu_equal": true,
  "cells_only_in_a": [],
  "cells_only_in_b": [],
  "different_cell_count": 1,
  "differences": [
    {
      "cell": "TOP",
      "layers": [
        {
          "layer": 1, "datatype": 0,
          "only_in_a_count": 1, "only_in_b_count": 1,
          "only_in_a": [{"type": "box", "bbox": [0, 0, 1000, 1000]}],
          "only_in_b": [{"type": "box", "bbox": [0, 0, 1100, 1000]}]
        }
      ],
      "instances": {"only_in_a_count": 0, "only_in_b_count": 0, "only_in_a": [], "only_in_b": []},
      "truncated": false
    }
  ],
  "truncated": false,
  "max_items": 50
}
```

- `stream_sha256` is the SHA-256 of each file's raw bytes. `canonical_hash` is
  a SHA-256 over the normalized content. **Equal `canonical_hash` with
  different `stream_sha256` means a timestamp/order-only difference** (the run
  exits `0`, `stream_identical` is `false`).
- **`canonical_hash` is not the signoff geometry digest (issue #5551).** It is
  this command's own normalization, meaningful only when compared with another
  `canonical_hash` from `klt diff`. It is a *different* value from the digest
  `klt signoff --attest 2 --regenerated-hash ... --hash-kind geometry` is
  graded against (and carries no `sha256:` prefix), so feeding it to that flag
  grades `stale_artifact`. Use `klt stats --geometry-digest --format json | jq -r
  .geometry_digest` for that — see [`docs/cli/stats.md`](stats.md) → "Geometry
  digest".
- Shape items are `box` (`bbox`), `polygon` (`hull`, `holes`), `path`
  (`width`, `bgn_ext`, `end_ext`, `round`, `points`) or `text` (`string`, `x`,
  `y`, `rotation`, `mirror`), all in database units. Instance items carry
  `cell`, `x`, `y`, `angle`, `mirror`, `mag` and, for arrays, `array`
  (`na`, `nb`, `a`, `b`). The stream reader may swap an array's a/b axes.
- Cells present in only one stream are listed by name in `cells_only_in_a` /
  `cells_only_in_b`; shape-level diffs are reported for cells present in both.
  A differing `dbu` sets `dbu_equal` to `false` and makes the run `different`.
- `differences` is sorted by cell name, then layer/datatype; items are sorted.

Not provided: tolerance-based comparison.

## Flattened mode (`--flattened`)

Answers "do the two streams hold the same drawn geometry, however it is
partitioned into cells?" — e.g. after a release renamed an emitted inner cell
that the default mode reports as `different` although nothing drawn moved.
Read-only; the default mode's output is unchanged.

Everything is taken on the instance-placed geometry under `--top` (without it,
every top cell). The run is `equivalent` (exit `0`) only if **all four** agree:

1. **per-layer XOR** — each side's polygon-bearing shapes (boxes, polygons,
   paths) on a layer/datatype are merged and XOR-ed. Exact; no tolerance.
2. **drawn layer/datatype set** — compared as its own field, over the *union*
   of both streams' pairs, so a layer only one stream drew is visible as itself
   and not merely as a non-empty XOR. Label-only pairs are not drawn layers.
3. **flattened bounding box** of the drawn geometry.
4. **labels** — compared outside the polygon XOR (text is not a polygon), per
   `(layer, texttype)`, as the multiset of `(string, x, y)` after flattening.

Exit codes are unchanged (`0` equivalent, `3` differ, `1` unreadable file or
`--top` absent from either stream). A differing `dbu` is `different`; the XOR
fields are then `null` because an XOR across database units is undefined.
`--max-items` caps the `layers[]` and per-layer label lists (differing layers
are listed first); `layer_count`, `different_layer_count` and every
`*_count` stay exact, and `truncated` says whether anything was cut.

```json
{
  "schema_version": 1,
  "mode": "flattened",
  "top": "TOP",
  "status": "equivalent",
  "equivalent": true,
  "a": {"file": "a.gds", "dbu_um": 0.001, "drawn_layers": [{"layer": 1, "datatype": 0}],
        "flattened_shapes": 2, "bbox_um": {"left": 0.0, "bottom": 0.0, "right": 3.0, "top": 0.5, "width": 3.0, "height": 0.5},
        "label_count": 0},
  "b": {"...": "same shape as a"},
  "dbu_equal": true,
  "drawn_layers": {"equal": true, "only_in_a": [], "only_in_b": []},
  "bbox": {"equal": true},
  "layer_count": 1,
  "different_layer_count": 0,
  "layers": [
    {"layer": 1, "datatype": 0, "shapes_a": 2, "shapes_b": 2,
     "xor_polygon_count": 0, "xor_area_dbu2": 0, "xor_area_um2": 0.0, "equivalent": true}
  ],
  "labels": {"equal": true, "different_layer_count": 0, "layers": []},
  "truncated": false,
  "max_items": 50
}
```

`xor_area_um2` is `xor_area_dbu2 × dbu²`; `shapes_a`/`shapes_b` are the
flattened polygon-bearing shape counts before merging. A flattened-compare is
not the signoff geometry digest either: `klt stats --geometry-digest` is
hierarchy-sensitive, so it differs across a cell rename.
