# `klt gen-compose`

Place a set of already-generated [`klt gen`](gen.md) blocks into one composed
GDS/OASIS cell — phase 1 of Epic #191, the build carried by the accepted
spike,
[`docs/design/gen-composition-spike.md`](../design/gen-composition-spike.md)
(section 2 for the contract, section 5 for the phased scope proposal). This
document is the shipped contract; where the two disagree, this document (and
the code) win.

```
klt gen-compose <request.json> [--format text|json]
```

Like `klt lvs`/`klt sim`, `klt gen-compose` takes a **request document**, not
positional block file args — it binds an arbitrary number of blocks plus
placement/connectivity/routing/options, richer than a flag line carries
cleanly.

- `<request.json>` — path to a request document (see "Request" below).
- `--format` — `text` (default, a human-readable summary) or `json`.

## Scope (phase 1)

- `placement.strategy: "row"` only — a single horizontal row, left to right
  in caller-declared `placement.order`. `"grid"` is spike-scoped for a later
  phase.
- `connectivity[]` is **validated** (every referenced block `id`/port must
  exist) but **not routed** — no metal is drawn between nets. `routing` is
  accepted in the request and otherwise ignored. `nets[]`/`unrouted_nets[]`
  are reserved, empty placeholders in the response so a future routing phase
  does not have to change the top-level envelope.
- `drc_hints.matched_groups[]` reporting (reading back
  `generator_report.drc_hints.matched_group_id`) is out of scope this phase
  — `drc_hints` is a reserved, empty placeholder.

## CLI shape (a Builder decision, per the spike's own flag)

The spike's contract section names `klt gen compose` as a working name only
("not a commitment to that exact CLI shape"), and explicitly leaves the
nested-subcommand-vs-new-verb call to whoever implements it.
[`klt gen`](gen.md)'s own `gen_parser` (`src/klayout_tools/cli/parser.py`)
takes a flat positional `<generator>` argument, not subparsers — restructuring
it into nested subparsers (`klt gen <subcommand>`) so `compose` could sit
alongside `<generator>` names would require argparse to disambiguate a
literal subcommand token (`compose`) from an arbitrary caller-chosen
generator name in the same positional slot, which argparse's own subparser
mechanism doesn't support without a larger, backward-incompatible rewrite of
`klt gen`'s existing CLI surface (see `test_gen.py`'s `klt gen <generator>`
callers).

This phase therefore implements `klt gen-compose` as a **distinct top-level
verb** (`gen_compose_cmd.py`, registered next to `gen_cmd.py` in `parser.py`),
not a `gen` sub-subcommand — the same request-document CLI shape
`klt sim`/`klt lvs` already use, and reversible: nothing prevents a later
phase from also exposing `klt gen compose` as an alias if a real need
surfaces.

## Engine

Runs fully headless via KLayout's native `pya` (`klayout.db`) — no GUI, no
Qt. Each block's own GDS/OASIS stream (`generator_report.gds_path`) is read
into a scratch `kdb.Layout`, its reported top cell
(`generator_report.cell_name`) is duplicated (`kdb.Cell.copy_tree`) into a
fresh sub-cell of the composed layout, and that sub-cell is instantiated
into the composed top cell at the block's computed `offset_um` — geometry is
copied exactly once (never re-derived from the GDS a second time), and each
block stays its own cell in the output hierarchy (not flattened into the
composed top cell).

**A block's `bbox_um`/`ports[]` are consumed exactly as its own
`generator_report` reported them** — this command never re-derives a
block's placement math from its GDS stream (the spike's "one new guarantee
specific to composition," section 2). Every block referenced by
`blocks[].generator_report` must share the same `dbu` (design-rule grid
resolution) — every `klt gen` generator uses `0.001` (see
[`docs/cli/gen.md`](gen.md)), so this only matters for a hand-crafted
`generator_report` with a different `dbu`; a mismatch is an application
error (exit 1).

