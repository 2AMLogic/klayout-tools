# `klt gen`

Run a named layout generator against a JSON `params` object and a PDK
reference, producing a GDS/OASIS stream plus a structured report. This is
phase 1 of Epic #152 — the request/response contract and a thin
[KLayout PCell](https://www.klayout.de/doc/programming/pcell.html) harness,
proven end-to-end with one reference generator (`resistor_strip`). See
[`docs/design/layout-generator-spike.md`](../design/layout-generator-spike.md)
section 2 for the contract this command implements.

```
klt gen --list [--format text|json]
klt gen <generator> [--params <path-or-inline>] [--pdk <variant>]
                     [--pdk-root <dir>] [--cell-name <name>] [-o/--output <path>]
                     [--format text|json]
```

- `--list` — enumerate available generators and their `params` schema, then exit.
- `<generator>` — which generator to run (e.g. `resistor_strip`).
- `--params` — either a path to a JSON file, or an inline JSON object (e.g.
  `--params '{"num": 8}'`). Omit to use every parameter's default. A value
  that resolves to an existing file path is read as a file; otherwise it is
  parsed as inline JSON.
- `--pdk`/`--pdk-root` — the **same** flags `klt pdk find` accepts
  ([`docs/cli/pdk.md`](pdk.md)); `klt gen` resolves its PDK reference through
  that one resolver, never a private lookup. Omitting both falls back to
  `$PDK`/`$PDK_ROOT`, same as `klt pdk find` with no flags.
- `--cell-name` — name for the generated top cell (default: `<generator>_0`).
- `-o`/`--output` — output GDS/OASIS path (default: `<cell_name>.gds`, written
  to the current directory). Format (`.gds`/`.oas`) is inferred from the
  extension, matching `klt render`'s auto-detection posture. The containing
  directory must already exist.
- `--format` — `text` (default, a human-readable summary) or `json`.

The command runs fully headless via KLayout's native
`pya.PCellDeclarationHelper` — a PCell parameter-declaration + `produce_impl()`
class, the same substrate KLayout's own GUI PCell panel uses, invoked here
through `Layout.add_pcell_variant()` with no GUI, no Qt, and no PCell library
GUI panel involved — safe to run in CI.

## The reference generator: `resistor_strip`

The one generator implemented at phase 1: a row of parametrized rectangles
standing in for a unit-resistor string
([spike section 4.2](../design/layout-generator-spike.md#4-scope-proposal-first-generators)'s
family). It exists to prove the request → PCell → response loop end to end —
it has **no** well/tap/contact logic and is **not** claimed to be DRC-clean on
any PDK. Phase 2 (tracked under Epic #152) replaces it with a real
resistor-array generator, alongside the other three analog primitive
families the spike scopes (matched MOS arrays, cap arrays, guard rings,
diff pair/current mirror).

| `params` field | Type   | Default | Description                          |
| --------------- | ------ | ------- | ------------------------------------- |
| `length_um`      | double | `2.0`   | Unit resistor length (µm). Must be `> 0`. |
| `width_um`       | double | `0.42`  | Unit resistor width (µm). Must be `> 0`.  |
| `spacing_um`     | double | `0.42`  | Spacing between unit resistors (µm). Must be `>= 0`. |
| `num`            | int    | `4`     | Number of unit resistors. Must be `>= 1`. |

`klt gen --list` reports this same table as structured data (see below) for
every registered generator — the request-facing parameter schema is never
hand-maintained in two places.

## JSON schema (the contract)

**JSON is the API.** Human-readable text output is a courtesy; the JSON
schema below is the stable contract. Per the project's rules, **breaking
(renaming, removing, or retyping) a field is a breaking change**. New fields
may be added without breaking the contract, so consumers should ignore
unknown fields. See [`docs/json-contract.md`](../json-contract.md) for the
envelope shared across all `klt` commands (`schema_version`, error shape,
exit codes).

### Request

`klt gen` assembles a `klt.gen.request/1` request internally from its CLI
flags (there is no separate request-file flag at phase 1 — every field below
maps directly onto a flag):

```json
{
  "schema": "klt.gen.request/1",
  "generator": "resistor_strip",
  "pdk": { "variant": "sky130A", "root": null },
  "params": { "length_um": 2.0, "width_um": 0.42, "spacing_um": 0.42, "num": 4 },
  "options": { "cell_name": "res_strip_0", "output": "res_strip_0.gds" }
}
```

| Field | Type | Description |
| ----- | ---- | ----------- |
| `schema` | string | Contract identifier and major version. |
| `generator` | string | Which generator to run — the CLI's `<generator>` positional. |
| `pdk.variant`/`pdk.root` | string \| null | The exact fields `klt pdk find --pdk`/`--pdk-root` accept — passed straight through to `find_pdk()`. |
| `params` | object | Generator-specific parameter set — see the per-generator table above (or `klt gen --list`). |
| `options.cell_name` | string | Name for the generated top cell. Defaults to `<generator>_0` if omitted. |
| `options.output` | string | Path to write the GDS/OASIS stream. Defaults to `<cell_name>.gds`. |

**Deviation from the spike:** the spike's example request carries a
`pdk.name`/`pdk.variant` pair distinguishing a PDK family (`"sky130"`) from a
specific install variant (`"sky130A"`). `klt pdk find`'s resolver
([`klayout_tools.pdk.find_pdk`](../../src/klayout_tools/pdk.py)) has no family
concept — it resolves a single `variant` string against an install root — so
this command keeps that one-resolver contract instead of inventing a
family/variant split the resolver doesn't have. The response's
`pdk.name`/`pdk.variant` (below) both echo the *resolved* variant.

### Response

```json
{
  "schema_version": 1,
  "generator": "resistor_strip",
  "cell_name": "res_strip_0",
  "gds_path": "res_strip_0.gds",
  "pdk": { "name": "sky130A", "variant": "sky130A", "version": "open_pdks 0fe599b" },
  "bbox_um": { "x0": 0.0, "y0": 0.0, "x1": 9.68, "y1": 0.42 },
  "device_count": 4,
  "ports": [
    {
      "name": "P1",
      "net": null,
      "layer": { "layer": 67, "datatype": 20, "name": null },
      "x_um": 0.0,
      "y_um": 0.21,
      "width_um": 0.42,
      "direction_deg": 180
    },
    {
      "name": "P2",
      "net": null,
      "layer": { "layer": 67, "datatype": 20, "name": null },
      "x_um": 9.68,
      "y_um": 0.21,
      "width_um": 0.42,
      "direction_deg": 0
    }
  ],
  "drc_hints": {
    "min_spacing_um": 0.42,
    "matched_group_id": null,
    "snapped_to_grid": false,
    "notes": []
  },
  "warnings": []
}
```

#### Top-level fields

| Field | Type | Description |
| ----- | ---- | ----------- |
| `schema_version` | integer | Version of this command's JSON shape (starts at `1`; per-command). |
| `generator` | string | Echo of the request's `generator`. |
| `cell_name` | string | Name of the top cell written into `gds_path`. |
| `gds_path` | string | Resolved output path (echoes `options.output`, or the computed default). |
| `pdk` | object | The resolved PDK reference, echoing `klt pdk find`'s own `variant`/`version` fields — see the request section's deviation note. |
| `bbox_um` | object | Bounding box of the generated cell in micrometres — `_um` suffix per this repo's units-in-field-name convention (`dbu_um` in `klt layers`). |
| `device_count` | integer | Number of unit instances placed (`resistor_strip`'s `num`). |
| `ports` | array\<object\> | Named terminals for downstream connection — see below. |
| `drc_hints` | object | DRC-relevant metadata the generator itself already knows — see below. Advisory only; `klt drc` remains the actual authority on rule compliance. |
| `warnings` | array\<string\> | Non-fatal generator notes (e.g. a requested dimension was snapped to the technology grid). Always present, empty when there is nothing to report. |

#### `ports[]` entries

| Field | Type | Description |
| ----- | ---- | ----------- |
| `name` | string | Stable port/pin name (`P1`/`P2` for `resistor_strip` — the two ends of the strip). |
| `net` | string \| null | Caller-supplied net label; always `null` at phase 1 (no request field feeds it yet). |
| `layer` | object | `{ layer, datatype, name }` — the same triple `klt layers` reports. `name` is `null` at phase 1 (no per-PDK layer-name lookup is wired up yet; see the module docstring in `src/klayout_tools/gen.py`). |
| `x_um`/`y_um` | number | Port location in micrometres, relative to the cell origin. |
| `width_um` | number | Port width. |
| `direction_deg` | number | Outward-facing direction in degrees (`0`/`90`/`180`/`270`). |

#### `drc_hints` fields

| Field | Type | Description |
| ----- | ---- | ----------- |
| `min_spacing_um` | number | The tightest design-rule spacing the generator actually used (`resistor_strip`'s `spacing_um`). |
| `matched_group_id` | string \| null | Identifier tying together instances that must remain matched. Always `null` at phase 1 — `resistor_strip` has no matching concept; a future array generator (phase 2) is expected to set it. |
| `snapped_to_grid` | boolean | Whether any requested dimension was rounded to the technology grid (`true`) or used exactly as given (`false`). |
| `notes` | array\<string\> | Free-form, generator-specific DRC-adjacent notes. Always present, empty when there is nothing to report. |

### Semantics and guarantees

Same guarantees as every generator per the spike (section 2 "Semantics and
guarantees"): the contract is engine-neutral (nothing names `pya` or
`PCellDeclarationHelper`), `ports[].layer` reuses `klt layers`' own numbering,
`drc_hints` is advisory not authoritative, PDK resolution goes through the one
resolver, and the envelope is additive — new fields may be added without a
schema/`schema_version` bump; renaming, removing, or retyping an existing
field requires one.

## `klt gen --list`

Enumerates every registered generator and its `params` schema — the same data
[the per-generator table above](#the-reference-generator-resistor_strip)
documents by hand for `resistor_strip`:

```json
{
  "schema_version": 1,
  "generators": [
    {
      "name": "resistor_strip",
      "summary": "Row of parametrized rectangles standing in for a unit-resistor string -- ...",
      "params": [
        { "name": "length_um", "type": "double", "default": 2.0, "description": "Unit resistor length (um)" },
        { "name": "width_um", "type": "double", "default": 0.42, "description": "Unit resistor width (um)" },
        { "name": "spacing_um", "type": "double", "default": 0.42, "description": "Spacing between unit resistors (um)" },
        { "name": "num", "type": "int", "default": 4, "description": "Number of unit resistors" }
      ]
    }
  ]
}
```

`params[].type` is one of `int`, `double`, `string`, `bool` (the KLayout PCell
parameter types this phase's harness supports). Implementation-only
parameters a generator's PCell declares (e.g. `resistor_strip`'s drawing
layer) are never listed — `params` documents exactly the fields a request's
`params` object may set.

## Text format

The default `text` format prints a short summary. It is intended for human
eyes and its exact layout is **not** part of the contract — parse the JSON
instead.

```
$ klt gen resistor_strip --pdk sky130A -o res_strip_0.gds
generator: resistor_strip
cell_name: resistor_strip_0
gds_path: res_strip_0.gds
pdk: sky130A (open_pdks 0fe599b)
bbox_um: (0.0, 0.0) - (9.68, 0.42)
device_count: 4

ports:
  P1  x=0.0  y=0.21  width=0.42  dir=180
  P2  x=9.68  y=0.21  width=0.42  dir=0
```

## Exit codes and errors

| Exit code | Meaning |
| --------- | ------- |
| `0` | Generation succeeded (or `--list` succeeded); `gds_path` was written and the report above is on stdout. |
| `1` | Application error — unknown generator name, unresolvable PDK, invalid/out-of-range `params`, or the `options.output` directory does not exist. |
| `2` | Usage error — no generator name given and `--list` not passed, or a bad `--format` value (from argparse or this command's own usage check). |

No third "partial success" code is defined at phase 1, unlike `klt drc`'s
`3` — a generator either produces a cell or it doesn't (see the spike's
"Proposed exit codes" section, which flags this as an open question for a
future phase if a generator family ever needs one).

On error, a concise message is written to **stderr** and nothing is written
to stdout (and no GDS/OASIS file is written). No Python traceback is printed.

- `--format text` (default): a plain-text line prefixed `klt gen:`.
- `--format json`: the documented JSON error envelope (see
  [`docs/json-contract.md`](../json-contract.md)):

  ```json
  { "schema_version": 1, "error": { "command": "gen", "message": "unknown generator 'bogus' -- available: resistor_strip (see `klt gen --list`)" } }
  ```

## Worked example

```bash
# What generators are available, and what do they take?
$ klt gen --list

# Generate a 3-unit resistor strip against an installed sky130A PDK:
$ klt gen resistor_strip --params '{"num": 3, "length_um": 1.5}' \
    --pdk sky130A -o output/res_strip.gds --format json
```
