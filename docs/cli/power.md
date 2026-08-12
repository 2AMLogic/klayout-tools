# `klt power`

Extract a routed layout's power grid as a resistive network (nodes +
segment resistances) — issue
[#844](https://github.com/2AMLogic/klayout-tools/issues/844), Phase 1a of
the power/IR-drop + EM signoff epic
[#712](https://github.com/2AMLogic/klayout-tools/issues/712).

```
klt power <file> <spec> [--top <cell>] [--format text|json]
```

- `<file>` — path to a routed GDSII (`.gds`) or OASIS (`.oas`) layout, e.g.
  a `klt place-and-route` output.
- `<spec>` — path to a JSON **spec file** (see "Spec file" below): the
  power nets to extract, the metal/label layer stackup, and the vias
  bridging it.
- `--top` — top cell to analyse when the stream has more than one
  (**required** in that case — like `klt mom`, `klt power` needs exactly
  one root to analyse, unlike `klt layers`, which defaults to summing
  across every top cell).
- `--format` — `text` (default, a human-readable summary) or `json`.

The command is headless (`klayout.db` batch API only, no GUI) and safe to
run in CI, with no external engine dependency (unlike `klt mom`, this does
not need the `klt_mom_native` Rust extension).

## Phase scope: what `klt power` is, and what this issue delivers

`klt power`'s full, intended interface (per epic #712) is:

- **JSON in**: a routed layout, a power-net definition, and a per-instance
  current model.
- **JSON out**: an IR-drop map, the worst-case droop, and a per-net EM
  (electromigration) verdict citing the PDK's current-density limit.

**This issue (#844, Phase 1a) delivers only the interface and the
resistive-network extraction.** The spec file's `power_nets`/`stackup`/
`vias` *are* the power-net definition; this command's own JSON response is
the resistive network (`networks[].islands[].nodes`/`edges` below) that a
later phase solves. Two fields from the full interface above are
**deliberately not part of this phase**:

- `current_model` (a per-instance current draw, spec input) — planned for
  [#845](https://github.com/2AMLogic/klayout-tools/issues/845) (Phase 1b,
  the static IR-drop solve). A spec that includes it today is not read; only
  `power_nets`/`stackup`/`vias` are consulted.
- `ir_drop_map` / `worst_case_droop_mv` (response, #845) and `em_verdict`
  (response, [#846](https://github.com/2AMLogic/klayout-tools/issues/846),
  Phase 1c) — these top-level response fields do not exist yet. Per
  [`docs/json-contract.md`](../json-contract.md)'s additive-envelope
  design, adding them in a later phase needs **no `schema_version` bump**:
  every field this document currently promises stays exactly as documented.

## Why a named net is (usually) several disconnected islands, not one mesh

`klt place-and-route` (issue #700) deliberately runs **no PDN (power-grid)
generation** — see `place_and_route.py`'s own "Deliberately out of scope for
this v1" note. A routed design's power/ground geometry is therefore
whatever the standard-cell rows themselves contribute: typically many
un-strapped per-row rail segments, not one continuous mesh from pad to
every cell. `klt power` reports this as-is — every electrically distinct
cluster sharing a power net's name is its own **island**
(`networks[].islands[]`) — because that is exactly the real electrical
topology a later IR-drop solve (#845) needs: an un-strapped island has no
path to a pad and cannot be solved for droop without saying so plainly.
This is not a defect in the extraction; it is what a PDN-free routed design
actually looks like. See "Worked example" below, where the real
`gcd` corpus fixture resolves to 88 separate `VPWR` islands and 105
separate `VGND` islands.

## Spec file

The spec file is a JSON object:

```json
{
  "power_nets": ["VPWR", "VGND"],
  "stackup": [
    {
      "name": "met1",
      "layer": "68/20",
      "label_layer": "68/5",
      "sheet_resistance_ohm_per_sq": 0.1
    },
    {
      "name": "met2",
      "layer": "69/20",
      "label_layer": "69/5",
      "sheet_resistance_ohm_per_sq": 0.05
    }
  ],
  "vias": [
    {
      "name": "via1",
      "layer": "68/44",
      "between": ["met1", "met2"],
      "resistance_ohm": 2.0
    }
  ]
}
```

- `power_nets` (required, non-empty array of strings) — net names to
  extract, e.g. `["VPWR", "VGND"]`. Duplicate entries are silently
  deduplicated (first-seen order).
- `stackup` (required, non-empty array) — each entry declares one metal
  role:
  - `name` (string, required) — the role's own name, referenced by `vias[]`
    below and echoed in every node/edge this role contributes.
  - `layer` (string, `"<layer>/<datatype>"`, required) — the GDS layer to
    read this role's drawn geometry from.
  - `label_layer` (string, `"<layer>/<datatype>"`, optional) — the GDS
    layer carrying this role's own pin/net-name text (the routed layout's
    own label convention, e.g. sky130's `met1.pin`/`68/5` datatype). **At
    least one `stackup` entry must set this** — otherwise no net in the
    layout can ever be matched by name. A role with no `label_layer` still
    contributes geometry to whatever net a via connects it to (see
    "Worked example" below and `tests/test_power.py`'s
    `test_run_power_via_bridged_island_has_metal_and_via_edges`).
  - `sheet_resistance_ohm_per_sq` (number, required, `>= 0`) — this role's
    sheet resistance, ohms per square, used to turn a merged rail
    polygon's length/width into a resistance (see "Resistor-network model"
    below).
- `vias` (optional array, default `[]`) — each entry bridges two `stackup`
  roles:
  - `name` (string, optional, defaults to `"via<index>"`) — echoed in every
    edge this via contributes.
  - `layer` (string, required) — the GDS layer carrying this via's drawn
    geometry.
  - `between` (array of exactly two distinct `stackup` names, required) —
    which two metal roles this via connects.
  - `resistance_ohm` (number, required, `>= 0`) — this via's resistance,
    used verbatim for every via shape matched (see "Scope and limitations"
    — a via array drawn as one merged shape is not split per cut).

A `stackup`/`vias` entry naming a layer absent from the given layout is not
itself an error (a shared spec can list layers a particular fixture doesn't
use); a `power_nets` entry matching no labelled net anywhere is reported in
`warnings` rather than failing the whole run, **unless every requested net
does** — in which case the command fails (a request with zero resolved
geometry is almost certainly a layer-number/net-name mismatch, not a
legitimately empty result).

## Resistor-network model (MVP, stated plainly)

Connectivity is traced with `klayout.db.LayoutToNetlist`, used purely for
wire/via connectivity — no device recognition is registered, unlike `klt
extract`'s deck-based extraction. This is the same API `extract.py`'s own
metal/via connectivity graph uses, scoped down to only the layers this
spec declares.

- Each metal role's net geometry (`LayoutToNetlist.polygons_of_net`,
  merged into maximal polygons) becomes **one metal edge per merged
  polygon**, between two endpoint nodes placed at the polygon's
  bounding-box ends along its longer axis:
  `resistance_ohm = sheet_resistance_ohm_per_sq * length_um / width_um`.
  Every merged polygon in this command's own real-fixture validation (the
  `gcd` corpus fixture below, a genuine `klt place-and-route` output) is an
  axis-aligned box, matching this model exactly. A non-rectangular polygon
  is still accepted — approximated by its bounding box — with a
  `warnings` entry, never a silent misrepresentation.
- Each via role's net geometry becomes **one via edge per merged via
  polygon**, connecting the node *nearest* (straight-line distance) the
  via's center on each of the two metal roles it bridges — not a true
  T-junction split of the rail it taps partway along. See "Scope and
  limitations" below for what this approximates away.

Node/edge ids (`n0`, `n1`, ... / `e0`, `e1`, ...) are scoped **per island**,
not globally unique across the whole response — a later per-island IR-drop
solve consumes one island at a time, so this is the natural granularity;
re-derive a globally unique key as `(net, island_id, id)` if you need one.

## JSON schema (the contract)

**JSON is the API.** See [`docs/json-contract.md`](../json-contract.md) for
the shared envelope (`schema_version`, error shape, exit codes).

```json
{
  "schema_version": 1,
  "file": "routed.gds",
  "spec": "power.json",
  "power_nets": ["VPWR", "VGND"],
  "networks": [
    {
      "net": "VPWR",
      "island_count": 1,
      "node_count": 2,
      "edge_count": 1,
      "islands": [
        {
          "island_id": "VPWR#0",
          "node_count": 2,
          "edge_count": 1,
          "nodes": [
            {"id": "n0", "layer": "met1", "x_um": 0.0, "y_um": 0.5},
            {"id": "n1", "layer": "met1", "x_um": 10.0, "y_um": 0.5}
          ],
          "edges": [
            {
              "id": "e0",
              "kind": "metal",
              "layer": "met1",
              "from": "n0",
              "to": "n1",
              "resistance_ohm": 1.0
            }
          ]
        }
      ]
    },
    {"net": "VGND", "island_count": 0, "node_count": 0, "edge_count": 0, "islands": []}
  ],
  "node_count": 2,
  "edge_count": 1,
  "island_count": 1,
  "warnings": [
    "power net 'VGND' matches no labelled net in this layout -- check 'power_nets' against the layout's own pin/label text, and that at least one 'stackup' entry's 'label_layer' actually carries it"
  ]
}
```

| Field                | Type               | Description                                                                                       |
| -------------------- | ------------------ | --------------------------------------------------------------------------------------------------|
| `schema_version`     | integer            | `1` (this phase's extraction-only shape; see "Phase scope" above for the additive fields to come). |
| `file`               | string             | The input layout path exactly as provided.                                                        |
| `spec`               | string              | The spec file path exactly as provided.                                                           |
| `power_nets`         | array\<string\>    | Deduplicated net names, in first-seen order (same order as `networks`).                           |
| `networks`           | array\<object\>    | One entry per `power_nets` name — see below.                                                      |
| `networks[].net`     | string             | The power net name.                                                                                |
| `networks[].island_count` | integer       | Number of electrically distinct clusters this net resolved to (`0` if unmatched).                 |
| `networks[].node_count`/`edge_count` | integer | Totals across every island of this net.                                                           |
| `networks[].islands` | array\<object\>    | One entry per electrically distinct cluster (see "Why a named net is usually several islands" above). |
| `islands[].island_id` | string            | `"<net>#<index>"`, stable within one run, in ascending internal net-id order (not guaranteed stable across `klt` versions/KLayout builds). |
| `islands[].node_count`/`edge_count` | integer | This island's own totals.                                                                         |
| `islands[].nodes[]`  | array\<object\>    | `{"id", "layer", "x_um", "y_um"}` — `id` is scoped to this island (see "Resistor-network model" above). |
| `islands[].edges[]`  | array\<object\>    | `{"id", "kind", "layer", "from", "to", "resistance_ohm"}` — `kind` is `"metal"` or `"via"`; `from`/`to` reference sibling `nodes[].id` values; `layer` names the contributing `stackup`/`vias` entry. |
| `node_count`/`edge_count`/`island_count` | integer | Totals across every requested net.                                                    |
| `warnings`           | array\<string\>    | Non-fatal diagnostics — an unmatched `power_nets` entry, a non-rectangular segment approximated by its bounding box, or a via with no matching rail on one side. Empty on a clean run.            |

## Worked example: real `klt place-and-route` output (the `gcd` corpus fixture)

```bash
klt power tests/corpus/place_and_route/gcd.gds.gz gcd.power.json --format json
```

with `gcd.power.json` built from sky130's own `metals`/`metal_labels`/`vias`
layer numbers (verified against a real sky130A install — see
`decks/sky130.py`'s own tuples):

```json
{
  "power_nets": ["VPWR", "VGND"],
  "stackup": [
    { "name": "met1", "layer": "68/20", "label_layer": "68/5", "sheet_resistance_ohm_per_sq": 0.1 },
    { "name": "met2", "layer": "69/20", "label_layer": "69/5", "sheet_resistance_ohm_per_sq": 0.05 }
  ],
  "vias": [
    { "name": "via1", "layer": "68/44", "between": ["met1", "met2"], "resistance_ohm": 2.0 }
  ]
}
```

`gcd.gds.gz` is a real `sky130_fd_sc_hd` GCD macro produced end to end by
`klt synthesize` + `klt place-and-route` (real Yosys + real OpenROAD) — the
same corpus fixture `klt drc`/`klt extract`/`klt lvs` already validate
against (see `tests/corpus/README.md`'s "Machine-generated macro-scale
fixture" section). This run resolves to **88 separate `VPWR` islands and
105 separate `VGND` islands** (386 nodes, 193 edges total, zero warnings) —
a regression pin against this specific, static, committed fixture (see
`tests/test_power.py`'s `test_gcd_fixture_extracts_a_real_power_grid`), and
a real, measured instance of "why a named net is usually several islands"
above: this design has no PDN, so almost every standard-cell row's power
rail is its own disconnected island.

## Scope and limitations

- **No device recognition, wire/via connectivity only (by design).** Unlike
  `klt extract`, `klt power` never registers a device extractor — a power
  net's geometry is whatever the declared `stackup`/`vias` layers
  physically connect, nothing more. A net that also draws current through
  a transistor body tie or a well tap is not modelled differently; this
  command only sees drawn metal/via shapes.
- **Rectangular-rail MVP.** Every merged polygon is turned into exactly one
  resistor edge via its bounding box's longer-axis length/width — an
  L-shaped or otherwise non-rectangular rail is approximated, not exactly
  decomposed into a chain of sub-rectangles. Flagged in `warnings`, never
  silent. See "Resistor-network model" above.
- **Via taps snap to the nearest existing rail node, not a true
  T-junction.** A via landing partway along a long rail is wired to
  whichever of that rail's two *endpoints* is closer, not to a new node
  spliced in at the via's exact position. This preserves every island's
  real connectivity and each rail's total resistance, at the cost of a
  small positional error in exactly where along a rail a given tap
  electrically lands — immaterial for a short rail (the common case; see
  the worked example above), more so for a very long strap with several
  taps along its length. Precise T-junction splitting is a natural
  follow-up once needed.
- **One via shape = one resistor, at the spec's given `resistance_ohm`.** A
  via drawn as a single merged shape covering what a real design would cut
  as an array of several via cuts is not split into a parallel-resistor
  combination — the spec's `resistance_ohm` is used verbatim per merged
  via polygon. Model a via array's true (lower) parallel resistance by
  passing that already-combined value in the spec.
- **No current model, no solve (this phase).** See "Phase scope" above —
  `current_model`/`ir_drop_map`/`worst_case_droop_mv`/`em_verdict` do not
  exist yet.

## Exit codes

| Exit code | Meaning                                                                                              |
| --------- | ------------------------------------------------------------------------------------------------------ |
| `0`       | Success — every requested power net was reported (with `island_count: 0` and a `warnings` entry for any net that matched no labelled geometry). |
| `1`       | Failed to run: layout/spec file not found or unreadable, a malformed `power_nets`/`stackup`/`vias` declaration, an ambiguous top cell (pass `--top`), or **every** requested power net matched no geometry at all. |
| `2`       | Usage error (argparse) — missing/invalid arguments.                                                    |

## See also

- [#712](https://github.com/2AMLogic/klayout-tools/issues/712) — the parent
  power/IR-drop + EM signoff epic (later phases: the static IR-drop solve,
  the per-net EM verdict).
- [#845](https://github.com/2AMLogic/klayout-tools/issues/845) — Phase 1b,
  the static IR-drop solve this module's resistive network feeds.
- [#846](https://github.com/2AMLogic/klayout-tools/issues/846) — Phase 1c,
  the per-net EM current-density verdict.
- [`docs/cli/place-and-route.md`](place-and-route.md) — `klt place-and-route`,
  the source of the routed layouts this command analyses (and of the "no
  PDN generation in this v1" fact "Why a named net is usually several
  islands" above depends on).
- [`docs/cli/mom.md`](mom.md) — `klt mom`'s spec-file convention this
  command's `stackup` deliberately mirrors (layer/datatype-keyed roles,
  caller-supplied electrical properties).
