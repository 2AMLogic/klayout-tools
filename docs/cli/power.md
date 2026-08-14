# `klt power`

Extract a routed layout's power grid as a resistive network (nodes +
segment resistances) and solve it for static (DC) IR drop — issues
[#844](https://github.com/2AMLogic/klayout-tools/issues/844) (Phase 1a) and
[#845](https://github.com/2AMLogic/klayout-tools/issues/845) (Phase 1b) of
the power/IR-drop + EM signoff epic
[#712](https://github.com/2AMLogic/klayout-tools/issues/712).

```
klt power <file> <spec> [--top <cell>] [--format text|json]
```

- `<file>` — path to a routed GDSII (`.gds`) or OASIS (`.oas`) layout, e.g.
  a `klt place-and-route` output.
- `<spec>` — path to a JSON **spec file** (see "Spec file" below): the
  power nets to extract, the metal/label layer stackup, the vias bridging
  it, and — to ask for an IR-drop solve — the supply `pads` and the
  per-instance `current_model`.
- `--top` — top cell to analyse when the stream has more than one
  (**required** in that case — like `klt mom`, `klt power` needs exactly
  one root to analyse, unlike `klt layers`, which defaults to summing
  across every top cell).
- `--format` — `text` (default, a human-readable summary) or `json`.

The command is headless (`klayout.db` batch API only, no GUI) and safe to
run in CI, with no external engine dependency (unlike `klt mom`, this does
not need the `klt_mom_native` Rust extension).

## Phase scope: what `klt power` is, and what has landed

`klt power`'s full, intended interface (per epic #712) is:

- **JSON in**: a routed layout, a power-net definition, and a per-instance
  current model.
- **JSON out**: an IR-drop map, the worst-case droop, and a per-net EM
  (electromigration) verdict citing the PDK's current-density limit.

Two phases have landed:

- **#844, Phase 1a — the interface and the resistive-network extraction.**
  The spec file's `power_nets`/`stackup`/`vias` *are* the power-net
  definition; the response's `networks[].islands[].nodes`/`edges` are the
  resistive network.
- **#845, Phase 1b — the static IR-drop solve.** The spec file's `pads`
  (where the supply is delivered) and `current_model` (what each instance
  draws) are the remaining inputs; the response's `ir_drop_map` and
  `worst_case_droop_mv` are the IR-drop map and worst-case droop. A spec
  that declares neither `pads` nor `current_model` still runs
  extraction-only, and both response fields are `null` — there is nothing
  to solve.

One field from the full interface above is **not part of these phases**:

- `em_verdict` (response,
  [#846](https://github.com/2AMLogic/klayout-tools/issues/846), Phase 1c) —
  the per-net EM current-density verdict, which consumes this phase's
  per-branch currents (`ir_drop_map.nets[].islands[].edges[].current_a`).

Per [`docs/json-contract.md`](../json-contract.md)'s additive-envelope
design, 1b's new fields needed **no `schema_version` bump** — every field
1a promised is unchanged — and 1c's will not either.

## Why a named net is (usually) several disconnected islands, not one mesh

`klt place-and-route` (issue #700) deliberately runs **no PDN (power-grid)
generation** — see `place_and_route.py`'s own "Deliberately out of scope for
this v1" note. A routed design's power/ground geometry is therefore
whatever the standard-cell rows themselves contribute: typically many
un-strapped per-row rail segments, not one continuous mesh from pad to
every cell. `klt power` reports this as-is — every electrically distinct
cluster sharing a power net's name is its own **island**
(`networks[].islands[]`) — because that is exactly the real electrical
topology the IR-drop solve needs: an un-strapped island has no path to a pad
and cannot be solved for droop without saying so plainly (it is reported
`solved: false` with `unsolved_reason: "no_pad"`, never guessed at).
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
  ],
  "pads": [
    { "name": "vdd0", "net": "VPWR", "x_um": 0.0, "y_um": 0.5, "voltage_v": 1.8 },
    { "name": "vss0", "net": "VGND", "x_um": 0.0, "y_um": 2.5, "voltage_v": 0.0 }
  ],
  "current_model": {
    "supply_net": "VPWR",
    "ground_net": "VGND",
    "instances": [
      { "name": "u1", "x_um": 10.0, "y_um": 0.5, "current_a": 2e-4 }
    ]
  }
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
- `pads` (optional array, default `[]`) — where each power net's supply is
  actually delivered. Each pad is held at a fixed voltage (an ideal source)
  and is the boundary condition the solve is relative to:
  - `name` (string, optional, defaults to `"pad<index>"`, must be unique) —
    echoed back in `ir_drop_map.pads[]`.
  - `net` (string, required) — must be one of `power_nets`.
  - `x_um` / `y_um` (numbers, required) — the pad's position. It attaches to
    the **nearest extracted node on that net** (see "Where pads and
    instances attach" below), not to a node spliced in at that exact point.
  - `voltage_v` (number, required) — the voltage this pad holds its node at,
    e.g. `1.8` for a supply and `0.0` for a ground.
- `current_model` (optional object) — what each instance draws:
  - `supply_net` / `ground_net` (strings, optional) — per-model defaults for
    the instances below; each must be one of `power_nets`.
  - `instances` (non-empty array, required when `current_model` is present):
    - `name` (string, optional, defaults to `"inst<index>"`, must be
      unique).
    - `x_um` / `y_um` (numbers, required) — the instance's position, snapped
      to the nearest extracted node *on each of its nets independently*.
    - `current_a` (number, required, `>= 0`) — a **magnitude**, not a signed
      injection: the instance draws it off `supply_net` and returns the same
      current to `ground_net`. The sign convention is set by which net is
      which, so a negative `current_a` is rejected rather than silently
      reinterpreted.
    - `supply_net` / `ground_net` (strings, optional) — per-instance
      overrides of the model defaults. An instance needs a `supply_net` from
      one place or the other; `ground_net` may legitimately be absent (the
      return path is then not modelled) but must differ from `supply_net`.

A spec declaring neither `pads` nor `current_model` runs the extraction only
(`ir_drop_map` and `worst_case_droop_mv` are `null`). Declaring `pads` alone
is legal and useful — it holds each strapped island at its pad voltage with
zero droop, which is the degenerate check that the pads land where you think
they do. Declaring `current_model` alone is legal too, but nothing has a DC
path to a source, so every island reports `unsolved_reason: "no_pad"` and
its current is totalled into `ir_drop_map.unsolved_current_a`.

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

## Static IR-drop solve (how the numbers are produced)

The solve is a plain DC operating point on the extracted network: modified
nodal analysis with the pad nodes held at their fixed voltages (a Dirichlet
boundary), solved per island with Jacobi-preconditioned conjugate gradients.
The linear algebra lives in `klayout_tools/ir_solver.py`, deliberately
**geometry-free** — its input is node ids, resistor edges, pad voltages and
current injections — so it can be validated against networks whose answers
are known analytically, with no layout involved.

- **Sign convention.** An instance *draws* its `current_a` off `supply_net`
  (a negative injection there) and *returns* the same current to
  `ground_net` (a positive injection). So a supply net's nodes sit **below**
  its pad voltage (droop) and a ground net's sit **above** its pad voltage
  (bounce). `droop_mv` is always the reported magnitude; the sign is
  recoverable from `voltage_v` versus the island's `reference_voltage_v`.
- **Where pads and instances attach.** Both snap to the nearest extracted
  node on the relevant net (straight-line distance, ties resolving to the
  first node in extraction order, so a given layout + spec always solves the
  same network). This is the same approximation via taps already use — see
  "Scope and limitations". A pad or instance on a net with no extracted
  geometry at all is a `warnings` entry, not a failure, and the pad reports
  `island_id: null`.
- **Conservation is reported, not assumed.** Each island's
  `pad_current_a` is what its pads together deliver, computed from the
  solved voltages; for a solved island it equals the negated sum of that
  island's injections (positive on a supply net, negative on a ground net,
  including any load that happens to attach to the pad's own node).
- **An island with no pad is reported unsolved, never guessed.** Its nodes'
  `voltage_v`/`droop_mv` and its edges' `current_a` are `null`, and
  `unsolved_reason` is `"no_pad"`. Any current the model put there is
  totalled into `unsolved_current_a` and named in `warnings` — a load with
  no path to a source is a modelling error worth calling out, whereas a
  quiet unloaded un-strapped island only contributes to one aggregate
  warning (a PDN-free design has hundreds of those).
- **Convergence is reported, never hidden.** Every island carries its
  `iterations` and achieved relative `residual`; an island whose residual
  is worse than `1e-6` is reported `unsolved_reason: "not_converged"`
  rather than returning numbers nobody should trust.
- **`resistance_ohm: 0` edges are ideal shorts**, merged into one supernode
  rather than divided by. Such a branch's own current is not recoverable
  from node voltages, so its `current_a` is `null`. Two pads shorted to
  *different* voltages are a contradiction, reported
  `unsolved_reason: "conflicting_pad_voltage"` rather than averaged.

**How it is validated.** `tests/test_ir_solver.py` checks the solver against
canonical closed-form networks — a series ladder (`I*N*R`), a uniformly
loaded rail (the triangular sum `I*R*N*(N+1)/2`), a double-fed rail (the
textbook factor of 4), a current divider, a balanced Wheatstone bridge (zero
bridge current), and the infinite-square-lattice Green's function (adjacent
nodes at `R/2`, diagonal at `2R/pi`) — to a relative tolerance of `1e-9` on
the exact networks (a round-off budget) and 0.5 %/1 % on the two lattice
cases, where the tolerance is dominated by the finite-grid truncation of the
*analytic* answer rather than by the solver.
`tests/test_power_ir_cross_check.py` then cross-checks the same solver
against an **independent implementation** — ngspice's own `.op` DC operating
point (its own sparse LU, its own netlist parser, the same engine `klt sim`
already uses) — node for node and branch for branch, on a synthetic 2-D mesh
and end to end on the real routed `gcd` corpus fixture, agreeing to `1e-9` V
and `1e-12` A.

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
  "ir_drop_map": {
    "pads": [
      {
        "name": "vdd0",
        "net": "VPWR",
        "x_um": 0.0,
        "y_um": 0.5,
        "voltage_v": 1.8,
        "island_id": "VPWR#0",
        "node_id": "n0"
      }
    ],
    "instance_count": 1,
    "total_current_a": 0.001,
    "unsolved_current_a": 0.0,
    "solved_node_count": 2,
    "unsolved_node_count": 0,
    "worst_case": {
      "net": "VPWR",
      "island_id": "VPWR#0",
      "node_id": "n1",
      "layer": "met1",
      "x_um": 10.0,
      "y_um": 0.5,
      "voltage_v": 1.799,
      "droop_mv": 1.0
    },
    "nets": [
      {
        "net": "VPWR",
        "pad_count": 1,
        "instance_count": 1,
        "current_a": 0.001,
        "solved_island_count": 1,
        "unsolved_island_count": 0,
        "worst_case_droop_mv": 1.0,
        "worst_case_node": {"net": "VPWR", "island_id": "VPWR#0", "node_id": "n1", "layer": "met1", "x_um": 10.0, "y_um": 0.5, "voltage_v": 1.799, "droop_mv": 1.0},
        "islands": [
          {
            "island_id": "VPWR#0",
            "solved": true,
            "unsolved_reason": null,
            "pad_count": 1,
            "instance_count": 1,
            "current_a": 0.001,
            "reference_voltage_v": 1.8,
            "pad_current_a": 0.001,
            "solved_node_count": 2,
            "unsolved_node_count": 0,
            "worst_case_droop_mv": 1.0,
            "worst_case_node_id": "n1",
            "iterations": 1,
            "residual": 0.0,
            "nodes": [
              {"id": "n0", "voltage_v": 1.8, "droop_mv": 0.0, "injected_current_a": 0.0, "pad_voltage_v": 1.8},
              {"id": "n1", "voltage_v": 1.799, "droop_mv": 1.0, "injected_current_a": -0.001, "pad_voltage_v": null}
            ],
            "edges": [{"id": "e0", "current_a": 0.001}]
          }
        ]
      }
    ]
  },
  "worst_case_droop_mv": 1.0,
  "warnings": [
    "power net 'VGND' matches no labelled net in this layout -- check 'power_nets' against the layout's own pin/label text, and that at least one 'stackup' entry's 'label_layer' actually carries it"
  ]
}
```

| Field                | Type               | Description                                                                                       |
| -------------------- | ------------------ | --------------------------------------------------------------------------------------------------|
| `schema_version`     | integer            | `1` (unchanged by Phase 1b, which only added fields; see "Phase scope" above for the one additive field still to come). |
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
| `ir_drop_map`        | object \| null     | The static IR-drop solve, or `null` when the spec declared neither `pads` nor `current_model` — see below. |
| `worst_case_droop_mv` | number \| null    | The largest \|voltage − island reference voltage\| anywhere solved, in millivolts (`null` when there was no solve; `0.0` when there was a solve but nothing drooped). Equal to `ir_drop_map.worst_case.droop_mv`. |
| `warnings`           | array\<string\>    | Non-fatal diagnostics — an unmatched `power_nets` entry, a non-rectangular segment approximated by its bounding box, a via with no matching rail on one side, a pad/instance on a net with no geometry, current stranded on a padless island, or an aggregate count of quiet unloaded padless islands. Empty on a clean run.            |

### `ir_drop_map` (Phase 1b)

| Field                | Type               | Description                                                                                       |
| -------------------- | ------------------ | --------------------------------------------------------------------------------------------------|
| `pads[]`             | array\<object\>    | Every spec pad echoed back with where it actually landed: `{"name", "net", "x_um", "y_um", "voltage_v", "island_id", "node_id"}`. `island_id`/`node_id` are `null` for a pad whose net has no extracted geometry (also a `warnings` entry). |
| `instance_count`     | integer            | Number of `current_model.instances[]` entries in the spec.                                        |
| `total_current_a`    | number             | Sum of every instance's `current_a` as specified (before any snapping).                           |
| `unsolved_current_a` | number             | Total modelled current that landed on islands with no operating point — current the report is *not* accounting for. `0.0` on a healthy run. |
| `solved_node_count`/`unsolved_node_count` | integer | Nodes that did / did not get an operating point, across every net.                    |
| `worst_case`         | object \| null     | The worst droop anywhere: `{"net", "island_id", "node_id", "layer", "x_um", "y_um", "voltage_v", "droop_mv"}`, or `null` if nothing solved. |
| `nets[]`             | array\<object\>    | One entry per requested net, same order as `networks`.                                            |
| `nets[].pad_count`/`instance_count` | integer | Pads declared on this net / instances whose current attached to it.                              |
| `nets[].current_a`   | number             | Total current magnitude attached to this net's islands.                                            |
| `nets[].solved_island_count`/`unsolved_island_count` | integer | This net's islands that did / did not solve.                                     |
| `nets[].worst_case_droop_mv`/`worst_case_node` | number \| null / object \| null | This net's own worst droop and the node it is at (same shape as `worst_case`). |
| `nets[].islands[]`   | array\<object\>    | One entry per island, in the same order as `networks[].islands[]`.                                |
| `islands[].solved`   | boolean            | Whether any part of this island got an operating point.                                            |
| `islands[].unsolved_reason` | string \| null | `null`, `"no_pad"`, `"conflicting_pad_voltage"`, or `"not_converged"`.                          |
| `islands[].pad_count`/`instance_count` | integer | Pads / instances that snapped onto this island.                                                 |
| `islands[].current_a` | number            | Total current magnitude injected on this island.                                                   |
| `islands[].reference_voltage_v` | number \| null | The pad voltage droop is measured against (the highest pad voltage on the island).            |
| `islands[].pad_current_a` | number \| null | What this island's pads together deliver, from the solved voltages — positive on a supply net, negative on a ground net (see "Static IR-drop solve" above). |
| `islands[].solved_node_count`/`unsolved_node_count` | integer | This island's own node totals.                                                     |
| `islands[].worst_case_droop_mv`/`worst_case_node_id` | number \| null / string \| null | This island's worst droop magnitude and where.                       |
| `islands[].iterations`/`residual` | integer / number | Conjugate-gradient iterations and the achieved relative residual (worst over the island's components). |
| `islands[].nodes[]`  | array\<object\>    | `{"id", "voltage_v", "droop_mv", "injected_current_a", "pad_voltage_v"}` — `id` matches the sibling `networks[]` node; `voltage_v`/`droop_mv` are `null` on an unsolved island; `injected_current_a` is signed (negative = drawn off this net) and `pad_voltage_v` is `null` unless a pad landed here. |
| `islands[].edges[]`  | array\<object\>    | `{"id", "current_a"}` — the signed `from -> to` branch current, `null` on an unsolved island or a `resistance_ohm: 0` (shorted) edge. This is what the Phase 1c EM verdict consumes. |

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

Adding `pads` (one per island, at each rail's left-hand end) and a
`current_model` (0.2 mA hung on each `VPWR` rail's far end) to that same
spec — with both metal roles at `0.125` ohm/sq, a sky130-order sheet
resistance — turns the run into a full IR-drop solve. Every one of the 193
islands gets an operating point, all 386 nodes solve, no current is
stranded (`unsolved_current_a: 0.0`), each island's `pad_current_a`
balances its own load exactly, and the run is warning-free. The 88 loads
total 17.6 mA; the worst droop is **4.84 mV** on `VGND#88`, exactly twice
the worst `VPWR` droop of 2.42 mV — because abutting standard-cell rows
share a ground rail, so 20 of the 105 `VGND` islands collect *two* rows'
return current (0.4 mA) while every `VPWR` island carries one row's 0.2 mA.
That is the rails' own `I * 0.125 ohm/sq * L/W` arithmetic, read back out
of a real routed layout. The same network is
then handed to ngspice, which reproduces every node voltage to `1e-9` V and
every branch current to `1e-12` A (`tests/test_power.py`'s
`test_gcd_fixture_solves_for_a_real_ir_drop_map` and
`tests/test_power_ir_cross_check.py`'s
`test_gcd_routed_design_matches_ngspice`).

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
- **Pads and instances snap to existing nodes, like via taps.** A pad or an
  instance is wired to the nearest *extracted* node on its net, not spliced
  into a rail at its exact position — same trade, and same "immaterial for
  a short rail, less so for a long strap" caveat, as via taps above.
- **Static (DC) only.** This is an operating-point solve on resistors:
  no decoupling capacitance, no package/board parasitics, no switching
  activity, no `di/dt`. `current_model` currents are the caller's own
  average/peak numbers, taken verbatim — `klt power` does not derive them
  from a netlist, a VCD, or a liberty file. Dynamic (transient) IR drop is
  explicitly a later phase of epic #712, not a limitation to be worked
  around here.
- **No EM verdict yet.** See "Phase scope" above — `em_verdict` does not
  exist yet; the per-branch currents it needs are already reported
  (`ir_drop_map.nets[].islands[].edges[].current_a`).

## Exit codes

| Exit code | Meaning                                                                                              |
| --------- | ------------------------------------------------------------------------------------------------------ |
| `0`       | Success — every requested power net was reported (with `island_count: 0` and a `warnings` entry for any net that matched no labelled geometry). |
| `1`       | Failed to run: layout/spec file not found or unreadable, a malformed `power_nets`/`stackup`/`vias`/`pads`/`current_model` declaration, an ambiguous top cell (pass `--top`), or **every** requested power net matched no geometry at all. |
| `2`       | Usage error (argparse) — missing/invalid arguments.                                                    |

## See also

- [#712](https://github.com/2AMLogic/klayout-tools/issues/712) — the parent
  power/IR-drop + EM signoff epic (remaining phases: the per-net EM verdict,
  then dynamic currents).
- [#845](https://github.com/2AMLogic/klayout-tools/issues/845) — Phase 1b,
  the static IR-drop solve this command's resistive network feeds
  (`klayout_tools/ir_solver.py`, validated in `tests/test_ir_solver.py` and
  cross-checked against ngspice in `tests/test_power_ir_cross_check.py`).
- [#846](https://github.com/2AMLogic/klayout-tools/issues/846) — Phase 1c,
  the per-net EM current-density verdict.
- [`docs/cli/place-and-route.md`](place-and-route.md) — `klt place-and-route`,
  the source of the routed layouts this command analyses (and of the "no
  PDN generation in this v1" fact "Why a named net is usually several
  islands" above depends on).
- [`docs/cli/mom.md`](mom.md) — `klt mom`'s spec-file convention this
  command's `stackup` deliberately mirrors (layer/datatype-keyed roles,
  caller-supplied electrical properties).
