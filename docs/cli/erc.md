# `klt erc`

Define the `klt erc` interface and build the layer-by-layer connectivity
model that both the per-gate antenna-ratio check and the core electrical
rule checks (ERC) depend on — issue
[#859](https://github.com/2AMLogic/klayout-tools/issues/859), Phase 1a of
the antenna + ERC signoff epic
[#713](https://github.com/2AMLogic/klayout-tools/issues/713).

```
klt erc <file> <spec> [--top <cell>] [--format text|json]
```

- `<file>` — path to a routed GDSII (`.gds`) or OASIS (`.oas`) layout, e.g.
  a `klt place-and-route` output.
- `<spec>` — path to a JSON **spec file** (see "Spec file" below): the
  gate/conductor stackup, in fabrication order, and the vias bridging it.
- `--top` — top cell to analyse when the stream has more than one
  (**required** in that case — like `klt mom`/`klt power`, `klt erc` needs
  exactly one root to analyse, unlike `klt layers`, which defaults to
  summing across every top cell).
- `--format` — `text` (default, a human-readable summary) or `json`.

The command is headless (`klayout.db` batch API only, no GUI) and safe to
run in CI.

## Phase scope: what `klt erc` is, and what this issue delivers

`klt erc`'s full, intended interface (per epic #713) is:

- **JSON in**: a routed layout, a netlist, and the PDK's antenna/ERC rule
  set.
- **JSON out**: a per-gate antenna-ratio verdict citing the specific PDK
  limit it was checked against, plus an ERC finding list (floating gates,
  unconnected/multiply-driven nets, missing substrate/well ties, supply
  shorts).

**This issue (#859, Phase 1a) delivers only the interface and the
layer-by-layer connectivity model.** The spec file's `stackup`/`vias` *are*
the connectivity declaration; this command's own JSON response
(`gates[].levels[]` below) is the per-gate, per-fabrication-step connected
conductor area that a later phase turns into a verdict. Two things from the
full interface above are **deliberately not part of this phase**:

- An explicit `netlist`/PDK antenna/ERC rule-set spec input — not read in
  this phase. `klt erc` traces connectivity itself via
  `klayout.db.LayoutToNetlist` (no device recognition), the same way `klt
  power` traces power-net connectivity without reading an external netlist
  — see "Connectivity model" below. A PDK rule set (per-layer antenna-ratio
  limits, the core ERC rule definitions) is consumed starting in 1b/1c.
- `antenna_ratio`/`verdict` (per-level, Phase 1b,
  [#860](https://github.com/2AMLogic/klayout-tools/issues/860)) and
  `erc_findings` (Phase 1c,
  [#861](https://github.com/2AMLogic/klayout-tools/issues/861)) — these
  fields do not exist in this phase's response at all. Per
  [`docs/json-contract.md`](../json-contract.md)'s additive-envelope
  design, adding them in a later phase needs **no `schema_version`
  bump**: every field this document currently promises stays exactly as
  documented. 1b's `antenna_ratio` is expected to be a simple derived value
  (`cumulative_area_um2 / gate_area_um2` at a given level) compared against
  a PDK-specific limit; 1c's `erc_findings` is expected to follow the same
  shape as `klt drc`'s `violations[]` (a rule id, a description, and the
  specific net/gate/layer implicated) — see #860/#861 for the shipped
  shape once each lands.

## "Per gate" means "per gate net", not "per drawn poly finger"

Antenna charge accumulates across an entire electrically connected net, not
per individually drawn polysilicon shape — this is how a real
process-antenna-area-ratio (PAAR) check actually works, and it is also
exactly what `klayout.db.LayoutToNetlist` already computes for free: each
distinct **net** it extracts is, by construction, one electrically
connected cluster of geometry. So `klt erc` reports one `gates[]` entry per
net whose geometry includes the declared gate-role layer — two transistor
gates tied together by the same poly/metal net are correctly one
accumulation, not two.

This means gate identification needs **no labelling at all** — unlike `klt
power`'s caller-named `power_nets`, `klt erc` auto-discovers every gate net
directly from connectivity. A `stackup` entry's `label_layer` is optional
purely for readability (a gate's `net` field is `null` when nothing labels
it — see `gates[].net` below).

## Spec file

The spec file is a JSON object:

```json
{
  "stackup": [
    { "name": "poly", "layer": "66/20", "role": "gate" },
    { "name": "li1", "layer": "67/20" },
    { "name": "met1", "layer": "68/20" },
    { "name": "met2", "layer": "69/20", "label_layer": "69/5" }
  ],
  "vias": [
    { "name": "licon1", "layer": "66/44", "between": ["poly", "li1"] },
    { "name": "mcon", "layer": "67/44", "between": ["li1", "met1"] },
    { "name": "via1", "layer": "68/44", "between": ["met1", "met2"] }
  ]
}
```

- `stackup` (required, at least **two** entries) — fabrication order,
  starting from the gate layer:
  - **`stackup[0]` must set `"role": "gate"`** — the polysilicon/gate-poly
    layer that starts the accumulation. Exactly one entry may set this;
    every other entry must omit `role` (an ordinary metal/local-interconnect
    role).
  - `name` (string, required) — the role's own name, referenced by `vias[]`
    below and echoed in every `levels[]` entry this role contributes.
  - `layer` (string, `"<layer>/<datatype>"`, required) — the GDS layer to
    read this role's drawn geometry from.
  - `label_layer` (string, `"<layer>/<datatype>"`, optional) — the GDS
    layer carrying this role's own pin/net-name text. Purely cosmetic (see
    "'Per gate' means..." above) — populates `gates[].net` when a gate's
    net happens to carry a label on this layer; a `klt erc` run with no
    `label_layer` anywhere still reports every gate, just with `net: null`.
- `vias` (optional array, default `[]`) — each entry bridges two `stackup`
  roles:
  - `name` (string, optional, defaults to `"via<index>"`) — echoed nowhere
    in the response (unlike `klt power`'s `edges[].layer`) — `vias` exists
    purely to establish connectivity between `stackup` roles, not to be
    reported on directly.
  - `layer` (string, required) — the GDS layer carrying this via's drawn
    geometry.
  - `between` (array of exactly two distinct `stackup` names, required) —
    which two roles this via connects.

A `stackup`/`vias` entry naming a layer absent from the given layout is not
itself an error (a shared spec can list layers a particular fixture doesn't
use) — matching `klt power`'s own convention.

## Connectivity model

Connectivity is traced with `klayout.db.LayoutToNetlist`, used purely for
wire/via connectivity — no device recognition is registered, unlike `klt
extract`'s deck-based extraction. This is the same API `extract.py`'s own
metal/via connectivity graph and `klt power`'s resistive-network extraction
already use, scoped down to only the layers this spec declares — and the
"LVS's shared net extraction" [#861](https://github.com/2AMLogic/klayout-tools/issues/861)
(Phase 1c) names as the connectivity model this phase builds and that phase
reuses.

For every net the extraction discovers whose geometry includes the declared
gate-role layer (`stackup[0]`):

1. **`gate_area_um2`** is that net's own merged area on the gate-role
   layer (`LayoutToNetlist.polygons_of_net`, merged into maximal polygons,
   scaled by the layout's own `dbu`).
2. **`levels[]`** walks `stackup` in array order — the declared fabrication
   order — and for each role reports:
   - `step_area_um2` — that net's own merged area on this role's layer
     (exact polygon area, not a bounding-box approximation — unlike `klt
     power`'s resistor-network model, no rectangle/L-shape distinction
     applies here, since this phase only sums area).
   - `cumulative_area_um2` — the running sum of `step_area_um2` across
     every role from `stackup[0]` through this one, inclusive.

A gate net with no geometry above the gate layer (an "unstrapped" gate —
common for an isolated poly shape with no contact at all) still reports one
`levels[]` entry per `stackup` role, each with `step_area_um2: 0.0` and
`cumulative_area_um2` unchanged from the previous level — the accumulation
simply does not grow past the gate level. This is not an error: it is the
electrically correct answer for that net.

## JSON schema (the contract)

**JSON is the API.** See [`docs/json-contract.md`](../json-contract.md) for
the shared envelope (`schema_version`, error shape, exit codes).

```json
{
  "schema_version": 1,
  "file": "routed.gds",
  "spec": "erc.json",
  "gate_role": "poly",
  "gate_count": 1,
  "gates": [
    {
      "gate_id": "gate0",
      "net": "GATE_A",
      "gate_area_um2": 2.0,
      "levels": [
        { "layer": "poly", "step_area_um2": 2.0, "cumulative_area_um2": 2.0 },
        { "layer": "li1", "step_area_um2": 2.0, "cumulative_area_um2": 4.0 },
        { "layer": "met1", "step_area_um2": 1.5, "cumulative_area_um2": 5.5 },
        { "layer": "met2", "step_area_um2": 1.6, "cumulative_area_um2": 7.1 }
      ]
    }
  ]
}
```

| Field            | Type            | Description                                                                                    |
| ---------------- | --------------- | ------------------------------------------------------------------------------------------------ |
| `schema_version` | integer         | `1` (this phase's connectivity-model-only shape; see "Phase scope" above for the additive fields to come). |
| `file`           | string          | The input layout path exactly as provided.                                                       |
| `spec`           | string          | The spec file path exactly as provided.                                                          |
| `gate_role`      | string          | The `stackup[0].name` value — the gate-role layer's own name.                                    |
| `gate_count`     | integer         | `len(gates)`.                                                                                     |
| `gates`          | array\<object\> | One entry per net with nonzero area on the gate-role layer — see below.                          |
| `gates[].gate_id`| string          | `"gate<index>"`, ascending in internal net-id order (stable within one run, not guaranteed stable across `klt`/KLayout versions). |
| `gates[].net`    | string \| null  | The net's own label text, if any `stackup` role's `label_layer` carries one; `null` if unlabelled. |
| `gates[].gate_area_um2` | number   | This net's own merged area on the gate-role layer, in µm². Identical to `levels[0].cumulative_area_um2`. |
| `gates[].levels` | array\<object\> | One entry per `stackup` role, in fabrication order — see below.                                  |
| `levels[].layer` | string          | The contributing `stackup` role's own `name`.                                                     |
| `levels[].step_area_um2` | number   | This net's own merged area on this role's layer, in µm².                                         |
| `levels[].cumulative_area_um2` | number | Running sum of `step_area_um2` from `stackup[0]` through this role, inclusive, in µm².     |

## Exit codes

| Exit code | Meaning                                                                                              |
| --------- | ------------------------------------------------------------------------------------------------------ |
| `0`       | Success — at least one gate net was found and reported.                                              |
| `1`       | Failed to run: layout/spec file not found or unreadable, a malformed `stackup`/`vias` declaration, an ambiguous top cell (pass `--top`), or no net in the layout carries any geometry on the declared gate role at all. |
| `2`       | Usage error (argparse) — missing/invalid arguments.                                                    |

## See also

- [#713](https://github.com/2AMLogic/klayout-tools/issues/713) — the parent
  antenna + ERC signoff epic (later phases: the per-gate antenna-ratio
  verdict, the core ERC finding list).
- [#860](https://github.com/2AMLogic/klayout-tools/issues/860) — Phase 1b,
  the per-gate antenna-ratio check this module's connectivity model feeds.
- [#861](https://github.com/2AMLogic/klayout-tools/issues/861) — Phase 1c,
  the core ERC finding list (floating gate, missing tie, supply short) that
  reuses this model and `klt lvs`'s net extraction.
- [`docs/cli/power.md`](power.md) — `klt power`, the sibling Phase 1a
  connectivity-only verb this command's spec-file/phase-scope conventions
  deliberately mirror.
- [`docs/cli/drc.md`](drc.md)'s `"antenna"` check kind — a purely
  geometric, whole-cell (not net-aware) approximation of an antenna check
  that predates this connectivity model; `klt erc` is the net-aware
  successor this document's "Coverage" section anticipates.
