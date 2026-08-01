# `klt gen-compose`

Place a set of already-generated [`klt gen`](gen.md) blocks into one composed
GDS/OASIS cell and route two-pin nets between their named ports — phases 1–2
of Epic #191, the build carried by the accepted spike,
[`docs/design/gen-composition-spike.md`](../design/gen-composition-spike.md)
(section 2 for the contract, section 3 for the build-native-not-wrap routing
decision, section 5 for the phased scope proposal). This document is the
shipped contract; where the two disagree, this document (and the code) win.

```
klt gen-compose <request.json> [--format text|json]
```

Like `klt lvs`/`klt sim`, `klt gen-compose` takes a **request document**, not
positional block file args — it binds an arbitrary number of blocks plus
placement/connectivity/routing/options, richer than a flag line carries
cleanly.

- `<request.json>` — path to a request document (see "Request" below).
- `--format` — `text` (default, a human-readable summary) or `json`.

## Scope (phases 1–2)

- **Placement** — `placement.strategy: "row"` only: a single horizontal row,
  left to right in caller-declared `placement.order`. `"grid"` is spike-scoped
  for a later phase.
- **Routing** — two-pin, point-to-point Manhattan routing between the named
  ports listed in `connectivity[]`. Each 2-pin net is drawn as a native
  `pya.Path` (backbone → corner bends → straight fill) on the resolved
  `routing.layer_role` layer at `routing.width_um` width; `nets[]` reports
  `routed` and `route_length_um` per net. A net the router cannot connect is
  reported in `unrouted_nets[]` (a **partial success**, exit code `3`), not a
  hard failure. **Bundle (>2-pin) routing is out of scope this phase** — a net
  with more than two pins is left unrouted (reported in `unrouted_nets[]` with
  an explanatory `drc_hints.notes[]` entry), not rejected.
- **`drc_hints`** — `matched_groups[]` reports every distinct
  `matched_group_id` seen among the input blocks (read-only echo of
  `generator_report.drc_hints.matched_group_id`, `placement_symmetric: null` —
  symmetry *verification* is out of scope this phase); `min_spacing_um` reports
  the tightest spacing actually used across placement and routing.
- **Geometry is advisory.** A routed net (`routed: true`) is *not* a DRC-clean
  guarantee — `klt drc` remains the rule-compliance authority on the composed
  output, exactly as it is on any single generator's output.

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

