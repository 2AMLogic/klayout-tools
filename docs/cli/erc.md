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
klt erc <file> <spec> [--top <cell>] [--pdk <name>] [--deck <name>]
                      [--findings-only] [--format text|json]
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
- `--deck` (issue #2204) — a curated extraction deck (currently: `gf180mcu`,
  `sg13cmos5l`, `sg13g2`, `sky130` — the same registry `klt extract
  --deck`/`klt lvs --deck` resolve, no PDK install needed) whose own
  device-body marker declarations are auto-detected and carved out of the
  matching `stackup`/`vias` role — see "Deck-driven device-marker
  auto-detection" below. Optional and independent of `--pdk` (which selects
  only the antenna-ratio limit table). Not validated by argparse: an
  unrecognised name is a clean exit-1 error, same convention as `--pdk`.
- `--findings-only` — report the `erc_findings` half only, skipping the
  per-gate per-level antenna accumulation that dominates runtime on a dense
  layout (issue
  [#2219](https://github.com/2AMLogic/klayout-tools/issues/2219)). Every ERC
  finding is identical to a full run; the antenna half reports `null`
  accumulation fields and `"unchecked"` verdicts. Mutually exclusive with
  `--pdk` (exit 1). See "Findings-only runs" below.
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

**Issue #1979 additively delivers the optional `stackup[0].active_layer`
spec field**: true `poly ∩ diff` gate-area computation, correctly excluding
tie-cell/decap/filler-cell poly resistors (no gate oxide) from `gates[]` and
the antenna-ratio denominator. See "Gate area: `poly ∩ diff` vs. raw poly
area" below.

**Issue #2179 (this document's current state) additively delivers
`erc_status` and `erc_coverage`**: the connectivity half's own verdict and
checked-work scope, beside the antenna-driven `status`/`coverage`. This
command answers two independent questions in one envelope, and only one of
them needs a PDK — see "Two verdicts: `status` (antenna) vs. `erc_status`
(connectivity)" below.

Per [`docs/json-contract.md`](../json-contract.md)'s additive-envelope
design, none of 1b's, 1c's, Phase 3's, #1968's, #1979's, or #2179's fields
needed a **`schema_version` bump**: every field 1a's own version of this
document promised is still exactly as documented, unchanged.

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
  - `well_layer` (string `"<layer>/<datatype>"` **or `null`**, required) —
    the well/tub diffusion layer; each of its physically distinct (merged)
    shapes is checked independently. `null` (issue #2255) declares that
    this block draws **no** well/tub layer for this tie at all — the
    native-substrate case — and requires `well_boxes` below to assert the
    region instead. The key itself is always required: the absence of a
    drawn well must be declared, never inferred from an omitted key.
  - `well_boxes` (optional array of `[left, bottom, right, top]` micrometre
    boxes, default `[]`, issue #2255) — a caller **assertion** of the
    substrate region this tie covers, for a block that draws no well/tub
    layer to name. Valid **only** with `well_layer: null`, and required
    with it (an empty/omitted list there is a spec error, not a tie that
    quietly checks nothing); declaring it alongside a drawn `well_layer` is
    an error too — to narrow a drawn well's taps, use `tap_boxes`. Unlike
    every other optional key on this entry, this one *substitutes* for a
    required field rather than narrowing one, so it is graded under its own
    coverage classification (`erc_coverage.checked_by_well_assertion`,
    separate from `tap_boxes`' `checked_by_assertion`) and carries its own
    falsifiability test: an asserted region indistinguishable from the whole
    top-cell extent is recorded as skipped work, never accepted as evidence
    (see "A block with no drawn well at all" below). Each box's
    `left`/`bottom`/`right`/`top` must satisfy `left < right` and
    `bottom < top`.
  - `tap_layer` (string, `"<layer>/<datatype>"`, required) — the tap
    (substrate/well contact) layer expected inside each well shape.
  - `tap_requires` (optional array of `"<layer>/<datatype>"`, default
    `[]`, issue #2169) — further layers intersected into the tap, so the
    tap this check looks for is the **boolean** a real PDK draws rather
    than one raw layer: `{"tap_layer": "22/0", "tap_requires": ["32/0"]}`
    is `Comp ∩ Nplus`. Without it, no single-layer value can name a real
    tap — an implant layer alone is not a conductor, and a
    diffusion/contact layer alone also matches every source/drain contact
    in the same well (see "Well/tap connectivity" below for why that
    matters). Same "second plain layer field, intersected at use time"
    shape as `stackup[0].active_layer`, generalised to a list; omitted ->
    `tap_layer` alone, unchanged.
  - `tap_is_dedicated` (optional boolean, default `false`, issue #2199) —
    assert that `tap_layer` already names a **tap-only** layer (sky130's
    own `tap`, a PDK tub-contact marker), so there is nothing for
    `tap_requires` to narrow. This changes no geometry; it is the
    declaration that keeps such a tie graded as *checked* work instead of
    the skipped-degenerate classification a bare `tap_layer` otherwise
    gets (see "Well/tap connectivity" below). Anything but a JSON boolean
    is an error rather than a coercion — a truthy `"false"` string quietly
    asserting the opposite of what it reads is the exact silent-pass shape
    this key exists to close.
  - `tap_boxes` (optional array of `[left, bottom, right, top]` micrometre
    boxes, default `[]`, issue #2234) — a caller **assertion** of exactly
    where the tap geometry is, intersected into `tap_layer` (composes with
    `tap_requires`, which narrows the same region). Unlike `tap_requires`/
    `tap_is_dedicated`, this needs no PDK marker layer to exist at all —
    the escape hatch for a stream whose taps are genuinely drawn but carry
    no distinguishing implant/marker layer at all (see "A tie with no
    distinguishing marker layer at all" below). Graded under its own
    coverage classification (`erc_coverage.checked_by_assertion`) rather
    than folded into an ordinary geometrically-derived pass, and held to
    the same degenerate/falsifiability test as every other narrowing form
    (see "A degenerate tie is reported as skipped, not as a pass" below):
    an assertion that removes nothing from the drawn `tap_layer` inside the
    well is exactly as degenerate as an omitted `tap_requires`. Each box's
    `left`/`bottom`/`right`/`top` must satisfy `left < right` and
    `bottom < top`.
  - `connect_to` (string, required) — the `stackup` role name the tap is
    wired up to (e.g. `"li1"`) — must name an entry in `stackup`.
  - `net` (string, required) — the net name (matched the same way as
    `nets[].name` above) the tap must ultimately reach.
  - Omitted entirely -> `erc.missing_tie` is never computed.
- `ties_disclosure` (optional object, default `null`, issue #2234) — a
  top-level statement that this run declares no tap, and why:
  - `reason` (string, required, non-empty) — free-text explanation, e.g.
    `"no implant layers are drawn on this stream"`.
  - `kind` (optional string, default `"unexpressible"`, issue #2247) —
    *which* obstacle is being disclosed. Exactly two values are accepted;
    anything else (including a near-miss like `"tool-limitation"`) is a spec
    error rather than a silent fallback to the default, because a typo that
    quietly downgraded one disclosure to the other would misdirect the
    reader of the report of record:
    - `"unexpressible"` — there is no tap to name: no narrowing marker, no
      dedicated tap layer, no nameable tap geometry. Cleared by drawing
      something. Records `"ties_disclosed_unexpressible"`.
    - `"tool_limitation"` — the tap *is* expressible, but the `klt` build
      this evidence has to be produced on cannot grade a declared tie
      safely. The reported instance is issue #2169: on a build whose tie
      extraction is not isolated from the primary connectivity graph, a
      correct `ties[]` declaration produces a false `erc.supply_short` on
      any routed design, so the honest thing a release-pinned flow can do is
      declare no tie and say why. Cleared by a different *build*, not by a
      redrawn layout. Records `"ties_disclosed_tool_limitation"`.
  - Changes no geometry and no finding. What it changes is the
    `erc_coverage.inapplicable` reason recorded for the undeclared
    `erc.missing_tie` work when `ties` is empty (one of the two tokens
    above, instead of `"no_ties_declared"`) — so a consumer (`klt signoff`'s
    T1 item 11, `docs/design-evidence-tiers.md`) can distinguish a
    considered, disclosed omission from "nobody declared ties at all", which
    previously rendered identically, *and* can tell the two disclosed
    obstacles apart.
  - **Neither kind is verified by this run, and neither can be.** A
    disclosure is the caller's word about their own stream or their own
    toolchain — which is exactly why it never moves a finding and why item
    11 stays unmet on both. What the report does pin down is the build that
    produced it: `provenance.klt_version`, so a reader can check for
    themselves whether a disclosed tool limitation applies to the run in
    front of them.
  - Echoed verbatim as the top-level `ties_disclosure` field, carrying
    `kind` only when the spec itself declared it (so a pre-#2247 spec's
    report is byte-identical); `null` when omitted.
- `devices` (optional array, default `[]`, issue #2183) — where a drawn
  **device body** sits on an already-declared conductor role, so the
  connectivity model stops reading it as a wire (see "Device bodies are
  not wires" below for what goes wrong without it):
  - `name` (string, optional, defaults to `"device<index>"`) — echoed in
    `provenance.devices`; must be unique within the array.
  - `body_layer` (string, `"<layer>/<datatype>"`, required) — the PDK's
    own device-body **marker** layer: gf180mcu's `Resistor`/`RES_MK`/`SAB`
    for a poly resistor, `CAP_MK`/`MIM_L_MK`/`FuseTop` for a MiM cap — the
    same markers the curated decks already use for device recognition and
    `klt extract` already consults. A `body_layer` absent from the given
    layout is not an error (matching `stackup`/`vias`' own convention); it
    subtracts nothing and reports `body_area_um2: 0.0`. A `body_layer`
    that *is* drawn here but does not overlap its declared `on` role
    reports the same `0.0` — that field is the intersection with the role,
    not the marker's own area (issue #2226) — plus a one-line warning on
    stderr, since that combination is a spec bug rather than an unused
    layer.
  - `on` (string, required) — which declared role that body's geometry is
    drawn on: either a `stackup` `name` (a poly resistor body, a fuse, a
    capacitor plate) **or** a `vias` `name` (a MiM/MOM cap whose whole
    plate-to-plate bridge between two declared metal roles *is* the via
    role's own geometry). That role's conductor region is registered with
    this entry's region subtracted; two entries sharing one `on` are
    unioned, not last-one-wins.
  - Omitted entirely -> no carve-out at all, i.e. the pre-#2183 behaviour
    (`gates[]`, the antenna verdicts, and every finding are unchanged).
  - An explicit entry always wins over a `--deck`-detected carve-out for the
    same role (issue #2204) — see "Deck-driven device-marker auto-detection"
    below.

## Connectivity model

Connectivity is traced with `klayout.db.LayoutToNetlist`, used purely for
wire/via connectivity — no device *extraction* is registered, unlike `klt
extract`'s deck-based extraction. This is the same API `extract.py`'s own
metal/via connectivity graph and `klt power`'s resistive-network extraction
already use, scoped down to only the layers this spec declares (`stackup`
and `vias`).

**That makes a drawn device body indistinguishable from a wire unless you
say where it is** — see "Device bodies are not wires (`devices[]`)" below
for the consequence (a false `erc.supply_short` on any rail-to-rail device
string) and for the `devices[]` declaration that fixes it.

`gates[]`, every antenna ratio derived from it, and the `nets[]`-driven
findings (`erc.unconnected_net` / `erc.multiply_driven_net` /
`erc.supply_short`) all come from that one graph. Since issue #2169 the
`ties[]` declarations are **not** part of it — see "Well/tap connectivity"
below.

### Known false-positive: diffusion/well continuity is not modeled (issue #2180)

**`erc.unconnected_net` can be a false positive for any declared net whose
real electrical continuity in silicon depends on diffusion/well material,
not just the declared `stackup`/`vias` conductors.** The graph above has no
well/substrate conductor in it at all — a well or tub region is never a
node in this graph, only `ties[]`'s own separate extraction ever looks at
one, and that extraction answers a narrower question ("is a tap present
and does it reach the declared net?", see "Well/tap connectivity" above)
than "does this well electrically merge two islands the primary graph sees
as disconnected?". It never does the latter, before or after #2169's fix.

The concrete failure shape: a guard-ring or n-well tap band strapped to a
device row's supply rail **through the well body itself** — not through a
metal jumper between the two tap sites — is genuinely one electrical node
in the fabricated part, but with no well conductor in the primary graph,
`klt erc` sees only the metal/via geometry each tap band happens to carry
and reports the net as two or more disconnected islands. A documented real
case: a guard-ring/n-well-tap-strapped supply net resolved to **3** separate
`erc.unconnected_net` islands under `klt erc`, on a layout whose independent
LVS run reported the identical net as **one**, zero-mismatch electrical
node. **Do not treat a multi-island `erc.unconnected_net` finding as a
confirmed design defect by itself** — cross-check against a real,
device-aware LVS run before acting on it (see also `docs/design-evidence-
tiers.md`'s item 11). The finding's `islands[]` array (issue #2194, see
"Locating the islands of a multi-island `erc.unconnected_net`" below) is
what makes that triage possible per island rather than per net: one
finding can cover a genuine floating-supply defect *and* a well-continuity
false positive on the same net, and only the per-island boxes let you tell
which is which.

**That cross-check is not automatically an independent confirmation,
either.** At least one widely used open-foundry LVS deck's connectivity
setup ends with a name-joining rule (e.g. `connect_implicit('*')`): two
physically disjoint supply islands that merely share a net *label* still
extract, compare, and report a clean match, even though nothing in the
drawn geometry actually ties them together. An LVS run built on a deck like
that does not independently verify this specific property — check what the
LVS deck's own connectivity setup actually does before trusting its "match"
verdict as proof that a `klt erc` multi-island finding is a false positive
rather than a real one.

This is a documented modeling limitation, not a commitment to a fix
timeline: expressing "this well/tap geometry conducts, scoped only to its
declared taps" as a general net-merging conductor (reusing `ties[]`'s
`well_layer`/`tap_requires` declaration shape) is a larger, separate
follow-on — see #2180 for the option this section defers.

**The mirror-image case is "Device bodies are not wires" below**: this
section is *too little* declared connectivity (real continuity the stackup
cannot see, producing a false `erc.unconnected_net`); that one is *too
much* (declared geometry that is a device rather than a wire, producing a
false `erc.supply_short`).

### Well/tap connectivity (`ties[]`, issue #2169)

A `ties[]` entry is evaluated in its own second extraction: the same
`stackup`/`vias` graph, plus one extra conductor per tie — that tie's
**tap sites**, i.e. `tap_layer` intersected with every `tap_requires`
layer and then clipped to the well itself (a tap is by definition inside
the well it taps), wired up to the tie's `connect_to` role.

Two properties follow, both deliberate:

- **A declared well is never itself a conductor.** It contributes only
  *where its taps are*, never across its plan-view extent, and two taps in
  the same well are not shorted to each other through it. Before #2169 the
  whole `well_layer` region was registered and self-connected, so a
  blanket well — one polygon spanning whole standard-cell rows — conducted
  to every shape that merely overlapped it in plan view. On a routed
  design that collapsed the layout into one or two electrical islands: a
  false `erc.supply_short` between VDD and VSS (via any ordinary CMOS
  output net with a contact in both wells), and a `gates[]` list collapsed
  to a single entry.
- **A `ties[]` declaration cannot affect anything but `erc.missing_tie`.**
  Because the primary graph above never sees the tie layers, `gates[]`,
  the antenna verdicts, and the `nets[]` findings are bit-identical
  whether `ties[]` is omitted, declared correctly, or declared with an
  over-broad `tap_layer`. A tie mis-declaration can therefore make the
  missing-tie answer wrong, but it can no longer silently invalidate the
  antenna half of the same report. `tests/test_erc.py` asserts this
  directly (`test_ties_never_alter_gates_or_net_findings`), together with
  the four-case table from issue #2169.

The cost of the isolation is one extra connectivity extraction, incurred
**only** when `ties[]` is non-empty; a spec without ties runs exactly one
extraction, as before.

**Still not modelled**: there is no device recognition and no layer
adjacency (z) rule here, so `tap_requires` (or `tap_is_dedicated`) is the
only way to say "this tap is a real tap". Express it — a single-layer
`tap_layer` naming a diffusion or contact layer will match every
source/drain contact inside the well, and those contacts are real
conductors, so `erc.missing_tie` will report the well as tied whenever any
of them happens to reach the declared supply.

A block sitting in a native substrate with no *drawn* well/tub layer is
**no longer unmodellable** (issue #2255): `well_layer` accepts `null`, with
`well_boxes` asserting the substrate region in its place — see "A block
with no drawn well at all" below for the form, its falsifiability test, and
what that costs the resulting evidence. What stays unmodelled is anything
that would *derive* such a region on the caller's behalf: there is no
implicit "the whole top cell is the substrate" region and no derived
`substrate = extent − nwell` boolean, both of which were considered and
rejected as unfalsifiable — they make `erc.missing_tie` satisfiable by any
contact anywhere that reaches the declared net, which is exactly the silent
pass the sections above exist to close.

#### A degenerate tie is reported as skipped, not as a pass (issue #2199)

A caller cannot always express the narrowing. `tap_requires` needs the
PDK's real tap boolean to be *drawn in the layout* — on gf180mcu that is
`Comp ∩ Nplus` / `Comp ∩ Pplus`, and a stream that draws no implant layers
at all (common for generated/full-custom analog whose implants are derived
downstream) has no geometry for it to intersect. The only declarable form
is then the bare `tap_layer`, which is exactly the form the paragraph above
says not to write.

So `klt erc` says so in the report instead of quietly passing. A tie is
**degenerate** when all three hold:

- the spec did not affirm `tap_is_dedicated`;
- the declared narrowing removed *nothing* from the drawn `tap_layer`
  inside the well. Omitting `tap_requires` is the usual way to land here,
  but a `tap_requires` that happens to intersect the whole drawn layer in
  *this* stream (an implant drawn over every contact, not only the taps) is
  exactly as unfalsifiable, so the test is geometric rather than a check
  for the key's presence;
- the resulting tap region is non-empty. An empty one cannot produce a
  false pass — every well in it is reported as having no tap drawn, which
  is an honest finding.

Such a tie's `erc.missing_tie` work is recorded in `erc_coverage.skipped`
with reason `degenerate_tap_declaration` (the skip record's work identity
names the tie), instead of in `checked`. The connectivity roll-up then
reports `erc_status: "clean_partial"` rather than `"clean"`, so committed
evidence carries the caveat and
[`docs/design-evidence-tiers.md`](../design-evidence-tiers.md)'s item 11
— "zero `erc.missing_tie`" — can no longer be satisfied by a declaration
that never looked at a tap (`klt signoff` renders that as
`supply_spec_incomplete`, the same reason it gives a spec that declared no
`ties[]` at all).

**`erc_findings` is unchanged by this.** The same findings are emitted for
the same geometry as before, for degenerate and well-formed declarations
alike; what changed is that the envelope now states that the check could
not be performed instead of letting a clean verdict stand for it. To clear
the skip, narrow the tap with `tap_requires`, or affirm `tap_is_dedicated`
when the layer really is tap-only.

#### A tie with no distinguishing marker layer at all (`tap_boxes`, `ties_disclosure`, issue #2234)

`tap_requires` and `tap_is_dedicated` both need *something drawn* to lean
on — a PDK implant layer to intersect, or a tap-only marker layer to
affirm. Neither exists for every stream: a stream whose taps are genuinely
drawn but carry **no distinguishing mark at all** (the implant-free
full-custom case the degenerate-tie section above describes) has nothing
true to narrow or affirm, so every `ties[]` entry for it lands degenerate.

Two ways to close that gap, each with a different cost:

- **`tap_boxes`** — a caller **assertion** of exactly where the tap
  geometry is, as a list of `[left, bottom, right, top]` micrometre boxes
  intersected into `tap_layer` (composes with `tap_requires`, which
  narrows the same region, but needs no PDK marker layer at all). This is
  caller assertion rather than layer-derived narrowing, so a non-degenerate
  asserted tie is graded under its own coverage classification —
  `erc_coverage.checked_by_assertion`, a list of the same `erc.missing_tie`
  work identities that also appear in `checked` — rather than folded into
  an ordinary geometrically-derived pass. It is held to the exact same
  falsifiability test as every other narrowing form: an assertion that
  removes nothing from the drawn `tap_layer` inside the well is exactly as
  degenerate as an omitted `tap_requires` (the test in "A degenerate tie is
  reported as skipped" above is geometric, not "which key was given"), and
  an assertion matching no drawn geometry at all is not a silent pass
  either — it produces the same honest "no tap drawn inside it"
  `erc.missing_tie` finding a real absent tap would.
- **`ties_disclosure`** — a top-level statement that this run declares no
  tap, and why, when even `tap_boxes` cannot name real geometry. This
  changes no finding and no geometry; it only changes the
  `erc_coverage.inapplicable` reason recorded for the undeclared
  `erc.missing_tie` work when `ties` is empty
  (`"ties_disclosed_unexpressible"` instead of `"no_ties_declared"`), so
  `klt signoff`'s T1 item 11 can render a distinct
  `supply_spec_disclosed_unexpressible` reason instead of the plain
  `supply_spec_incomplete` it gives an omission nobody considered — see
  [`docs/design-evidence-tiers.md`](../design-evidence-tiers.md) item 11.
  Item 11 still reports **unmet** either way: a disclosure proves nothing
  about the tap's actual connectivity, it only makes the reason honest.
  A second obstacle is disclosable the same way — `kind:
  "tool_limitation"`, when the tap *is* expressible but the build cannot be
  trusted to grade it; see "When the obstacle is the build, not the stream"
  below.

A worked `tap_boxes` declaration — a tap ring drawn on the transistor
active layer, with no implant anywhere in the stream, named one ring edge
at a time (a single box spanning the whole ring would also span everything
the ring encloses):

```json
{
  "ties": [
    {
      "name": "substrate_tie",
      "well_layer": "21/0",
      "tap_layer": "22/0",
      "tap_boxes": [
        [-10.7, -74.5, 191.3, -73.1],
        [-10.7, -33.1, 191.3, -31.7],
        [-10.7, -73.1, -9.3, -33.1],
        [189.9, -73.1, 191.3, -33.1]
      ],
      "connect_to": "metal1",
      "net": "vss"
    }
  ],
  "ties_disclosure": null
}
```

**Why boxes rather than cell names.** A `tap_cells` form (naming the cells
whose geometry is the tap) was considered alongside this one and is not
implemented, because it answers strictly fewer streams: a tap ring is
routinely drawn as *top-cell geometry* rather than as an instance — that is
exactly how the stream this feature was reported from draws both of its
rings — and a cell-name assertion has nothing to name there, while a box
assertion covers the hierarchical case too (a placement's own window is a
box). Nothing here forecloses adding one later: it would be another
narrowing input to the same tap region, graded by the same
`checked_by_assertion` classification and the same geometric degeneracy
test.

##### When the obstacle is the build, not the stream (`ties_disclosure.kind`, issue #2247)

Everything above is about a stream that has no tap to *name*. There is a
second reason a careful flow ends up with zero `ties[]`, and it is not the
same one: the tap is perfectly nameable, but the `klt` build the evidence
has to be produced on cannot grade a declared tie safely.

That is not hypothetical. Before issue #2169, a declared `ties[]` entry
joined its well/tap regions into the *same* connectivity graph the `nets[]`
findings are computed on, so a blanket well conducted across its whole
plan-view extent and any routed design collapsed into one or two electrical
islands — a false `erc.supply_short` between the supplies, from a correct
declaration. A flow pinned to a released build that predates the fix (the
deterministic thing to pin, and what
[`../../README.md`](../../README.md)'s pinned-install guidance recommends)
therefore has exactly two honest options: declare the tie and publish a
report with a supply short it knows is an artifact, or declare no tie.

Declaring no tie is the right call — but before #2247 it was
indistinguishable from the other two zero-`ties[]` states, and
`"unexpressible"` positively misdescribes it: it tells a reader to go draw a
tap that is already drawn. `ties_disclosure.kind: "tool_limitation"` is how
a spec says which obstacle it hit:

```json
{
  "ties": [],
  "ties_disclosure": {
    "kind": "tool_limitation",
    "reason": "klayout-tools#2169: on the pinned klt release a declared tie joins the well/tap regions into the primary connectivity graph and reports a false erc.supply_short; well-tie continuity is evidenced by the cited device-aware LVS match instead"
  }
}
```

The run records `"ties_disclosed_tool_limitation"` for the undeclared
`erc.missing_tie` work, and `klt signoff`'s T1 item 11 renders
`supply_spec_disclosed_tool_limitation` — a third reason, distinct from both
`supply_spec_incomplete` and `supply_spec_disclosed_unexpressible`.

**It is still unmet, on exactly the same principle.** A disclosure is a
statement about the caller's toolchain, not a computed `erc.missing_tie`
result, and this one cannot even be checked by the run it appears in. What
*is* checkable is the build that produced the report — `provenance.klt_version`
— so a reader can see for themselves whether the disclosed limitation
applies to the run in front of them. The distinction earns its keep by
naming the right remedy: re-run against a build whose tie extraction is
isolated and declare the tie, rather than go looking for a tap that is
already there.

#### A block with no drawn well at all (`well_boxes`, issue #2255)

Everything above relaxes how the **tap** side is expressed. The well side
stayed mandatory and drawn: `well_layer` parsed a `"<layer>/<datatype>"`,
so a block sitting in a **native substrate** — NMOS-in-bulk, the substrate
diffusion-derived rather than layer-marked, nothing drawn anywhere in the
stream to point at — could not declare its substrate tie in any form. Only
the drawn-well (n-well) half of such a design was ever graded, and
`ties_disclosure` could not cover the gap either: a disclosure describes
*undeclared* work, so a spec that declares its n-well tie and can express
nothing for its substrate had nothing to disclose.

The declarable form is `well_layer: null` plus a `well_boxes` list naming
the substrate region the tie covers, in the same `[left, bottom, right,
top]` micrometre boxes `tap_boxes` uses:

```json
{
  "ties": [
    {
      "name": "substrate_tie",
      "well_layer": null,
      "well_boxes": [
        [-10.7, -74.5, 191.3, -60.0],
        [-10.7, -45.0, 191.3, -31.7]
      ],
      "tap_layer": "22/0",
      "tap_boxes": [
        [-10.7, -74.5, 191.3, -73.1],
        [-10.7, -33.1, 191.3, -31.7]
      ],
      "connect_to": "metal1",
      "net": "vss"
    }
  ]
}
```

**This is not the `tap_boxes` mechanism applied one layer up, and the
difference matters.** `tap_boxes` *narrows* a required, drawn `tap_layer`:
it composes with geometry, and geometry is still what gets measured. This
*substitutes* for a required drawn layer, so nothing in the stream
corroborates the region at all. Three consequences follow, all deliberate:

- **The two forms are mutually exclusive.** `well_boxes` is valid only with
  `well_layer: null`, and required with it — an empty or omitted list there
  is a spec error. "There is no well and I am not asserting one" is already
  expressible (omit the entry, say why in `ties_disclosure`), and unlike
  this form that route cannot be mistaken for a graded check.
- **It has its own coverage bucket**, `erc_coverage.checked_by_well_assertion`
  — a list of the same `erc.missing_tie` work identities that also appear in
  `checked`, parallel to but separate from `tap_boxes`' own
  `checked_by_assertion`. A tie can appear in both (an asserted substrate
  region whose tap is also caller-named), in either, or in neither. They are
  not merged because they are different claims: one says *which drawn
  geometry is the tap*, the other says *where the substrate is*.
- **It has its own falsifiability test**, and it is not `tap_narrowed`.
  That test measures what an assertion removes from a drawn baseline; here
  there is no baseline to remove from. The equivalent bar is
  indistinguishability from "everything": **an asserted region that covers
  the top cell's own bounding box, to within 1% of its area, is recorded as
  skipped work** (`erc_coverage.skipped`, reason
  `degenerate_well_assertion`) rather than accepted as evidence — and the
  connectivity roll-up reports `erc_status: "clean_partial"`, exactly as it
  does for a degenerate tap declaration.

**Why the whole-extent form has to be rejected.** `erc.missing_tie` loops
over each merged polygon of the well region and asks "does this one contain
a tap that reaches the declared net?". One die-sized polygon reduces that to
"does *any* contact anywhere reach the declared net?" — satisfied by every
PMOS source sitting on the rail, tap or not. That is the same unfalsifiable
pass issue #2199 rejected on the tap side, one level up, and it is the
reason the two obvious alternatives (an implicit "the whole top cell is the
substrate" region, or a derived `substrate = extent − nwell` boolean) are
not implemented: both land in that shape by construction, the latter
whenever the n-wells are small relative to the block.

The corollary is that **evidence value grows with how finely the assertion
partitions the block**: each merged asserted polygon must independently hold
a tap that reaches the net, so two disjoint boxes are a strictly stronger
claim than one box spanning both, and adjacent boxes merge into one polygon
(assert the ring edges, not the ring's enclosing rectangle — the same
guidance the `tap_boxes` example above gives). The reverse also holds: an
asserted region the layout does not actually tie produces the ordinary "no
tap contact drawn inside it" `erc.missing_tie` finding, per asserted
polygon, exactly as an untied drawn well would. The assertion can be wrong,
and the run says so.

**A drawn `well_layer` is never subject to this test**, however much of the
block it covers. A blanket drawn well is a fact about the stream — checkable
by opening the GDS — not an unverifiable claim, so every spec written before
this feature grades exactly as it did.

`klt signoff`'s T1 item 11 reaches **met** on a substrate tie declared this
way, on the same terms as a drawn-well one: the assertion is falsifiable and
this command falsifies it where it can, and a degenerate one lands in
`erc_coverage.skipped`, which item 11 already refuses to read as a clean
missing-tie verdict. The citation's
`power_delivery.ties_checked_by_well_assertion` names which ties rested on an
asserted well, so the weaker provenance is stated in the verdict of record
rather than reachable only by re-opening the ERC envelope — see
[`docs/design-evidence-tiers.md`](../design-evidence-tiers.md) item 11.

### Device bodies are not wires (`devices[]`, issue #2183)

A conductor role carries *geometry*, and the model above has no way to tell
a wire from a **drawn device body** on the same layer: a poly resistor, a
poly fuse, a MiM/MOM capacitor plate. Whatever the body is electrically, it
conducts across its own extent in this graph.

**Without a `devices[]` declaration, that makes `erc.supply_short` a false
positive for any design whose topology deliberately spans two declared
supplies through a drawn device.** A supply-sensing resistive divider
across the rails is the defining topology of a power-on-reset comparator, a
brown-out detector, a supply-referenced bias string, and most start-up
circuits; on such a block, two `nets[]` entries with `"kind": "supply"`
report a short no matter how clean the layout is — the same mechanism
applies to a poly fuse, or to a MiM/MOM cap whose two plates sit on
declared metal roles bridged by a declared via role. **Until the device
bodies on the path are declared, read an `erc.supply_short` on such a block
as "the supply verdict is unavailable", not as a power-delivery defect.**

`devices[]` is how a spec declares them (schema in "Spec file" above). Each
entry names a device-body marker layer — the `RES_MK`/`SAB`/`Resistor`,
`CAP_MK`/`MIM_L_MK`/`FuseTop`-style layer the curated PDK decks already use
for device recognition, and which `klt extract` already consults — plus the
`stackup` or `vias` role that body is drawn on. That region is **subtracted
from the role's conductor region before it is registered**, so the body
breaks the net instead of bridging it. This is exactly the manual
derivation a caller would otherwise have to perform by pre-processing the
GDS (deleting the marked geometry and running against the edited stream),
promoted from a caller hack to a declaration — which matters because a
pre-processed stream means the committed report no longer describes the
committed layout, defeating the point of a content-hash-pinned artifact.

Three properties, all deliberate:

- **The carve-out is visible in the report.** `provenance.devices` echoes
  every declaration with the area it actually removed (`body_area_um2` —
  the marker **intersected with the `on` role's own conductor region**,
  issue #2226, not the marker layer's own area), so a declaration that
  silently matched nothing — wrong datatype, wrong `on` role, marker layer
  absent from this stream — is distinguishable from one that bit, and two
  runs of the same layout that disagree about `erc.supply_short` carry the
  reason in the payload. A marker that is drawn on the stream but misses
  its declared role (`body_area_um2: 0.0` with geometry elsewhere) also
  warns on stderr; JSON goes to stdout only, so a piped report is
  unaffected.
- **It applies to both graphs.** The `ties[]` extraction (above) sees the
  same carve-out: a drawn resistor body is not a wire there either.
- **It cuts, so declare it where the device is.** Subtraction is purely
  geometric — a marker layer that over-covers real routing will break that
  routing's connectivity too, which typically shows up as a new
  `erc.unconnected_net` or `erc.floating_gate`. `body_area_um2` and the
  finding list are the cross-check; a marker that covers only the device
  body (the usual PDK convention) leaves everything else untouched.

**Still not modelled**: what the device *is*. Nothing here recognises a
resistor as a resistor, checks its terminals, or knows the body is
resistive rather than open — `devices[]` only removes the body from the
wire graph. A real device-aware read of the same layout is `klt extract` /
`klt lvs`'s job, and the two are complementary: LVS confirms the divider
exists and matches the schematic, `klt erc` confirms nothing *else* joins
the rails.

### Deck-driven device-marker auto-detection (`--deck`, issue #2204)

`devices[]` requires the caller to know, and correctly transcribe, the PDK's
device-body marker layer/datatype — `62/0` for gf180mcu's `Resistor`,
`RES_MK`/`SAB`, `CAP_MK`/`MIM_L_MK`/`FuseTop` for its MiM caps. That
information already exists in this repo's curated extraction decks, and a
mis-transcription fails *silently*: it subtracts nothing, and the only
signal is `provenance.devices[].body_area_um2 == 0.0`. `--deck <name>`
(currently: `gf180mcu`, `sg13cmos5l`, `sg13g2`, `sky130`) resolves one of
those curated decks — the same name-keyed registry `klt extract --deck`/`klt
lvs --deck` use, needing no PDK install — and reads its own device-marker
declarations directly, instead of asking the spec author to transcribe them.

**The matching rule.** A deck names its device-recognition layers as
`(layer, datatype)` pairs on `ResistorDevice`/`CapacitorDevice` entries (see
`src/klayout_tools/decks/extraction.py`), and this command's own `stackup`/
`vias` roles carry `(layer, datatype)` too. A deck device applies to a
declared role **only when the device's conducting-body layer equals that
role's layer/datatype exactly** — no name-guessing, no partial-overlap
heuristics:

- **`ResistorDevice`** — the conducting-body layer is `body` (e.g. Poly2 for
  gf180mcu's `ppolyf_u`, which is also that deck's own gate role). The
  subtracted region is `body & marker`, narrowed by `requires` (every layer
  must also cover it) and `excludes` (each subtracted) — the *same* region
  `klt extract`'s own resistor recognition computes, not a second,
  potentially drifting derivation.
- **`CapacitorDevice`** — two independent conducting-body layers, each
  checked separately:
  - `top_plate` (e.g. gf180mcu's `FuseTop`) — the recognised top-plate
    region, narrowed by `top_plate_requires`/`top_plate_excludes`.
  - `top_plate_via`, when the deck declares one (e.g. gf180mcu's `Via4`) —
    **only** the geometric overlap between that via's own footprint and the
    capacitor's recognised bottom plate, exactly the region issue #364/#1388
    already exclude from `klt extract`'s own generic via connectivity — not
    the whole via layer. A capacitor's `top_plate_via` is typically *also*
    the deck's ordinary inter-metal via layer (gf180mcu's `Via4` both lands
    a MiM cap's top plate on Metal5 and routes ordinary Metal4↔Metal5 vias
    everywhere else), so cutting the entire layer would silently disconnect
    every legitimate via on it, not just the ones under a capacitor.
  - `bottom_plate` is **not** a matched layer: unlike a resistor body or a
    MiM top plate, a capacitor's bottom plate is ordinary conductor that
    genuinely carries the same net's real routing (`klt extract` ties it
    into the metal's own connectivity node rather than cutting it out) —
    subtracting it here would introduce a false disconnect, not fix one.

Only `ResistorDevice`/`CapacitorDevice` are matched today — `BipolarDevice`/
`DiodeDevice`/`MomCapacitorDevice` are a candidate follow-on, not a silent
omission.

**Visibility, both ways.** A deck device whose conducting-body layer matches
no declared role is still listed in `provenance.devices` (`"on": null`)
whenever that layer actually carries geometry on this layout — so a spec
that omits, or misnames, the role a real device sits on is visible rather
than silently invisible. A device whose layer carries *no* geometry at all
here is omitted outright: a curated deck ships dozens of resistor/capacitor
flavours a given design never draws, and listing every one of them on every
`--deck`-selected run would bury the signal this guarantee exists to
surface.

**Precedence.** An explicit `devices[]` entry for a role always wins over
this deck's own auto-detection for that role: the declared entry is the one
that actually cuts, and the deck-detected match for the same role is still
listed (`"body_area_um2": 0.0`, `"superseded_by"` naming the declared entry
that won) rather than silently dropped.

**Byte-identical when unused.** Omitting `--deck` (every caller before this
issue) leaves `gates[]`, every antenna ratio, every finding,
`provenance.deck`, and every `provenance.devices` entry's shape
byte-identical to a run before this feature existed — see "JSON schema"
below for exactly which fields are conditional on `--deck`.

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
  A multi-island finding says **where** each island is, not just how many
  there are (issue #2194): `islands[]` carries one
  `{"bbox", "layer", "shape_count"}` entry per island, and the finding's
  own `bbox` spans all of them — see "Locating the islands of a
  multi-island `erc.unconnected_net`" below.
- **`erc.multiply_driven_net`** / **`erc.supply_short`** — two *different*
  declared `nets[]` names whose matched geometry resolves to the very same
  electrical island (a short), reported per unordered pair. When both
  names are declared `"kind": "supply"`, this is the more severe
  `erc.supply_short`; any other combination (signal-signal or
  signal-supply) is `erc.multiply_driven_net`. KLayout's own
  `Net.expanded_name()` already joins every label attached to one shorted
  island into a single comma-separated name (e.g.
  `"SHORTED_VDD,VSS"`) — this check splits that string to recover which
  declared names collided. **A drawn device body between the two names
  produces this finding too**, since the body is a conductor in this graph
  — declare it in `devices[]` (see "Device bodies are not wires" above), or
  read the finding as unreliable for that block.
- **`erc.missing_tie`** — for every physically distinct well/tub shape
  (one per merged polygon of a `ties[]` entry's `well_layer` — or of its
  asserted `well_boxes`, issue #2255, for a block that draws none), a tap must
  be drawn inside it (`tap_layer`, narrowed by `tap_requires`) *and* at
  least one such tap must be electrically connected — via the tie
  connectivity graph, where the tap is wired to `connect_to`'s `stackup`
  region during registration — to the declared `net`. Both failure modes
  (no tap drawn at all, or taps present but none reaching the declared
  net) are reported under this one rule id. A well holding several taps
  passes as soon as *one* of them reaches the net; see "Well/tap
  connectivity" above for what this check does and does not model.

### Locating the islands of a multi-island `erc.unconnected_net` (issue #2194)

"This net resolves to 3 islands" is the alarm, not the answer: the islands
are not interchangeable, and one finding can cover both a genuine
floating-supply defect and a false positive (see "Known false-positive:
diffusion/well continuity is not modeled" above) on the same net. So a
multi-island finding carries the islands themselves:

```json
{
  "rule": "erc.unconnected_net",
  "description": "declared net 'VDD' resolves to 3 disconnected electrical islands (expected exactly one)",
  "net": "VDD",
  "other_net": null,
  "gate_id": null,
  "layer": null,
  "bbox": {"left": 5000, "bottom": 0, "right": 21000, "top": 1000},
  "islands": [
    {"bbox": {"left": 5000, "bottom": 0, "right": 6000, "top": 1000}, "layer": "li1", "shape_count": 1},
    {"bbox": {"left": 8000, "bottom": 0, "right": 11000, "top": 1000}, "layer": "poly", "shape_count": 2},
    {"bbox": {"left": 20000, "bottom": 0, "right": 21000, "top": 1000}, "layer": "li1", "shape_count": 1}
  ]
}
```

- `islands[]` has one entry per island, in the same order `klt erc` walks
  them internally (ascending KLayout cluster id) — deterministic for a
  given layout and spec, so two runs of the same input list them the same
  way.
- `islands[].bbox` is the island's whole extent, unioned across every
  `stackup` role it has geometry on, in the same raw-database-unit
  `{"left", "bottom", "right", "top"}` convention as `klt drc`'s
  `violations[].bbox`. It is what you point a layout viewer at.
- `islands[].layer` names the `stackup` role carrying the most of that
  island's area (ties broken by stackup order) — the single most useful
  layer to open first, not an exhaustive list of the roles it touches.
- `islands[].shape_count` is the number of merged polygons across those
  roles, which separates a one-shape orphan stub from a whole sub-block
  that failed to strap up.
- The finding's own top-level `bbox` is the box spanning every island, so a
  caller that only reads `bbox` still lands in the right part of the block.

`islands` is `null` for every other finding — including the *zero*-match
`erc.unconnected_net` case, which has no geometry to point at at all.

Because each island is located, two reports of the same net can be diffed:
going from 3 islands to 2 now says *which* island was resolved, instead of
leaving "the fix worked" and "the fix broke something else and merged a
different pair" indistinguishable.

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
  limits" below for the exact names it recognises). A `--findings-only`
  run (issue #2219) is likewise `"unchecked"` everywhere, and there
  `antenna_ratio` is `null` as well — the accumulation it would be derived
  from was never performed. See "Findings-only runs" below.

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
  including the graded ones comes back `"unchecked"`, or `--findings-only`
  skipped the accumulation outright).

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

## Two verdicts: `status` (antenna) vs. `erc_status` (connectivity)

`klt erc` answers **two independent questions** in one envelope, and since
issue #2179 each carries its own roll-up:

| Field | Question it answers | Needs a PDK antenna table? |
| ----- | ------------------- | -------------------------- |
| `status` | Both signals together: did every *graded* antenna level pass **and** are there no `erc_findings`? | Yes — it is `not_checked` when no level could be graded |
| `erc_status` | The `erc_findings` rules alone: `erc.unconnected_net`, `erc.multiply_driven_net`, `erc.supply_short`, `erc.floating_gate`, `erc.missing_tie` | No |

The distinction is not academic. `--pdk` resolves against a limit table
**this command carries for sky130 only** (see "Sky130 antenna-ratio limits"
above); any other PDK name is an exit-1 error, so a non-sky130 design is run
with `--pdk` omitted. Every level then comes back `verdict: "unchecked"`,
the antenna scope's checked work is known-zero, and the
[common rollup rule](../coverage-contract.md) correctly refuses to call that
a pass: `status: "not_checked"`, exit `4` — **for every layout on that PDK,
no matter what the design does**.

That is the right answer for the antenna question. It says nothing about the
connectivity rules, which are purely geometric/connectivity, need no `--pdk`
whatsoever, and run to completion on any PDK. `erc_status` is their verdict:

- **`erc_status: "clean"`** — every connectivity rule that ran passed
  (`erc_finding_count == 0`).
- **`erc_status: "violations"`** — at least one `erc_findings` entry.
  Antenna violations never appear here; they are `status`'s business.
- **`erc_status: "clean_partial"`** — no finding, but some requested
  connectivity work was skipped: today that means a **degenerate `ties[]`
  declaration**, whose `erc.missing_tie` verdict could not be told apart
  from one that never looked at a tap. Two forms, both under "Well/tap
  connectivity" above: a degenerate *tap* (issue #2199,
  `degenerate_tap_declaration` — the declared tap region is
  indistinguishable from an ordinary source/drain contact reaching the
  declared net) and a degenerate *well assertion* (issue #2255,
  `degenerate_well_assertion` — a caller-asserted substrate region
  indistinguishable from the whole top-cell extent). A successful, but not
  unconditional, result: read `erc_coverage.skipped` for which tie, and
  which of the two.
- `"not_checked"` is a reachable token of the shared rollup vocabulary,
  listed for completeness: `erc_coverage` always grades at least one gate
  (a run with no gate net at all is exit 1), so a successful run reports
  one of the three above.

**Which one to gate on.** Gate a connectivity/structural-supply CI check on
`erc_status`; gate an antenna check on `status`. Do not derive either from
the exit code on a PDK without a limit table — the exit code follows
`status`, so it is permanently `4` there. And do not re-derive the
connectivity verdict from `erc_finding_count` by hand: `erc_status` is that
roll-up, computed once, so every caller reads the same rule.

[`docs/design-evidence-tiers.md`](../design-evidence-tiers.md)'s item 11
(power delivery, structural) grades exactly the `erc_findings` supply rules
— "those are the rules this item grades, not the report's overall `status`"
— and so is unaffected by a missing antenna table, before or after #2179.
`klt signoff`'s **envelope-aggregation** mode, which does grade an `erc`
citation on the envelope's own verdict, reads `erc_status` when `status` is
`not_checked`: see [`docs/cli/signoff.md`](signoff.md).

## Findings-only runs (`--findings-only`, issue #2219)

`klt erc`'s inner loop is the per-gate, per-level accumulation: for
**every** gate net, on **every** `stackup` role, a merged connected-area
measurement. It scales as *gate nets × stackup roles*, and on a dense
layout (the issue measured 16,640 gate nets × a four-role stackup at ~26
minutes single-threaded) it is the dominant per-gate cost.

A caller who only wants the `erc_findings` half pays all of it for nothing.
The structural supply read that
[`docs/design-evidence-tiers.md`](../design-evidence-tiers.md) item 11
grades — `nets[]` island/short checks plus `ties[]` — never reads an
accumulated area, and without a `--pdk` limit table the accumulation grades
nothing either: every level comes back `"unchecked"` and `status` is
`not_checked` regardless. `--findings-only` skips the walk:

```bash
klt erc routed.gds supply.erc.json --findings-only --format json
```

**What is unchanged.** Every ERC finding. All five rules still run, and
their output is identical field-for-field to the same run without the flag
— including `erc.floating_gate`, the one rule that reads the per-level
model. Its predicate ("no connected geometry on any role above the gate")
is evaluated directly off the same connectivity graph instead, stopping at
the first role that carries area rather than measuring every role, so the
answer is the same and the work is strictly less. `erc_findings`,
`erc_finding_count`, `erc_status`, and `erc_coverage` are therefore all
byte-identical to a full run, as are `gate_count` and every
`gates[].gate_id`/`gates[].gate_area_um2`.

**What changes.** Only the antenna half, and only to say honestly that it
did not run:

- `gates[].levels[]` still enumerates every `stackup` role in fabrication
  order, but `step_area_um2`, `cumulative_area_um2`, and `antenna_ratio` are
  **`null`** — the measurement was not taken. `null` rather than `0.0` on
  purpose: a zero area is itself a real, checkable measurement (it is
  exactly what `erc.floating_gate` keys off), so reporting zeros for work
  that never ran would make a skipped report indistinguishable from a
  layout of entirely floating gates.
- Every `levels[].verdict` is `"unchecked"` and every
  `gates[].antenna_verdict` is `"unchecked"`.
- Every non-gate level lands in `coverage.skipped` with reason
  `findings_only`, so the envelope names which antenna work was declined
  and why.
- `status` is `"not_checked"` and the exit code `4` — exactly the antenna
  answer a `--pdk`-less run already gives, not a new token. Gate on
  `erc_status`, as any `--pdk`-less run already must (see "Two verdicts"
  above).
- The top-level `findings_only` field is `true`, so a reader that finds a
  `null` accumulation can tell "this run declined to measure it" from a
  malformed report without inferring it from `coverage`.

**`--findings-only` and `--pdk` are mutually exclusive.** Passing both is a
contradiction — a limit table with nothing to grade — and is a clean exit-1
error rather than a silently ungraded antenna half returned to a caller who
asked for one. (If a wrapper always passes `--pdk`, drop it for the
findings-only invocation; that invocation's antenna answer was `not_checked`
either way.)

The flag is **opt-in and inert by default**: an invocation that does not
pass it produces exactly the report it always did, `findings_only: false`
included, with no field newly nullable.

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
  "erc_status": "violations",
  "status": "violations",
  "provenance": {
    "klt_version": "0.4.2",
    "klayout_version": "0.29.8",
    "pdk": { "name": "sky130", "source": "built-in", "version": null },
    "deck": null,
    "input": { "content_hash": "sha256:<hex>", "role": "layout" },
    "spec": { "content_hash": "sha256:<hex>" },
    "devices": []
  }
}
```

A run whose spec declares `devices[]` echoes what each declaration actually
removed from the connectivity graph:

```json
"devices": [
  {
    "name": "poly_resistor",
    "body_layer": "62/0",
    "on": "poly",
    "body_area_um2": 3.6
  }
]
```

A run that additionally selects `--deck <name>` (issue #2204) populates
`provenance.deck` and adds `source`/`superseded_by` to every
`provenance.devices` entry — both hand-declared and deck-detected. Here an
explicit `devices[]` entry for `poly` wins over the deck's own
auto-detected `ppolyf_u` match for the same role:

```json
"provenance": {
  "deck": {
    "name": "gf180mcu",
    "content_hash": "sha256:<hex>",
    "released": true
  },
  "devices": [
    {
      "name": "my_resistor",
      "body_layer": "110/5",
      "on": "poly",
      "body_area_um2": 3.6,
      "source": "declared",
      "superseded_by": null
    },
    {
      "name": "ppolyf_u",
      "body_layer": "30/0",
      "on": "poly",
      "body_area_um2": 0.0,
      "source": "deck",
      "superseded_by": "my_resistor"
    }
  ]
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
| `findings_only`  | boolean         | (issue #2219) Whether `--findings-only` was passed — i.e. whether the per-gate antenna accumulation was skipped. `false` for every ordinary run. When `true`, `levels[].step_area_um2`/`cumulative_area_um2`/`antenna_ratio` are `null` and every level is in `coverage.skipped` for reason `findings_only` — see "Findings-only runs" above. |
| `gate_role`      | string          | The `stackup[0].name` value — the gate-role layer's own name.                                    |
| `gate_count`     | integer         | `len(gates)`.                                                                                     |
| `gates`          | array\<object\> | One entry per net with nonzero area on the gate-role layer — see below.                          |
| `gates[].gate_id`| string          | `"gate<index>"`, ascending in internal net-id order (stable within one run, not guaranteed stable across `klt`/KLayout versions). |
| `gates[].net`    | string \| null  | The net's own label text, if any `stackup` role's `label_layer` carries one; `null` if unlabelled. |
| `gates[].gate_area_um2` | number   | This net's own merged area on the gate-role layer, in µm² — or, when `stackup[0].active_layer` is supplied, that area intersected with the active/diffusion layer (`poly ∩ diff`, issue #1979). Identical to `levels[0].cumulative_area_um2` only when `active_layer` is omitted. |
| `gates[].antenna_verdict` | string | `"violate"` if any *graded* `levels[1:]` entry (excludes `levels[0]`, the gate role, which is always `"unchecked"`) violates; else `"pass_partial"` if at least one graded level passed but at least one other graded level is `"unchecked"` (issue #1997 — a genuine coverage gap, e.g. met3-5 on a full sky130 stack); else `"pass"` if every graded level passed; else `"unchecked"`. |
| `gates[].levels` | array\<object\> | One entry per `stackup` role, in fabrication order — see below.                                  |
| `levels[].layer` | string          | The contributing `stackup` role's own `name`.                                                     |
| `levels[].step_area_um2` | number \| null | This net's own merged area on this role's layer, in µm². `null` on a `--findings-only` run (issue #2219) — the accumulation was not performed. |
| `levels[].cumulative_area_um2` | number \| null | Running sum of `step_area_um2` from `stackup[0]` through this role, inclusive, in µm². `null` on a `--findings-only` run. |
| `levels[].antenna_ratio` | number \| null | `cumulative_area_um2 / gate_area_um2` for this level; `null` on a `--findings-only` run. `1.0` at `stackup[0]` (the gate level) when `active_layer` is omitted; otherwise reflects the raw-poly-vs-`poly ∩ diff` area difference (see "Gate area: `poly ∩ diff` vs. raw poly area" above) — always `"unchecked"` there regardless. |
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
| `erc_findings[].bbox` | object \| null | Raw-database-unit `{"left", "bottom", "right", "top"}`, matching `klt drc`'s `violations[].bbox` convention; `null` when no single location applies (`erc.multiply_driven_net`/`erc.supply_short`, and the *zero*-match `erc.unconnected_net`, which have no one place to point at). For a **multi-island** `erc.unconnected_net` (issue #2194) this is the box spanning every island — see `islands[]` below for the per-island boxes. |
| `erc_findings[].islands` | array\<object\> \| null | (issue #2194) One entry per disconnected electrical island, populated **only** for a multi-island `erc.unconnected_net`; `null` for every other finding (including the zero-match one). Entries are in ascending KLayout cluster-id order — deterministic for a given layout+spec. See "Locating the islands of a multi-island `erc.unconnected_net`" above. |
| `erc_findings[].islands[].bbox` | object \| null | That island's whole extent, unioned across every `stackup` role it has geometry on, in the same raw-database-unit convention as `erc_findings[].bbox`. Populated for every island of a net that resolved to geometry (a labelled net always has `stackup` geometry by construction). |
| `erc_findings[].islands[].layer` | string \| null | The `stackup` role carrying the most of this island's area (ties broken by stackup order) — the most useful layer to open a viewer on, not an exhaustive list of the roles it touches. |
| `erc_findings[].islands[].shape_count` | integer | Number of merged polygons this island has across the `stackup` roles — separates a one-shape orphan stub from a whole sub-block that failed to strap up. |
| `erc_finding_count` | integer      | `len(erc_findings)`.                                                                              |
| `erc_status`     | string          | (issue #2179) The **connectivity** half's own roll-up, graded on `erc_findings` and the `nets[]`/`ties[]` work actually declared (`erc_coverage`), independently of any antenna table: `"violations"` if `erc_finding_count > 0`, else `"clean_partial"` if any requested connectivity work was skipped (a degenerate `ties[]` declaration — a degenerate tap, issue #2199, or a degenerate well assertion, issue #2255), else `"clean"`. Antenna violations never appear here — see "Two verdicts: `status` (antenna) vs. `erc_status` (connectivity)" above for which field to gate on. Vocabulary is the shared rollup one, so `"not_checked"` is a reachable token a reader must accept, but a successful run reports one of the three above today. |
| `erc_coverage`   | object          | (issue #2179) `erc_status`'s own checked-work block, `scope: "connectivity"` — see "Checked-work coverage" below. |
| `ties_disclosure` | object \| null | (issues #2234, #2247) The spec's top-level `ties_disclosure`, echoed verbatim (`{"reason": <string>}`, plus `"kind": "unexpressible"\|"tool_limitation"` when the spec declared one); `null` when the spec did not declare one. See "A tie with no distinguishing marker layer at all" and "When the obstacle is the build, not the stream" above. |
| `status`         | string          | (issue #1968; `"clean_partial"` added by #2115) `"violations"` if any connectivity/antenna finding exists; otherwise, per the [common rollup rule](../coverage-contract.md) (#2109) applied to `coverage`: `"not_checked"` if no antenna level was graded (known zero checked work), `"clean_partial"` if every graded level passed but some requested antenna work was skipped (e.g. a full sky130 stack whose met3-5 roles have no antenna-ratio limit), else `"clean"`. A roll-up of both independent violation signals this envelope carries, mirroring `klt drc`'s own `"clean"`/`"violations"` split. This is what `klt signoff` reads as this command's pass/fail verdict — `"clean_partial"` is not signoff's unconditional pass. |
| `provenance`     | object          | (issue #1968) The shared reproducibility block — see [`docs/json-contract.md`](../json-contract.md)'s "Shared `provenance` block". `provenance.input.content_hash` is `<file>`'s own hash; `provenance.pdk` is populated (`{"name": <pdk>, "source": "built-in", "version": null}`) only when `--pdk` was given, `null` otherwise — see that section's `klt erc` exception note on why `source`/`version` differ from every other verb's PDK-resolution-backed `provenance.pdk`. `provenance.deck` (issue #2204) is populated the same `{name, content_hash, released}` way every other `--deck`-taking verb populates it, only when `--deck` was given; `null` otherwise (and always `null` before issue #2204, since `klt erc` applied no rule/model deck at all until then). `provenance.spec.content_hash` (issue #2036) is `<spec>`'s own hash, in the same `sha256:`-prefixed form — the extra key `klt erc` carries because its verdict depends on two inputs, not one, and a report pinning only the layout can't be re-verified against the declarations it was actually run with. |
| `provenance.devices` | array\<object\> | (issue #2183) One entry per `devices[]` declaration, in spec order — `{"name", "body_layer", "on", "body_area_um2"}`, where `body_area_um2` is the area this declaration **actually** subtracted from `on`'s conductor region — `area(marker ∩ on's own drawn region)`, **not** the marker layer's own area (issue #2226), since a device-body marker is conventionally drawn with enclosure past the conductor it marks. `0.0` therefore means this declaration changed nothing at all: its marker layer carries no geometry in this layout, is drawn on a different datatype, or does not touch the role it was declared `on` (that last case also warns on stderr). `[]` when the spec declares no `devices` and no `--deck` was selected. A carve-out changes which nets exist, and therefore which `erc.supply_short`/`erc.unconnected_net` findings are possible, so it has to be readable from the report rather than only from the spec. When `--deck` selects a curated deck (issue #2204), every entry — hand-declared and deck-detected alike — additionally carries `source` (`"declared"` \| `"deck"`) and `superseded_by` (`string` \| `null`, the hand-declared device name that pre-empted a deck-detected match for the same role); the deck's own matches are appended after the spec's declared entries, and a deck match whose conducting-body layer names no declared role appears with `"on": null`. Both keys are omitted entirely when `--deck` was not given — see "Deck-driven device-marker auto-detection" above. |

## Checked-work coverage

The [common v1 coverage contract](../coverage-contract.md) is additive to
this command's existing envelope. This command reports **two** scopes,
because it performs two independent bodies of checked work (issue #2179):

`coverage.scope` is `antenna`. Checked IDs name each gate/non-gate level
actually compared against a limit. Missing PDK or level limits are skipped
(`missing_antenna_pdk`/`missing_antenna_limit`), as is every non-gate level
of a `--findings-only` run (`findings_only`, issue #2219 — the caller
declined the accumulation, the same shape of caller-side skip as omitting
`--pdk`); the gate reference level is inapplicable.

`erc_coverage.scope` is `connectivity`, and grades the `erc_findings` rules,
which need no `--pdk` at all. One checked ID per subject actually checked:
per discovered gate (`erc.floating_gate`), per declared `nets[]` entry
(`erc.net_connectivity` — the `erc.unconnected_net`/
`erc.multiply_driven_net`/`erc.supply_short` rules all key off the same
declaration), and per declared `ties[]` entry (`erc.missing_tie`). A spec
that declares no `nets`/`ties` asked for none of that work, so those rules
are recorded as **inapplicable** (`no_nets_declared`/`no_ties_declared`,
or — when the spec's top-level `ties_disclosure` was given — one of
`ties_disclosed_unexpressible` (issue #2234) / `ties_disclosed_tool_limitation`
(issue #2247, `kind: "tool_limitation"`) in place of `no_ties_declared`),
never skipped —
an undeclared rule must not make this scope partial, and the distinction is
what lets a consumer tell "no supply was declared, so `erc.supply_short`
was never computed" from "the declared supplies came back clean" off the
envelope alone. The gate scope is never empty: a run in which no net
carries gate-role geometry is exit 1, not a zero-coverage report.

A declared `ties[]` entry is the one case this scope records as **skipped**:
work that was requested and could not be performed. Two reasons today —
`degenerate_tap_declaration` (issue #2199: the declared tap region is
indistinguishable from an ordinary source/drain contact) and
`degenerate_well_assertion` (issue #2255: a caller-asserted substrate region
is indistinguishable from the whole top-cell extent). The well test is
applied first when both would hold, since the tap narrowing is measured
inside the well region. Either is a requested skip, so it does make the
scope partial — see "A degenerate tie is reported as skipped, not as a
pass" and "A block with no drawn well at all" above.

`erc_coverage` additionally carries two assertion lists, both subsets of
`checked`, both `[]` when unused, and both purely informational — additive
to the four common-contract lists, so a consumer that only reads
`checked`/`skipped`/`inapplicable` sees an asserted tie exactly as it sees
any other checked, non-degenerate tie:

- `checked_by_assertion` (array\<string\>, issue #2234) — the work
  identities whose **tap** region was derived (at least in part) from a
  caller assertion (`ties[].tap_boxes`) rather than pure PDK-marker
  narrowing.
- `checked_by_well_assertion` (array\<string\>, issue #2255) — the work
  identities whose **well** region was itself asserted
  (`ties[].well_layer: null` + `ties[].well_boxes`), because the block
  draws no well/tub layer at all. Deliberately a separate list rather than
  more entries in the first: asserting which drawn geometry counts as the
  tap and asserting where the substrate is are different claims, and the
  second is the weaker one. A tie may appear in both.

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
are retained unchanged; this paragraph is about the top-level `status` only.

`erc_status` is derived the same way, from `erc_coverage` and
`erc_finding_count` alone — an antenna violation is not a `failed` input to
it (#2179). Because the two scopes are rolled up separately, a run on a PDK
with no antenna-ratio table reports `status: "not_checked"` (nothing was
graded, and nothing could be) alongside a real `erc_status: "clean"` /
`"violations"`, instead of collapsing both answers into one unusable one.

Completed refusal/failure reports remain on stdout; actual invocation errors
retain exit 1 and the stderr error envelope.

## Exit codes

| Exit code | Meaning                                                                                              |
| --------- | ------------------------------------------------------------------------------------------------------ |
| `0` | `status: "clean"` (at least one antenna level was graded, with no antenna or connectivity finding) or `status: "clean_partial"` (every graded level passed, but some requested antenna work was skipped — #2115). |
| `1`       | Failed to run: layout/spec file not found or unreadable, a malformed `stackup`/`vias`/`nets`/`ties` declaration, an unrecognised `--pdk` name, `--findings-only` together with `--pdk`, an ambiguous top cell (pass `--top`), or no net in the layout carries any geometry on the declared gate role at all. |
| `2`       | Usage error (argparse) — missing/invalid arguments.                                                    |
| `3` | Antenna or connectivity violations. |
| `4` | No actual antenna checks; `status: "not_checked"` — including every `--findings-only` run, which skips them by request. |

**Gate on `status`, not the exit code.** These codes are additive — a
future release may add a new one above `4` — and the exit code is only a
shortcut derived from the payload's own `status` field, which is
authoritative. See [`docs/json-contract.md`](../json-contract.md#exit-codes)'s
"Exit codes" section.

**The exit code answers the *antenna* question**, because `status` does. On
a PDK with no antenna-ratio table — every PDK but sky130 today, any run
that omits `--pdk`, and every `--findings-only` run — it is therefore `4`
for every layout, however clean the design is. That is not a signal that the run failed or that nothing was
checked: the connectivity rules ran, and their verdict is `erc_status` in
the payload. A caller that wants only the structural/connectivity read gates
on `erc_status` and ignores the exit code (see "Two verdicts" above); it
must *not* special-case exit `4` as success, which would also swallow a
genuine zero-coverage run.

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
  area: `poly ∩ diff` vs. raw poly area" above).
- [#2179](https://github.com/2AMLogic/klayout-tools/issues/2179) — the
  connectivity roll-up `erc_status` and its `erc_coverage` scope ("Two
  verdicts: `status` (antenna) vs. `erc_status` (connectivity)" above),
  shipped in this document's current form.
- [#2180](https://github.com/2AMLogic/klayout-tools/issues/2180) — the
  documented diffusion/well-continuity false-positive risk for
  `erc.unconnected_net` ("Known false-positive: diffusion/well continuity
  is not modeled" above).
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
