# `klt array-check`

Assert **structural regularity of a cell array**: layer-by-layer mirror or
translation symmetry, and abutment of the unit-cell placements at a declared
pitch and count. Purely geometric; no extraction, netlist or deck.

```
klt array-check <file> [--cell <name>] [--layers <json> (--mirror-axis x|y --at <um> | --translate dx,dy)] [--tolerance <um>] [--pitch <um> --count <n> [--direction x|y]] [--top <cell>] [--format text|json]
```

At least one check must be requested; both may run in one invocation.

## Symmetry

- `--layers` -- **required**. Layer set as a path to a JSON file or an inline
  JSON array of `[layer, datatype]` pairs. Empty or malformed values exit `1`.
- `--mirror-axis x --at A` mirrors about the **vertical** line `x = A`
  (x -> 2A - x); `--mirror-axis y --at A` about the horizontal line `y = A`.
- `--translate dx,dy` checks translation symmetry. The XOR is taken **only
  inside the overlap** of the root's bounding box and its translated copy.
- `--mirror-axis` and `--translate` are mutually exclusive.
- `--cell` selects the root to flatten; default is every top cell (`--top`
  restricts to one).

Each root is flattened per layer, merged, transformed and XORed with itself;
every residual polygon is a violation. `--tolerance T` (um) ignores residual
features narrower than `2T`. A layer absent from the stream contributes nothing.

## Abutment

`--cell` (the **unit cell**), `--pitch` (um, `> 0`) and `--count` (integer
`>= 1`) are required together; `--direction` is `x` (default) or `y`. The
bounding box of every placement of the unit cell under each top cell (placement
transforms applied) is sorted along the direction; the check flags:

| `kind` | Meaning |
| ------ | ------- |
| `gap` | consecutive placements are separated by more than `--tolerance` |
| `overlap` | consecutive placements overlap by more than `--tolerance` |
| `pitch` | placements abut but advance by something other than the pitch |
| `count` | the number of placements differs from `--count` |
| `length` | the union extent differs from `count x pitch` |

The pitch is rounded to the layout's database grid. Top cells with no
placement of the unit cell are skipped; none at all exits `1`.

## JSON schema (the contract)

Emitted through the shared envelope (`docs/json-contract.md`).

```json
{
  "schema_version": 1,
  "file": "column.gds",
  "layers": [[68, 20]],
  "cell": "BIT",
  "mirror_axis": "x",
  "at_um": 10.0,
  "translate_um": null,
  "tolerance_um": 0.0,
  "pitch_um": 10.0,
  "count": 4,
  "direction": "x",
  "dbu_um": 0.001,
  "status": "clean | violated",
  "violation_count": 1,
  "violations": [
    {
      "rule": "array.symmetry_residual | array.abutment",
      "description": "layer 68/20 differs under mirror about x = 10.0 um",
      "check": "symmetry | abutment",
      "kind": "mirror | translation | gap | overlap | pitch | count | length",
      "layer": "68/20",
      "cell": "TOP",
      "bbox": {"left": 12.0, "bottom": 3.0, "right": 14.0, "top": 5.0,
               "width": 2.0, "height": 2.0}
    }
  ]
}
```

- `layer` is `null` for abutment violations, which also carry `measured_um`
  and `expected_um`.
- `bbox` is in **micrometres**. Violations are sorted deterministically.

Gate on `status`, not the exit code.

## Exit codes

| Code | Meaning |
| ---- | ------- |
| `0`  | Ran successfully -- symmetric / abutting as declared. |
| `1`  | Failed to run -- bad layout file, no check requested, empty `--layers`, unknown `--cell`/`--top`, bad `--pitch`/`--count`/`--tolerance`/`--translate`. |
| `2`  | Usage error (bad `--format`, `--mirror-axis`, `--direction`) -- from argparse. |
| `3`  | Ran successfully, at least one violation. |

## Not covered

- Keep-out of device layers under pads (use `klt keepout-check --region`).
- Net-level (electrical) symmetry or matching; rotational symmetry.
- Perpendicular alignment of placements.
