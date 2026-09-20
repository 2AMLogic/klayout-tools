# `klt erc`

Build the layer-by-layer connectivity model (issue
[#859](https://github.com/2AMLogic/klayout-tools/issues/859), Phase 1a), the
per-gate antenna-ratio verdict (issue
[#860](https://github.com/2AMLogic/klayout-tools/issues/860), Phase 1b), and
report the core ERC finding checks (issue
[#861](https://github.com/2AMLogic/klayout-tools/issues/861), Phase 1c) of
the antenna + ERC signoff epic
[#713](https://github.com/2AMLogic/klayout-tools/issues/713).

```
klt erc <file> <spec> [--top <cell>] [--pdk <name>] [--format text|json]
```

- `<file>` — path to a routed GDSII (`.gds`) or OASIS (`.oas`) layout, e.g.
  a `klt place-and-route` output.
- `<spec>` — path to a JSON **spec file** (see "Spec file" below): the
  gate/conductor stackup and the vias bridging it, plus (issue #861,
  optional) named nets and substrate/well ties to check.
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

## Phase scope: what `klt erc` is, and what has shipped so far

`klt erc`'s full, intended interface (per epic #713) is:

- **JSON in**: a routed layout, a netlist, and the PDK's antenna/ERC rule
  set.
- **JSON out**: a per-gate antenna-ratio verdict citing the specific PDK
  limit it was checked against, plus an ERC finding list (floating gates,
  unconnected/multiply-driven nets, missing substrate/well ties, supply
  shorts).

connectivity declaration; `gates[].levels[]` (`step_area_um2`/
`cumulative_area_um2` below) is the per-gate, per-fabrication-step
connected conductor area.

**Phase 1b (#860) adds the antenna-ratio verdict** on top of that
connectivity model: `--pdk` selects a real, source-cited PDK antenna-ratio
limit table (see "Antenna-ratio verdict" below), and every `levels[]` entry
gains `antenna_ratio`/`antenna_ratio_max`/`antenna_ratio_source`/`verdict`;
every `gates[]` entry gains an aggregate `antenna_verdict`.

**Phase 1c (#861) additively delivers `erc_findings`**: the four core
electrical-correctness rules — floating gate, unconnected/multiply-driven
net, missing substrate/well tie, supply short — computed from the same
connectivity model, plus two new optional spec sections (`nets`, `ties`).
See "ERC finding checks" and "Spec file" below.

**Phase 3 (#908) additively delivers `levels[].remedy`**: for every
`verdict: "violate"` level, a standard-fix recommendation — diode insertion
or layer jumping — naming the specific net and layer, so the violation is
directly actionable rather than just flagged. See "Antenna-violation fix
guidance" below.

**Issue #1968 additively delivers a top-level `status` and the shared
`provenance` block** — split out of #1959's correction comment, since
without either field this command's output could not be graded by `klt
signoff`. See "JSON schema" below.

**Issue #1979 (this document's current state) additively delivers the
optional `stackup[0].active_layer` spec field**: true `poly ∩ diff`
gate-area computation, correctly excluding tie-cell/decap/filler-cell poly
resistors (no gate oxide) from `gates[]` and the antenna-ratio denominator.
See "Gate area: `poly ∩ diff` vs. raw poly area" below.

Per [`docs/json-contract.md`](../json-contract.md)'s additive-envelope
design, none of 1b's, 1c's, Phase 3's, #1968's, or #1979's fields needed a
**`schema_version` bump**: every field 1a's own version of this document
promised is still exactly as documented, unchanged.

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

**"Includes the declared gate-role layer" is a layer test, not a "has gate
oxide" test.** By default, any net with poly on it qualifies as a `gates[]`
entry, whether or not that poly ever overlaps diffusion — a tie-cell
substrate/well-tap resistor body is drawn in poly and has no gate oxide at
all, but is indistinguishable from a real transistor gate by layer alone
(issue #1979). Supplying `stackup[0].active_layer` narrows gate
identification to `poly ∩ diff` instead, correctly excluding these —
see "Gate area: `poly ∩ diff` vs. raw poly area" below.

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
  - `active_layer` (string, `"<layer>/<datatype>"`, **optional, `stackup[0]`
    only**, issue #1979) — the diffusion/active layer. When supplied, gate
    identification and the antenna-ratio denominator (`gates[].gate_area_um2`)
    are computed from **`poly ∩ diff`** (a `klayout.db.Region` boolean AND
    between the net's own merged gate-role geometry and the whole-layout
    `active_layer` region) instead of raw poly-net area — see "Gate area:
    `poly ∩ diff` vs. raw poly area" below.

### Gate area: `poly ∩ diff` vs. raw poly area

By default (`active_layer` omitted), `gates[].gate_area_um2` is a net's own
raw merged area on the gate-role layer — **any** net that touches the
declared gate role is treated as a gate, including a poly shape that never
overlaps diffusion at all. That includes tie-cell substrate/well-tap
resistor bodies, decap-cell poly, and filler-cell poly bars: real,
intentional poly shapes with no gate oxide and therefore no antenna
mechanism, but indistinguishable from a real transistor gate net by layer
alone. On a routed design this produces **false antenna-ratio violations** —
a rail shared by one or two tie cells can report a `levels[].antenna_ratio`
well above a real gate's, purely because the tie cell's tiny poly-resistor
"gate area" makes an ordinary amount of connected metal look
disproportionately large (issue #1979).

Supplying `stackup[0].active_layer` fixes this: `gate_area_um2` (and
therefore every `levels[].antenna_ratio` for that net) is computed from
`poly ∩ diff` instead. A poly resistor with zero overlap against diffusion
gets `gate_area_um2 == 0` and is **excluded from `gates[]` entirely** — the
same zero-area skip that already excludes a net with no gate-role geometry
at all. A real transistor gate (poly over diffusion) is unaffected: its
`poly ∩ diff` area is the genuine gate-oxide area PAAR methodology actually
measures, so its ratio is unchanged (or, in practice, only ever shrinks
toward the physically correct value — raw poly area is always `>=`
`poly ∩ diff` area for the same net).

Only `gates[].gate_area_um2` and `gates[]` membership are affected.
`levels[0]` (the gate role's own fabrication-step entry — `step_area_um2`/
`cumulative_area_um2` at the gate level) is **not** changed by
`active_layer`: it still reports the net's raw poly area, matching every
other `stackup` level's step-area convention (see "Connectivity model"
below).

**Omitting `active_layer` leaves today's behaviour unchanged, with a caveat:
tie/decap/filler-cell antenna ratios reported this way are not physical.**
They do not indicate a real antenna-charge risk and should not be used to
justify a real P&R fix (diode insertion, layer jumping) — only that a poly
shape exists on a net with connected metal above it. Supplying
`active_layer` is the only way to get a genuinely physical gate-area
denominator from `klt erc`.
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

- `nets` (optional array, default `[]`, issue #861) — named nets to check
  for connectivity findings, mirroring `klt power`'s `power_nets` but with
  an added `kind`:
  - `name` (string, required) — the net name, matched against `stackup`
    label text the same way `klt power`'s `power_nets` are matched.
  - `kind` (`"signal"` | `"supply"`, optional, default `"signal"`) —
    classifies a two-net short (see "ERC finding checks" below):
    two shorted `"supply"` nets are `erc.supply_short`; any other
    combination is the more general `erc.multiply_driven_net`.
  - Omitted entirely -> `erc.unconnected_net`/`erc.multiply_driven_net`/
    `erc.supply_short` are never computed.
- `ties` (optional array, default `[]`, issue #861) — substrate/well tie
  declarations for the `erc.missing_tie` check:
  - `name` (string, optional, defaults to `"tie<index>"`) — echoed in each
    finding's `layer` field.
  - `well_layer` (string, `"<layer>/<datatype>"`, required) — the
    well/tub diffusion layer; each of its physically distinct (merged)
    shapes is checked independently.
  - `tap_layer` (string, `"<layer>/<datatype>"`, required) — the tap
    (substrate/well contact) layer expected inside each well shape.
  - `connect_to` (string, required) — the `stackup` role name the tap is
    wired up to (e.g. `"li1"`) — must name an entry in `stackup`.
  - `net` (string, required) — the net name (matched the same way as
    `nets[].name` above) the tap must ultimately reach.
  - Omitted entirely -> `erc.missing_tie` is never computed.

## Connectivity model

Connectivity is traced with `klayout.db.LayoutToNetlist`, used purely for
wire/via connectivity — no device recognition is registered, unlike `klt
extract`'s deck-based extraction. This is the same API `extract.py`'s own
metal/via connectivity graph and `klt power`'s resistive-network extraction
already use, scoped down to only the layers this spec declares (`stackup`,
`vias`, and — issue #861 — each `ties[]` entry's `well_layer`/`tap_layer`).
This is the "LVS's shared net extraction" issue #861's own description
names as the connectivity model Phase 1a builds and Phase 1c reuses: the
`erc_findings` checks below run against this exact same unified graph, not
a second extraction pass.

For every net the extraction discovers whose geometry includes the declared
gate-role layer (`stackup[0]`):

1. **`gate_area_um2`** is that net's own merged area on the gate-role
   layer (`LayoutToNetlist.polygons_of_net`, merged into maximal polygons,
   scaled by the layout's own `dbu`) — or, when `stackup[0].active_layer`
   is supplied, that same region intersected with the active/diffusion
   layer (`poly ∩ diff`, issue #1979; see "Gate area: `poly ∩ diff` vs.
   raw poly area" above). A net whose resulting area is `<= 0` is not a
   gate at all and is excluded from `gates[]` entirely.
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
simply does not grow past the gate level. This is not an error in the
connectivity model itself: see "ERC finding checks" below for when it
*is* reported as a finding.

## ERC finding checks (issue #861)

Four core electrical-correctness rules, each purely geometric/connectivity
-- no device recognition, matching this command's Phase 1a posture. Each
finding's shape is documented in "JSON schema" below; every rule ships
with a golden violate/pass layout pair in `tests/test_erc.py`.

- **`erc.floating_gate`** — a gate net (from `gates[]` above) whose
  accumulation stops immediately after the gate role: `step_area_um2 ==
  0.0` on every `stackup` level above `stackup[0]`. This is the electrical
  signature of an uncontacted/floating gate. Computed directly from
  `gates[]`; needs no `nets`/`ties` spec section.
- **`erc.unconnected_net`** — a declared `nets[]` entry that matches zero,
  or more than one, disconnected electrical island. Zero matches means
  nothing in the layout carries that net's label at all; more than one
  means the intended net is split into pieces that never actually touch.
- **`erc.multiply_driven_net`** / **`erc.supply_short`** — two *different*
  declared `nets[]` names whose matched geometry resolves to the very same
  electrical island (a short), reported per unordered pair. When both
  names are declared `"kind": "supply"`, this is the more severe
  `erc.supply_short`; any other combination (signal-signal or
  signal-supply) is `erc.multiply_driven_net`. KLayout's own
  `Net.expanded_name()` already joins every label attached to one shorted
  island into a single comma-separated name (e.g.
  `"SHORTED_VDD,VSS"`) — this check splits that string to recover which
  declared names collided.
- **`erc.missing_tie`** — for every physically distinct well/tub shape
  (one per merged polygon of a `ties[]` entry's `well_layer`), a tap must
  be drawn inside it (`tap_layer`) *and* that tap must be electrically
  connected — via this same connectivity graph, since the tap is wired to
  `connect_to`'s `stackup` region during registration — to the declared
  `net`. Both failure modes (no tap drawn at all, or a tap present but
  wired to the wrong net) are reported under this one rule id.

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

**The gate role itself (`stackup[0]`) is always `"unchecked"`.** Without
`active_layer`, its `antenna_ratio` is trivially `1.0`
(`cumulative_area_um2 == gate_area_um2` at that level by construction) —
and the source PDK table's own gate-layer rule measures a different
quantity (poly *perimeter*, not cumulative connected *area*) that this
area-only connectivity model does not compute, so it is left uncompared
rather than reported as a misleading trivial pass regardless. With
`active_layer` supplied, `levels[0]`'s own `step_area_um2`/
`cumulative_area_um2` still report the net's *raw* poly area (unchanged by
the fix — see "Gate area: `poly ∩ diff` vs. raw poly area" above), while
`gate_area_um2` is the smaller `poly ∩ diff` area, so this ratio is no
longer trivially `1.0`; it remains `"unchecked"` regardless, for the same
poly-perimeter-vs-area reason above.

Each `gates[]` entry also reports an aggregate `antenna_verdict`, rolled up
from its *graded* levels (`levels[1:]` — everything except the gate role
itself, which is always `"unchecked"` and never counts towards coverage
here):

- **`antenna_verdict: "violate"`** — at least one graded level violates.
  Wins outright regardless of any other level's coverage.
- **`antenna_verdict: "pass_partial"`** (issue #1997) — no graded level
  violates, at least one graded level passed, but at least one other
  graded level is `"unchecked"` — a genuine coverage gap, not a clean
  bill of health. The canonical example: a full sky130 stack through met5,
  since sky130's own antenna-ratio table (below) has no met3/met4/met5
  entries at all — only li1/met1/met2 are ever actually graded, so a
  stack that also declares met3-5 always reports `"pass_partial"`, never
  a plain `"pass"`, unless every graded level's role happens to be one the
  table covers.
- **`antenna_verdict: "pass"`** — every graded level was actually compared
  against a limit, and none violated. Reserved for a spec whose `stackup`
  declares only roles the selected PDK's table covers (or when every
  declared role's antenna ratio is otherwise fully graded).
- **`antenna_verdict: "unchecked"`** — no graded level was ever compared
  against a limit at all (e.g. `--pdk` was omitted, so every level
  including the graded ones comes back `"unchecked"`).

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

## Antenna-violation fix guidance (Phase 3, issue #908)

Every `levels[]` entry with `verdict: "violate"` gains a `remedy` object
naming one of the two standard antenna fixes — **diode insertion** or
**layer jumping** — for the specific net and layer that violated, so the
finding is directly actionable rather than just flagged. A non-violating
level's `remedy` is always `null` (including every level when `--pdk` was
omitted, since no level can be `"violate"` there).

The remedy choice is not a fixed default — it depends on whether an
adjacent `stackup` level actually has headroom to absorb more of this
net's routing:

- **`"layer_jumping"`** — recommended when both hold:
  - **the violation is layer-local**: the immediately preceding `stackup`
    level does not itself violate (`cumulative_area_um2` only ever grows
    going up the stack, so a violation already present one level down is
    already baked into every level above it — jumping cannot undo that).
  - **an adjacent level has margin**: the level immediately below or above
    reports `verdict: "pass"`, i.e. it has real headroom under its own
    limit. The higher neighbour is preferred when both qualify (continuing
    the route forward onto the next fabrication step is the more common
    real remedy); the lower neighbour is used when only it qualifies (e.g.
    the violating level is the last one in the stack, with nothing above
    it to jump to).
  - `target_layer` names which neighbour to route through instead.
- **`"diode_insertion"`** — the fallback for every other case: either the
  violation cascades from a lower level that already violates (jumping to
  a neighbour would not resolve the underlying excess), or no adjacent
  level has margin to redistribute onto. Diode insertion is the
  general-purpose fix — it bleeds off accumulated charge electrically
  regardless of which layer is at fault — so it is always a valid
  fallback. `target_layer` is `null` in this case.

See `klayout_tools.erc._antenna_remedy`'s own docstring for the exact
per-level logic, and `tests/test_erc.py`'s `# --- run_erc: antenna-
violation fix guidance` section for a golden case of each remedy type
(including the "last-level, lower-margin-only" `layer_jumping` case and a
cascading-violation `diode_insertion` case where no neighbour has margin
anywhere in the stack).

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
          "verdict": "unchecked",
          "remedy": null
        },
        {
          "layer": "li1",
          "step_area_um2": 2.0,
          "cumulative_area_um2": 4.0,
          "antenna_ratio": 2.0,
          "antenna_ratio_max": 75.0,
          "antenna_ratio_source": "https://github.com/google/skywater-pdk/blob/main/docs/rules/antenna/table-Ia-antenna-rules-s8d.csv rule '.li.1', 'Max EA/A w/o diode' column",
          "verdict": "pass",
          "remedy": null
        },
        {
          "layer": "met1",
          "step_area_um2": 1.5,
          "cumulative_area_um2": 5.5,
          "antenna_ratio": 2.75,
          "antenna_ratio_max": 400.0,
          "antenna_ratio_source": "https://github.com/google/skywater-pdk/blob/main/docs/rules/antenna/table-Ia-antenna-rules-s8d.csv rule '.met1.1', 'Max EA/A w/o diode' column",
          "verdict": "pass",
          "remedy": null
        },
        {
          "layer": "met2",
          "step_area_um2": 1.6,
          "cumulative_area_um2": 7.1,
          "antenna_ratio": 3.55,
          "antenna_ratio_max": 400.0,
          "antenna_ratio_source": "https://github.com/google/skywater-pdk/blob/main/docs/rules/antenna/table-Ia-antenna-rules-s8d.csv rule '.met2.1', 'Max EA/A w/o diode' column",
          "verdict": "pass",
          "remedy": null
        }
      ]
    }
  ],
  "erc_findings": [
    {
      "rule": "erc.floating_gate",
      "description": "gate net has no connected geometry above the gate layer (floating/uncontacted gate)",
      "net": null,
      "other_net": null,
      "gate_id": "gate1",
      "layer": "poly",
      "bbox": { "left": 10000, "bottom": 0, "right": 10500, "top": 1000 }
    }
  ],
  "erc_finding_count": 1,
  "status": "violations",
  "provenance": {
    "klt_version": "0.4.2",
    "klayout_version": "0.29.8",
    "pdk": { "name": "sky130", "source": "built-in", "version": null },
    "deck": null,
    "input": { "content_hash": "sha256:<hex>", "role": "layout" },
    "spec": { "content_hash": "sha256:<hex>" }
  }
}
```

A violating `levels[]` entry's `remedy` is populated instead of `null` —
e.g. a `layer_jumping` remedy (`li1` violates its own 75 limit, but the
carried-over cumulative area still clears `met1`'s much looser 400 limit,
so `met1` has margin):

```json
{
  "layer": "li1",
  "step_area_um2": 80.0,
  "cumulative_area_um2": 81.0,
  "antenna_ratio": 81.0,
  "antenna_ratio_max": 75.0,
  "antenna_ratio_source": "https://github.com/google/skywater-pdk/blob/main/docs/rules/antenna/table-Ia-antenna-rules-s8d.csv rule '.li.1', 'Max EA/A w/o diode' column",
  "verdict": "violate",
  "remedy": {
    "type": "layer_jumping",
    "net": "GATE_A",
    "layer": "li1",
    "target_layer": "met1",
    "justification": "net 'GATE_A' exceeds the antenna-ratio limit at 'li1' (ratio 81 > 75); 'met1' has margin (ratio 82 <= 400) -- route more of this net's connection through 'met1' instead of continuing to accumulate area on 'li1'"
  }
}
```

or, when the violation already originated at a lower level and so carries
forward regardless (a `diode_insertion` remedy):

```json
{
  "layer": "met1",
  "step_area_um2": 350.0,
  "cumulative_area_um2": 431.0,
  "antenna_ratio": 431.0,
  "antenna_ratio_max": 400.0,
  "antenna_ratio_source": "https://github.com/google/skywater-pdk/blob/main/docs/rules/antenna/table-Ia-antenna-rules-s8d.csv rule '.met1.1', 'Max EA/A w/o diode' column",
  "verdict": "violate",
  "remedy": {
    "type": "diode_insertion",
    "net": "GATE_A",
    "layer": "met1",
    "target_layer": null,
    "justification": "net 'GATE_A' exceeds the antenna-ratio limit at 'met1' (ratio 431 > 400); the violation is already present at the preceding stackup level ('li1'), so it carries forward regardless of how routing on 'met1' is redistributed -- insert an antenna diode on this net at 'met1' to bleed off accumulated charge"
  }
}
```

| Field            | Type            | Description                                                                                    |
| ---------------- | --------------- | ------------------------------------------------------------------------------------------------ |
| `schema_version` | integer         | `1`, unchanged since Phase 1a — Phase 1b's and Phase 1c's fields were both added additively (see "Phase scope" above). |
| `file`           | string          | The input layout path exactly as provided.                                                       |
| `spec`           | string          | The spec file path exactly as provided.                                                          |
| `pdk`            | string \| null  | The `--pdk` value exactly as provided; `null` if omitted.                                        |
| `gate_role`      | string          | The `stackup[0].name` value — the gate-role layer's own name.                                    |
| `gate_count`     | integer         | `len(gates)`.                                                                                     |
| `gates`          | array\<object\> | One entry per net with nonzero area on the gate-role layer — see below.                          |
| `gates[].gate_id`| string          | `"gate<index>"`, ascending in internal net-id order (stable within one run, not guaranteed stable across `klt`/KLayout versions). |
| `gates[].net`    | string \| null  | The net's own label text, if any `stackup` role's `label_layer` carries one; `null` if unlabelled. |
| `gates[].gate_area_um2` | number   | This net's own merged area on the gate-role layer, in µm² — or, when `stackup[0].active_layer` is supplied, that area intersected with the active/diffusion layer (`poly ∩ diff`, issue #1979). Identical to `levels[0].cumulative_area_um2` only when `active_layer` is omitted. |
| `gates[].antenna_verdict` | string | `"violate"` if any *graded* `levels[1:]` entry (excludes `levels[0]`, the gate role, which is always `"unchecked"`) violates; else `"pass_partial"` if at least one graded level passed but at least one other graded level is `"unchecked"` (issue #1997 — a genuine coverage gap, e.g. met3-5 on a full sky130 stack); else `"pass"` if every graded level passed; else `"unchecked"`. |
| `gates[].levels` | array\<object\> | One entry per `stackup` role, in fabrication order — see below.                                  |
| `levels[].layer` | string          | The contributing `stackup` role's own `name`.                                                     |
| `levels[].step_area_um2` | number   | This net's own merged area on this role's layer, in µm².                                         |
| `levels[].cumulative_area_um2` | number | Running sum of `step_area_um2` from `stackup[0]` through this role, inclusive, in µm².     |
| `levels[].antenna_ratio` | number   | `cumulative_area_um2 / gate_area_um2` for this level. `1.0` at `stackup[0]` (the gate level) when `active_layer` is omitted; otherwise reflects the raw-poly-vs-`poly ∩ diff` area difference (see "Gate area: `poly ∩ diff` vs. raw poly area" above) — always `"unchecked"` there regardless. |
| `levels[].antenna_ratio_max` | number \| null | The resolved PDK limit for this role, or `null` when unchecked (no `--pdk`, the gate level, or an unrecognised role name). |
| `levels[].antenna_ratio_source` | string \| null | A citation for `antenna_ratio_max` (source URL + rule id + column), or `null` when unchecked. |
| `levels[].verdict` | string        | `"pass"`, `"violate"`, or `"unchecked"` — see "Antenna-ratio verdict" above.                      |
| `levels[].remedy` | object \| null | Fix guidance (issue #908, Phase 3) — `null` unless `verdict == "violate"`. See "Antenna-violation fix guidance" above and below. |
| `remedy.type`    | string          | `"layer_jumping"` or `"diode_insertion"` — see "Antenna-violation fix guidance" above for the selection rule. |
| `remedy.net`     | string \| null  | The violating gate's own net name — identical to `gates[].net`.                                  |
| `remedy.layer`   | string          | The violating `levels[].layer` value — identical to the entry this remedy is attached to.        |
| `remedy.target_layer` | string \| null | For `"layer_jumping"`, the adjacent `stackup` role name to route through instead; `null` for `"diode_insertion"`. |
| `remedy.justification` | string    | Human-readable explanation citing the specific ratio/limit values and (for `"layer_jumping"`) the target layer's own margin.     |
| `erc_findings`   | array\<object\> | One entry per ERC violation found by the four checks in "ERC finding checks" above (issue #861) — empty when clean, or when no `nets`/`ties` spec sections were provided (the `erc.floating_gate` check still always runs). |
| `erc_findings[].rule` | string     | One of `erc.floating_gate`, `erc.unconnected_net`, `erc.multiply_driven_net`, `erc.missing_tie`, `erc.supply_short` — matching `klt drc`'s `violations[].rule` convention. |
| `erc_findings[].description` | string | Human-readable explanation of this specific finding.                                       |
| `erc_findings[].net` | string \| null | The primary net name implicated (a `nets[].name`/`ties[].net` value, or a gate's own `gates[].net`). |
| `erc_findings[].other_net` | string \| null | The second net name implicated, for `erc.multiply_driven_net`/`erc.supply_short` only; `null` otherwise. |
| `erc_findings[].gate_id` | string \| null | The `gates[].gate_id` implicated, for `erc.floating_gate` only; `null` otherwise.           |
| `erc_findings[].layer` | string \| null | The `stackup`/`ties[].name` role implicated (`erc.floating_gate`'s gate role, or a tie's own `name`); `null` for the two net-connectivity rules. |
| `erc_findings[].bbox` | object \| null | Raw-database-unit `{"left", "bottom", "right", "top"}`, matching `klt drc`'s `violations[].bbox` convention; `null` when no single location applies (`erc.unconnected_net`/`erc.multiply_driven_net`/`erc.supply_short`, which can span disconnected geometry). |
| `erc_finding_count` | integer      | `len(erc_findings)`.                                                                              |
| `status`         | string          | (issue #1968; `"clean_partial"` added by #2115) `"violations"` if any connectivity/antenna finding exists; otherwise, per the [common rollup rule](../coverage-contract.md) (#2109) applied to `coverage`: `"not_checked"` if no antenna level was graded (known zero checked work), `"clean_partial"` if every graded level passed but some requested antenna work was skipped (e.g. a full sky130 stack whose met3-5 roles have no antenna-ratio limit), else `"clean"`. A roll-up of both independent violation signals this envelope carries, mirroring `klt drc`'s own `"clean"`/`"violations"` split. This is what `klt signoff` reads as this command's pass/fail verdict — `"clean_partial"` is not signoff's unconditional pass. |
| `provenance`     | object          | (issue #1968) The shared reproducibility block — see [`docs/json-contract.md`](../json-contract.md)'s "Shared `provenance` block". `provenance.input.content_hash` is `<file>`'s own hash; `provenance.pdk` is populated (`{"name": <pdk>, "source": "built-in", "version": null}`) only when `--pdk` was given, `null` otherwise — see that section's `klt erc` exception note on why `source`/`version` differ from every other verb's PDK-resolution-backed `provenance.pdk`. `provenance.deck` is always `null` (`klt erc` applies no rule/model deck). `provenance.spec.content_hash` (issue #2036) is `<spec>`'s own hash, in the same `sha256:`-prefixed form — the extra key `klt erc` carries because its verdict depends on two inputs, not one, and a report pinning only the layout can't be re-verified against the declarations it was actually run with. |

## Checked-work coverage

The [common v1 coverage contract](../coverage-contract.md) is additive to
this command's existing envelope.

`coverage.scope` is `antenna`. Checked IDs name each gate/non-gate level
actually compared against a limit. Missing PDK or level limits are skipped;
the gate reference level is inapplicable. Structural connectivity findings
remain independent.

`status` is derived by applying the [common rollup rule](../coverage-contract.md)
(#2109) to `coverage`, with any connectivity/antenna finding reported as
`failed` ahead of coverage, per that rule's precedence (#2115): any
finding/antenna violation yields `violations` and exit 3; otherwise known
zero graded antenna levels yields `not_checked` and exit 4; otherwise a
nonempty `coverage.skipped` (some requested antenna work — a level whose PDK
role has no antenna-ratio limit — was never graded) yields `clean_partial`
and exit 0; a fully-graded clean run yields `clean` and exit 0. `clean_partial`
is a real, successful result — it is simply not this command's unconditional
success token, and `klt signoff` grades it accordingly (see
[coverage-contract.md](../coverage-contract.md)'s "Signoff qualification").
Existing per-gate `pass_partial` (`gates[].antenna_verdict`, #1997) verdicts
are retained unchanged; this section is about the top-level `status` only.

Completed refusal/failure reports remain on stdout; actual invocation errors
retain exit 1 and the stderr error envelope.

## Exit codes

| Exit code | Meaning                                                                                              |
| --------- | ------------------------------------------------------------------------------------------------------ |
| `0` | `status: "clean"` (at least one antenna level was graded, with no antenna or connectivity finding) or `status: "clean_partial"` (every graded level passed, but some requested antenna work was skipped — #2115). |
| `1`       | Failed to run: layout/spec file not found or unreadable, a malformed `stackup`/`vias`/`nets`/`ties` declaration, an unrecognised `--pdk` name, an ambiguous top cell (pass `--top`), or no net in the layout carries any geometry on the declared gate role at all. |
| `2`       | Usage error (argparse) — missing/invalid arguments.                                                    |
| `3` | Antenna or connectivity violations. |
| `4` | No actual antenna checks; `status: "not_checked"`. |

**Gate on `status`, not the exit code.** These codes are additive — a
future release may add a new one above `4` — and the exit code is only a
shortcut derived from the payload's own `status` field, which is
authoritative. See [`docs/json-contract.md`](../json-contract.md#exit-codes)'s
"Exit codes" section.

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
  antenna + ERC signoff epic.
- [#859](https://github.com/2AMLogic/klayout-tools/issues/859) — Phase 1a,
  the `klt erc` interface and layer-by-layer connectivity model this
  document's "Connectivity model" section describes.
- [#860](https://github.com/2AMLogic/klayout-tools/issues/860) — Phase 1b,
  the per-gate antenna-ratio check ("Antenna-ratio verdict" above) this
  module's connectivity model feeds.
- [#861](https://github.com/2AMLogic/klayout-tools/issues/861) — Phase 1c,
  the core ERC finding list ("ERC finding checks" above).
- [#908](https://github.com/2AMLogic/klayout-tools/issues/908) — Phase 3,
  antenna-violation fix guidance ("Antenna-violation fix guidance" above).
- [#1968](https://github.com/2AMLogic/klayout-tools/issues/1968) — the
  top-level `status` and shared `provenance` block ("JSON schema" above).
- [#1979](https://github.com/2AMLogic/klayout-tools/issues/1979) — the
  optional `stackup[0].active_layer` `poly ∩ diff` gate-area fix ("Gate
  area: `poly ∩ diff` vs. raw poly area" above), shipped in this document's
  current form.
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