## JSON schema (the contract)

**JSON is the API.** Human-readable text output is a courtesy; the JSON
schema below is the stable contract. Per the project's rules, **breaking**
(renaming, removing, or retyping) a field is a breaking change. New fields
may be added without breaking the contract, so consumers should ignore
unknown fields. See [`docs/json-contract.md`](../json-contract.md) for the
envelope shared across all `klt` commands (`schema_version`, error shape,
exit codes).

### Request

```json
{
  "schema": "klt.gen_compose.request/1",
  "pdk": { "variant": "sky130A", "root": null },
  "blocks": [
    { "id": "diffpair", "generator_report": "diffpair.json" },
    { "id": "mirror", "generator_report": "mirror.json" },
    { "id": "tail", "generator_report": "tail.json" }
  ],
  "placement": {
    "strategy": "row",
    "order": ["diffpair", "mirror", "tail"],
    "spacing_um": 1.0
  },
  "connectivity": [
    {
      "net": "VOUT",
      "pins": [
        { "block": "diffpair", "port": "Q1_1_D" },
        { "block": "mirror", "port": "M1_1_D" }
      ]
    }
  ],
  "routing": { "layer_role": "metal", "width_um": 0.17 },
  "options": { "cell_name": "ota_top_0", "output": "ota_top_0.gds" }
}
```

| Field | Type | Description |
| ----- | ---- | ----------- |
| `schema` | string | Contract identifier and major version. |
| `pdk.variant`/`pdk.root` | string \| null | The exact fields `klt pdk find --pdk`/`--pdk-root` accept ([`docs/cli/pdk.md`](pdk.md)) — resolved through that one resolver, never a private lookup. |
| `blocks[]` | array\<object\> | Each already-generated primitive to place — see below. |
| `blocks[].id` | string | Caller-chosen label used to address the block's ports elsewhere in this request (`placement.order`, `connectivity[].pins[].block`). Must be unique within `blocks[]`. |
| `blocks[].generator_report` | object \| string | The block's own [`klt gen`](gen.md) JSON response — either an inline object, or a path to a file holding one (mirrors `klt gen --params`'s own path-or-inline duality). This command's only input about a block's geometry is its already-reported `bbox_um`/`ports[]`/`cell_name`/`gds_path` — never a second, private inspection of the GDS stream at request-parse time. |
| `placement.strategy` | string | `"row"` (single horizontal row, left to right in `order`) — the only strategy implemented this phase. Any other value is an application error (exit 1). |
| `placement.order` | array\<string\> | Block `id`s in placement order. Every `id` in `blocks[]` must appear exactly once — a missing or extra/unknown `id` is an application error. |
| `placement.spacing_um` | number | Fixed gap between adjacent blocks' bounding boxes. Must be `>= 0`. |
| `connectivity[]` | array\<object\> | One entry per net: a `net` label (caller-chosen, response traceability only) and `pins[]` (at least 2), each `{block, port}` addressing one named port from that block's own `generator_report.ports[]`. **Validated, not routed** this phase — see "Scope" above. A `pins[].block`/`pins[].port` referencing a nonexistent block `id` or port name is an application error (exit 1). |
| `routing.layer_role`/`routing.width_um` | string / number | Accepted and echoed nowhere in the response this phase — reserved for phase 2's point-to-point routing. Not validated beyond "must be a JSON object" if present. |
| `options.cell_name`/`options.output` | string | Same semantics as `klt gen`'s own `options` fields — see [`docs/cli/gen.md`](gen.md). `cell_name` defaults to `"gen_compose_0"`; `output` defaults to `"<cell_name>.gds"`. |

### Response

