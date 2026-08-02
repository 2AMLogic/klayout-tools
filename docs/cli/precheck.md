# `klt precheck`

Run a named battery of layout-*hygiene* checks against a GDSII or OASIS
layout stream and report each check's own pass/fail/skipped result as
structured data.

```
klt precheck <file> [--grid-um <float>] [--allowed-layers <json-or-path>]
             [--deck sky130|gf180mcu] [--format text|json]
```

- `<file>` — path to a GDSII (`.gds`) or OASIS (`.oas`) file. KLayout
  auto-detects the stream format on read; the extension is not authoritative.
- `--grid-um` — manufacturing grid in micrometres for the `offgrid` check
  (e.g. `0.005`). Omitted by default, which **skips** that check — see
  "Checks" below for why this repo does not guess a default.
- `--allowed-layers` — a path to a JSON file, or an inline JSON string, of
  `[layer, datatype]` pairs (e.g. `'[[65, 20], [66, 20]]'`) for the
  `layer_whitelist` check. Omitted by default, which **skips** that check.
- `--deck` — extraction deck (`sky130` or `gf180mcu`) to source label/drawing
  layer pairs from for the `pin_labels_over_drawing` check. Not validated by
  argparse — an unknown deck name exits `1` with a clean error. Omitted by
  default, which **skips** that check.
- `--format` — `text` (default, a human-readable summary) or `json`.

## Why a separate verb from `klt drc`

`klt drc` runs a curated width/space/enclosure **design-rule** deck — the
class of check a foundry's DRM defines per layer/spacing. `klt precheck` runs
a different class of check entirely: layout *hygiene* that sits outside any
design-rule deck — malformed geometry (off-grid vertices, zero-area
polygons), naming that breaks downstream tooling (cell names), and
labelling mistakes (a pin label that doesn't land on any drawn geometry). A
real layout can be 100% DRC-clean and still fail every one of these checks,
and vice versa — they are orthogonal, not overlapping, coverage. Modelled
after Tiny Tapeout's `precheck.py`, which runs the same class of check as an
ordered list of independently-named checks ahead of every submitted GDS.

Each check reports its own named result rather than one monolithic verdict,
so an agent can route a specific failure to a specific fix: a `cell_names`
failure routes to a rename, an `offgrid` failure routes to a snap-to-grid —
one flat "precheck failed" cannot be routed that way.

These checks are also **fast** relative to a full DRC deck (no
`Region.*_check()` polygon-processing pass), so they are suited to running on
every edit in an iterative design loop, reserving the full `klt drc` deck for
signoff.

## Engine

`klt precheck` runs fully headless via the pip `klayout` package's native
batch database API (`klayout.db`) — direct shape/text iteration and
`kdb.Region`/`kdb.Texts` set operations, no `Region.*_check()` design-rule
primitives. No dependency on the standalone `klayout` application binary.

## Checks

Every run reports exactly five checks, in this order, each with its own
`status` of `"pass"`, `"fail"`, or `"skipped"`:

### `offgrid`

Every polygon vertex, across every layer and every top cell — with instance
placement transforms applied, so a shape that is on-grid in its own cell's
local coordinates but placed at an off-grid absolute position by an array/
offset instance is still flagged — lands on a multiple of `--grid-um` in
both x and y.

**Skipped when `--grid-um` is omitted.** A layout's own `dbu_um` (the
database unit every GDSII/OASIS coordinate is already an integer multiple
of, by construction of the format) is *not* the same thing as the
manufacturing grid a real foundry flow enforces, which is typically coarser
-- and this repo curates no per-PDK manufacturing-grid table (unlike the DRC
decks' width/space thresholds, a manufacturing grid isn't transcribed here
as a discrete, stable rule id the way DRM checks are), so the grid is a
required, caller-supplied physical value rather than a guessed default.

### `zero_area`

Every polygon/box/path shape, across every layer and every top cell, has
nonzero area. Text label shapes are exempt (they carry no polygon at all —
see `pin_labels_over_drawing` for the check that validates labels). Always
runs; needs no extra input.

### `cell_names`

No cell anywhere in the layout's hierarchy (not just top cells) has a name
containing a character known to break this repo's downstream tooling:
filesystem-path-reserved characters (` / \ : * ? " < > | `), whitespace, or
`#` (the SPICE netlist comment marker — a cell name containing it silently
truncates every line that references it once written to a `.spice`
netlist by `klt extract`). This is a deliberately narrow, documented
deny-list, not a full GDSII cell-name legality check (the GDSII spec itself
is far more permissive). Always runs.

### `layer_whitelist`

Every `(layer, datatype)` pair present in the stream is a member of
`--allowed-layers`.

**Skipped when `--allowed-layers` is omitted.** This repo's per-PDK deck
layer tables (`src/klayout_tools/decks/sky130.py`,
`src/klayout_tools/decks/gf180mcu.py`) are documented **curated starter
subsets**, not full valid-layer enumerations — see
[`klt drc`'s "Coverage" section](drc.md#coverage). Reusing one as an implied
whitelist would misflag every legitimate PDK layer the curated deck simply
hasn't modelled yet (e.g. sky130's `met2`-`met5`) as "invalid," which is
actively misleading for a check whose whole point is to name genuinely
illegal layers. The caller supplies the real whitelist explicitly instead —
e.g. transcribed from the PDK's own `.lyp`/layer-map file for a given
project's actual layer usage.

### `pin_labels_over_drawing`

Every text label on a deck's known label layer
(`ExtractionDeck.well_label`/`poly_label`/`metal_labels`, see
`src/klayout_tools/decks/__init__.py`) overlaps a drawn shape on that
label's paired drawing layer (`nwell`/`poly`/the matching `metals[]` entry
respectively) — i.e. it names something that is actually drawn, not a
dangling label floating over empty layout.

