# `klt socket-check`

Check a GDSII or OASIS layout file against a **socket/template descriptor**
-- a JSON contract declaring the outline, named pins, reserved layers, and
numeric interface budgets a block's layout must fit -- and report each
mechanically-checkable class of violation as structured data.

```
klt socket-check <file> --socket <descriptor.json> [--format text|json]
```

- `<file>` -- path to a GDSII (`.gds`) or OASIS (`.oas`) file. KLayout
  auto-detects the stream format on read; the extension is not authoritative.
- `--socket` -- required. Path to a socket descriptor JSON file (see
  [Descriptor schema](#descriptor-schema) below). Not validated by argparse --
  a missing, unreadable, or malformed descriptor exits `1` with a clean error
  rather than argparse's usage-error exit `2`.
- `--format` -- `text` (default, a human-readable summary) or `json`.

## Why this is a separate verb from `klt precheck`

`klt precheck` (issue #245) runs a **PDK-generic** layout-hygiene battery: off-
grid geometry, zero-area polygons, cell-name hygiene, an optional layer
whitelist, and pin labels landing on drawn geometry. Its checks apply to any
layout on a given PDK, independent of any specific block's interface.

`klt socket-check` runs a different, **block-specific** battery: does *this*
layout fit *this* block's declared socket -- its outline, its named pins at
their declared positions/layers, and its reserved layers. The two verbs check
orthogonal things and share no code beyond both operating on
`klayout.db.Region`/`Layout`/`Texts` primitives.

## Motivation

Nothing else in this toolkit can answer "does this layout fit its socket?"
mechanically. Pin positions/layers, outline, reserved layers, and interface
budgets otherwise exist only as prose in a spec. `klt gen`'s generators emit a
`ports` dict describing their own output (self-authored metadata a caller can
trust only as far as it trusts the generator) -- `klt socket-check` instead
reads the actual drawn geometry and text labels back out of a real
GDS/OASIS stream and diffs them against a declared, independent contract.

A future `klt gen-compose` placement pass, or an S2 architecture-partition
design stage, could emit/consume the same descriptor -- see
[`docs/schemas/socket.schema.json`](../schemas/socket.schema.json)'s own
description -- but no such integration exists yet; this verb only checks.

## Engine

`klt socket-check` runs fully headless via the pip `klayout` package's native
batch database API (`klayout.db`) -- direct shape/text iteration and
`kdb.Texts` collection, the same primitives `klt precheck` uses (no
`Region.*_check()` design-rule primitives, unlike `klt drc`). No dependency on
the standalone `klayout` application binary.

## Descriptor schema

A socket descriptor is a JSON document validated against
[`docs/schemas/socket.schema.json`](../schemas/socket.schema.json) (draft
2020-12 JSON Schema). Minimal example:

```json
{
  "schema": "klt.socket/1",
  "name": "example_block",
  "outline": { "x0": 0.0, "y0": 0.0, "x1": 10.0, "y1": 10.0 },
  "pins": [
    { "name": "VDD", "layer": [67, 20], "x": 1.0, "y": 1.0, "tolerance_um": 0.01 }
  ],
  "reserved_layers": [[64, 20]],
  "budgets": [
    { "name": "signal_r_max", "value": 500, "unit": "ohm", "notes": "worst-case corner" }
  ]
}
```

- `schema` -- optional; if present must be `"klt.socket/1"`.
- `name` -- optional human-readable block name, echoed back as
  `socket_name` in the response.
- `outline` -- **required**. `{x0, y0, x1, y1}` in micrometres, the same
  coordinate space `pins[].x`/`pins[].y` use. `x1 > x0` and `y1 > y0` are
  enforced.
- `pins` -- optional (default `[]`). Each entry: `name` (the expected label
  text), `layer` (`[layer, datatype]` the label is expected on), `x`/`y`
  (expected position, micrometres), and optionally `tolerance_um` (default
  `0`, exact match) and `width_um`/`height_um`.

  **`width_um`/`height_um` are descriptive metadata only in this version** --
  they are not checked against drawn geometry (see [Scope](#scope-what-is-and-isnt-mechanically-checked) below).
- `reserved_layers` -- optional (default `[]`). A list of `[layer,
  datatype]` pairs forbidden to this block.
- `budgets` -- optional (default `[]`). Arbitrary named numeric interface
  budgets: `{name, value, unit, notes?}`. **Never mechanically verified** --
  see [Scope](#scope-what-is-and-isnt-mechanically-checked).

## Scope: what is (and isn't) mechanically checked

Of a socket descriptor's four fields, three are geometric and genuinely
checked -- `pins`, `outline`, `reserved_layers`. The fourth, `budgets`, is
**not**: verifying a signal path's maximum resistance/capacitance/current
against a drawn layout needs parasitic-extraction or simulation data this
checker does not have (`klt extract --parasitics` produces a first-order
lumped estimate, but wiring that comparison in is out of scope for this
verb). Rather than half-implementing a budget check that can't actually run,
`klt socket-check` reports every declared budget back verbatim with a
`"declared_unverified"` status -- present in the response for downstream
tooling to see, never affecting the overall `status`.

Similarly, a pin's `width_um`/`height_um` describe the pin's expected
footprint (useful to a future `klt gen-compose` placement consumer) but are
not checked against the drawn geometry underneath the label in this version.

## Checks

Every run reports exactly three checks, in this order, each with its own
`status` of `"pass"`, `"fail"`, or `"skipped"`:

### `pins`

Every declared pin has a text label somewhere in the layout matching its
`name` verbatim, on its declared `layer`, within `tolerance_um` (per-axis,
Chebyshev distance) of its declared `(x, y)`. Three distinct failure reasons,
checked in this priority order per pin:

1. **`missing`** -- no text label anywhere in the layout matches the pin's
   `name` at all.
2. **`wrong_layer`** -- a label matching `name` exists, but not on the
   declared `layer` (every matching label found is reported under `actual`,
   regardless of position).
3. **`misplaced`** -- a label matching `name` exists on the declared `layer`,
   but not within `tolerance_um` of the declared position (every matching
   label on the right layer is reported under `actual`).

**Skipped when the descriptor declares no `pins`.**

### `outline`

Every drawn shape (polygon, box, or path -- text labels are not "drawn
geometry" for this check; a stray/misplaced label is a `pins` concern) across
every layer and every top cell falls entirely within the descriptor's
`outline` bounding box. Always runs (`outline` is a required descriptor
field).

### `reserved_layers`

No shape is drawn on a `(layer, datatype)` pair listed in the descriptor's
`reserved_layers`. A layer present in the input stream's layer table with
zero shapes is not a violation -- only actual usage is flagged. **Skipped
when the descriptor declares no `reserved_layers`.**

## JSON schema (the contract)

**JSON is the API.** Human-readable text output is a courtesy; the JSON
schema below is the stable contract. Per the project's rules, **breaking
(renaming, removing, or retyping) a field is a breaking change**. New fields
may be added without breaking the contract, so consumers should ignore
unknown fields. See [`docs/json-contract.md`](../json-contract.md) for the
envelope shared across all `klt` commands (`schema_version`, error shape,
exit codes).

Unlike `klt drc`/`klt precheck` (which report bounding boxes in database
units), `klt socket-check` reports positions/bounding boxes in
**micrometres**, matching the descriptor's own coordinate convention.

```json
{
  "schema_version": 1,
  "file": "design.gds",
  "socket": "socket.json",
  "socket_name": "example_block",
  "dbu_um": 0.001,
  "status": "pass",
  "check_count": 3,
  "checks": [
    { "name": "pins", "status": "pass", "violation_count": 0, "violations": [], "skip_reason": null },
    { "name": "outline", "status": "pass", "violation_count": 0, "violations": [], "skip_reason": null },
    { "name": "reserved_layers", "status": "skipped", "violation_count": 0, "violations": [], "skip_reason": "socket descriptor declares no reserved layers" }
  ],
  "budgets": [
    { "name": "signal_r_max", "value": 500, "unit": "ohm", "notes": "worst-case corner", "status": "declared_unverified" }
  ]
}
```

### Top-level fields

| Field             | Type                | Description                                                                 |
| ----------------- | ------------------- | ---------------------------------------------------------------------------- |
| `schema_version`  | integer             | Version of this command's JSON shape (starts at `1`).                       |
| `file`            | string              | The input layout path exactly as provided on the command line.              |
| `socket`          | string              | The `--socket` descriptor path exactly as provided.                         |
| `socket_name`     | string \| null      | The descriptor's own `name` field, or `null`.                               |
| `dbu_um`          | number (float)      | The input layout's database unit in micrometres, same semantics as `klt layers`. |
| `status`          | `"pass"` \| `"fail"`| `"fail"` iff any check's own `status` is `"fail"`. A `"skipped"` check never causes an overall failure. `budgets` never affects this field. |
| `check_count`     | integer              | Always `3` today -- `len(checks)`.                                          |
| `checks`          | array\<object\>      | One entry per check, always in the order documented above, see below.       |
| `budgets`         | array\<object\>      | One entry per descriptor `budgets[]` item, see below. `[]` when the descriptor declares none. |

### `checks[]` entries

| Field             | Type                                | Description                                                             |
| ----------------- | ----------------------------------- | ---------------------------------------------------------------------- |
| `name`            | string                               | Check name (`"pins"`, `"outline"`, `"reserved_layers"`).                |
| `status`          | `"pass"` \| `"fail"` \| `"skipped"`  | `"skipped"` means the descriptor declared no input for this check.      |
| `violation_count` | integer                              | `len(violations)`.                                                     |
| `violations`      | array\<object\>                      | One entry per finding; shape is check-specific, see below. Always `[]` when `status` is `"pass"` or `"skipped"`. |
| `skip_reason`     | string \| null                       | Non-null only when `status` is `"skipped"`.                            |

#### `pins` violations

| Field      | Type            | Description                                                                 |
| ---------- | --------------- | ------------------------------------------------------------------------- |
| `pin`      | string          | The declared pin's `name`.                                                 |
| `reason`   | string          | `"missing"`, `"wrong_layer"`, or `"misplaced"` -- see [Checks](#checks) above. |
| `expected` | object          | `{"layer", "x_um", "y_um", "tolerance_um"}` -- the descriptor's declared values. |
| `actual`   | array\<object\> | `[]` for `"missing"`; otherwise every matching-name label found: `{"layer", "x_um", "y_um", "cell"}`, sorted by `(cell, layer, x_um, y_um)`. |

#### `outline` violations

| Field     | Type   | Description                                                                 |
| --------- | ------ | --------------------------------------------------------------------------- |
| `cell`    | string | Name of the cell the offending shape is *defined* in (not multiplied through instantiation). |
| `layer`   | string | `"<layer>/<datatype>"`.                                                     |
| `bbox_um` | object | `{"x0", "y0", "x1", "y1"}`, in micrometres, in absolute (top-cell) coordinates. |

#### `reserved_layers` violations

| Field    | Type          | Description                                                    |
| -------- | ------------- | ---------------------------------------------------------------- |
| `layer`  | string        | `"<layer>/<datatype>"` of the reserved layer that has shapes drawn on it. |
| `name`   | string \| null | The layer's own GDS layer-name property, or `null` (GDSII carries no layer-name field, so this is almost always `null` after a write/read round trip -- unlike a `.lyp` file). |
| `shapes` | integer       | Shape count on this layer, summed across all cell definitions.   |

### `budgets[]` entries

| Field    | Type           | Description                                                    |
| -------- | -------------- | ---------------------------------------------------------------- |
| `name`   | string          | The descriptor's declared budget name.                           |
| `value`  | number          | The descriptor's declared numeric value.                         |
| `unit`   | string          | The descriptor's declared unit string.                           |
| `notes`  | string \| null  | The descriptor's declared notes, or `null`.                       |
| `status` | string          | Always `"declared_unverified"` -- see [Scope](#scope-what-is-and-isnt-mechanically-checked) above.     |

## Exit codes

| Code | Meaning                                                     |
| ---- | ------------------------------------------------------------ |
| `0`  | Ran successfully -- every check passed or was skipped, none failed. |
| `1`  | Failed to run -- bad layout file, or a missing/unreadable/malformed `--socket` descriptor. |
| `2`  | Usage error (missing argument, bad `--format` value) -- from argparse. |
| `3`  | Ran successfully, at least one check failed.                 |

On error (exit `1`), a concise message is written to **stderr** and nothing
is written to stdout. No Python traceback is printed.

- `--format text` (default): a plain-text line prefixed `klt socket-check:`.
- `--format json`: the documented JSON error envelope (see
  [`docs/json-contract.md`](../json-contract.md)):

  ```json
  { "schema_version": 1, "error": { "command": "socket-check", "message": "socket descriptor not found: socket.json" } }
  ```