```json
{
  "schema_version": 1,
  "cell_name": "ota_top_0",
  "gds_path": "ota_top_0.gds",
  "pdk": { "name": "sky130A", "variant": "sky130A", "version": "open_pdks 0fe599b" },
  "bbox_um": { "x0": -0.92, "y0": -0.92, "x1": 14.2, "y1": 2.16 },
  "blocks": [
    {
      "id": "diffpair",
      "generator": "diff_pair",
      "offset_um": { "x": 0.0, "y": 0.0 },
      "bbox_um": { "x0": -0.92, "y0": -0.92, "x1": 3.56, "y1": 2.16 }
    }
  ],
  "nets": [],
  "unrouted_nets": [],
  "drc_hints": { "min_spacing_um": null, "matched_groups": [], "notes": [] },
  "warnings": [
    "connectivity[] was validated but not routed -- routing is not implemented until phase 2 (see docs/design/gen-composition-spike.md)"
  ]
}
```

#### Top-level fields

| Field | Type | Description |
| ----- | ---- | ----------- |
| `schema_version` | integer | Version of this command's JSON shape (starts at `1`). |
| `cell_name` | string | Name of the top cell written into `gds_path`, containing every placed block's cell as a translated sub-cell instance. |
| `gds_path` | string | Resolved output path (echoes `options.output`, or the computed default). |
| `pdk` | object | The resolved PDK reference, echoing `klt pdk find`'s own `variant`/`version` fields. |
| `bbox_um` | object | Bounding box of the *composed* cell — the union of every placed block's own `bbox_um`, translated by its `offset_um` (computed arithmetically from each block's reported `bbox_um`, never re-derived from drawn geometry). |
| `blocks[]` | array\<object\> | Per-block placement result — see below. |
| `nets[]` | array\<object\> | **Reserved, always empty this phase.** Phase 2 populates one entry per `connectivity[]` net with routing results. |
| `unrouted_nets[]` | array\<string\> | **Reserved, always empty this phase** (routing never runs, so nothing is reported as unrouted rather than everything). Phase 2 populates net labels that could not be routed. |
| `drc_hints` | object | **Reserved, placeholder values this phase**: `min_spacing_um: null`, `matched_groups: []`, `notes: []`. Advisory only, same "not authoritative" semantics as `klt gen`'s own `drc_hints` once populated — `klt drc` remains the actual authority on rule compliance. |
| `warnings[]` | array\<string\> | Non-fatal notes. Includes a note that `connectivity[]` was validated but not routed whenever the request supplied a non-empty `connectivity[]`. Always present, empty when there is nothing to report. |

#### `blocks[]` entries

| Field | Type | Description |
| ----- | ---- | ----------- |
| `id` | string | Echo of the request's `blocks[].id`. |
| `generator` | string | Echoed from that block's own `generator_report.generator`. |
| `offset_um` | object | `{x, y}` — the translation applied to place this block. The first block in `placement.order` always has `offset_um: {x: 0.0, y: 0.0}`; every subsequent block is translated along `x` only (row placement never translates `y`) so its bbox sits exactly `placement.spacing_um` past the previous (already translated) block's right edge — regardless of that block's own `bbox_um.x0` (which need not be `0`; a guard-ringed block's bbox can extend to negative coordinates). |
| `bbox_um` | object | That block's own `generator_report.bbox_um`, translated by `offset_um`, in the composed cell's coordinate frame. |

### Semantics and guarantees

Same guarantees as `klt gen` itself and the spike's proposed contract
(section 2, "Semantics and guarantees"): the contract is engine-neutral
(nothing names `pya`/`klayout.db` in the JSON shape), layer resolution for
future routing will go through the one per-PDK role-layer table every
generator already uses, `drc_hints` is advisory not authoritative, PDK
resolution goes through the one resolver, and the envelope is additive — new
fields may be added without a schema/`schema_version` bump; renaming,
removing, or retyping an existing field requires one.

**One new guarantee specific to composition:** a block's `bbox_um`/`ports[]`
are consumed exactly as its own `generator_report` reported them — this
command never re-derives a block's placement math from its GDS stream (see
"Engine" above).