**Skipped when `--deck` is omitted** (no label/drawing layer pairing to
check against), **or when the resolved deck declares no label layers at
all** (neither currently-registered deck hits this — it's a defensive skip
for a hypothetical future deck).

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
  "file": "design.gds",
  "dbu_um": 0.001,
  "status": "fail",
  "check_count": 5,
  "checks": [
    { "name": "offgrid", "status": "skipped", "violation_count": 0, "violations": [], "skip_reason": "no --grid-um given" },
    { "name": "zero_area", "status": "pass", "violation_count": 0, "violations": [], "skip_reason": null },
    {
      "name": "cell_names",
      "status": "fail",
      "violation_count": 1,
      "violations": [
        { "cell": "TOP#bad", "reason": "cell name contains forbidden character(s): '#'" }
      ],
      "skip_reason": null
    },
    { "name": "layer_whitelist", "status": "skipped", "violation_count": 0, "violations": [], "skip_reason": "no --allowed-layers given" },
    { "name": "pin_labels_over_drawing", "status": "skipped", "violation_count": 0, "violations": [], "skip_reason": "no --deck given" }
  ]
}
```

### Top-level fields

| Field            | Type             | Description                                                                 |
| ---------------- | ---------------- | ----------------------------------------------------------------------------- |
| `schema_version` | integer          | Version of this command's JSON shape (starts at `1`).                       |
| `file`           | string           | The input path exactly as provided on the command line.                     |
| `dbu_um`         | number (float)   | The input layout's database unit in micrometres, same semantics as `klt layers`. |
| `status`         | `"pass"` \| `"fail"` | `"fail"` iff any check's own `status` is `"fail"`. A `"skipped"` check never causes an overall failure. |
| `check_count`    | integer          | Always `5` today — `len(checks)`.                                            |
| `checks`         | array\<object\>  | One entry per check, always in the order documented above, see below.       |

### `checks[]` entries

| Field             | Type                          | Description                                                             |
| ----------------- | ----------------------------- | ---------------------------------------------------------------------- |
| `name`            | string                        | Check name (`"offgrid"`, `"zero_area"`, `"cell_names"`, `"layer_whitelist"`, `"pin_labels_over_drawing"`). |
| `status`          | `"pass"` \| `"fail"` \| `"skipped"` | `"skipped"` means the check's optional input wasn't given — see "Checks" above for each check's skip condition. |
| `violation_count` | integer                       | `len(violations)`.                                                     |
| `violations`      | array\<object\>                | One entry per finding; shape is check-specific, see below. Always `[]` when `status` is `"pass"` or `"skipped"`. |
| `skip_reason`     | string \| null                 | Non-null only when `status` is `"skipped"`.                            |

`violations` entries are sorted for deterministic, diff-clean output (by
cell/layer/position, ascending — the exact key differs per check but is
stable across runs of the same input).

#### `offgrid` / `zero_area` violations

| Field     | Type                   | Description                                                        |
| --------- | ---------------------- | -------------------------------------------------------------------- |
| `cell`    | string                 | Name of the cell the offending shape is *defined* in (not multiplied through instantiation). |
| `layer`   | string                 | `"<layer>/<datatype>"`.                                              |
| `bbox`    | object (dbu ints)      | `{"left", "bottom", "right", "top"}`, in database units, in absolute (top-cell) coordinates. |
| `polygon` | array\<[x, y]\>        | Vertices in database units, absolute (top-cell) coordinates.        |

#### `cell_names` violations

| Field    | Type   | Description                                                    |
| -------- | ------ | ---------------------------------------------------------------- |
| `cell`   | string | The offending cell's name.                                       |
| `reason` | string | Human-readable description naming the forbidden character(s) found. |

#### `layer_whitelist` violations

| Field    | Type          | Description                                                    |
| -------- | ------------- | ---------------------------------------------------------------- |
| `layer`  | string        | `"<layer>/<datatype>"` of the layer not in `--allowed-layers`.   |
| `name`   | string \| null | The layer's own GDS layer-name property, or `null` if unnamed.   |
| `shapes` | integer       | Shape count on this layer, summed across all cell definitions.   |

#### `pin_labels_over_drawing` violations

| Field      | Type   | Description                                                    |
| ---------- | ------ | ---------------------------------------------------------------- |
| `cell`     | string | Name of the top cell the stray label was found under.            |
| `layer`    | string | `"<layer>/<datatype>"` of the label layer.                       |
| `text`     | string | The label's text string.                                         |
| `position` | object | `{"x", "y"}`, in database units, absolute (top-cell) coordinates. |

## Exit codes

| Code | Meaning                                                     |
| ---- | ------------------------------------------------------------ |
| `0`  | Ran successfully — every check passed or was skipped, none failed. |
| `1`  | Failed to run — bad file, unknown `--deck`, or a bad `--grid-um`/`--allowed-layers` value. |
| `2`  | Usage error (missing argument, bad `--format` value) — from argparse. |
| `3`  | Ran successfully, at least one check failed.                 |

On error (exit `1`), a concise message is written to **stderr** and nothing
is written to stdout. No Python traceback is printed.

- `--format text` (default): a plain-text line prefixed `klt precheck:`.
- `--format json`: the documented JSON error envelope (see
  [`docs/json-contract.md`](../json-contract.md)):

  ```json
  { "schema_version": 1, "error": { "command": "precheck", "message": "unknown deck 'nope' (available: gf180mcu, sky130)" } }
  ```