Routed metal is built **natively** against `pya.Path` (the spike's
build-not-wrap decision, section 3) — not via a runtime dependency on any
external router. For each 2-pin net, the two ports' positions are resolved
into the composed coordinate frame (each port's own reported `x_um`/`y_um`
translated by its block's `offset_um`), a Manhattan backbone is generated
(leave each port along its outward `direction_deg`, then join the stubs with
right-angle-only segments — a single jog for same-axis ports, a single corner
for mixed-axis ports), and the resulting waypoint list is drawn as one
`pya.Path` on the resolved routing layer, on the composed **top** cell (not
inside any block's sub-cell). A `pya.Path` renders each corner as a square
miter that fully fills the bend, so no separate bend-insertion pass is needed.

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
| `connectivity[]` | array\<object\> | One entry per net: a `net` label (caller-chosen, response traceability only) and `pins[]` (at least 2), each `{block, port}` addressing one named port from that block's own `generator_report.ports[]`. A **2-pin** net is routed point-to-point (see "Scope"); a **>2-pin** (bundle) net is left unrouted this phase. A `pins[].block`/`pins[].port` referencing a nonexistent block `id` or port name is an application error (exit 1). |
| `routing.layer_role` | string | A layer *role* (e.g. `"metal"`) resolved through the **same** per-PDK-family role→layer table every [`klt gen`](gen.md) generator uses — never a raw `{layer, datatype}` pair. **Required** (and must name a role the resolved PDK family actually has a layer for) when `connectivity[]` is non-empty; otherwise ignored. |
| `routing.width_um` | number | Route wire width. **Required and must be `> 0`** when `connectivity[]` is non-empty; otherwise ignored. |
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
  "nets": [
    {
      "net": "VOUT",
      "pins": [
        { "block": "diffpair", "port": "Q1_1_D" },
        { "block": "mirror", "port": "M1_1_D" }
      ],
      "routed": true,
      "route_length_um": 3.2
    }
  ],
  "unrouted_nets": [],
  "drc_hints": {
    "min_spacing_um": 1.0,
    "matched_groups": [
      {
        "matched_group_id": "diff_pair:pair:2",
        "blocks": ["diffpair"],
        "placement_symmetric": null
      }
    ],
    "notes": []
  },
  "warnings": []
}
```

#### Top-level fields

| Field | Type | Description |
| ----- | ---- | ----------- |
| `schema_version` | integer | Version of this command's JSON shape (starts at `1`). |
| `cell_name` | string | Name of the top cell written into `gds_path`, containing every placed block's cell as a translated sub-cell instance plus all routed metal. |
| `gds_path` | string | Resolved output path (echoes `options.output`, or the computed default). |
| `pdk` | object | The resolved PDK reference, echoing `klt pdk find`'s own `variant`/`version` fields. |
| `bbox_um` | object | Bounding box of the *composed* cell — the union of every placed block's own `bbox_um`, translated by its `offset_um` (computed arithmetically from each block's reported `bbox_um`, never re-derived from drawn geometry). |
| `blocks[]` | array\<object\> | Per-block placement result — see below. |
| `nets[]` | array\<object\> | One entry per `connectivity[]` net: an echo of `net`/`pins`, plus `routed` (boolean) and `route_length_um` (total routed wire length in um, or `null` when the net was not routed — for a caller doing a first-order parasitic estimate before extraction). Present for every net including bundle (>2-pin) and unroutable ones (with `routed: false`). |
| `unrouted_nets[]` | array\<string\> | Net labels the router could not connect (an unroutable 2-pin net, or a >2-pin bundle net deferred this phase). Always present, empty when everything routed. **A non-empty array is a partial success** (exit code `3`), not silently dropped connectivity. |
| `drc_hints` | object | Advisory, same "not authoritative" semantics as `klt gen`'s own `drc_hints` — `klt drc` remains the actual authority on rule compliance. See fields below. |
| `warnings[]` | array\<string\> | Non-fatal notes. Always present, empty when there is nothing to report. |

#### `drc_hints` fields

| Field | Type | Description |
| ----- | ---- | ----------- |
| `min_spacing_um` | number \| null | The tightest spacing actually used across placement and routing (the placement gap when any net was routed). `null` when no `connectivity[]` was supplied (nothing was routed, so no routing/placement spacing was exercised as a clearance). |
| `matched_groups[]` | array\<object\> | One entry per distinct `matched_group_id` seen among the input blocks' own `generator_report.drc_hints.matched_group_id` (in first-seen order): `matched_group_id` (echoed), `blocks` (the request-level block `id`s carrying it), and `placement_symmetric` (always `null` this phase — symmetry *verification* against a declared symmetry axis is out of scope). Empty when no input block carries a `matched_group_id`. |
| `notes[]` | array\<string\> | Free-form composition notes — e.g. why a specific net was left unrouted (narrow channel, or a bundle net deferred this phase). Always present, empty when there is nothing to report. |

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
(nothing names `pya`/`klayout.db` in the JSON shape), routing-layer resolution
goes through the one per-PDK role-layer table every generator already uses (a
`routing.layer_role`, never a raw `{layer, datatype}`), `drc_hints` is advisory
not authoritative, PDK resolution goes through the one resolver, and the
envelope is additive — new fields may be added without a schema/`schema_version`
bump; renaming, removing, or retyping an existing field requires one.

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

nets:
  VOUT  routed  length=3.2um

matched_groups:
  diff_pair:pair:2  (diffpair)
```

## Exit codes and errors

| Exit code | Meaning |
| --------- | ------- |
| `0` | Every block placed and every net routed; `gds_path` was written and the report above is on stdout. |
| `1` | Application error — unresolvable PDK, malformed request (missing/invalid `blocks[]`, `placement.order` not matching `blocks[]`, negative `spacing_um`, or a missing/invalid `routing.layer_role`/`routing.width_um` when `connectivity[]` is non-empty), an unsupported `placement.strategy`, a `connectivity[]` entry referencing a nonexistent block `id`/port, a block's `generator_report`/GDS could not be read, or the `options.output` directory does not exist. |
| `2` | Usage error — missing `<request.json>` argument, or a bad `--format` value (from argparse). |
| `3` | **Partial success** — every block placed, but `unrouted_nets[]` is non-empty (a net could not be routed, or a >2-pin bundle net was deferred this phase). The full success payload above is still on stdout, mirroring `klt drc`'s own `3` for "ran clean but found violations" (spike section 2, "Proposed exit codes"). |

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
  "routing": { "layer_role": "metal", "width_um": 0.17 },
  "options": { "cell_name": "ota_top_0", "output": "ota_top_0.gds" }
}
EOF
# Exit 0 when every net routes; exit 3 (partial success) if any net lands in
# unrouted_nets[] -- the full report is still emitted either way.
$ klt gen-compose compose_request.json --format json
```
