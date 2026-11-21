# `klt seam-check`

Check the **seams of a layout assembled from abutting cells**: that the cells
tile with no gap or overlap, and that a named conductor layer is one continuous
piece across every cell boundary inside a declared band. Purely geometric --
no extraction, netlist, or deck.

`klt drc` checks rules inside a flattened layout and `klt ring-check` checks
that a layer set forms a closed annulus; neither answers "does this rail cover
its band across all seams". Loop closure and hole count are deliberately out
of scope here -- use [`klt ring-check`](ring-check.md) (optionally with
`--cells`).

```
klt seam-check <file> [--tiling <cell>]... [--layer <L/D> --band <x0,y0,x1,y1>...] [--top <cell>] [--format text|json]
```

At least one assertion is required; `--layer` and `--band` go together.

- `<file>` -- GDSII or OASIS stream.
- `--tiling` -- repeatable, exact cell name. For every placement of the named
  cells under each checked top cell (nesting, rotation, mirroring and arrays
  are honoured; a nested placement inside a selected cell is not counted
  separately), the placements' bounding boxes must cover their union bounding
  box completely (**`gap`**) and no area may be covered by more than one
  placement (**`overlap`**). The gap test is against the union's *bounding
  box*, so an assembly that deliberately surrounds an opening (a ring of
  cells) reports the opening as a gap -- check its straight sides separately.
- `--layer L/D` with repeatable `--band x0,y0,x1,y1` (micrometres) -- the
  layer's flattened, merged shapes clipped to each band must cover the band
  fully (**`gap`**, one violation per uncovered region) and form a single
  connected polygon (**`fragmented`**, one per piece, otherwise). Shapes that
  touch only at a corner are not connected.
- `--top` -- restrict to one top cell; omit to check every top cell.
- `--format` -- `text` (default) or `json`.

Empty or absent geometry is never a vacuous pass: no placements of the
`--tiling` cells, or no shapes on the layer inside a band (including a band
outside the geometry or a layer absent from the stream), is an **`empty`**
violation.

## JSON output

```json
{
  "schema_version": 1,
  "file": "row.gds",
  "tiling": ["TILE"],
  "layer": [68, 20],
  "bands_um": [[0.0, 0.0, 30.0, 2.0]],
  "dbu_um": 0.001,
  "status": "fail",
  "violation_count": 1,
  "violations": [
    {
      "rule": "seam.continuity",
      "description": "band is not fully covered: gap in the conductor",
      "check": "seam_continuity",
      "kind": "gap",
      "layer": "68/20",
      "cell": "TOP",
      "bbox": { "left": 9900, "bottom": 0, "right": 10100, "top": 2000 },
      "polygon": [[9900, 0], [9900, 2000], [10100, 2000], [10100, 0]]
    }
  ]
}
```

| Field             | Type                       | Description |
| ----------------- | -------------------------- | ----------- |
| `schema_version`  | integer                    | `1`. |
| `file`            | string                     | The input path as provided. |
| `tiling`          | array\<string\> \| null    | The `--tiling` cells, or `null` when not requested. |
| `layer`           | [int, int] \| null         | The `--layer`, or `null`. |
| `bands_um`        | array\<[num ×4]\> \| null  | The `--band` windows in micrometres, or `null`. |
| `dbu_um`          | number                     | Database unit in micrometres. |
| `status`          | `"pass"` \| `"fail"`       | `"pass"` iff every requested assertion holds on every checked top cell. |
| `violation_count` | integer                    | `len(violations)`. |
| `violations`      | array\<object\>            | Sorted by `(cell, check, kind, bbox)`; shaped like `klt drc`'s entries. |

`violations[]` entries: `rule` (`"seam.tiling"` or `"seam.continuity"`, stable
ids), `description`, `check` (`"seam_tiling"` / `"seam_continuity"`), `kind`
(`"empty"`, `"gap"`, `"overlap"`, `"fragmented"`), `layer` (`"<l>/<d>"`, or
`"tiling"` for the tiling assertion), `cell` (the top cell), `bbox`
(`{left, bottom, right, top}` in **database units**), and `polygon` (hull
points in database units, `null` for `empty`).

## Exit codes

| Code | Meaning |
| ---- | ------- |
| `0`  | Every requested assertion holds. |
| `1`  | Failed to run -- unreadable file, no assertion requested, `--layer` without `--band` (or the reverse), malformed `--layer`/`--band`, degenerate band, unknown `--top` or `--tiling` cell. |
| `2`  | Usage error from argparse. |
| `3`  | Ran successfully; at least one seam violation. |

Gate on `status`, not the exit code (see
[`docs/json-contract.md`](../json-contract.md#exit-codes)). On exit `1` the
shared error envelope is emitted.

## `klt signoff`

The envelope is recognised by `klt signoff` as kind `seam-check` (identified by
its `tiling` and `bands_um` keys, ahead of the `klt drc` shape that shares the
`violations` list). It is an opt-in kind and may satisfy T1 item 2 (Layout)
only; a passing seam check says nothing about DRC, LVS or simulation. It is
also exposed over MCP automatically, as every `klt` verb is.