## Text format

The default `text` format prints a short summary. It is intended for human
eyes and its exact layout is **not** part of the contract — parse the JSON
instead.

```
$ klt gen-compose request.json
cell_name: ota_top_0
gds_path: ota_top_0.gds
pdk: sky130A (open_pdks 0fe599b)
bbox_um: (-0.92, -0.92) - (14.2, 2.16)

blocks:
  diffpair (diff_pair)  offset=(0.0, 0.0)  bbox=(-0.92, -0.92) - (3.56, 2.16)
  mirror (diff_pair)  offset=(5.48, 0.0)  bbox=(4.56, -0.92) - (9.04, 2.16)
  tail (mos_array)  offset=(11.56, 0.0)  bbox=(10.04, 0.0) - (14.2, 0.42)

warnings:
  connectivity[] was validated but not routed -- routing is not implemented until phase 2 (see docs/design/gen-composition-spike.md)
```

## Exit codes and errors

| Exit code | Meaning |
| --------- | ------- |
| `0` | Composition succeeded; `gds_path` was written and the report above is on stdout. |
| `1` | Application error — unresolvable PDK, malformed request (missing/invalid `blocks[]`, `placement.order` not matching `blocks[]`, negative `spacing_um`), an unsupported `placement.strategy`, a `connectivity[]` entry referencing a nonexistent block `id`/port, a block's `generator_report`/GDS could not be read, or the `options.output` directory does not exist. |
| `2` | Usage error — missing `<request.json>` argument, or a bad `--format` value (from argparse). |

No third "partial success" code (unlike the spike's own proposed `3` for a
non-empty `unrouted_nets[]`) is defined at this phase — routing does not run
yet, so there is nothing to report as partially unrouted. A future routing
phase may introduce it once `unrouted_nets[]` is actually populated (see the
spike's "Proposed exit codes" section).

On error, a concise message is written to **stderr** and nothing is written
to stdout (and no GDS/OASIS file is written). No Python traceback is
printed.

- `--format text` (default): a plain-text line prefixed `klt gen-compose:`.
- `--format json`: the documented JSON error envelope (see
  [`docs/json-contract.md`](../json-contract.md)):

  ```json
  { "schema_version": 1, "error": { "command": "gen-compose", "message": "request.connectivity[0] (net 'VOUT') references unknown port 'NOPE' on block 'diffpair' -- available: Q1_1_D, Q1_1_G, Q1_1_S, ..." } }
  ```

## Worked example

```bash
# Generate three real blocks with `klt gen` (a differential pair, a
# current-mirror-labelled load, and a single-device tail current source --
# the #164 5T OTA case the composition spike's section 4 worked through):
$ klt gen diff_pair --params '{"mirror": false}' --pdk sky130A \
    -o diffpair.gds --format json > diffpair.json
$ klt gen diff_pair --params '{"mirror": true}' --pdk sky130A \
    -o mirror.gds --format json > mirror.json
$ klt gen mos_array --params '{"rows": 1, "cols": 1}' --pdk sky130A \
    -o tail.gds --format json > tail.json

# Compose them into one row-placed cell:
$ cat > compose_request.json <<'EOF'
{
  "pdk": { "variant": "sky130A" },
  "blocks": [
    { "id": "diffpair", "generator_report": "diffpair.json" },
    { "id": "mirror", "generator_report": "mirror.json" },
    { "id": "tail", "generator_report": "tail.json" }
  ],
  "placement": { "strategy": "row", "order": ["diffpair", "mirror", "tail"], "spacing_um": 1.0 },
  "connectivity": [
    { "net": "VOUT", "pins": [{ "block": "diffpair", "port": "Q1_1_D" }, { "block": "mirror", "port": "M1_1_D" }] }
  ],
  "options": { "cell_name": "ota_top_0", "output": "ota_top_0.gds" }
}
EOF
$ klt gen-compose compose_request.json --format json
```
