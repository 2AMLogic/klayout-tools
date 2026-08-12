# `klt erc`

Build the layer-by-layer connectivity model (issue
[#859](https://github.com/2AMLogic/klayout-tools/issues/859), Phase 1a) and
the per-gate antenna-ratio verdict (issue
[#860](https://github.com/2AMLogic/klayout-tools/issues/860), Phase 1b) of
the antenna + ERC signoff epic
[#713](https://github.com/2AMLogic/klayout-tools/issues/713).

```
klt erc <file> <spec> [--top <cell>] [--pdk <name>] [--format text|json]
```

- `<file>` — path to a routed GDSII (`.gds`) or OASIS (`.oas`) layout, e.g.
  a `klt place-and-route` output.
- `<spec>` — path to a JSON **spec file** (see "Spec file" below): the
  gate/conductor stackup, in fabrication order, and the vias bridging it.
- `--top` — top cell to analyse when the stream has more than one
  (**required** in that case — like `klt mom`/`klt power`, `klt erc` needs
  exactly one root to analyse, unlike `klt layers`, which defaults to
  summing across every top cell).
- `--pdk` — PDK antenna-ratio limit table each non-gate `stackup` level's
  `antenna_ratio` is compared against (currently: `sky130`). Optional — see
  "Antenna-ratio verdict" below for what happens when it's omitted. Not
  validated by argparse: an unrecognised name is a clean exit-1 error, like
  `klt drc`'s own `--deck`.
- `--format` — `text` (default, a human-readable summary) or `json`.

The command is headless (`klayout.db` batch API only, no GUI) and safe to
run in CI.

## Phase scope: what `klt erc` is, and what has shipped

`klt erc`'s full, intended interface (per epic #713) is:

- **JSON in**: a routed layout, a netlist, and the PDK's antenna/ERC rule
  set.
- **JSON out**: a per-gate antenna-ratio verdict citing the specific PDK
  limit it was checked against, plus an ERC finding list (floating gates,
  unconnected/multiply-driven nets, missing substrate/well ties, supply
  shorts).

**Phase 1a (#859) delivered the interface and the layer-by-layer
connectivity model.** The spec file's `stackup`/`vias` *are* the
connectivity declaration; `gates[].levels[]` (`step_area_um2`/
`cumulative_area_um2` below) is the per-gate, per-fabrication-step
connected conductor area.

**Phase 1b (#860, this document's current state) adds the antenna-ratio
verdict** on top of that connectivity model: `--pdk` selects a real,
source-cited PDK antenna-ratio limit table (see "Antenna-ratio verdict"
below), and every `levels[]` entry gains `antenna_ratio`/
`antenna_ratio_max`/`antenna_ratio_source`/`verdict`; every `gates[]` entry
gains an aggregate `antenna_verdict`. Per
[`docs/json-contract.md`](../json-contract.md)'s additive-envelope design,
this needed **no `schema_version` bump**: every field 1a's own version of
this document promised is still exactly as documented, unchanged.

**`erc_findings` (Phase 1c, [#861](https://github.com/2AMLogic/klayout-tools/issues/861))
is still not part of this phase's response.** It is expected to follow the
same shape as `klt drc`'s `violations[]` (a rule id, a description, and the
specific net/gate/layer implicated) — see #861 for the shipped shape once
it lands, again added additively with no `schema_version` bump.

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

## Antenna-ratio verdict (Phase 1b, issue #860)

For every non-gate `stackup` level (`stackup[1:]` — the gate role itself,
`stackup[0]`, is never PDK-checked; see below), `klt erc` derives
`antenna_ratio = cumulative_area_um2 / gate_area_um2` and, when `--pdk` is
given, compares it against that PDK's real antenna-ratio limit for the
level's own role `name`:

- **`verdict: "pass"`** — `antenna_ratio <= antenna_ratio_max`.
- **`verdict: "violate"`** — `antenna_ratio > antenna_ratio_max`.
- **`verdict: "unchecked"`** — no PDK limit was available for this level,
  either because `--pdk` was omitted (every level comes back `"unchecked"`
  in that case — `antenna_ratio` is still reported, just with nothing to
  compare it against) or because this role's `name` does not match any
  role the selected PDK's table defines (see "Sky130 antenna-ratio
  limits" below for the exact names it recognises).

**The gate role itself (`stackup[0]`) is always `"unchecked"`.** Its
`antenna_ratio` is trivially `1.0` (`cumulative_area_um2 == gate_area_um2`
at that level by construction), and the source PDK table's own gate-layer
rule measures a different quantity (poly *perimeter*, not cumulative
connected *area*) that this area-only connectivity model does not compute
— comparing a always-`1.0` ratio against that table's numeric limit would
be meaningless, so it is left uncompared rather than reported as a
misleading trivial pass.

Each `gates[]` entry also reports an aggregate `antenna_verdict`: `
"violate"` if any of its `levels[]` violate, else `"pass"` if any level
passed, else `"unchecked"`.

### Sky130 antenna-ratio limits

`--pdk sky130` uses the real SkyWater sky130 antenna-rule table, "MAX_EGAR"
(maximum effective-area/gate-area ratio *without* an antenna diode),
transcribed from the official PDK repository's own published rule tables:
[`google/skywater-pdk`, `docs/rules/antenna/table-Ia-antenna-rules-s8d.csv`](https://github.com/google/skywater-pdk/blob/main/docs/rules/antenna/table-Ia-antenna-rules-s8d.csv)
(the "Max EA/A w/o diode" column, fetched 2026-08-12).

| `stackup` role name | Limit | Source rule id |
| -------------------- | ----: | --------------- |
| `poly`                | *(never checked — see above)* | `.poly.1` |
| `li1`                  | 75    | `.li.1`   |
| `met1`                 | 400   | `.met1.1` |
| `met2`                 | 400   | `.met2.1` |

These limits are **verified stack-invariant**: identical across every
sky130 metal-stack option table checked (S8D, S8P/SP8P/S8P-10R,
S8TM/S8TMC/S8TMA-5R, S8P12/S8PIR/S8PF-10R, S8TNV-5R —
`docs/rules/antenna/table-I{a,e,c,g,b}-antenna-rules-*.csv` in the same
repository), so this one table applies to every sky130 stack variant, not
just S8D specifically. **met3-and-above limits vary by stack option**
(0.8–2.0× in the checked tables) and are **intentionally not
transcribed** — a candidate follow-on, not a silent omission, matching
[`decks/sky130.py`](../../src/klayout_tools/decks/sky130.py)'s own
convention of calling out every deliberately-uncovered rule.

`licon1`/`mcon`/`via1` also appear in the source table (`.licon.1`/
`.mcon.1`/`.via.1`) but can never produce a `levels[]` verdict here: those
are `klt erc` *via* roles (the spec's `vias[]` array), not `stackup`
roles, so they have no `levels[]` entry to attach one to.

An unrecognised `--pdk` name (anything other than `sky130`) is a clean
exit-1 error, checked eagerly before the layout is even loaded — see "Exit
codes" below.

## JSON schema (the contract)

**JSON is the API.** See [`docs/json-contract.md`](../json-contract.md) for
the shared envelope (`schema_version`, error shape, exit codes).

```json
{
  "schema_version": 1,
  "file": "routed.gds",
  "spec": "erc.json",
  "pdk": "sky130",
  "gate_role": "poly",
  "gate_count": 1,
  "gates": [
    {
      "gate_id": "gate0",
      "net": "GATE_A",
      "gate_area_um2": 2.0,
      "antenna_verdict": "pass",
      "levels": [
        {
          "layer": "poly",
          "step_area_um2": 2.0,
          "cumulative_area_um2": 2.0,
          "antenna_ratio": 1.0,
          "antenna_ratio_max": null,
          "antenna_ratio_source": null,
          "verdict": "unchecked"
        },
        {
          "layer": "li1",
          "step_area_um2": 2.0,
          "cumulative_area_um2": 4.0,
          "antenna_ratio": 2.0,
          "antenna_ratio_max": 75.0,
          "antenna_ratio_source": "https://github.com/google/skywater-pdk/blob/main/docs/rules/antenna/table-Ia-antenna-rules-s8d.csv rule '.li.1', 'Max EA/A w/o diode' column",
          "verdict": "pass"
        },
        {
          "layer": "met1",
          "step_area_um2": 1.5,
          "cumulative_area_um2": 5.5,
          "antenna_ratio": 2.75,
          "antenna_ratio_max": 400.0,
          "antenna_ratio_source": "https://github.com/google/skywater-pdk/blob/main/docs/rules/antenna/table-Ia-antenna-rules-s8d.csv rule '.met1.1', 'Max EA/A w/o diode' column",
          "verdict": "pass"
        },
        {
          "layer": "met2",
          "step_area_um2": 1.6,
          "cumulative_area_um2": 7.1,
          "antenna_ratio": 3.55,
          "antenna_ratio_max": 400.0,
          "antenna_ratio_source": "https://github.com/google/skywater-pdk/blob/main/docs/rules/antenna/table-Ia-antenna-rules-s8d.csv rule '.met2.1', 'Max EA/A w/o diode' column",
          "verdict": "pass"
        }
      ]
    }
  ]
}
```

| Field            | Type            | Description                                                                                    |
| ---------------- | --------------- | ------------------------------------------------------------------------------------------------ |
| `schema_version` | integer         | `1` (unchanged since Phase 1a — Phase 1b's fields were added additively; see "Phase scope" above). |
| `file`           | string          | The input layout path exactly as provided.                                                       |
| `spec`           | string          | The spec file path exactly as provided.                                                          |
| `pdk`            | string \| null  | The `--pdk` value exactly as provided; `null` if omitted.                                        |
| `gate_role`      | string          | The `stackup[0].name` value — the gate-role layer's own name.                                    |
| `gate_count`     | integer         | `len(gates)`.                                                                                     |
| `gates`          | array\<object\> | One entry per net with nonzero area on the gate-role layer — see below.                          |
| `gates[].gate_id`| string          | `"gate<index>"`, ascending in internal net-id order (stable within one run, not guaranteed stable across `klt`/KLayout versions). |
| `gates[].net`    | string \| null  | The net's own label text, if any `stackup` role's `label_layer` carries one; `null` if unlabelled. |
| `gates[].gate_area_um2` | number   | This net's own merged area on the gate-role layer, in µm². Identical to `levels[0].cumulative_area_um2`. |
| `gates[].antenna_verdict` | string | `"violate"` if any `levels[]` entry violates, else `"pass"` if any level passed, else `"unchecked"`. |
| `gates[].levels` | array\<object\> | One entry per `stackup` role, in fabrication order — see below.                                  |
| `levels[].layer` | string          | The contributing `stackup` role's own `name`.                                                     |
| `levels[].step_area_um2` | number   | This net's own merged area on this role's layer, in µm².                                         |
| `levels[].cumulative_area_um2` | number | Running sum of `step_area_um2` from `stackup[0]` through this role, inclusive, in µm².     |
| `levels[].antenna_ratio` | number   | `cumulative_area_um2 / gate_area_um2` for this level. `1.0` at `stackup[0]` (the gate level) by construction. |
| `levels[].antenna_ratio_max` | number \| null | The resolved PDK limit for this role, or `null` when unchecked (no `--pdk`, the gate level, or an unrecognised role name). |
| `levels[].antenna_ratio_source` | string \| null | A citation for `antenna_ratio_max` (source URL + rule id + column), or `null` when unchecked. |
| `levels[].verdict` | string        | `"pass"`, `"violate"`, or `"unchecked"` — see "Antenna-ratio verdict" above.                      |

## Exit codes

| Exit code | Meaning                                                                                              |
| --------- | ------------------------------------------------------------------------------------------------------ |
| `0`       | Success — at least one gate net was found and reported.                                              |
| `1`       | Failed to run: layout/spec file not found or unreadable, a malformed `stackup`/`vias` declaration, an unrecognised `--pdk` name, an ambiguous top cell (pass `--top`), or no net in the layout carries any geometry on the declared gate role at all. |
| `2`       | Usage error (argparse) — missing/invalid arguments.                                                    |

## Cross-checked against klayout's own built-in antenna engine

`klayout.db.LayoutToNetlist.antenna_check` is klayout's own, independently
implemented (C++ core) per-net antenna check — a genuinely separate
engine from this module's own manual `polygons_of_net`/area-accumulation
arithmetic. `tests/test_erc.py`'s
`test_antenna_verdict_agrees_with_klayout_builtin_antenna_check` runs it
directly against every golden violate/pass fixture (li1/met1/met2, both
outcomes) alongside `klt erc`'s own verdict and asserts agreement.

The epic's named corpus for this cross-check, the Tiny Tapeout corpus
([#520](https://github.com/2AMLogic/klayout-tools/issues/520)), is **not
usable yet**: #520 is itself an unimplemented, `loom:operator-only` epic —
no ingestion harness exists, and no Tiny Tapeout GDS is cached anywhere in
this repo (verified 2026-08-12). This is a documented discrepancy, not a
silently-skipped acceptance criterion: the golden-fixture cross-check
above against klayout's own antenna engine is what this repo can do
headlessly today; re-running it against the real #520 corpus once its
ingestion harness exists is a natural follow-on.

## See also

- [#713](https://github.com/2AMLogic/klayout-tools/issues/713) — the parent
  antenna + ERC signoff epic (later phases: the core ERC finding list).
- [#859](https://github.com/2AMLogic/klayout-tools/issues/859) — Phase 1a,
  the layer-by-layer connectivity model this module's antenna-ratio verdict
  is built on.
- [#861](https://github.com/2AMLogic/klayout-tools/issues/861) — Phase 1c,
  the core ERC finding list (floating gate, missing tie, supply short) that
  reuses this model and `klt lvs`'s net extraction.
- [#520](https://github.com/2AMLogic/klayout-tools/issues/520) — the Tiny
  Tapeout corpus epic named as this feature's cross-check corpus; not yet
  implemented (see "Cross-checked against klayout's own built-in antenna
  engine" above).
- [`docs/cli/power.md`](power.md) — `klt power`, the sibling Phase 1a
  connectivity-only verb this command's spec-file/phase-scope conventions
  deliberately mirror.
- [`docs/cli/drc.md`](drc.md)'s `"antenna"` check kind — a purely
  geometric, whole-cell (not net-aware) approximation of an antenna check
  that predates this connectivity model; `klt erc` is the net-aware
  successor this document's "Coverage" section anticipates.
