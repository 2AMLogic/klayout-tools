# `klt diff`

Compare two GDSII or OASIS layout streams and report whether they hold the
same geometry, labels and hierarchy. Built for regenerate-and-compare
(`--check`) gates: DRC/LVS status cannot tell "same layout" from "different
layout that also passes", and a raw byte hash trips on stream timestamps.

```
klt diff <file_a> <file_b> [--top <cell>] [--max-items N] [--format text|json]
```

- `--top` — compare only this cell's hierarchy (the cell plus everything it
  calls). The cell must exist in **both** streams, else exit `1`. Without it,
  every cell in each stream is compared.
- `--max-items` — cap on every listed difference list (default `50`). The
  `*_count` fields always carry the true totals; `truncated` is `true` when any
  list was cut.

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
- Shape items are `box` (`bbox`), `polygon` (`hull`, `holes`), `path`
  (`width`, `bgn_ext`, `end_ext`, `round`, `points`) or `text` (`string`, `x`,
  `y`, `rotation`, `mirror`), all in database units. Instance items carry
  `cell`, `x`, `y`, `angle`, `mirror`, `mag` and, for arrays, `array`
  (`na`, `nb`, `a`, `b`). The stream reader may swap an array's a/b axes.
- Cells present in only one stream are listed by name in `cells_only_in_a` /
  `cells_only_in_b`; shape-level diffs are reported for cells present in both.
  A differing `dbu` sets `dbu_equal` to `false` and makes the run `different`.
- `differences` is sorted by cell name, then layer/datatype; items are sorted.

Not provided: a flattened per-layer XOR ("same mask, different hierarchy")
mode and tolerance-based comparison.
