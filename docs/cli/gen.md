# `klt gen`

Run a named layout generator against a JSON `params` object and a PDK
reference, producing a GDS/OASIS stream plus a structured report. Phase 1 of
Epic #152 landed the request/response contract and a thin
[KLayout PCell](https://www.klayout.de/doc/programming/pcell.html) harness,
proven end-to-end with one reference generator (`resistor_strip`). Phase 2
adds the four analog primitive generators the accepted spike scopes:
`mos_array`, `res_array`, `guard_ring`, and `diff_pair`. Phase 4 (Epic #152)
adds `bjt_array`, a matched vertical-bipolar (PNP/BJT) array. Issue #568
adds `bond_pad`, the first generator covering the chip *boundary* rather
than a core analog device. Issue #1421 (this document's current state) adds
`well_island`, a family-3 sibling of `guard_ring` that binds its tie to a
caller-named net and isolates its well from a caller-named set of other
wells. See
[`docs/design/layout-generator-spike.md`](../design/layout-generator-spike.md)
section 2 for the contract this command implements, and section 4 for the
four phase-2 families' scope; see
[`docs/design/gen-bjt-array-spike.md`](../design/gen-bjt-array-spike.md) for
the phase-4 mechanism-choice decision behind `bjt_array`.

```
klt gen --list [--format text|json]
klt gen --list-pdk-pcells [--pdk <variant>] [--pdk-root <dir>]
                     [--format text|json]
klt gen <generator> [--params <path-or-inline>] [--pdk <variant>]
                     [--pdk-root <dir>] [--cell-name <name>] [-o/--output <path>]
                     [--format text|json]
klt gen --pdk-pcell <library>/<cell> [--params <path-or-inline>] [--pdk <variant>]
                     [--pdk-root <dir>] [--cell-name <name>] [-o/--output <path>]
                     [--format text|json]
```

- `--list` — enumerate available generators and their `params` schema, then exit.
- `--list-pdk-pcells` — enumerate the PCell libraries the **resolved PDK
  itself** ships (issue #1535), then exit. See
  [PDK-shipped PCells](#pdk-shipped-pcells-list-pdk-pcells--pdk-pcell).
- `--pdk-pcell <library>/<cell>` — instantiate a PCell from the resolved PDK's
  own library instead of a klt built-in generator. Mutually exclusive with
  `--list`, `--list-pdk-pcells`, and a positional `<generator>`.
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

## Generators

`klt gen --list` reports every generator's `params` schema as structured
data (see below) — the tables in this section are never hand-maintained
separately from the PCell declarations that back them
(`src/klayout_tools/gen.py`).

### `resistor_strip` (phase 1 reference generator)

A row of parametrized rectangles standing in for a unit-resistor string
([spike section 4.2](../design/layout-generator-spike.md#4-scope-proposal-first-generators)'s
family). It exists to prove the request → PCell → response loop end to end —
it has **no** well/tap/contact logic and is **not** claimed to be DRC-clean on
any PDK; it is also the only generator that is PDK-agnostic (its drawing
layer is a fixed placeholder, not resolved against a PDK family — see the
PDK-family note below). `res_array` (below) is its phase-2, DRC-clean
successor.

| `params` field | Type   | Default | Description                          |
| --------------- | ------ | ------- | ------------------------------------- |
| `length_um`      | double | `2.0`   | Unit resistor length (µm). Must be `> 0`. |
| `width_um`       | double | `0.42`  | Unit resistor width (µm). Must be `> 0`.  |
| `spacing_um`     | double | `0.42`  | Spacing between unit resistors (µm). Must be `>= 0`. |
| `num`            | int    | `4`     | Number of unit resistors. Must be `>= 1`. |

### PDK-family support (phase 2 generators)

`mos_array`, `res_array`, `cap_array`, `guard_ring`, `well_island`,
`diff_pair`, `esd_device`, `bond_pad`, and `bjt_array` (every generator except
`resistor_strip`, the deliberately PDK-agnostic phase-1 reference generator)
draw on their resolved PDK's own curated-DRC-deck layers (the same
layer/datatype pairs `klt drc --deck sky130`/`--deck gf180mcu`/`--deck
sg13g2` check — see [`klt drc`](drc.md)), so each one only supports the PDK
families whose curated deck it has been verified against — see the
per-generator table below. Any other resolved PDK family (or a family a
given generator has not been wired up for) is an application error (exit
code `1`) — including a family `klt pdk find`/`list`/`env` themselves
resolve successfully, since PDK *resolution* and generator layer-role
*support* are independent: the resolver is generic across families, but each
generator's layer lookup is a hardcoded per-family table (see "Adding a
third PDK family" below). Every generator's **documented default `params`**
are chosen to pass `klt drc --deck <family>` clean on every family listed for
it below; a non-default `params` set is not guaranteed to (see each
generator's "advisory, not authoritative" `drc_hints.notes` behaviour below).

**"yes" means `klt drc --deck <family>` clean, not necessarily signoff-clean
(issue #1575).** `klt drc --deck <family>` runs *this repo's own* curated
deck for that family — a deliberately partial starter subset of the PDK's
real design rule manual, not a drop-in replacement for the PDK's own
foundry-authored signoff deck (e.g. open_pdks' `run_drc.py`); see
[`klt drc`](drc.md)'s "Coverage" section for exactly which rule ids each
curated deck does and does not transcribe. A "yes" cell below is therefore a
narrower claim than "verified against the target PDK's own signoff tooling"
— no generator/family pair in this table has been checked against a PDK's
own official deck end to end, and the gap is not always benign: see the
`gf180mcu`/`mos_array` note immediately after the table for a documented,
concrete instance where curated-deck-clean output has real, reproducible
violations under gf180mcu's own signoff deck.

| Generator | `sky130` | `gf180mcu` | `sg13g2` | `sg13cmos5l` |
| --------- | :------: | :--------: | :------: | :----------: |
| `mos_array` | yes | yes | yes | yes |
| `res_array` | yes | yes | yes | yes |
| `cap_array` | yes | yes | yes | no |
| `guard_ring` | yes | yes | yes | no |
| `well_island` | yes | yes | no | no |
| `diff_pair` | yes | yes | yes | no |
| `esd_device` | yes | yes | no | no |
| `bond_pad` | yes | yes | no | no |
| `bjt_array` | yes | yes | no | no |

**gf180mcu — `mos_array`'s unit-device geometry was margined against the
six-rule signoff gap (issue #1577), but this remains an *unverified*
best-effort fix (issue #1575's gap is not confirmed closed).** #1575
originally found that `mos_array`'s documented default `params` pass `klt
drc --deck gf180mcu` clean, but running gf180mcu's own official signoff DRC
deck (open_pdks `gf180mcuD`, `libs.tech/klayout/drc/run_drc.py`, table
`main`, via the standalone KLayout application binary) against that same
default output found real violations of six rule ids: `DF.6_LV` ("COMP
extend beyond gate" / source-drain overhang, 0.24 µm minimum), `PL.4_LV`
(Poly2 extension beyond COMP / gate endcap, 0.22 µm minimum), `PL.5a_LV`/
`PL.5b_LV` (field Poly2 to unrelated/related COMP spacing, 0.10 µm minimum
each), `CO.7` (COMP contact to Poly2-on-COMP spacing, 0.15 µm minimum), and
`DF.12` (COMP not covered by Nplus/Pplus is forbidden — a coverage rule, not
a distance). All six rule ids are DRM rules this repo's curated `gf180mcu`
deck (`klayout_tools/decks/gf180mcu.py`) does not transcribe at all — see
[`klt drc`](drc.md)'s "Coverage" section, which lists exactly which
`PL.*`/`DF.*`/`CO.*` rule ids from the DRM's "7.5 Comp", "7.7 Poly2", and
"7.12 Contact" chapters *are* modeled (`PL.1`, `PL.3a`; `DF.1a`, `DF.3a`,
`DF.4d`; `CO.1`-`CO.4`, `CO.6`) — so `klt drc --deck gf180mcu` reporting
`status: "clean"` never exercised any of the six, and `mos_array`'s gf180mcu
geometry was never actually margined against them.

**What #1577 changed.** `mos_array`'s (and, since they compose the same
unit-device drawing, `diff_pair`'s/`esd_device`'s) gf180mcu geometry now
draws: a gate-pad clearance off the diffusion edge sized past
`PL.5a_LV`/`PL.5b_LV` (mirroring `sg13g2`'s own #1450 fix); a symmetric
bottom-edge gate endcap sized past `PL.4_LV`/`DF.6_LV` (the top edge already
cleared both via the #461 landing pad — only the bottom edge sat flush);
an extra S/D-contact-to-gate clearance floor sized past `CO.7`; a
source/drain implant (`Nplus`/`Pplus`, selected by `flavor`) fully covering
every drawn unit device's `Comp` body, closing `DF.12`'s unconditional
coverage requirement; and (for `voltage_flavor: "medium_voltage"`) a wider
`Dualgate` marker box sized past `DV.6`/`DV.8`. Each margin was sized from
the DRM's own published rule values (the same values #1575's own
reproduction cited above), not re-derived from a live signoff-deck run —
**this sandbox has no real gf180mcu `run_drc.py` to verify the fix against**
(the same limitation that made #1575 a documentation-only fix in the first
place). `klt drc --deck gf180mcu` — this repo's own curated, partial-subset
deck — stays clean, which was already true before this fix; that check
alone cannot confirm or deny signoff-deck cleanliness either way, since it
never transcribed any of the six rules to begin with.

**What #1580 changed (guard/tap/collector-ring `Comp` coverage).** #1577's
source/drain implant only covers each unit device's own drawn `active` body
— it does not reach `guard_ring`'s (and, since they compose the same
ring-drawing code, `mos_array`'s/`diff_pair`'s/`bjt_array`'s/`esd_device`'s)
own guard/tap/collector ring, which on gf180mcu shares the identical `Comp`
mask (no distinct tap layer in this curated deck) and so needs the same
`DF.12` coverage. Issue #1580 closes that gap: every one of those
generators now draws a second implant ring exactly coincident with the
tap/collector ring itself (never a blanket over the enclosed array/pair/
well, which would re-dope whatever a caller places inside it), gated on a
`ring_implant_present`-style flag that resolves `None` — byte-for-byte
unchanged geometry — on every other family. The doping selected depends on
whether the ring encloses a well: a ring drawn inside an enclosing Nwell
(`guard_ring`'s own `add_well` default, or a `flavor="pfet"` well on
`mos_array`/`diff_pair`) reuses the same `"well_tap_implant"` role (Nplus)
`well_island` already reuses for its own ring (issue #1421's precedent); a
ring with no enclosing well (a bare substrate tie, or `bjt_array`'s
collector ring, which is always composed outside the shared base well's own
footprint) instead gets the opposite doping, a `"pplus"` role (the same p+
implant #1577 added for a `flavor="pfet"` unit device's own source/drain) —
reusing the well-tie implant there would misrepresent a bare substrate
contact as a well tie. Like #1577, this margin was sized from the DRM's own
published `DF.12` rule text, not re-derived from a live signoff-deck run —
**this sandbox has no real gf180mcu `run_drc.py` to verify the fix
against**, the same limitation #1575/#1577 both carry. `klt drc --deck
gf180mcu` stays clean, which cannot itself confirm or deny signoff-deck
cleanliness either way.

**sg13g2 (IHP-Open-PDK, issues #1448/#1450/#1455).** `res_array`/`guard_ring`
(#1448), `mos_array`/`diff_pair` (#1450), and `cap_array` (#1455) are wired
up against this family's curated deck (`klayout_tools.decks.sg13g2`) today;
`bjt_array`/`esd_device`/`bond_pad`/`well_island` are not:

- `bjt_array` has no recognised device class in this deck's
  `EXTRACTION_DECK` at all (it declares `nfet`/`pfet`/three drawn poly
  resistors/two junction diodes/two MiM capacitors only — see that module's
  own "Device-class coverage" note — no bipolars). `cap_array` was
  originally excluded here too (`EXTRACTION_DECK.capacitors` was empty), but
  issue #1454 populated it (`cap_cmim`/`rfcmim`, once #1243 extended this
  deck's own metals/vias stack up through `Metal5`/`TopMetal1`) and issue
  #1455 wired the generator's own `cap_top_plate`/`cap_bottom_plate`/
  `cap_top_via`/`cap_top_via_metal` layer roles up to match (`MIM`/`Metal5`/
  `Vmim`/`TopMetal1`) — the drawn output classifies as the base `cap_cmim`
  flavour (never `rfcmim`, this generator's own `flavor`-less first
  increment).
- `mos_array` (and, since it composes `mos_array`'s own unit-device drawing,
  `diff_pair`) initially failed a **real** DRC check on this family:
  `gatpoly.separation.activ.1` (`Gat.d`, "minimum GatPoly space to unrelated
  Activ", 0.07µm). The unit device's gate-poly landing pad (issue #461 — a
  `CONTACT_SIZE_UM + 2*ENCLOSURE_MARGIN_UM` square, wider than the gate
  stripe it sits on) overhangs the channel gate on both sides so a contact
  can land outside the channel; with the pad sitting flush on the diffusion,
  that overhang's own underside edge faced unrelated `Activ` at zero lateral
  distance. Neither `sky130` nor `gf180mcu` transcribes an equivalent
  poly-to-unrelated-active spacing rule in this repo's curated deck, so the
  overhang had never tripped a DRC check before. **Issue #1450 fixed the
  geometry**: on a family that declares a gate-pad clearance
  (`gen.py`'s `_PDK_GATE_PAD_ACTIVE_CLEARANCE_UM` — `sg13g2`: 0.1µm, the
  only entry today), the landing pad is lifted that far clear of the
  diffusion edge and the gap is bridged by a poly stem of exactly the gate
  length, so no poly edge faces `Activ` at zero distance. On `sky130` and
  `gf180mcu` the clearance is `0`, so their drawn geometry and their
  `U<i>_G` port position/width are byte-for-byte unchanged. On `sg13g2` the
  reported `U<i>_G` port sits 0.1µm higher than the equivalent sky130 device
  (its *width* is still the landing pad's, unchanged).

`esd_device`/`bond_pad`/`well_island` were simply not attempted for
`sg13g2` by issue #1448 (no citable curated passivation-opening layer for
`bond_pad`; no verification effort spent on the other two) — a documented
gap, not a discovered defect, and open to a future contribution the same way
the rest of this section describes. `esd_device` draws the same unit device
`mos_array` does and so would likely inherit #1450's fix for free, but it
also composes a ring/marker stack that was not verified on this family, so
it stays deferred rather than shipped untested. The resolved PDK-family *name* also
differs from every other family here: IHP-Open-PDK's own install-directory
name (the `pdk.variant` string `klt pdk find` reports for a real fetched
install) is `"ihp-sg13g2"`, not a `"sg13g2"`-prefixed string the way
`"sky130A"`/`"gf180mcuC"` are literal prefixes of their own family names —
see "Family key and resolution" below.

`res_array` exposes all three of this family's recognised poly-resistor
classes (issue #1451): `"generic"` (`rsil`, 7Ω/□), `"rppd"` (260Ω/□) and
`"rhigh"` (1360Ω/□) — see the `res_array` `flavor` description below for the
mask set each one draws. sky130's differently-named `"high"`/`"xhigh"` are
still rejected there, with an error naming this family's own flavours.
`guard_ring`'s tap ring is drawn on `sg13g2`'s `Activ` layer (it
declares no distinct tap mask, the same "shared with transistor active"
situation `gf180mcu`'s own `Comp` reuse documents) — DRC-clean, but not
itself recognised as a distinct tap/tie device by `klt extract --deck
sg13g2` (no currently-supported family's `guard_ring` output is). `cap_array`'s
default output round-trips through `klt extract --deck sg13g2` to the
`"cap_cmim"` device class (issue #1455); `TopMetal1`'s own coarse 1.64µm
minimum-width DRC rule widens the drawn top-plate landing pad past the
generic default for this family only (see the `cap_array` section above).

**sg13cmos5l (IHP-Open-PDK's SG13G2_CMOS5L sibling, issue #1462).** Only
`mos_array`/`res_array` are wired up against this family's curated deck
(`klayout_tools.decks.sg13cmos5l`) — every other generator is deferred:

- `bjt_array`: this deck's `EXTRACTION_DECK` declares no bipolar device
  class at all (MOS and drawn poly resistors only), the identical gap
  `sg13g2`'s own entry documents.
- `guard_ring`/`diff_pair`/`well_island`/`esd_device`: this deck declares no
  distinct `tap` mask (like `sg13g2`), but unlike `sg13g2` — whose curated
  deck derives a well/substrate tie from plain `Activ ∩ nwell`, so an
  undoped tap ring is at least DRC-legal there — this deck's own tie
  derivation (`EXTRACTION_DECK.tap_nplus`/`.tap_pplus`) requires an
  `nSD`/`pSD` implant *on top of* the tap shape. Reusing `sg13g2`'s
  bare-`Activ` tap-ring pattern here unmodified would draw DRC-legal
  geometry that silently fails to extract as a recognised tie (an
  `unbiased_pmos_body_nets`/`device.body_unverified` surprise DRC would
  never catch). Issue #1462 does not attempt that geometry change — a
  follow-on issue that wires up any of these four generators for this
  family must add both a real `tap` role *and* the implant-covered tie
  geometry, then verify the result classifies as a tie under `klt extract
  --deck sg13cmos5l` rather than assuming parity with `sg13g2`'s simpler tap
  derivation. **`mos_array` itself was exactly this trap** (issue #1473):
  its own `flavor="pfet"` well shape drew a well with nothing tying it to a
  net, so `klt extract --deck sg13cmos5l` reported every generated PMOS
  body as unbiased. Fixed *without* adding a `tap` role — `mos_array` draws
  its own well-tie tap pad directly on the shared `active` role (see the
  `mos_array` section's own "Well-tie tap pad" note below) — so this list of
  four still-deferred generators, which *do* go through a `tap` role, is
  unaffected by that fix. `mos_array`'s own `add_guard_ring` (issue #1493)
  hits this identical `tap`-role gap, but — unlike the four generators
  above, which are unconditionally deferred for this family — only when a
  request actually sets `add_guard_ring: true`: every other `mos_array`
  request on this family (the overwhelming majority, since the param
  defaults `false`) never touches ring-layer resolution at all, so it is
  unaffected. A request that does set it gets this module's own clear
  `GenError` naming the family, not a crash.
- `cap_array`: this deck's module docstring states cmos5l has no MIM
  capacitor at all (a forbidden-layer requirement), so there is no
  `cap_top_plate`/`cap_bottom_plate` role for this generator to draw from.
- `bond_pad`: this deck cites no passivation-opening/pad-boundary layer, the
  identical `sg13g2` gap.

`mos_array`'s documented default `params` are DRC-clean against `klt drc
--deck sg13cmos5l` and re-extract to the `nfet`/`pfet` device class (this
deck also models an HV, `ThickGateOx`-gated MOS flavour, distinguished by
`sim.py`'s own MOS-model-binding table — `mos_array` does not draw that
marker itself, so its default output always extracts as the LV/default
pair). Unlike `sg13g2` (issue #1450), this deck transcribes no
`gatpoly.separation.activ.1`-shaped poly-to-unrelated-active spacing rule at
all, so `mos_array`'s unit device needs no per-family gate-pad clearance
here — verified against `klt drc --deck sg13cmos5l`, not assumed by analogy.
`res_array` exposes this family's three recognised poly-resistor classes —
`"generic"` (an alias of `"rsil"`, 7Ω/□, both spellings accepted and
resolving to the identical mask set), `"rppd"` (260Ω/□), and `"rhigh"`
(1360Ω/□) — transcribed byte-identically from `sg13g2`'s own table, since
cmos5l's resistor LVS rules are themselves symlinks into the pinned sibling
`ihp-sg13g2` checkout. The resolved PDK-family *name* also differs from
every other family here the same way `sg13g2`'s own does: the real
`ihp-sg13cmos5l` install's own directory name is `"ihp-sg13cmos5l"`, resolved
via a `_PDK_VARIANT_FAMILY_ALIASES` entry rather than a literal-prefix match
— see "Family key and resolution" below.

#### Adding a third PDK family

There is no generator flag, config, or fallback to a PDK's own native PCell
library today — supporting a new family means contributing that family's
entry to `src/klayout_tools/gen.py`'s `_PDK_ROLE_LAYERS` table (and, for
`res_array`'s poly-resistor flavours and `mos_array`'s/`diff_pair`'s
higher-voltage flavours, the companion `_PDK_RES_FLAVOR_LAYERS`/
`_PDK_VOLTAGE_FLAVOR_LAYERS` tables). Concretely:

- **Family key and resolution**: `_pdk_family()` maps a resolved
  `pdk.variant` string to a family key in `_PDK_ROLE_LAYERS` by delegating to
  `klayout_tools.pdk_models._pdk_variant_family()` (the same helper `sim.py`
  already imports directly for its own MOS-model-binding lookup) — a plain
  literal-prefix match against most variant strings (e.g. `"sky130A"` ->
  `"sky130"`, `"gf180mcuC"` -> `"gf180mcu"`), *or* an explicit
  `_PDK_VARIANT_FAMILY_ALIASES` entry for a variant whose resolved name is
  not a prefix of its own family key at all (`"ihp-sg13g2"` -> `"sg13g2"`,
  the exact shape a standalone, non-open_pdks-style PDK clone like
  IHP-Open-PDK's SG13G2 or its sibling SG13CMOS5L produces — see `klt pdk
  find`'s own flat-layout resolution in [`klt pdk`](pdk.md)). A brand-new
  family needs a new `_KNOWN_PDK_FAMILIES`
  entry in `pdk_models.py` (plus an alias there only if its resolved variant
  name isn't a literal prefix of the family key), not a change to `gen.py`
  itself.
- **Mandatory roles**: `active`, `poly`, `contact`, and `metal` are drawn by
  essentially every generator (the base MOS/resistor unit-device geometry)
  and must resolve to a real `(layer, datatype)` pair — a generator cannot
  produce a device at all without them.
- **Conditionally-required roles**: `well` (needed only when a generator
  draws a well-tap device, e.g. `guard_ring` or a `pfet`-flavoured
  `mos_array`/`diff_pair`), `tap` (substrate/well tap rings — always a real
  layer for every family a `guard_ring`-composing generator supports, even
  when it is the same physical layer as `active`, e.g. `gf180mcu`'s `Comp`
  or `sg13g2`'s `Activ`), `res_mark`/`bjt_mark` (device-class markers `klt
  extract`'s curated deck keys off of — omitting them means the drawn device
  extracts as a generic short/wire rather than its intended class),
  `metal_label` (the pin/label purpose of the `metal` role — `well_island`
  names its tie's net there) and `well_tap_implant` (only on a family whose
  well tie is drawn on the same layer as transistor active, so the implant
  is the only thing marking it as a tie — gf180mcu's `Nplus`; sky130 has a
  dedicated `tap` layer and needs none), and the routing-plane roles
  (`metal2`/`via1`, `metal3`/`via2`) `gen_compose`'s router resolves. Each
  role's purpose and the exact curated-deck citation it must match is
  documented inline as a comment on that role's entry in the existing
  `sky130`/`gf180mcu`/`sg13g2` blocks of `_PDK_ROLE_LAYERS` — follow those
  comments' citation style (point at the specific `klayout_tools.decks.<family>`
  rule or `EXTRACTION_DECK` entry the number comes from, never an unverified
  guess).
- **Optional roles**: any role a family's curated deck has no equivalent
  layer for (e.g. sky130's `_PDK_ROLE_LAYERS` entry has no `esd_mark`/
  `salicide_block`) may simply be omitted — `_role_layer_info()` already
  returns `None` for a missing key, and generators report the resulting
  "role not drawn on this family" state through `drc_hints.notes` rather
  than failing.
- **Family-specific *geometry*, not just layer numbers**: most generator
  geometry is deliberately family-agnostic (every shared margin constant in
  `gen.py` is sized to exceed *every* curated deck's equivalent rule), but a
  family whose deck checks a rule the others simply do not transcribe can
  need its own dimension. `_PDK_GATE_PAD_ACTIVE_CLEARANCE_UM` is the
  precedent (issue #1450): a small per-family table, defaulting to `0` — the
  pre-existing geometry byte-for-byte — for every family not listed, threaded
  to the PCell as a resolved hidden param exactly like
  `well_island`'s `well_margin_resolved_um`. Prefer that shape over changing
  a shared constant, which would move already-shipped families' drawn
  geometry and reported port coordinates for no DRC benefit on them.
- **A family entry does not have to support every generator on day one**:
  `_GENERATOR_FAMILY_DEFERRED` (plus a generator's own explicit missing-role
  check, e.g. `_cap_family_layers`'s/`_bond_pad_layer_params`'s) lets a
  specific generator reject a family that otherwise resolves fine —
  `sg13g2`'s own entry above is the precedent: a family can land with a
  strict subset of generators actually wired up, each one verified rather
  than assumed.
- **The bar for landing a generator against a family**: that generator's
  documented default `params` must pass `klt drc --deck <family>` clean, and
  — where the deck recognises a device class the generator's output should
  round-trip to — `klt extract --deck <family>` must recognise it as that
  class (`res_array`'s `sg13g2` support round-trips to `"rsil"`, mirroring
  `res_generic_po`/`ppolyf_u` on the other two families). A family/generator
  pair that does not clear this bar is deferred (see above), never shipped
  with an asterisk.

This is a scope statement for what a contribution needs to satisfy, not a
commitment that a fourth family or full `sg13g2` generator coverage is
currently planned or in progress.

### `mos_array` (family 1: matched transistor array)

> **gf180mcu note (issues #1575/#1577):** this generator's documented default
> `gf180mcu` output was margined against six real gf180mcu signoff-DRC rule
> ids `klt drc --deck gf180mcu` (this repo's own curated, partial-subset
> deck) does not check at all — but that margining is an **unverified**
> best-effort fix: this sandbox has no real gf180mcu `run_drc.py` to confirm
> it against. See the "gf180mcu — `mos_array`'s unit-device geometry was
> margined against the six-rule signoff gap" note in "PDK-family support"
> above for exactly what changed and what remains unverified.

A `rows` x `cols` grid of identical unit MOS-like devices (active/diffusion
strip + poly gate(s) + contact + local-metal source/drain pads), with
`dummy` extra unit-device columns flanking each side for etch/gradient
matching. On sky130 (issue #491), each dummy column's gate footprint is also
covered by the curated `dummy` marker layer `klayout_tools.decks.sky130`
declares, so `klt extract`'s dummy-device suppression (see "Dummy devices:
the `dummy` marker layer" in `docs/cli/extract.md`) drops it from the
extracted netlist instead of reporting it as a spurious unmatched device
under `klt lvs` — gf180mcu draws no equivalent marker (its curated deck
declares no `dummy` layer). For a single-finger unit device the (one) gate
finger carries a **poly landing pad** that extends one
`CONTACT_SIZE_UM + 2*ENCLOSURE_MARGIN_UM` (0.42 µm) square past the
diffusion's gate-side edge, so a contact can land on the gate *outside* the
channel — without it the gate poly shared the diffusion's exact extent and a
contact at the reported gate port straddled the diff edge
(`poly.enclosing.licon.1`/`diff.enclosing.licon.1`) with nowhere legal to sit
(issue #461). The reported `U<i>_G` port now sits at that pad's centre (its
`y_um` is offset past the diffusion edge, and its `width_um` reports the pad
width rather than `l_um`) — a JSON-contract-visible move of the gate port
coordinate. The opt-in `finger_topology: "series"` shape described below pads
every finger this way, not just the first (issue #781).

On a PDK family whose curated deck checks poly-to-*unrelated*-active spacing,
that pad is additionally stood off the diffusion edge by that family's
gate-pad clearance and the gap bridged by a poly stem of exactly `l_um` (see
"PDK-family support" above — `sg13g2`: 0.1 µm; `sky130`/`gf180mcu`: none, so
their geometry and `U<i>_G` are unchanged). The clearance is resolved from the
PDK, never a request param: a caller does not (and cannot) ask for it, but on
`sg13g2` the reported `U<i>_G` `y_um` includes it.

`gate_contact` (issue #492) finishes that stack rather than leaving it to the
caller: it draws a contact **and** a local-metal pad on the landing pad, and
reports `U<i>_G` on the `metal` role, symmetric with `U<i>_S`/`U<i>_D`. That
is what makes a gate reachable by [`klt gen-compose`](gen-compose.md)'s
router — a `connectivity[]` net naming a *bare-poly* gate port is rejected as
unroutable (no via in the metals stack lands on poly), so without it a gate
still has to be contacted by hand. (With the strapped `finger_topology:
"parallel"` shape below, the contact and pad land on the shared gate comb
instead of on a per-finger landing pad, already a full `0.4` µm clear of the
drain rail's metal.) Enabling it raises the landing pad's
contact region `0.4` µm clear of the diffusion edge before drawing on it: a
contact-enclosure metal square centred on the bare `#461` pad would share an
edge with the S/D local-metal pads either side and merge into one polygon,
shorting the gate to source/drain. The unit device (and, for `diff_pair`, its
automatically-sized guard ring) therefore grows taller, and `U<i>_G`'s `y_um`
moves with it. `gate_contact` defaults to `false`, which reproduces the
bare-poly gate byte-for-byte — a gate you intend to name with
`gen-compose`'s `pins[]` (on the `poly_label` layer) rather than route to
wants the default.

`fingers > 1` folds the unit device: `finger_topology` (issue #777) decides
what that means electrically. The default `"parallel"` draws the
conventional multi-finger device — the alternating source/drain segments are
strapped to a **source rail** below the diffusion and a **drain rail** above
it, and every gate stripe runs up past the drain rail into a shared **poly
comb**, so the unit is one device of width `fingers * w_um` (`klt extract`
reports `fingers` transistors between the same two S/D nets on one gate net,
which `klt lvs`'s
[`options.combine_devices`](lvs.md) folds into that single device). Every
rail, stub, and clearance uses the same `0.42` µm width / `0.4` µm spacing
budget the S/D pads use, so the strapping is DRC-clean on both curated decks;
the column pitch is unchanged and only the unit's height grows. The gate comb
crosses *under* the drain rail on `poly` — no contact, so no connection —
which is how a single-routing-metal generator gets both rails and a gate
terminal out of one cell.

`"series"` draws the pre-#777 bare stripes with no straps at all: `fingers`
transistors chained source-to-drain on `fingers` independent gate nets. Unlike
the pre-#781 shape, every finger is padded (the same #461 landing pad -- and,
with `gate_contact`, the same #492 contact + local-metal pad -- the first
finger always got) and every terminal is reported: with `fingers > 1` the unit
contributes `2 * fingers + 1` ports instead of three -- the `fingers + 1` S/D
segments as `U<i>_S<j>`/`U<i>_D<j>` (alternating west to east: segment `2j` is
`S<j>`, segment `2j + 1` is `D<j>`) and each finger's gate as `U<i>_G<j>`. The
two end segments keep the usual `180`/`0` `direction_deg` and the diffusion's
own width (`w_um`); the interior segments -- boxed in by a gate on either side,
so their only free approach is from below -- report `direction_deg: 270` and
the gate pad's own x-width (`CONTACT_SIZE_UM + 2 * ENCLOSURE_MARGIN_UM`, not
`w_um`). Choosing `"series"` still emits a `drc_hints.notes` entry describing
the chained-transistor shape (so it never surfaces as a surprise in an
extracted netlist or an LVS diff), but -- since every terminal is now
reachable -- no longer a `warnings[]` entry: for `fingers=3`,
`klt gen mos_array` reports exactly `U0_S0`, `U0_D0`, `U0_S1`, `U0_D1`,
`U0_G0`, `U0_G1`, `U0_G2`. `device_count` is `rows * cols * fingers` in this
mode (matching what `klt extract` reports back), not `rows * cols` --
`"parallel"` mode still folds `fingers` into one device per cell, so its
`device_count` is unaffected. `fingers=1` has nothing to strap or pad beyond
the first finger either way, so `finger_topology` does not move a single edge
or port there -- output stays the plain `U<i>_S`/`U<i>_D`/`U<i>_G` triple,
byte-for-byte identical under both topologies.

Each unit device contributes three ports -- `U<i>_S`/`U<i>_D`
(local-metal) and `U<i>_G` (poly, or local-metal with `gate_contact`) -- in
`"parallel"` mode (any `fingers`) and in `"series"` mode with `fingers=1`; see
above for `"series"` with `fingers > 1`. `<i>` is the device's position in
the `topology`-selected numbering order — `"array"` numbers row-major;
`"common_centroid"` (the default) numbers nearest-center-first, pairing each
instance immediately with its point-reflection through the array center, so
a downstream matching/LVS consumer can pair index `2k`/`2k+1` as
centroid-symmetric partners. (Physical placement is the same uniform grid
either way — `topology` changes the *numbering*, not the layout; see
`diff_pair` below for a generator that physically interleaves two distinct
devices.) `flavor="pfet"` on `sg13cmos5l` additionally contributes one
shared `WELL_TAP` port (local-metal) for the whole array's well-tie tap pad
— see "Well-tie tap pad" below — never a per-unit `U<i>_B`, since one pad
ties the entire shared well every unit device in the array sits in.
`device_count` is `rows * cols` (dummies excluded) except for
`"series"` mode's `fingers > 1` case noted above.
`drc_hints.matched_group_id` is `"mos_array:<rows>x<cols>:<topology>"`
(`flavor` is not folded in — a matched group is always one generator call by
construction, so every instance in it already shares one `flavor`).

`flavor="pfet"` additionally encloses every unit device's (real and dummy)
active region in a single shared well shape, sized by `WELL_ENCLOSURE_MARGIN_UM`,
on PDK families whose curated deck checks a well layer (`sky130`, `gf180mcu`,
and `sg13cmos5l`, as of this generator's `flavor` support — see `guard_ring`
below for the layer numbers). `flavor="nfet"` (the default) draws no well
shape at all — output for the default case is unchanged.

**Well-tie tap pad (issue #1473, `sg13cmos5l` only).** A drawn well with
nothing tying it to a real net still leaves every PMOS body an anonymous,
floating net under `klt extract` (`unbiased_pmos_body_nets[]`) — an LVS
defect DRC never catches. On `sg13cmos5l` — whose curated deck has no
distinct tap mask but derives a well tie from `nSD`-covered `Activ`
diffusion *inside* `NWell` (`EXTRACTION_DECK.tap_nplus`) — `flavor="pfet"`
additionally draws one small well-tie tap pad (active + contact + local
metal, implanted with `nSD`, plus a fixed `"WELL_TAP"` net-name label so the
tied well is never KLayout's anonymous `$<n>` placeholder) clear of every
unit device's own diffusion by `MIN_SAME_LAYER_SPACING_UM`, and reports it
as a shared `WELL_TAP` port (`ports[]`) a caller can route externally —
`klt extract --deck sg13cmos5l` on the result reports an empty
`unbiased_pmos_body_nets[]`. This is deliberately scoped to `sg13cmos5l`
only: `gf180mcu`'s curated deck derives an equivalent well tie the identical
way (`Nplus`-covered `Comp` inside `Nwell`), and likely has the same latent
gap, but that was not verified by issue #1473 (its own friction report only
tested `sg13cmos5l`) — `gf180mcu`'s/`sky130`'s `flavor="pfet"` output stays
byte-for-byte unchanged (no `WELL_TAP` port, no tap-pad geometry) until a
follow-on issue verifies the equivalent fix there. `flavor="nfet"`'s own
substrate-tie gap (a `pSD`-covered tap outside every well, tying the NMOS
body) was not addressed by this issue either: unlike a PMOS body, an NMOS
body's terminal always falls back to the curated deck's synthesized
`substrate_net` global regardless of any drawn tie, so `klt extract` never
flags it as anonymous/unbiased the way it does a floating PMOS body — there
is no equivalent `unbiased_nmos_body_nets[]` check for `flavor="nfet"` to
satisfy.

`voltage_flavor` (issue #1054) is a separate, orthogonal opt-in for a PDK's
**medium-voltage/thick-oxide transistor variant** — a common multi-voltage-
domain process feature distinct from `flavor`'s NMOS-vs-PMOS selection. The
default, an empty string, draws nothing (byte-for-byte unchanged geometry).
`voltage_flavor="medium_voltage"` on `gf180mcu` additionally draws that
family's `Dualgate` marker layer (`55/0`), sized to enclose every unit device
(real and dummy) the same shared box `flavor="pfet"`'s well shape already
encloses, and echoes the selection back in the response's
`drc_hints.voltage_flavor`/`drc_hints.voltage_flavor_mark_present` fields (see
below) so downstream tooling (`klt gen-compose`, `klt extract`, `klt lvs`)
doesn't have to re-derive it from raw geometry. `voltage_flavor="hv"` on
`ihp-sg13g2`/`ihp-sg13cmos5l` similarly draws that family's `ThickGateOx`
marker layer (`44/0`) — the same marker each curated deck's own
`EXTRACTION_DECK.mos_flavours` entry keys its thick-gate-oxide
`sg13_hv_nmos`/`sg13_hv_pmos` device class on, so a unit device drawn with
`voltage_flavor="hv"` extracts as that class instead of the default
thin-oxide `sg13_lv_nmos`/`sg13_lv_pmos`. **`sky130`'s curated deck cites no
numbered medium/high-voltage transistor marker layer** — any `voltage_flavor`
value on that family (or an unrecognised value on any family) draws nothing
and is reported via a `drc_hints.notes` entry, never silently dropped.
`voltage_flavor` is independent of `flavor`: requesting both `flavor="pfet"`
and `voltage_flavor="medium_voltage"`/`"hv"` draws both the well and the
marker with no conflict.

**`add_guard_ring` (issue #1493).** Encloses the array in an
automatically-sized tap/guard ring, composed the same way `diff_pair`'s own
`add_guard_ring` composes `guard_ring`'s ring-drawing (`ring_gap_side`/
`ring_gap_um`/`ring_gap_offset_um`/`ring_padding_um` are the identical
param set — see `guard_ring`'s "Ring routing openings" above and
`diff_pair`'s own row descriptions below). The ring is sized off the array's
own shared-footprint box (`well_box_um` — the same box `flavor="pfet"`'s
well shape and `voltage_flavor`'s marker already enclose) plus
`ring_padding_um`, so it always clears every real and dummy unit device
regardless of `flavor`. Reports `TAP_N`/`TAP_S`/`TAP_E`/`TAP_W` ports
(local-metal) when `add_guard_ring` is `true` (and a `GAP_<side>` marker when
the ring carries a routing opening). Unlike `diff_pair`'s `add_guard_ring`
(which defaults `true`, since a guard ring is that generator's own primary
composition), `mos_array`'s defaults to `false` — the ring is a strictly
additive, opt-in capability here, so a request that omits it draws
byte-for-byte identical geometry to every `mos_array` request that predates
this param. The ring's own well tie (independent of, and merging with, the
array's own `flavor="pfet"` well on the same well layer) is gated on
`flavor == "pfet"` exactly like `diff_pair`'s equivalent ring well tie — the
default `flavor="nfet"` never gets enclosed in a well by requesting a ring
alone. Because the ring composes directly into the block's own geometry
(rather than requiring a separately generated `guard_ring` block placed
inside the array's cavity), `klt gen-compose` can route a net between the
array's own device ports and its own ring's `TAP_*` ports as an ordinary
same-block self-net — no `gen-compose`-level change was needed for this
(issue #1493's own scope: a general declared-enclosure relationship for a
ring around a *separately*-generated block is an explicit non-goal, tracked
as a future follow-up if still needed).

**Below `UNIT_MIN_W_UM`: dog-bone terminals drawn by hand (issue #1574).**
`w_um` below `0.42` µm is rejected outright, with no override — that floor is
about the *contact*, not the target PDK's own diffusion-width minimum (sky130
`diff.width.1`: 0.15 µm; gf180mcu `comp.width.1`: 0.22 µm, both well below
`0.42`): at a uniform width that narrow, the enclosure margin a legally-placed
source/drain contact needs on both long edges no longer fits. A real
full-custom device below that floor — e.g. a narrow-and-long always-on weak
pull-up (`W` far under `0.42` µm, `L` tens of µm, kept narrow deliberately for
its channel resistance, not as a matched-array element) — still needs to be
drawn, just not through `mos_array`/`diff_pair` directly: draw it by hand as a
standard **dog-bone terminal** and place it in the same cell as your ordinary-
width `mos_array`/`diff_pair` output. Widen only the source/drain pads (to
`CONTACT_SIZE_UM + 2*ENCLOSURE_MARGIN_UM`, `0.42` µm — the exact pad size
`mos_array`'s own unit devices use) far enough to legally enclose a contact,
keep an unwidened "shoulder" of diffusion between each pad and the gate edge
so the width step lands clear of it, and draw the gate-crossing diffusion
segment itself — the channel a downstream `klt extract` reads `W` from — at
the requested narrow width, unchanged. `examples/dogbone-terminal/generate.py`
is a runnable, checked-in worked example of exactly this: it hand-draws one
such device with `klayout.db` on the same `active`/`poly`/`contact`/`metal`
role layers `mos_array` itself resolves to, places it beside a real `klt gen
mos_array` unit device in one cell, and verifies the result `klt drc`-clean on
both `sky130` and `gf180mcu` (`example_sky130.gds`/`example_gf180mcu.gds`,
with their `.drc.json` reports checked in alongside).

| `params` field | Type   | Default            | Description |
| -------------- | ------ | ------------------ | ----------- |
| `w_um`         | double | `0.42`             | Unit device width (µm). Must be `>= 0.42` (the smallest width that fits an enclosed contact -- a generator-side structural floor, not a target PDK's own diffusion-width minimum). |
| `l_um`         | double | `0.28`             | Gate length (µm). Must be `> 0`. Below `0.28`um the S/D local-metal pads are automatically padded away from the gate (issue #1187) so the S/D-pad-to-pad spacing stays clear of the target PDK's same-layer metal-spacing rule at any gate length, including a PDK's own minimum (e.g. sky130's `0.15`um) — `l_um` itself is drawn exactly as requested and may still violate the target PDK's own poly minimum-width rule, which is not checked here (flagged via `drc_hints.notes`, not rejected). |
| `fingers`      | int    | `1`                | Gate fingers per unit device. Must be `>= 1`. With `finger_topology: "parallel"` (the default) the unit device is one folded transistor of width `fingers * w_um`. |
| `finger_topology` | string | `"parallel"`    | How a `fingers > 1` unit is wired: `"parallel"` (alternating S/D segments strapped, every gate tied — one folded device) or `"series"` (bare stripes, no straps — every finger padded and every S/D/gate terminal reported as its own `U<i>_S<j>`/`U<i>_D<j>`/`U<i>_G<j>` port, `device_count` multiplied by `fingers`). Must be `"parallel"` or `"series"`. No effect when `fingers` is `1`. |
| `rows`/`cols`  | int    | `2`/`2`            | Array shape. Must each be `>= 1`. |
| `topology`     | string | `"common_centroid"`| `"array"` or `"common_centroid"` — see above. |
| `dummy`        | int    | `1`                | Dummy unit-device columns added on each side. Must be `>= 0`. |
| `flavor`       | string | `"nfet"`           | Device flavor: `"nfet"` (no well drawn) or `"pfet"` (unit devices enclosed in a well on PDK families that check one, plus a `WELL_TAP` well-tie tap-pad port on `sg13cmos5l` — see above). Must be `"nfet"` or `"pfet"`. |
| `voltage_flavor` | string | `""`             | Optional medium-voltage/thick-oxide device-class marker: `""` (default, no marker drawn) or a name the resolved PDK family's role-layer table recognises — `"medium_voltage"` on `gf180mcu` (its `Dualgate` layer) or `"hv"` on `ihp-sg13g2`/`ihp-sg13cmos5l` (their shared `ThickGateOx` layer). Any other value on any family draws nothing and is flagged via `drc_hints.notes`, never rejected outright. |
| `gate_contact` | bool   | `false`            | Draw a contact + local-metal pad on each gate landing pad and report `U<i>_G` on the `metal` role instead of `poly` — see above. Grows the unit device by `0.4` µm to keep the gate metal clear of the S/D pads. |
| `add_guard_ring` | bool | `false`           | Enclose the array in an automatically-sized tap/guard ring (issue #1493) — see above. Unlike `diff_pair`'s own param of the same name, defaults to `false`. |
| `ring_gap_side` | string | `""`              | Cut one routing opening through the guard ring on this side (`""`/`"N"`/`"S"`/`"E"`/`"W"`) — see `guard_ring`'s "Ring routing openings" above. |
| `ring_gap_um`  | double | `0.0`              | Length of that opening along its side (µm). Required (`>= 0.4`) with `ring_gap_side`, `0` otherwise. |
| `ring_gap_offset_um` | double | `0.0`         | Slide the opening off its side's midpoint (µm) — e.g. onto the row of device ports a route needs to reach. |
| `ring_padding_um` | double | `0.5`            | Padding between the array's own shared-footprint box (`well_box_um`) and the guard ring's inner edge (µm), when `add_guard_ring` is set. Must be `>= 0`. |

### `res_array` (family 2: resistor/capacitor array)

A row of `num` matched unit elements (poly body + contact + local-metal pads
at both ends) with `dummy` dummy elements at each end — the
`kb/entries/sky130-bandgap-reference.json` KB entry's resistor-array layout
idiom ("unit-resistor strings ... with dummy elements at both ends, matched
in orientation"), generalized here to also stand in for a unit MoM/MiM
capacitor cell footprint (neither curated DRC deck defines cap-specific
layers, so the footprint — not the layer stack — is what's shared). Each
real (non-dummy) unit gets two ports, `R<i>_A`/`R<i>_B`. `device_count` is
`num` (dummies excluded). `drc_hints.matched_group_id` is `"res_array:<num>"`
(unaffected by `rows`).

`rows` (default `1`, the original single-row layout) folds the `num` unit
resistors into that many parallel rows in boustrophedon ("snake") order —
row 0 runs left to right, row 1 right to left, and so on — instead of one
long row, mirroring how `mos_array`/`bjt_array` use `rows`/`cols` to keep an
array roughly square. Without it, a resistor string with a realistic
unit-segment count (dummies and trim taps included) produces a bounding box
dominated by one axis, which can blow a floorplan's area budget even though
the drawn area itself is small (issue #415). Row-to-row pitch is fixed (unit
height plus the same `MIN_SAME_LAYER_SPACING_UM` margin `mos_array` uses for
its own row pitch), independent of `spacing_um`, which stays scoped to
within-row unit spacing. Port naming keeps the physically-adjacent pad pair
short across every row transition: on a right-to-left (odd) row, `R<i>_A`
and `R<i>_B` report the *opposite* physical pad from an even row (the unit's
own drawn geometry never mirrors), so `R<i>_B` stays next to `R<i + 1>_A` in
every row, not just even ones — the same short-hop connection a downstream
router closes between any two consecutive real units today.

Every unit (dummies included) has its resistive *body segment* — the middle
span between the two contacted end pads — covered by the target PDK's own
resistor-ID marker layer (sky130's `poly.res` `(66, 13)`; gf180mcu's
`RES_MK` `(110, 5)`, plus the `Pplus`/`SAB` layers its own `ppolyf_u`
device additionally requires; sg13g2's `PolyRes.drawing` `(128, 0)`, plus
the `EXTBlock`/`Res` layers its own `rsil` device additionally requires — see
below), so `klt gen res_array`'s output is directly recognised as a resistor
device by `klt extract --deck <pdk>` rather than being absorbed into ordinary
poly interconnect as a short (issue #369). Neither curated *DRC* deck checks
any of these layers, so drawing them never affects `klt drc` status. On
sky130 only (issue #491), each *dummy* unit's body segment is additionally
covered by the curated `dummy` marker layer, so `klt extract`'s dummy-device
suppression (see "Dummy devices: the `dummy` marker layer" in
`docs/cli/extract.md`) drops it from the extracted netlist instead of
reporting it as a spurious unmatched device under `klt lvs` — gf180mcu/sg13g2
draw no equivalent marker.

`flavor` selects which recognised poly-resistor *device class* the array
draws, by covering each body segment with the implant/precision-resistor
masks that class is keyed off in the curated extraction deck (issue #463).
`"generic"` (the default) is each family's base, lowest-sheet-rho flavour —
sky130's `res_generic_po` (~48 Ω/□, marker only) and gf180mcu's `ppolyf_u`
(marker + `Pplus` + `SAB`). On sky130, `"high"` (`res_high_po`, ~320 Ω/□)
additionally draws the `psdm` P+ implant `(94, 20)` and the `rpm` mask
`(86, 20)`, and `"xhigh"` (`res_xhigh_po`, ~2 kΩ/□) draws `psdm` plus the
`urpm` mask `(79, 20)` — the higher-sheet-rho flavours a precision resistor
ladder uses to spend far fewer squares per ohm. The layer/purpose numbers
come straight from the same `klayout_tools.decks.sky130` extraction deck that
recognises them, so a `flavor="high"`/`"xhigh"` array round-trips through
`klt extract` to the matching device class rather than always reading
`res_generic_po`. On gf180mcu (issue #1550), `"1k"`/`"2k"`/`"3k"` each draw
`SAB` `(49, 0)` plus the `Resistor` high-sheet-rho marker `(62, 0)` over the
`RES_MK` body — no `Pplus`, since `ppolyf_u_1k`'s (and its `_2k`/`_3k`
siblings') own `requires` set neither needs nor excludes it. All three names
draw *identical* geometry, since `ppolyf_u_1k`/`_2k`/`_3k` are distinguished
only by which `poly_res` value `klt extract --deck-option poly_res=<1k|2k|3k>`
selects at extraction time (1000/2000/3000 Ω/□ respectively), not by any
drawn layer — so a `res_array` built with `flavor="1k"` (or `"2k"`/`"3k"`)
must be paired with the matching `--deck-option poly_res=` value to extract
as that class rather than the default. sg13g2 exposes all three of its
recognised poly-resistor classes (issue #1451), each drawing that class's own
`requires` set from `klayout_tools.decks.sg13g2.EXTRACTION_DECK.resistors`:

| `flavor` | sg13g2 device class | Sheet ρ | Masks drawn over each body |
| -------- | ------------------- | ------- | -------------------------- |
| `"generic"` (default) | `rsil` | 7 Ω/□ | `EXTBlock` `(111, 0)` + `Res` `(24, 0)` |
| `"rppd"` | `rppd` | 260 Ω/□ | `EXTBlock` + `pSD` `(14, 0)` + `SalBlock` `(28, 0)` |
| `"rhigh"` | `rhigh` | 1360 Ω/□ | `EXTBlock` + `pSD` + `nSD` `(7, 0)` + `SalBlock` |

Those classes' own `excludes` sets keep them mutually unambiguous — `rsil`
excludes `pSD`/`SalBlock`/`nSD` and `rppd` excludes `nSD`, so drawing exactly
one flavour's `requires` set can only ever extract as that flavour. Flavour
names are per-family: requesting a sky130-only name (`"high"`/`"xhigh"`) on
gf180mcu or sg13g2 — or an sg13g2-only name on sky130 — raises a clear error
listing the flavours that family does expose. As with the marker, no curated
*DRC* deck checks these masks, so they never affect `klt drc` status.

sg13cmos5l (issue #1462) exposes the identical three classes with the
identical mask sets, transcribed byte-for-byte from `sg13g2`'s own table
(cmos5l's resistor LVS rules are themselves symlinks into the pinned sibling
`ihp-sg13g2` checkout) — with one difference: this family's lowest-sheet-rho
class is reachable by *either* `"generic"` (the cross-family default every
other family uses) or `"rsil"` (its own literal device-class name), both
resolving to the identical `EXTBlock` + `Res` mask set.

`metal_level` (issue #1639, default `0`) switches this generator entirely off
the poly-body path above: `1`..`5` on sky130 instead draws that level's
*drawn metal-layer* resistor — `res_generic_m1`..`res_generic_m5`
(`klayout_tools.decks.sky130.EXTRACTION_DECK.resistors`, added by issue
#1621) — with the body on `metN.drawing`, that level's own resistor-ID
marker (`metN.res`), and an end via/landing-pad stack connecting *down* to
the metal level immediately below (`li1` for `met1`; `met1`..`met4` for
`met2`..`met5`) instead of up through a contact to `li1` the way the poly
body's own end pads do — `metN` is already a routing metal, so `R<i>_A`/
`R<i>_B` are reported directly on it rather than on a separate landing
layer. `flavor` has no effect while `metal_level` is non-zero (sky130's
`res_generic_mN` classes need no extra requires-mask at all, unlike the
poly flavours above). No other family exposes a drawn metal-resistor device
class today, so a non-zero `metal_level` on gf180mcu/sg13g2/sg13cmos5l
raises a clear error.

Every level but `met5` (sheet ρ 0.120/0.120/0.047/0.047/0.029 Ω/□ for
`met1`..`met5` respectively — met1/met2 and met3/met4 deliberately share a
sheet ρ) draws DRC-clean with `res_array`'s own generic contact/enclosure/
spacing constants, since sky130's own `mcon`/`via`/`via2`/`via3` and their
enclosing `met1`..`met4` rules are all looser than those constants. `met5`
is the one exception — sky130's top redistribution metal carries markedly
coarser rules than every level below it (`sky130A_mr.drc`'s `via4.1_a`/
`m5.3`/`m5.2`), so a `metal_level=5` request automatically draws a wider end
via (0.8µm), wider via enclosure (0.31µm), and wider unit-to-unit
`spacing_um` (floored to 1.6µm, both within a row and across a `rows`
fold, with a `drc_hints.notes` entry when it widens the request) — every
one of these is the generator's own internal geometry, resolved from the
target PDK, never a caller-facing param. `width_um` is the one exception:
like `mos_array`'s `w_um`/`cap_array`'s `plate_w_um`, it is never
auto-widened, so a `met5` request under `met5.width.1`'s own 1.6µm minimum
is instead flagged via `drc_hints.notes`, not silently widened or rejected.

| `params` field | Type   | Default | Description |
| -------------- | ------ | ------- | ----------- |
| `length_um`    | double | `2.0`   | Unit resistor body length (µm). Must be `> 0`. |
| `width_um`     | double | `0.42`  | Unit resistor width (µm). Must be `>= 0.42`. |
| `spacing_um`   | double | `0.5`   | Spacing between unit resistors (µm). Must be `>= 0`; below `0.4`um risks violating a target PDK's minimum same-layer spacing rule (flagged via `drc_hints.notes`, not rejected) — floored under `metal_level`'s own minimum on sky130's `met5` (see above). |
| `num`          | int    | `4`     | Number of matched unit resistors. Must be `>= 1`. |
| `dummy`        | int    | `1`     | Dummy unit resistors added at each end. Must be `>= 0`. |
| `rows`         | int    | `1`     | Fold the `num` unit resistors into this many parallel rows (boustrophedon order) instead of one long row. Must be `>= 1`. |
| `flavor`       | string | `"generic"` | Poly-resistor flavour / recognised device class: `"generic"` (base sheet-rho — `res_generic_po` on sky130, `ppolyf_u` on gf180mcu, `rsil` on sg13g2/sg13cmos5l — also reachable on sg13cmos5l as `"rsil"`); on sky130, `"high"` (`res_high_po`) / `"xhigh"` (`res_xhigh_po`); on gf180mcu, `"1k"` / `"2k"` / `"3k"` (`ppolyf_u_1k`/`_2k`/`_3k`, 1000/2000/3000 Ω/□ — pair with `klt extract --deck-option poly_res=<value>`); on sg13g2/sg13cmos5l, `"rppd"` (260 Ω/□) / `"rhigh"` (1360 Ω/□). Must be a flavour the resolved PDK family exposes. Ignored when `metal_level` is non-zero. |
| `metal_level`  | int    | `0`     | Draw a metal-layer resistor body instead of poly: `0` (default) draws the original poly-body resistor selected by `flavor`; `1`..`5` on sky130 draws that level's `res_generic_mN` device (`met1`..`met5`) instead (issue #1639). Must be between `0` and `5`; a level a resolved PDK family does not expose (every non-zero value on gf180mcu/sg13g2/sg13cmos5l today) raises a clear error. |

### `cap_array` (MiM capacitor array, issue #1117)

A row of `num` matched unit MiM (Metal-Insulator-Metal) capacitor cells, each
a top-plate-metal-over-bottom-plate-metal stack (sky130's `capm` top-plate
mark over a `met3` bottom-plate conductor; sg13g2's `MIM` top-plate mark over
a `Metal5` bottom-plate conductor, issue #1455; gf180mcu's `FuseTop` top
plate over a `Metal4` bottom plate, issue #1555) with a top-plate via and
local-metal landing pad (sky130's `via3`/`met4`; sg13g2's `Vmim`/`TopMetal1`;
gf180mcu's `Via4`/`Metal5`),
plus a same-layer escape stub and second landing pad (issue #1494) reaching
past the unit cell's own bounding box — the capacitor sibling of `res_array`,
drawing the *same* layer/datatype numbers each family's own curated
`EXTRACTION_DECK.capacitors[0]` entry declares
(`klayout_tools.decks.sky130`'s `sky130_fd_pr__model__cap_mim`;
`klayout_tools.decks.sg13g2`'s `cap_cmim`; `klayout_tools.decks.gf180mcu`'s
`cap_mim_2f0_m4m5_noshield`), never a second, private layer map.
Each unit gets two ports: `C<i>_BOT` on the bottom-plate conductor's
local-left edge, and `C<i>_TOP` on the escape pad, due north of the unit cell
and clear of the bottom plate's own footprint — unlike the via/landing-pad
centre directly over the bottom plate, a via stack landed at `C<i>_TOP`
(following the family's own metal/via stack) does not touch, let alone
merge with, the bottom plate's layer/net. `direction_deg` for `C<i>_TOP` is
therefore a real, geometrically-derived `90` (due north), not a placeholder.
(A family with plates but no top-plate-via-metal layer — not exercised by
any currently-supported family — falls back to the old, unroutable interior
centre with a fixed `direction_deg` and a `drc_hints.notes` warning, mirroring
`mos_array`'s interior gate-contact port.) `device_count` is `num`.
`drc_hints.matched_group_id` is `"cap_array:<num>"`.

`sky130`, `sg13g2` and `gf180mcu` are supported; requesting `cap_array` for
any other PDK family raises a clear application error (exit code `1`) rather
than silently drawing sky130's layer numbers onto the wrong stack. On
sg13g2, `TopMetal1`'s own coarse minimum-width DRC rule (1.64µm, vs.
sky130's `met4` at 0.3µm) widens the drawn top-plate landing pad (and its
reported `C<i>_TOP` port width) past the generic default for that family
only — sky130 geometry is byte-for-byte unchanged.

**gf180mcu (issue #1555 — the follow-on #1117 deferred).** This family's MiM
stack needs three things beyond the four plate/via layer roles, all of them
per-family data in `gen.py`'s own tables rather than special-cased geometry
code — and every one of them is a hard DRC or device-recognition
requirement, not a style choice:

- **Recognition masks.** `cap_mim_2f0_m4m5_noshield` declares
  `top_plate_requires=(CAP_MK, MIM_L_MK)`, which `klt extract` applies as a
  mandatory AND, so the generator draws both masks exactly coincident with
  the drawn `FuseTop` plate (`_PDK_CAP_TOP_PLATE_REQUIRES`). Without them the
  drawn stack is geometrically plausible but extracts as **zero** capacitors.
- **The oversized "virtual bottom plate."** The DRM derives this family's
  bottom plate as `FuseTop.sized(1.06µm) & Metal4.interacting(FuseTop)` (per
  `CapacitorDevice.bottom_plate_oversize_um`), and requires `Metal4` to
  overlap `FuseTop` by at least 0.6µm (`mim.enclosing.fusetop.1`, MIMTM.3) —
  more than the generic 0.5µm bottom-plate margin draws. The generator draws
  `Metal4` at the full 1.06µm oversize, so the drawn shape *is* the derived
  virtual plate, and each extracted unit's `area_um2`/`c_f` matches its
  geometric top-plate area exactly.
- **Two coarser floors.** `Via4`'s own minimum size (0.26µm,
  `via4.width.1`) exceeds the generic contact size (0.22µm), and adjacent
  virtual bottom plates must stay 1.2µm apart (`mim.space.1`, MIMTM.1) —
  far more than this generator's 0.5µm default `spacing_um`. A `spacing_um`
  below that floor is **widened** to it rather than rejected: the effective
  value is what `drc_hints.min_spacing_um` reports, alongside a
  `drc_hints.notes` entry naming the widening. Both floors live in
  `_PDK_CAP_GEOMETRY_MIN_UM` and apply as `max(generic, floor)`, so no other
  family's geometry moves.

`cap_array`'s default output on this family is DRC-clean against `klt drc
--deck gf180mcu` and round-trips through `klt extract --deck gf180mcu` to the
`"cap_mim_2f0_m4m5_noshield"` device class (pair with `--deck-option
mim_cap=cap_mim_1f0_m4m5_noshield`/`...1f5...`, issue #1151, to read the same
drawn geometry at one of the PDK's other two MiM densities).

Neither `rows` folding (`res_array`'s own boustrophedon fold, issue #415) nor
`dummy` padding elements are implemented yet — both are natural `res_array`-
parity follow-ups, not correctness gaps for a first `cap_array` `num`-only
generator. There is also no `flavor` param (`res_array`'s own #463) — sky130's
second MiM stack (`capm2`/`met4`) and sg13g2's RF variant (`rfcmim`) are out
of this generator's initial scope; every family's drawn output classifies as
its base flavour (`sky130_fd_pr__model__cap_mim`/`cap_cmim`/
`cap_mim_2f0_m4m5_noshield`), never the RF/second-stack variant.
gf180mcu's own three MiM *densities* need no `flavor` param at all — all
three are the identical drawn geometry, selected at extraction time with
`klt extract --deck-option mim_cap=…` (issue #1151).

| `params` field | Type   | Default | Description |
| -------------- | ------ | ------- | ----------- |
| `plate_w_um`   | double | `5.0`   | Unit top-plate width (µm). Must be `>= 0.42`. |
| `plate_h_um`   | double | `5.0`   | Unit top-plate height (µm). Must be `>= 0.42`. |
| `spacing_um`   | double | `0.5`   | Spacing between unit capacitors (µm). Must be `>= 0`; below `0.4`um risks violating a target PDK's minimum same-layer spacing rule (flagged via `drc_hints.notes`, not rejected). On a family with its own MiM-capacitor spacing rule (gf180mcu's `mim.space.1`, 1.2µm) a smaller value is widened to that rule instead — see the gf180mcu note above; `drc_hints.min_spacing_um` always reports the spacing actually drawn. |
| `num`          | int    | `4`     | Number of matched unit capacitors. Must be `>= 1`. |

### `guard_ring` (family 3: substrate/well tap ring)

A tap ring (drawn as a single unbroken outer-box-minus-inner-box polygon, so
there is no same-layer spacing violation between "segments") with
`contacts_per_side` contacts evenly spaced along each side and a
local-metal ring on top, optionally enclosed by a well tie (`add_well`) on
PDK families whose curated deck checks a well layer (gf180mcu's `Nwell`
`(21, 0)`; sky130's `nwell.drawing` `(64, 20)`, matching
`klayout_tools.decks.sky130.EXTRACTION_DECK.nwell`; sg13g2's `NWell.drawing`
`(31, 0)`, matching `klayout_tools.decks.sg13g2.EXTRACTION_DECK.nwell`).
Every currently supported family draws the well tie by default — sky130's
and sg13g2's curated *DRC* decks both happen to check no rule against that
layer (so drawing it there never affects DRC status), which is different
from "the layer doesn't exist"; a family with neither a well layer nor a
well rule would still get the documented no-op, reported via
`drc_hints.notes`. The tap ring itself is drawn on the target PDK's own
`active` layer (gf180mcu's `Comp`, sg13g2's `Activ.drawing`) on the two
families with no separate, dedicated tap mask; sky130 has one (`tap.drawing`)
and uses it instead. Four ports —
`TAP_N`/`TAP_S`/`TAP_E`/`TAP_W` — sit at the midpoint of each ring side.
`device_count` is the number of tap contacts actually drawn
(`2 * (contacts_ns + contacts_ew)`, less any the ring opening below clipped
away, where `contacts_ns`/`contacts_ew` are `contacts_per_side_ns`/
`contacts_per_side_ew` when set, else `contacts_per_side` — see below).
**`drc_hints.matched_group_id` is always `null`** — a guard ring has no
matching concept (it is excluded from the matched-device generators: 1, 2,
and 4).

| `params` field          | Type   | Default | Description |
| ------------------------ | ------ | ------- | ----------- |
| `inner_width_um`         | double | `3.0`   | Width of the protected inner area (µm). Must be `> 0`. |
| `inner_height_um`        | double | `3.0`   | Height of the protected inner area (µm). Must be `> 0`. |
| `ring_width_um`          | double | `0.42`  | Tap ring thickness (µm). Must be `>= 0.42`. |
| `contacts_per_side`      | int    | `4`     | Tap contacts evenly spaced along each ring side, applied uniformly to all four sides unless overridden per-axis below. Must be `>= 1`. |
| `contacts_per_side_ns`   | int    | `0`     | Tap contacts on the N/S (top/bottom) sides, spaced along `inner_width_um`. `0` (the default) inherits `contacts_per_side`, so existing single-scalar callers are unaffected. Must be `>= 0`. |
| `contacts_per_side_ew`   | int    | `0`     | Tap contacts on the E/W (left/right) sides, spaced along `inner_height_um`. `0` (the default) inherits `contacts_per_side`. Must be `>= 0`. |
| `ring_gap_side`          | string | `""`    | Cut one routing opening through the ring on this side: `""` (a closed ring), `"N"`, `"S"`, `"E"` or `"W"`. See "Ring routing openings" below. |
| `ring_gap_um`            | double | `0.0`   | Length of the opening along its side (µm). Required (and must be `>= 0.4`, the minimum same-layer spacing) when `ring_gap_side` is set; must be `0` otherwise. |
| `ring_gap_offset_um`     | double | `0.0`   | Slide the opening off its side's midpoint (µm): `+x` on `"N"`/`"S"`, `+y` on `"E"`/`"W"`. The opening must stay inside the side's straight run. |
| `add_well`               | bool   | `true`  | Enclose the ring in a well tie when the resolved PDK family checks one. |

For each axis, a resolved contact count (`contacts_per_side_ns`/
`contacts_per_side_ew` when non-zero, else `contacts_per_side`) too large for
`inner_width_um`/`inner_height_um` on that axis — one that would produce
literally overlapping contacts — is rejected outright (`GenError`); this
check is PDK-agnostic (a structural error, not a DRC-adjacent one). A count
that fits but leaves less than `0.3um` between adjacent contacts on that
axis is *not* rejected — it is DRC-clean on sky130 (whose curated deck
checks no contact-spacing rule at all) but close enough to gf180mcu's real
`contact.space.1` limit (`>= 0.25um`) to be worth a caller's attention, so
`drc_hints.notes` flags it instead (per-axis) rather than rejecting a value
that may be perfectly legal on the resolved family — issue #685.

Independent per-axis contact counts (`contacts_per_side_ns`/
`contacts_per_side_ew`) let a caller sizing a ring around a strongly
non-square inner region target a pitch on the long axis that a single
scalar `contacts_per_side` cannot reach — a uniform count is capped by
whichever side is shorter. Before every `contacts_per_side` value the
generator accepted was also confirmed DRC-clean (issue #685): independently
rounding each contact box's four edges to the manufacturing grid could round
one edge up and the other down, silently drawing a contact 1 dbu narrower or
shorter than `CONTACT_SIZE_UM` and tripping gf180mcu's `contact.width.1` for
some individually-legal `contacts_per_side` values (e.g. `3` and `7` on a
`78.91 x 4.75um` inner region) while neighbouring values (`2`, `4`) stayed
clean. Contact boxes are now built by snapping the contact's centre to the
manufacturing grid first, so every accepted value draws a full-size,
DRC-clean contact.

#### Ring routing openings (`ring_gap_side`)

A closed ring encloses whatever it protects — which is the point, and also
why [`klt gen-compose`](gen-compose.md) refuses to route a signal net to a
non-tap port on a ringed block: the wire would cross the ring's own metal
loop and merge that net with the ring's tap net. `ring_gap_side` cuts **one**
opening through the ring's band so a route can pass through it instead:

- Only one side can be opened, so the ring stays a **single connected**
  (C-shaped) conductor — its remaining tap ports still describe one tap net.
  Two openings would split it into two electrically separate arcs, so a
  second one is not expressible.
- The opening is cut from **every** layer the ring is drawn on (the tap ring
  and the local-metal ring; `bjt_array`'s collector ring likewise on both its
  diffusion and metal roles). A well tie drawn under the ring (`add_well`) is
  *not* cut — the opening is a routing hole through the ring conductor, not
  through the well.
- Any tap contact the opening would clip is dropped, and the side's own
  `TAP_*`/`COLL_*` port is not reported when its midpoint lands in the
  opening (there is no metal left under it there).
- The opening is reported as a `GAP_<side>` entry in `ports[]` on the layer a
  route would cross the ring on: `x_um`/`y_um` is the opening's centre on the
  ring's own centre line, **`width_um` is the opening's length along that
  side**, and `direction_deg` is the side's outward normal. A `GAP_*` entry
  marks the *absence* of metal, so it is a marker rather than a connectable
  port — `klt gen-compose` rejects any attempt to wire (`connectivity[]`) or
  label (`pins[]`) one.
- A `drc_hints.notes[]` entry records the opening (and that substrate
  isolation is interrupted there), or — when `ring_gap_side` is set on a
  block whose ring is switched off — that no opening was cut.

The same three params, with the same semantics, are accepted by `diff_pair`
(on its `add_guard_ring` ring), `bjt_array` (on its `add_collector_ring`
ring) and `well_island` (below). Openings are opt-in: omitting them draws
exactly the closed ring these generators always drew.

### `well_island` (family 3 sibling: named-net, isolated well/tap island)

`guard_ring`'s geometry — the same tap ring, contacts, local-metal ring and
enclosing well tie — with the two properties a *well island* needs and a
plain guard ring cannot express (issue #1421):

1. **The tie carries a caller-named net** (`net`). It is reported as every
   `TAP_*` port's `net` and as `drc_hints.well_net`, so a composition can
   route to it by name, and it is drawn into the layout as a text on the
   **ring metal's** label layer (`metal_label` role — sky130's `li1.pin`
   `(67, 5)`, gf180mcu's `Metal1` pin purpose `(34, 10)`).
2. **The island is isolated from a caller-named set of other wells**
   (`isolate_from` + `separation_um`). The generator either sizes itself to
   clear them or rejects the request outright — it never emits a well that
   silently merges with one of them.

On a PDK family whose well tie is drawn on the same layer as transistor
active (gf180mcu, whose `Comp` does double duty), the island also draws the
well-tie implant that makes it *recognisable* as a tie — `Nplus` `(32, 0)`,
the same layer `klayout_tools.decks.gf180mcu`'s `EXTRACTION_DECK.tap_nplus`
declares — as a ring exactly coincident with the tap ring, never as a
blanket over the enclosed area (which would re-dope whatever the caller
places inside it). Without it, gf180mcu extracts the island's enclosed PMOS
bodies as anonymous nets. sky130 needs no implant: its `tap.drawing` layer
is already a tie by virtue of sitting inside `nwell`.

**Why this is not just `guard_ring` with a label.** On any PDK whose
extraction deck derives a well-flavour device's body from `active & well`
and names the well from the tap inside it, the only way to give two groups
of same-flavour devices two different body potentials is two physically
separate well islands with two separately-routed taps. `klt extract`'s
`unbiased_pmos_body_nets` being empty is *necessary but not sufficient*
evidence that you got it: a single well over everything, tied to one supply,
satisfies it too. The per-device `devices[].nets["b"]` names are what
distinguish two body domains from one — see the well-label note below.

| `params` field          | Type   | Default | Description |
| ------------------------ | ------ | ------- | ----------- |
| `inner_width_um`         | double | `3.0`   | Width of the enclosed inner area (µm). Must be `> 0`. |
| `inner_height_um`        | double | `3.0`   | Height of the enclosed inner area (µm). Must be `> 0`. |
| `ring_width_um`          | double | `0.42`  | Tap ring thickness (µm). Must be `>= 0.42`. |
| `contacts_per_side`      | int    | `4`     | Tap contacts evenly spaced along each ring side, applied uniformly unless overridden per-axis below. Must be `>= 1`. |
| `contacts_per_side_ns`   | int    | `0`     | Tap contacts on the N/S sides, spaced along `inner_width_um`. `0` inherits `contacts_per_side`. Must be `>= 0`. |
| `contacts_per_side_ew`   | int    | `0`     | Tap contacts on the E/W sides, spaced along `inner_height_um`. `0` inherits `contacts_per_side`. Must be `>= 0`. |
| `ring_gap_side`          | string | `""`    | Cut one routing opening through the ring: `""`, `"N"`, `"S"`, `"E"` or `"W"` — identical semantics to `guard_ring`'s (see "Ring routing openings" above). The opening is cut through the tap/metal/implant rings, never through the well. |
| `ring_gap_um`            | double | `0.0`   | Length of the opening along its side (µm). Must be `>= 0.4` when `ring_gap_side` is set; `0` otherwise. |
| `ring_gap_offset_um`     | double | `0.0`   | Slide the opening off its side's midpoint (µm). |
| `net`                    | string | `""`    | Net name the island's tie carries. Reported as every `TAP_*` port's `net` and drawn as a label on the ring metal. `""` (the default) draws no label and notes that the body net will extract anonymously. Must contain no whitespace and must not start with `$` (KLayout's own anonymous-net spelling). |
| `well_margin_um`         | double | `0.15`  | Well enclosure of the tap ring (µm). Must be `>= 0.15` (the enclosure the ring under it needs). Trimmed towards `0.15` when a larger value would violate the requested separation — see below. |
| `separation_um`          | double | `0.0`   | Required clearance (µm, **euclidian**) from every `isolate_from` well at a different potential. `0` (the default) uses the resolved PDK family's own different-potential well rule; a non-zero value *below* that rule is rejected. |
| `isolate_from`           | list   | `[]`    | Other well regions this island must stay clear of. Each entry is `[x0_um, y0_um, x1_um, y1_um]` or `[x0_um, y0_um, x1_um, y1_um, net]`, in **this cell's own coordinate frame** (the same frame `bbox_um`/`ports[]` use). An entry naming this island's own `net` is *equipotential*: no separation is required against it. |

Four ports — `TAP_N`/`TAP_S`/`TAP_E`/`TAP_W` — sit at the midpoint of each
ring side (less any the ring opening removed), each carrying `net`.
`device_count` is the number of tap contacts drawn.
**`drc_hints.matched_group_id` is always `null`.**

#### Well separation: which rule, and what happens when it cannot be met

| Family | Rule | Value |
| ------ | ---- | ----- |
| `sky130` | `nwell.2` — "Minimum spacing between N-well and N-well of different potential", the rule the PDK's own KLayout deck codes as `nwell.isolated(1.27, euclidian)` | `1.27µm` |
| `gf180mcu` | DRM 7.4 Nwell `NW.2b` — "Min. Nwell Space (Outside DNWELL) [Different potential]", 3.3V column | `1.4µm` |

Neither value is the one `klt drc` checks, and that is deliberate:

- This repo's **curated sky130 DRC deck transcribes no well-layer rule at
  all** (issue #1420), so `klt drc --deck sky130` cannot catch two islands
  that merged.
- The curated **gf180mcu** deck's `nwell.space.1` deliberately transcribes
  only the *equipotential* half of the `NW.2a`/`NW.2b` split (`0.6µm`),
  because a geometry-only checker cannot tell the two contexts apart. `klt
  gen` *can* — the caller names the potential — so it applies the strict
  half.

The generator is therefore the source of truth for the geometry it draws,
independent of whether the checker can confirm it. Behaviour, in order:

- **`separation_um` below the family's rule → `GenError`.** Honouring it
  would draw exactly the too-close pair of wells this generator exists to
  prevent.
- **A same-net `isolate_from` entry is equipotential.** No separation is
  required against it (the two wells are expected to tie together, not
  isolate); `drc_hints.notes` records that the different-potential rule was
  not applied to it.
- **A `well_margin_um` that does not clear every different-potential
  neighbour is trimmed** — down to, never below, the `0.15µm` the tap ring's
  own well enclosure needs — and both `drc_hints.notes` and `warnings`
  record the trim.
- **If even the minimum enclosure cannot clear a neighbour → `GenError`**,
  naming the offending rectangle, the gap actually achieved and the
  separation required. Never a silently merged well.
- A family with no well layer at all gets the same "no well shape was drawn"
  treatment `guard_ring`'s `add_well` already gives (a `drc_hints.notes`
  entry plus a top-level `warning`), not a hard failure.

The response reports the resulting geometry so a placer never has to
re-derive it:

| `drc_hints` field | Type | Description |
| ----------------- | ---- | ----------- |
| `well_net` | string \| null | The island's tie net (echo of `params.net`), or `null`. |
| `well_box_um` | object \| null | `{x0, y0, x1, y1}` — the well rectangle actually drawn, at the *resolved* (possibly trimmed) enclosure. `null` on a family with no well layer. |
| `well_separation_um` | number \| null | The clearance actually enforced. |
| `well_keepout_box_um` | object \| null | `well_box_um` grown by `well_separation_um` on every side — the region no *other* well may enter. Placing the next island's well exactly on this edge is the tightest legal placement. |

#### The well-label tautology (and why this generator avoids it)

It is tempting to name a well island by drawing a text on the deck's
**well-label** layer (sky130's `nwell.pin` `(64, 5)`). Don't: a text there
names the `nwell` polygon *directly*, so `klt extract` reports the intended
body net **even when the tap tie meant to bias the well is broken or
missing** — the verification becomes a tautology.

`well_island` never draws on the well-label layer. It names the ring's
**metal** instead, so the name can only reach the well through the physical
tie (`li1` → `licon1` → `tap` → `nwell`). Consequently
`devices[].nets["b"]` from [`klt extract`](extract.md) is real evidence that
the tie works: break the tie and the body net falls back to an anonymous
KLayout net and shows up in `unbiased_pmos_body_nets`. This is exercised
directly by the round-trip test in `tests/test_gen.py`
(`test_well_island_body_net_name_needs_the_real_tie_not_just_the_label`).

#### Worked example: two body domains

```bash
# Island A, tied to VBODY_A.
klt gen well_island --pdk sky130A --params '{"net": "VBODY_A"}' \
    --cell-name island_a -o island_a.gds --format json
# -> drc_hints.well_box_um     = {"x0": -0.15, ..., "x1": 3.99, "y1": 3.99}
#    drc_hints.well_keepout_box_um = {"x0": -1.42, ..., "x1": 5.26, "y1": 5.26}

# Island B, tied to VBODY_B, placed 5.41µm to the right of A (A's keepout
# edge). A's well, expressed in B's own frame, is handed over as the region
# B must stay clear of -- tagged with A's net, so the rule applied is the
# different-potential one.
klt gen well_island --pdk sky130A --cell-name island_b -o island_b.gds \
    --params '{"net": "VBODY_B",
               "isolate_from": [[-5.56, -0.15, -1.42, 3.99, "VBODY_A"]]}'
```

Place a `pfet`-flavoured `mos_array` inside each island (its own well merges
into the island's), and `klt extract` reports two distinct body nets:
`devices[].nets["b"]` is `VBODY_A` for the devices in A and `VBODY_B` for
those in B. Nudge island B one database unit closer and the second `klt gen`
call exits `1` instead of drawing a merged well.

### `diff_pair` (family 4: differential pair / current mirror cell)

Composes `mos_array`'s unit-device drawing and `guard_ring`'s ring drawing:
two matched devices, each split into `splits` sub-instances, interleaved in
a true common-centroid cross-quad checkerboard over a 2-row x
`splits`-column grid (`label(row, col) = "A"` if `row + col` is even, else
`"B"` — for `splits=2` this is exactly the classic "A B / B A"
differential-pair layout), optionally enclosed by an automatically-sized
guard ring (`add_guard_ring` — the ring's own thickness and contact count are
fixed; use the standalone `guard_ring` generator directly for a
fully-parametrized ring, or see `ring_padding_um`/`row_spacing_um` below to
grow the ring-to-core and inter-row bands themselves). Ports are
named `Q1_<n>_S`/`_D`/`_G` and `Q2_<n>_S`/`_D`/`_G` (or `M1_`/`M2_` when
`params.mirror` is `true`, for a current-mirror naming convention — geometry
is identical either way), plus `TAP_N`/`TAP_S`/`TAP_E`/`TAP_W` when
`add_guard_ring` is `true` (and a `GAP_<side>` marker when the ring carries a
routing opening — see `guard_ring`'s "Ring routing openings" above).
`device_count` is `2 * splits`.
`drc_hints.matched_group_id` is `"diff_pair:pair:<splits>"` (or
`"diff_pair:mirror:<splits>"` with `params.mirror`; `flavor` is not folded in
— see `mos_array`'s equivalent note above).

**Both legs share one x column per terminal.** Every column of the 2-row
checkerboard holds exactly one `Q1` and one `Q2` sub-instance, and both rows
place their unit device at the same `x0 = col * col_pitch` — so `Q1_<n>_S`
and that column's `Q2_*_S` report **identical `x_um`**, differing only in
`y_um` by one row pitch (likewise `_D` and `_G`). Across the whole cell the
two legs' sets of port x values are equal, for every `splits`. A composed
floorplan in the **one-column-per-pin** style (one horizontal track per net,
reached by a single vertical routing column per pin) therefore cannot route
the two legs: their distinct nets would have to share a column. Route
the two rows out in opposite `y` directions instead — the `_S`/`_D` ports
face `direction_deg` `180`/`0` and the gates `90`; widen `row_spacing_um`
for the band between the rows and `ring_padding_um` for the band outside the
outermost active edge — or, if a one-column-per-net grid is mandatory, place
two independent `mos_array` singles side by side and give up the
common-centroid interleave.

This is inherent to common-centroid interleaving, not a fixable oversight,
and in particular it is *not* removable by a per-leg x offset: pushing one
leg off the shared column by a constant shifts that leg's x centroid
relative to the other's, which is precisely the gradient cancellation the
generator exists to provide. Nor is there a cleverer ordering in general —
placing `2 * splits` units at distinct multiples of one pitch and splitting
them into two halves with an equal x centroid requires the position sum
`splits * (2 * splits - 1)` to be even, which fails for every odd `splits`.

`flavor="pfet"` (composable with `mirror` — a mirror-labelled `pfet` pair is
a PMOS current mirror) encloses the device pair's own active footprint in a
well, independent of `add_guard_ring` (a PMOS pair with no automatic ring
still needs its own well). `flavor="nfet"` (the default) draws no additional
well shape — the default case's output is unchanged. Note that
`add_guard_ring`'s own well tie (see `guard_ring` above) is orthogonal to
`flavor`: on a PDK family whose curated deck checks a well layer, the
automatically-sized ring already draws its own well tie regardless of
`flavor`, same as the standalone `guard_ring` generator's `add_well` default.

`voltage_flavor` (issue #1054), same semantics as `mos_array`'s own param of
the same name: an opt-in, orthogonal-to-`flavor` medium-voltage/thick-oxide
device-class marker over the device pair's own footprint. The default, an
empty string, draws nothing; `voltage_flavor="medium_voltage"` on `gf180mcu`
draws its `Dualgate` marker layer, and `voltage_flavor="hv"` on
`ihp-sg13g2`/`ihp-sg13cmos5l` draws their shared `ThickGateOx` marker layer
(extracting as `sg13_hv_nmos`/`sg13_hv_pmos` instead of the default
thin-oxide class) — either echoes the selection in
`drc_hints.voltage_flavor`/`drc_hints.voltage_flavor_mark_present` (see
below); any other value on any family (including every value on `sky130`,
which cites no such marker layer) draws nothing and is reported via
`drc_hints.notes`, never silently dropped.

`w_um` below `0.42` µm is rejected the same way `mos_array`'s own `w_um` is —
see `mos_array`'s "Below `UNIT_MIN_W_UM`: dog-bone terminals drawn by hand"
above for why, and the hand-drawn-recipe workaround.

| `params` field    | Type   | Default | Description |
| ------------------ | ------ | ------- | ----------- |
| `w_um`             | double | `0.42`  | Unit device width (µm). Must be `>= 0.42` (the smallest width that fits an enclosed contact -- a generator-side structural floor, not a target PDK's own diffusion-width minimum). |
| `l_um`             | double | `0.28`  | Gate length (µm). Must be `> 0`. Composes `mos_array`'s unit device, so it inherits the same S/D-pad-to-gate padding (issue #1187) below `0.28`um — see `mos_array`'s own `l_um` row above. |
| `splits`           | int    | `2`     | Interleaved sub-instances per device (cross-quad splits). Must be `>= 1`. Every column holds one `Q1` and one `Q2` sub-instance at the same x, at any `splits` — see "Both legs share one x column per terminal" above before floorplanning a one-column-per-net grid around this generator. |
| `add_guard_ring`   | bool   | `true`  | Enclose the pair in an automatically-sized guard ring. |
| `ring_gap_side`    | string | `""`    | Cut one routing opening through the guard ring on this side (`""`/`"N"`/`"S"`/`"E"`/`"W"`) — see `guard_ring`'s "Ring routing openings" above. |
| `ring_gap_um`      | double | `0.0`   | Length of that opening along its side (µm). Required (`>= 0.4`) with `ring_gap_side`, `0` otherwise. |
| `ring_gap_offset_um` | double | `0.0` | Slide the opening off its side's midpoint (µm) — e.g. onto the row of device ports a route needs to reach. |
| `ring_padding_um`  | double | `0.5`   | Padding between the device core and the guard ring's inner edge (µm), when `add_guard_ring` is set. Must be `>= 0`. Widening this grows the band between the outermost active edge and the ring — the only room available for a gate contact's routing stub (issue #484). |
| `row_spacing_um`   | double | `0.4`   | Spacing between the two interleaved device rows (µm). Must be `>= 0`. Widening this grows the inter-row band both matched devices' gate contacts share (issue #484). |
| `mirror`           | bool   | `false` | Label devices `M1`/`M2` (current mirror) instead of `Q1`/`Q2` (differential pair) — naming only. |
| `flavor`           | string | `"nfet"`| Device flavor: `"nfet"` (no additional well drawn) or `"pfet"` (device pair enclosed in a well on PDK families that check one). Must be `"nfet"` or `"pfet"`. |
| `voltage_flavor`   | string | `""`    | Optional medium-voltage/thick-oxide device-class marker — same semantics as `mos_array`'s own `voltage_flavor` above. `"medium_voltage"` resolves on `gf180mcu`, `"hv"` resolves on `ihp-sg13g2`/`ihp-sg13cmos5l`; any other value draws nothing and is flagged via `drc_hints.notes`. |
| `gate_contact`     | bool   | `false` | Draw a contact + local-metal pad on each gate landing pad and report `*_G` on the `metal` role instead of `poly` — see `mos_array`'s equivalent note above. Grows each device row (and the automatically-sized guard ring with it) by `0.4` µm. |

### `bjt_array` (phase 4: matched vertical-bipolar / PNP array)

A `rows` × `cols` common-centroid (or plain-`array`) grid of identical unit
vertical-bipolar devices, with `dummy` flanking columns each side. Each unit
device is an emitter diffusion pad beside a base-tie diffusion pad (each with
a contact and a covering local-metal pad, plus a `tap` shape over the
base-tie contact so `klt extract` resolves the base terminal to a real net
instead of a floating node); all units share **one** base well (drawn on both
`sky130` and `gf180mcu` — see `guard_ring` above for the well-layer numbers)
and are surrounded by a single collector guard ring. Every unit is
individually covered by a device-mark layer, drawn on every PDK family whose
*extraction* deck declares a bipolar marker (issue #432) — enclosing only
that unit's emitter pad, not the whole array or the base-tie pad next to it:

- **gf180mcu**: `DRC_BJT`, which its curated *DRC* deck's
  `bjt.separation.comp.1` / `BJT.3` rule also keys off.
- **sky130**: `pnp.drawing` — no curated *DRC* rule checks this layer, but
  `klt extract --deck sky130`'s device recognition does (mirrors `res_array`'s
  `res_mark`, issue #369).

On sky130 only (issue #491), each *dummy* unit's device-mark footprint is
additionally covered by the curated `dummy` marker layer, so `klt extract`'s
dummy-device suppression (see "Dummy devices: the `dummy` marker layer" in
`docs/cli/extract.md`) drops it from the extracted netlist instead of
reporting it as a spurious unmatched device under `klt lvs` — gf180mcu draws
no equivalent marker.

Ports are named `Q<i>_E` (emitter) and `Q<i>_B` (base) per unit device, plus
`COLL_N`/`COLL_S`/`COLL_E`/`COLL_W` on the collector ring when
`add_collector_ring` is `true` (and a `GAP_<side>` marker when that ring
carries a routing opening — see `guard_ring`'s "Ring routing openings" above;
the `COLL_*` taps sit on the diffusion role, the `GAP_*` marker on the metal
role a route crosses the ring on). `device_count` is `rows * cols` (dummies
excluded). `drc_hints.matched_group_id` is
`"bjt_array:<rows>x<cols>:<topology>:ratio<ratio>"`.

**This generator draws from base layers, not from a vendor library cell.** The
result is a DRC-clean, matching-faithful *floorplan* of the device (layer
stack + common-centroid/dummy arrangement), **not** a SPICE-model-exact
replacement for the PDK's characterized vertical PNP — the curated decks check
no implant layer, so emitter/base/collector all draw on the one diffusion
role. See [`docs/design/gen-bjt-array-spike.md`](../design/gen-bjt-array-spike.md)
for why draw-from-scratch was chosen over vendor-cell instantiation, and the
fidelity limits that choice carries.

| `params` field       | Type   | Default            | Description |
| -------------------- | ------ | ------------------ | ----------- |
| `emitter_um`         | double | `0.6`              | Emitter diffusion side length (µm). Must be `>= 0.42`. |
| `rows`               | int    | `3`                | Array rows. Must be `>= 1`. |
| `cols`               | int    | `3`                | Array columns. Must be `>= 1`. |
| `topology`           | string | `common_centroid`  | `array` (row-major) or `common_centroid` (centroid-symmetric pairing, same order convention as `mos_array`). |
| `dummy`              | int    | `1`                | Dummy unit-device columns added on each side of the array. Must be `>= 0`. |
| `ratio`              | int    | `8`                | Intended emitter matching ratio (e.g. `8` for a bandgap's 8:1 group) — recorded in `matched_group_id`; a `drc_hints.notes` entry warns if the array is too small to realise it. Must be `>= 1`. |
| `add_collector_ring` | bool   | `true`             | Surround the array with a collector/substrate guard ring. |
| `ring_gap_side`      | string | `""`               | Cut one routing opening through the collector ring on this side (`""`/`"N"`/`"S"`/`"E"`/`"W"`) — see `guard_ring`'s "Ring routing openings" above. |
| `ring_gap_um`        | double | `0.0`              | Length of that opening along its side (µm). Required (`>= 0.4`) with `ring_gap_side`, `0` otherwise. |
| `ring_gap_offset_um` | double | `0.0`              | Slide the opening off its side's midpoint (µm) — e.g. onto the column of emitter ports a route needs to reach. |

### `bond_pad` (chip-boundary bond pad, issue #568)

Every generator above draws a *core analog* device, on the shared `metal`
role (li1/Metal1) every one of them uses. `bond_pad` is the first generator
covering the chip **boundary** instead: a passivation opening (the `pad`
role) enclosed by the resolved PDK family's own **topmost** routing metal
(the new `top_metal` role — sky130's `met5.drawing` `(72, 20)`, gf180mcu's
`Metal5` `(81, 0)`), overlapping the opening by `enclosure_um` on every side.
That overlap is gf180mcu's *only* hard, DRC-coded bond-pad rule — DRM 9.1
"PAD.4" ("Top layer metal overlap of pad opening" → 2.0µm, transcribed at
`decks/gf180mcu.py`'s `pad.enclosing.metal5.1`) — so `enclosure_um`'s default
and hard floor (`PAD_TOP_METAL_ENCLOSURE_MIN_UM`) is `2.0`; a request below it
is rejected outright (`GenError`), the same treatment `guard_ring`'s
`ring_width_um` gets against its own structural floor. sky130's curated deck
has no equivalent hard rule (only an unrelated 1.27µm pad-to-pad *spacing*
check) — the same constant is applied to both families as a conservative
floor, binding only on gf180mcu today.

`bond_type` (`"wedge"` default, `"ball_cup"`, or `"bump"`) selects which of
gf180mcu's DRM 9.2 "PAD.1" **guideline** (not DRC-hard) minimum pad-opening
sizes `opening_um` is checked against — 40µm for `wedge`/`ball_cup`, 4µm for
`bump`. Unlike `enclosure_um`, this is only a guideline: a smaller
`opening_um` is flagged via `drc_hints.notes`, never rejected. sky130's
curated deck has no equivalent guideline table; the same values apply there
too.

**Known limitation — gf180mcu 6LM is out of scope.** gf180mcu ships both a
5-metal (5LM) and 6-metal (6LM) stack; 6LM's true top metal is `MetalTop`
`(53, 0)`, not `Metal5`. The resolved `pdk.variant` string (`gf180mcuA`-`D`)
names voltage/process options, never the metal-stack height, so this
generator's gf180mcu output **always** assumes 5LM and resolves `top_metal`
to `Metal5` — matching `pad.enclosing.metal5.1`'s own scoping note. A caller
on a real 6LM gf180mcu variant gets 5LM-shaped output; a `drc_hints.notes`
entry says so on every gf180mcu request. Widening this to a real
variant-selection mechanism is a documented follow-up, not silently punted.

**Known limitation — `down_to`/`via_style` are forward-looking.** A bond pad
in a real layout straps its top-metal pad down through a via/metal stack to
wherever the core circuit routes — but neither curated deck models a via
role between `top_metal` and any lower level this generator's sibling
generators already expose (`metal`/`metal2`/`metal3` for sky130, and — since
issue #1058 — the same `metal`/`metal2`/`metal3` for gf180mcu; see
`_PDK_ROLE_LAYERS`'s `"top_metal"` entry).
`down_to` therefore supports only its default, `"top_metal"` (no via drawn);
any other value is rejected (`GenError`) rather than silently drawing a
DRC-illegal via between layers that were never meant to touch directly.
`via_style` (`"ring"` default — a peripheral ring of vias, or `"array"` — a
filled via array) is validated as a closed set but currently has no
geometric effect until `down_to` gains a supported lower level; a
`drc_hints.notes` entry says so on every request.

The pad reports exactly one port, `PAD`, on the `top_metal` role, centred at
the cell origin with `width_um` equal to `opening_um`. `device_count` is
always `1` (the pad itself). **`drc_hints.matched_group_id` is always
`null`** — a bond pad has no matching concept, the same as `guard_ring`.

| `params` field  | Type   | Default        | Description |
| --------------- | ------ | -------------- | ----------- |
| `opening_um`    | double | `40.0`         | Pad opening (passivation window) side length (µm). Must be `> 0`; below the `bond_type` guideline minimum is flagged via `drc_hints.notes`, not rejected. |
| `bond_type`     | string | `"wedge"`      | Bond process: `"wedge"` (wire bond without CUP), `"ball_cup"` (wire bond with CUP), or `"bump"` (gold bump) — selects the PAD.1 guideline minimum opening size. Must be one of these three. |
| `enclosure_um`  | double | `2.0`          | Top-metal overlap of the pad opening on every side (µm). Must be `>= 2.0` (gf180mcu's PAD.4 hard rule). |
| `down_to`       | string | `"top_metal"`  | Lowest metal level the pad straps to. Only `"top_metal"` is supported today — see "Known limitation" above. |
| `via_style`     | string | `"ring"`       | Via arrangement once `down_to` supports a level below `top_metal`: `"ring"` or `"array"`. Currently has no geometric effect — see "Known limitation" above. |

### `esd_device` (grounded-gate multi-finger ESD protection MOS)

Composes `mos_array`'s unit-device drawing and `guard_ring`'s ring drawing —
the same composition mechanism `diff_pair` already uses for families 1 and 3
(see `diff_pair` above): one multi-finger MOS unit device (`fingers` gate
stripes across a shared diffusion strip, the standard ggNMOS ESD-clamp
layout idiom), optionally enclosed by an automatically-sized tap ring
(`add_guard_ring` — the ring's own thickness and contact count are fixed,
same as `diff_pair`'s own automatic ring; use the standalone `guard_ring`
generator directly for a fully-parametrized ring). Always an NMOS-style
device — unlike `mos_array`/`diff_pair` there is no `flavor` option, and
unlike every other ring-composing generator here, **the tap ring itself
draws no well tie either**: a grounded-gate ESD clamp is conventionally
NMOS, and enclosing it in an Nwell the way `guard_ring`'s own `add_well`
default would silently misclassifies the device as `pfet` under `klt
extract`'s `active & nwell` test — the same well `diff_pair` already
suppresses for its own default `flavor="nfet"` case. Ports are named
`M1_S`/`M1_D`/`M1_G`, plus
`TAP_N`/`TAP_S`/`TAP_E`/`TAP_W` when `add_guard_ring` is `true` (and a
`GAP_<side>` marker when the ring carries a routing opening — see
`guard_ring`'s "Ring routing openings" above). `device_count` is always `1`.
`drc_hints.matched_group_id` is always `null` — a single ESD device has no
matching concept.

Two additional marker layers, neither DRC-checked by either curated deck:

- **`esd_mark`** — an unconditional device-class marker, drawn whenever the
  resolved PDK family cites one (mirrors `bjt_mark`'s own
  always-on-when-present precedent). **gf180mcu** reuses `Dualgate` `(55,
  0)` — the same layer the curated deck's junction-diode extraction already
  ties to the PDK's own ESD-clamp library ("the two 6V (`Dualgate`-marked,
  medium-voltage) flavours are the ones the PDK's own I/O library uses for
  its ESD clamps"). **sky130** cites no numbered layer for this role in this
  repo's curated deck (`sky130.lvs`'s own exclusion set names `hvtr`/`hvtp`
  only, with no transcribed layer/datatype pair) — the role is simply
  omitted there, reported via `drc_hints.notes` like every other
  role-absent case `guard_ring`'s `add_well` note documents.
- **`salicide_block`** (`params.salicide_block`, opt-in, default `false`) —
  a ballast-style unsalicided region drawn over the whole finger-array
  footprint (a "floorplan fidelity, not a process-exact cross-section"
  approximation, the same trade-off `bjt_array`'s own device draws from base
  layers rather than a vendor cell). **gf180mcu** reuses `SAB` `(49, 0)` —
  the *same* salicide-block layer `res_array`'s own `"generic"` flavour
  already cites, not a second private one. **sky130** cites none in this
  repo's curated deck; requesting it there is a documented no-op (via
  `drc_hints.notes`), not an error.

`finger_width_um`/`fingers` are validated against this generator's own
hardcoded, engineering-derived bounds (not a literal transcribed DRM rule id
— see `ESD_FINGER_WIDTH_MIN_UM`/`ESD_FINGER_WIDTH_MAX_UM`/
`ESD_MAX_FINGERS_PER_RING`'s own docstrings in `gen.py` for the full
provenance caveat, which mirrors the `bond_pad` sibling issue's
guideline-vs-DRC-coded-table finding): a finger narrower than
`ESD_FINGER_WIDTH_MIN_UM` (`0.42`µm, `UNIT_MIN_W_UM`) is a structural error
(no room for an enclosed contact); a finger wider than
`ESD_FINGER_WIDTH_MAX_UM` (`20.0`µm) or a `fingers` count above
`ESD_MAX_FINGERS_PER_RING` (`32`) risks the non-uniform per-finger
current-sharing an ESD pulse depends on avoiding.

| `params` field       | Type   | Default | Description |
| --------------------- | ------ | ------- | ----------- |
| `finger_width_um`    | double | `2.0`   | Width of each gate finger (µm). Must be `>= 0.42` and `<= 20.0`. |
| `l_um`               | double | `0.28`  | Gate length (µm). Must be `> 0`. |
| `fingers`            | int    | `4`     | Gate fingers. Must be `>= 1` and `<= 32`. |
| `add_guard_ring`     | bool   | `true`  | Enclose the device in an automatically-sized tap ring. |
| `ring_gap_side`      | string | `""`    | Cut one routing opening through the tap ring on this side (`""`/`"N"`/`"S"`/`"E"`/`"W"`) — see `guard_ring`'s "Ring routing openings" above. |
| `ring_gap_um`        | double | `0.0`   | Length of that opening along its side (µm). Required (`>= 0.4`) with `ring_gap_side`, `0` otherwise. |
| `ring_gap_offset_um` | double | `0.0`   | Slide the opening off its side's midpoint (µm). |
| `ring_padding_um`    | double | `0.5`   | Padding between the finger array and the tap ring's inner edge (µm), when `add_guard_ring` is set. Must be `>= 0`. |
| `gate_contact`       | bool   | `false` | Draw a contact + local-metal pad on the gate landing pad and report `M1_G` on the `metal` role instead of `poly` — see `mos_array`'s equivalent note above. |
| `salicide_block`     | bool   | `false` | Draw the PDK's salicide-block layer over the finger array on families that curate one (gf180mcu only — see above). |

## Output database unit (dbu)

**Every generator writes its GDS at the resolved PDK's own database unit,
read from that PDK's tech LEF `DATABASE MICRONS` declaration** — `0.001µm`
for a PDK declaring `DATABASE MICRONS 1000` (sky130), `0.0005µm` for one
declaring `DATABASE MICRONS 2000` (gf180mcu). The resolved value is reported
as the response's `dbu_um`.

This is the same tech-LEF-derived resolution
[`klt place-and-route`](place-and-route.md)'s DEF→GDS merge already performs,
so a `klt gen` block and a `klt place-and-route` macro **resolved against the
same PDK agree on their dbu by construction** — which is what lets
[`klt gen-compose`](gen-compose.md) place them in one composed cell (it
requires a single shared dbu across every block and rejects a mismatch as an
application error). Before this, `klt gen` wrote at a fixed `0.001µm`
regardless of the PDK, so mixing the two verb families in one `gen-compose`
request was impossible for any PDK whose tech LEF declares something other
than `DATABASE MICRONS 1000` (issue #1496).

Notes and guarantees:

- **Fallback.** A resolved PDK that ships no readable tech LEF, or whose tech
  LEF declares no `DATABASE MICRONS`, falls back to `0.001µm` — the value
  `klt gen` always used. Resolution never fails a request that previously
  succeeded.
- **Multiple tech LEFs.** The lookup is family-wide (every
  `libs.ref/<library>/techlef/*.tlef` under the resolved install). Real open
  PDKs declare one value across the whole family; if they ever disagreed, the
  **finest** declared dbu wins, so the answer does not depend on library
  naming.
- **Geometry is unchanged.** A finer dbu changes only the integer grid the
  shapes are stored on, not the shapes: `bbox_um`, `ports[].x_um`/`y_um`, and
  every drawn polygon are identical in micrometres, and every generator's
  documented default `params` stay `klt drc --deck gf180mcu` clean at
  `0.0005µm`. sky130 output is byte-identical to before.
- **`dbu` is not a request parameter.** It is a property of the resolved PDK,
  not a caller knob — there is no `options.dbu`. To compose blocks at a given
  dbu, resolve them all against the same PDK.

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
  "dbu_um": 0.001,
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
| `dbu_um` | number | Database unit (µm) the output stream was written at, resolved from the PDK's own tech LEF — see "Output database unit (dbu)" above. Every block handed to [`klt gen-compose`](gen-compose.md) must agree on this value. |
| `bbox_um` | object | Bounding box of the generated cell in micrometres — `_um` suffix per this repo's units-in-field-name convention (`dbu_um` in `klt layers`). |
| `device_count` | integer | Number of unit instances placed (`resistor_strip`'s `num`). |
| `ports` | array\<object\> | Named terminals for downstream connection — see below. |
| `drc_hints` | object | DRC-relevant metadata the generator itself already knows — see below. Advisory only; `klt drc` remains the actual authority on rule compliance. |
| `warnings` | array\<string\> | Non-fatal generator notes (e.g. a requested dimension was snapped to the technology grid). Always present, empty when there is nothing to report. |

#### `ports[]` entries

| Field | Type | Description |
| ----- | ---- | ----------- |
| `name` | string | Stable port/pin name — see each generator's section above for its naming convention (`P1`/`P2` for `resistor_strip`, `U<i>_S`/`_D`/`_G` for `mos_array` (plus a shared `WELL_TAP` on `flavor="pfet"`, `sg13cmos5l` only — issue #1473), `Q<i>_E`/`_B` for `bjt_array`, `PAD` for `bond_pad`, `M1_S`/`_D`/`_G` for `esd_device`, `C<i>_TOP`/`_BOT` for `cap_array`, etc). |
| `net` | string \| null | The net this port carries. `null` for every generator except `well_island` (issue #1421), whose `TAP_*` ports carry its `params.net` — the one request field that feeds a port's net today. A `GAP_*` marker is never given one (it marks the *absence* of ring conductor). |
| `layer` | object | `{ layer, datatype, name }` — the same triple `klt layers` reports, resolved against the *actual* PDK-family layer for the four phase-2 generators (see `klayout_tools.gen._PDK_ROLE_LAYERS`); `resistor_strip` always reports its fixed placeholder. `name` is always `null` (no per-PDK layer-*name* lookup is wired up yet). |
| `x_um`/`y_um` | number | Port location in micrometres, relative to the cell origin. |
| `width_um` | number | Port width. |
| `direction_deg` | number | Outward-facing direction in degrees (`0`/`90`/`180`/`270`). |

#### `drc_hints` fields

| Field | Type | Description |
| ----- | ---- | ----------- |
| `min_spacing_um` | number | The tightest design-rule spacing the generator actually used (or its own safe-margin constant, for a generator with no single caller-supplied spacing param — see each generator's section above). |
| `matched_group_id` | string \| null | Identifier tying together instances that must remain matched. Non-null for the array/matched-device generators (`mos_array`, `res_array`, `diff_pair`, `bjt_array`, `cap_array`); always `null` for `resistor_strip`, `guard_ring`, `well_island`, `bond_pad`, and `esd_device`, none of which has a matching concept. |
| `snapped_to_grid` | boolean | Whether any requested dimension was rounded to the technology grid (`true`) or used exactly as given (`false`). |
| `notes` | array\<string\> | Free-form, generator-specific DRC-adjacent notes — e.g. a `params` value that is legal but risks violating the target PDK's DRC deck (the spike's "advisory, not authoritative" semantics: such a value is *not* rejected, only flagged here). Always present, empty when there is nothing to report. |
| `voltage_flavor` | string \| null | `mos_array`/`diff_pair` only (issue #1054): echo of the request's `params.voltage_flavor`, or `null` when omitted/empty — lets a downstream tool (`klt gen-compose`, `klt extract`, `klt lvs`) see the requested device-class marker without re-deriving it from raw geometry. |
| `voltage_flavor_mark_present` | boolean | `mos_array`/`diff_pair` only (issue #1054): whether a real marker layer was drawn for `voltage_flavor` on the resolved PDK family. `false` both when `voltage_flavor` is omitted/empty and when it was requested but not recognised (see `notes` for the latter case). |
| `well_net` | string \| null | `well_island` only (issue #1421): the net its tie carries (echo of `params.net`), or `null` when unnamed. |
| `well_box_um` | object \| null | `well_island` only: `{x0, y0, x1, y1}` of the well rectangle actually drawn, at the resolved (possibly trimmed) enclosure. `null` on a PDK family with no well layer. |
| `well_separation_um` | number \| null | `well_island` only: the well-to-well clearance actually enforced against `params.isolate_from` — the resolved family's own different-potential rule unless `params.separation_um` raised it. |
| `well_keepout_box_um` | object \| null | `well_island` only: `well_box_um` grown by `well_separation_um` on every side — the region no *other* well may enter. |

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
[the per-generator tables above](#generators) document by hand:

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

`params[].type` is one of `int`, `double`, `string`, `bool`, or `list` (the
KLayout PCell parameter types this harness supports). A `list` param takes a
JSON array — `well_island`'s `isolate_from` is the only one today; its
element *shape* is that generator's own business, documented in its section
above and validated by it. Implementation-only
parameters a generator's PCell declares (e.g. `resistor_strip`'s drawing
layer) are never listed — `params` documents exactly the fields a request's
`params` object may set.

## PDK-shipped PCells (`--list-pdk-pcells` / `--pdk-pcell`)

Every generator documented above is one klt *authored* — a
`pya.PCellDeclarationHelper` subclass living in `src/klayout_tools/gen.py`. A
PDK also ships **its own** PCell library, the authoritative drawing of that
PDK's devices, as importable Python packages under
`libs.tech/klayout/python/<package>/`. These two flags (issue #1535) reach
those directly, so a caller never has to hand-transcribe a vendor PCell's
geometry into a klt generator — the exact drift-prone step this command exists
to avoid.

klt is a **thin passthrough** here, per
[`docs/ARCHITECTURE.md`](../ARCHITECTURE.md)'s "wrap the proven engine" rule:
it puts the PDK's own `python/` directory on `sys.path`, imports the vendor
package, lets the vendor's own `pya.Library` register itself under the
vendor's own name, and drives KLayout's own `Layout.add_pcell_variant()`
against the vendor's own declaration — the same sequence the PDK's own KLayout
autoload macro performs. klt never re-executes, re-implements, or
reinterprets vendor PCell code.

`klt pdk find`/`klt pdk list` carry a `has_pcell_library` boolean
([`docs/cli/pdk.md`](pdk.md)) so a caller can discover PyCell availability
without invoking this command at all. As of issue #1610,
[`klt pdk pcell-check`](pdk.md#klt-pdk-pcell-check) goes one step further and
surfaces this very importability probe (the same `unavailable`/`libraries`
result `--list-pdk-pcells` computes below) directly from `klt pdk`, so
checking whether a PDK's own PyCell library actually *imports* here never
requires invoking `klt gen` at all.

### Phase 1 scope: packages that import with no extra compat layer

A PDK's PyCell package routinely imports third-party modules klt has no
dependency on. klt does **not** vendor or reimplement those. Verified against
the three real installs available when this landed:

| Install | Ships `libs.tech/klayout/python/`? | Shape | Loadable with klt's own dependencies? |
| --- | --- | --- | --- |
| `sky130A` (volare) | yes — `cells`, `import_netlist` | **plain `pya`** (`class pfet(pya.PCellDeclarationHelper)`, registered by `class sky130(pya.Library)`) — *not* Cadence-DLO/`cni`-based | no — its `__init__` chain reaches `import gdsfactory` (and `kfactory`), a genuine third-party PyPI dependency klt has no relationship to. The PDK's own `pymacros/sky130_pcells.lym` checks for exactly that import and disables the PCells when it is absent. |
| `ihp-sg13g2` | yes — `sg13g2_native_pcell_lib`, `sg13g2_pycell_lib`, … | `sg13g2_native_pcell_lib` is plain `pya`, but transitively imports `sg13g2_pycell_lib`, whose first statement is `from cni.tech import Tech` | **yes, on a fully-provisioned install** (issue #1630) — `cni` is not a third-party PyPI package but the `pycell4klayout-api` compat layer *the PDK itself vendors*, at `pycell4klayout-api/source/python/cni`, one level deeper than the `sys.path` entry klt adds for every other package. klt adds that extra, PDK-relative directory to `sys.path` for this specific package before importing it (see `_VENDOR_COMPAT_SHIMS` in `src/klayout_tools/pdk_pcell.py`), and works around an unrelated `tkinter`-presence crash inside that same shim's PCell-instantiation path. Still reported `unavailable` when `pycell4klayout-api` itself checked out empty (an uninitialized git submodule) — see the `reason` breakdown below. |
| `gf180mcuD` (volare) | **no** `python/` at all | — | n/a (`--list-pdk-pcells` enumerates empty, exit `0`) |

A package that cannot be imported here is reported, never guessed at:

- `--list-pdk-pcells` lists it under `unavailable` with the missing module
  **named**, and still reports every library that *did* load. One unloadable
  package must not hide the rest, and "this PDK ships PCells but needs `X`
  installed" is precisely what the enumeration exists to answer.
- `--pdk-pcell` fails with an **application error** (exit `1`) naming the
  missing module, since there it is fatal. Never a traceback.

For a genuine third-party PyPI dependency (sky130A's `gdsfactory`), the fix is
always to install the named module into the environment (per the PDK's own
setup docs), not for klt to ship a copy of it — klt does not vendor or
reimplement arbitrary vendor dependencies as a matter of course. A PDK-bundled
compat shim klt already knows how to reach (ihp-sg13g2's `cni`, above) is a
narrow, deliberate exception to that rule: it is a small, per-package-name
`sys.path` hint table, not a vendored copy of the shim itself. See
[`unavailable[].reason`](#--list-pdk-pcells-json) below for how the two cases
are told apart when a package still fails to load.

### `--list-pdk-pcells` JSON

```json
{
  "schema_version": 1,
  "pdk": { "name": "ihp-sg13g2", "variant": "ihp-sg13g2", "version": null },
  "pcell_lib_dir": "/…/ihp-sg13g2/libs.tech/klayout/python",
  "libraries": [
    {
      "library": "SG13_native_pcell_lib",
      "package": "sg13g2_native_pcell_lib",
      "description": "SG13G2 Native PCells",
      "cells": [
        {
          "name": "Via",
          "params": [
            { "name": "w", "type": "double", "default": 1.0, "description": "Width (um)", "hidden": false, "settable": true }
          ]
        }
      ]
    }
  ],
  "unavailable": [
    { "package": "sg13g2_pycell_lib", "missing_dependency": "cni", "reason": "requires the PDK's own bundled compat shim 'cni' (normally vendored at 'pycell4klayout-api', next to 'sg13g2_pycell_lib'), but that directory is missing or appears empty in this install -- likely an uninitialized git submodule. This is a compat shim the PDK itself vendors, not a third-party PyPI dependency klt would need to add." }
  ]
}
```

The example above is the specific `ihp-sg13g2` case where `pycell4klayout-api`
checked out empty (a release tarball, or an uninitialized git submodule). On a
fully-provisioned install (`git submodule update --init --recursive`) `cni`
imports and `sg13g2_pycell_lib` loads instead — see the table above (issue
#1630). A genuinely missing third-party dependency (sky130A's `cells` needing
`gdsfactory`) keeps the original, simpler message:
`{ "package": "cells", "missing_dependency": "gdsfactory", "reason": "requires
Python module 'gdsfactory', which is not importable in this environment" }`.

- `pcell_lib_dir` — the resolved `libs.tech/klayout/python` directory, or
  `null` when the PDK ships none. A PDK with no such directory returns
  `libraries: []`, `unavailable: []` and exit `0` — a successful "this PDK
  ships none", never an error.
- `library` — the name the vendor's own `pya.Library.register()` call used.
  This, not the Python package name, is the `<library>` half of
  `--pdk-pcell <library>/<cell>`. `package` reports which Python package it
  came from.
- `params[].type` — as for `--list` (`int`, `double`, `string`, `bool`,
  `list`), plus `layer`, `shape`, `callback`, and `none` for the KLayout PCell
  parameter types no klt built-in generator declares.
- `params[].settable` — `false` for a `shape`/`callback`/`none` parameter,
  which has no JSON spelling. Such a parameter cannot be set from `--params`
  (the vendor's default is used); setting one anyway is an application error.
- `params[].hidden` — the vendor's own `hidden` flag. Unlike `--list`, hidden
  parameters are **reported** rather than filtered: they are the vendor's
  implementation detail, not klt's, and a headless caller may still need to
  see them.
- `params[].default` — the vendor's default, rendered as JSON. A `layer`
  default becomes KLayout's own `"<layer>/<datatype>"` string (e.g. `"67/20"`),
  which is exactly the spelling `--params` accepts back.
- `unavailable[].reason` — distinguishes three causes, never the same generic
  import-error text:
  - a genuinely **missing third-party PyPI dependency** (issue #1535,
    `missing_dependency` non-`null`): `"requires Python module '<name>', which
    is not importable in this environment"` — klt does not vendor or
    reimplement it; install it per the PDK's own setup docs.
  - an **empty or near-empty vendored git-submodule directory** (issue
    #1610) nested inside the failing package's own directory tree (a
    subdirectory that exists but has no files, or whose only file is an
    empty/comment-only `__init__.py`-shaped stub — the on-disk signature of
    an uninitialized git submodule): `"vendored submodule directory
    '<package>[/<subpath>]' appears empty -- PDK was likely installed from a
    release tarball that does not include git submodule contents"`.
  - a **PDK-bundled compat shim that is missing or empty at its own,
    separate expected location** (issue #1630 — e.g. `sg13g2_pycell_lib`'s
    `pycell4klayout-api`, which sits *beside* the package rather than nested
    inside it): `"requires the PDK's own bundled compat shim '<module>'
    (normally vendored at '<path>', next to '<package>'), but that directory
    is missing or appears empty in this install -- likely an uninitialized
    git submodule. This is a compat shim the PDK itself vendors, not a
    third-party PyPI dependency klt would need to add."` — the fix here is
    the same re-fetch-with-submodules fix as the previous case, never a
    third-party `pip install`.

  See
  [`klt pdk pcell-check`'s "The `reason` string" section](pdk.md#the-reason-string-missing-dependency-vs-empty-vendored-directory)
  for the full breakdown and the fix for each case; both `--list-pdk-pcells`
  and `klt pdk pcell-check` render the identical `reason` text since they
  share the same underlying `list_pdk_pcells` function.

### `--pdk-pcell` response

The response is the **same envelope** a built-in generator emits (so an
existing [`klt gen-compose`](gen-compose.md) block consumer needs no change),
plus one additive `pdk_pcell` object:

```json
{
  "generator": "SG13_native_pcell_lib/Via",
  "pdk_pcell": { "library": "SG13_native_pcell_lib", "cell": "Via", "package": "sg13g2_native_pcell_lib" },
  "cell_name": "Via_0",
  "…": "as documented under `Response` above"
}
```

- `generator` is `"<library>/<cell>"`, and `cell_name` defaults to
  `<cell>_0`.
- `dbu_um` is resolved from the PDK's tech LEF exactly as for a built-in
  generator, so a `--pdk-pcell` block and a built-in block resolved against
  the same PDK agree by construction.
- `device_count` is always `1` and `ports` is always `[]`: a vendor PCell is
  instantiated as an **opaque** cell. klt does not interpret its geometry to
  infer devices or pins — doing so would be exactly the "transcribe a vendor
  artifact" step this feature removes. `drc_hints.notes` carries a line saying
  so; `drc_hints.min_spacing_um` and `matched_group_id` are `null` and
  `snapped_to_grid` is `false` for the same reason.
- `--params` names the *vendor's* parameters, type-checked against the
  vendor's own declaration. Every parameter the request does not name keeps
  the vendor's default, resolved by KLayout itself — klt never restates a
  vendor default.

```bash
# What does this PDK ship, and what do those cells take?
$ klt gen --list-pdk-pcells --pdk ihp-sg13g2 --format json

# Instantiate one of them.
$ klt gen --pdk-pcell SG13_native_pcell_lib/Via --params '{"w": 1.5}' \
    --pdk ihp-sg13g2 -o output/via.gds --format json
```

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
dbu_um: 0.001
bbox_um: (0.0, 0.0) - (9.68, 0.42)
device_count: 4

ports:
  P1  x=0.0  y=0.21  width=0.42  dir=180
  P2  x=9.68  y=0.21  width=0.42  dir=0
```

## Exit codes and errors

| Exit code | Meaning |
| --------- | ------- |
| `0` | Generation succeeded (or `--list`/`--list-pdk-pcells` succeeded); `gds_path` was written and the report above is on stdout. |
| `1` | Application error — unknown generator name, a malformed (not `<library>/<cell>`) or unknown `--pdk-pcell` value, a PDK PCell package that cannot be imported in this environment, unresolvable PDK, invalid/out-of-range `params`, or the `options.output` directory does not exist. |
| `2` | Usage error — no generator name given and no mode flag passed, two mode flags at once (`--list`/`--list-pdk-pcells`/`--pdk-pcell`), a positional generator name alongside `--pdk-pcell`, or a bad `--format` value (from argparse or this command's own usage check). |

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

# A 2x2 common-centroid matched MOS array on sky130A, then verify it's
# DRC-clean:
$ klt gen mos_array --pdk sky130A -o output/mos_array.gds --format json
$ klt drc output/mos_array.gds --deck sky130

# The same defaults on gf180mcu -- every phase-2 generator's documented
# defaults pass klt drc clean on both curated decks:
$ klt gen mos_array --pdk gf180mcuC -o output/mos_array_gf180.gds
$ klt drc output/mos_array_gf180.gds --deck gf180mcu

# A guard-ringed differential pair, current-mirror-labelled:
$ klt gen diff_pair --params '{"mirror": true, "splits": 2}' \
    --pdk sky130A -o output/current_mirror.gds --format json

# A matched vertical-PNP array on gf180mcu (draws the DRC_BJT device mark),
# then verify it's DRC-clean including the bipolar mark-layer rule:
$ klt gen bjt_array --pdk gf180mcuD -o output/bjt_array.gds --format json
$ klt drc output/bjt_array.gds --deck gf180mcu

# A grounded-gate ESD clamp on gf180mcu (draws the Dualgate device mark and
# an unsalicided ballast region), then verify it's DRC-clean:
$ klt gen esd_device --params '{"fingers": 8, "salicide_block": true}' \
    --pdk gf180mcuD -o output/esd_device.gds --format json
$ klt drc output/esd_device.gds --deck gf180mcu
```

## See also

[`klt gen-compose`](gen-compose.md) places a set of already-generated `klt
gen` blocks (each block's own JSON response, from this command's `--format
json` output) into one composed cell — a single horizontal row, with
two-pin point-to-point routing between named ports, per
[`docs/design/gen-composition-spike.md`](../design/gen-composition-spike.md).
Verified end to end against a real sky130 5T OTA (a `diff_pair` +
`diff_pair` (`mirror: true`) + `mos_array` composition) through `klt
extract`/`klt lvs`/`klt sim` — see `gen-compose.md`'s worked example and
"Known limitations" section.
