# `klt keepout-check`

Assert that **no shape on a forbidden layer set lies within a margin of a
guarded cell or region** -- a keep-out around a spiral inductor, a sensitive
analog block, etc. -- and report each intruding shape when one does. Purely
geometric; no extraction, netlist or deck.

```
klt keepout-check <file> --forbidden <json> --margin <um> [--cells <name>]... [--region <json>] [--top <cell>] [--format text|json]
```

- `<file>` -- path to a GDSII (`.gds`) or OASIS (`.oas`) file.
- `--forbidden` (alias `--layers`) -- **required**. The forbidden layer set, as
  a path to a JSON file or an inline JSON array of `[layer, datatype]` pairs
  (e.g. `'[[67, 20]]'`). Empty or malformed values exit `1`.
- `--margin` -- **required**. Keep-out margin in micrometres, `>= 0`. A shape
  at distance **exactly** the margin is clear (clear iff distance >= margin);
  `0` flags only overlap with the guard itself. Non-numeric or negative values
  exit `1`.
- `--cells` -- repeatable exact cell name to guard. The guard is the bounding
  box of **every placement** of the cell reachable from each checked top cell
  (placement transforms applied, in the top cell's coordinates). A name absent
  from the stream, or never placed under the checked top cell(s), exits `1`.
- `--region` -- guard window as an inline JSON array `[left, bottom, right,
  top]` in micrometres. May be combined with `--cells` (the guards are
  unioned). **At least one of `--cells` / `--region` is required.**
- `--top` -- check only this top cell; omit to check every top cell. Top cells
  where a `--cells` guard is not placed (and no `--region` is given) are
  skipped.
- `--format` -- `text` (default) or `json`.

The keep-out zone is the guard sized outward by `--margin`. Shapes owned by the
guarded cells' own subtrees (the inductor's own metal, say) are **never**
counted, however they overlap the zone; every other shape on the forbidden
layers is, including shapes owned by the top cell or by sibling cells.

## JSON schema (the contract)

Emitted through the shared envelope (`docs/json-contract.md`).

```json
{
  "schema_version": 1,
  "file": "design.gds",
  "layers": [[67, 20]],
  "margin_um": 10.0,
  "region_um": null,
  "cells": ["SPIRAL"],
  "dbu_um": 0.001,
  "status": "clean | violated",
  "violation_count": 1,
  "violations": [
    {
      "rule": "keepout.forbidden_shape",
      "description": "forbidden layer 67/20 shape lies within the 10.0 um keep-out margin",
      "check": "keepout",
      "kind": "inside | margin",
      "layer": "67/20",
      "cell": "TOP",
      "bbox": {"left": 105.0, "bottom": 20.0, "right": 110.0, "top": 30.0,
               "width": 5.0, "height": 10.0}
    }
  ]
}
```

- `kind` is `inside` when the intruding piece overlaps the guard itself and
  `margin` when it lies only in the margin band.
- `bbox` is the bounding box of the **part of the shape inside the zone**, in
  **micrometres** (unlike `klt ring-check`/`klt drc`, which report database
  units).
- Violations are sorted deterministically by cell, layer, kind and bbox.

Gate on `status`, not the exit code.

## Exit codes

| Code | Meaning |
| ---- | ------- |
| `0`  | Ran successfully -- the keep-out is clear. |
| `1`  | Failed to run -- bad layout file, empty/invalid `--forbidden`, no guard (`--cells`/`--region`), unknown `--cells` or `--top`, bad `--margin`, malformed `--region`. |
| `2`  | Usage error (missing argument, bad `--format`) -- from argparse. |
| `3`  | Ran successfully, forbidden shapes intrude on the keep-out. |

## Not covered

- Fill-density rules inside a region (use `klt layout-metrics`).
- Guard-ring construction or fill insertion.
- Non-rectangular guards: a cell is guarded by its bounding box per placement.
