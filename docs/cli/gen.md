# `klt gen`

Run a named layout generator against a JSON `params` object and a PDK
reference, producing a GDS/OASIS stream plus a structured report. Phase 1 of
Epic #152 landed the request/response contract and a thin
[KLayout PCell](https://www.klayout.de/doc/programming/pcell.html) harness,
proven end-to-end with one reference generator (`resistor_strip`). Phase 2
adds the four analog primitive generators the accepted spike scopes:
`mos_array`, `res_array`, `guard_ring`, and `diff_pair`. Phase 4 (Epic #152,
this document's current state) adds `bjt_array`, a matched vertical-bipolar
(PNP/BJT) array. See
[`docs/design/layout-generator-spike.md`](../design/layout-generator-spike.md)
section 2 for the contract this command implements, and section 4 for the
four phase-2 families' scope; see
[`docs/design/gen-bjt-array-spike.md`](../design/gen-bjt-array-spike.md) for
the phase-4 mechanism-choice decision behind `bjt_array`.

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

`mos_array`, `res_array`, `guard_ring`, and `diff_pair` draw on their
resolved PDK's own curated-DRC-deck layers (the same layer/datatype pairs
`klt drc --deck sky130`/`--deck gf180mcu` check — see
[`klt drc`](drc.md)), so they only support the `sky130`/`gf180mcu` PDK
families (matched by the resolved `pdk.variant`'s prefix, e.g. `sky130A` or
`gf180mcuC`). Any other resolved PDK family is an application error (exit
code `1`). Every generator's **documented default `params`** are chosen to
pass `klt drc --deck sky130` and `klt drc --deck gf180mcu` clean; a
non-default `params` set is not guaranteed to (see each generator's
"advisory, not authoritative" `drc_hints.notes` behaviour below).

### `mos_array` (family 1: matched transistor array)

A `rows` x `cols` grid of identical unit MOS-like devices (active/diffusion
strip + poly gate(s) + contact + local-metal source/drain pads), with
`dummy` extra unit-device columns flanking each side for etch/gradient
matching. The first gate finger carries a **poly landing pad** that extends
one `CONTACT_SIZE_UM + 2*ENCLOSURE_MARGIN_UM` (0.42 µm) square past the
diffusion's gate-side edge, so a contact can land on the gate *outside* the
channel — without it the gate poly shared the diffusion's exact extent and a
contact at the reported gate port straddled the diff edge
(`poly.enclosing.licon.1`/`diff.enclosing.licon.1`) with nowhere legal to sit
(issue #461). The reported `U<i>_G` port now sits at that pad's centre (its
`y_um` is offset past the diffusion edge, and its `width_um` reports the pad
width rather than `l_um`) — a JSON-contract-visible move of the gate port
coordinate.

`gate_contact` (issue #492) finishes that stack rather than leaving it to the
caller: it draws a contact **and** a local-metal pad on the landing pad, and
reports `U<i>_G` on the `metal` role, symmetric with `U<i>_S`/`U<i>_D`. That
is what makes a gate reachable by [`klt gen-compose`](gen-compose.md)'s
router — a `connectivity[]` net naming a *bare-poly* gate port is rejected as
unroutable (no via in the metals stack lands on poly), so without it a gate
still has to be contacted by hand. Enabling it raises the landing pad's
contact region `0.4` µm clear of the diffusion edge before drawing on it: a
contact-enclosure metal square centred on the bare `#461` pad would share an
edge with the S/D local-metal pads either side and merge into one polygon,
shorting the gate to source/drain. The unit device (and, for `diff_pair`, its
automatically-sized guard ring) therefore grows taller, and `U<i>_G`'s `y_um`
moves with it. `gate_contact` defaults to `false`, which reproduces the
bare-poly gate byte-for-byte — a gate you intend to name with
`gen-compose`'s `pins[]` (on the `poly_label` layer) rather than route to
wants the default.

Each unit device contributes three ports: `U<i>_S`/`U<i>_D`
(local-metal) and `U<i>_G` (poly, or local-metal with `gate_contact`), where
`<i>` is the device's position in
the `topology`-selected numbering order — `"array"` numbers row-major;
`"common_centroid"` (the default) numbers nearest-center-first, pairing each
instance immediately with its point-reflection through the array center, so
a downstream matching/LVS consumer can pair index `2k`/`2k+1` as
centroid-symmetric partners. (Physical placement is the same uniform grid
either way — `topology` changes the *numbering*, not the layout; see
`diff_pair` below for a generator that physically interleaves two distinct
devices.) `device_count` is `rows * cols` (dummies excluded).
`drc_hints.matched_group_id` is `"mos_array:<rows>x<cols>:<topology>"`
(`flavor` is not folded in — a matched group is always one generator call by
construction, so every instance in it already shares one `flavor`).

`flavor="pfet"` additionally encloses every unit device's (real and dummy)
active region in a single shared well shape, sized by `WELL_ENCLOSURE_MARGIN_UM`,
on PDK families whose curated deck checks a well layer (both `sky130` and
`gf180mcu`, as of this generator's `flavor` support — see `guard_ring` below
for the layer numbers). `flavor="nfet"` (the default) draws no well shape at
all — output for the default case is unchanged.

| `params` field | Type   | Default            | Description |
| -------------- | ------ | ------------------ | ----------- |
| `w_um`         | double | `0.42`             | Unit device width (µm). Must be `>= 0.42` (the smallest width that fits an enclosed contact -- a generator-side structural floor, not a target PDK's own diffusion-width minimum). |
| `l_um`         | double | `0.28`             | Gate length (µm). Must be `> 0`; below `0.28`um risks violating a target PDK's poly-width or S/D metal-spacing rule (flagged via `drc_hints.notes`, not rejected). |
| `fingers`      | int    | `1`                | Gate fingers per unit device. Must be `>= 1`. |
| `rows`/`cols`  | int    | `2`/`2`            | Array shape. Must each be `>= 1`. |
| `topology`     | string | `"common_centroid"`| `"array"` or `"common_centroid"` — see above. |
| `dummy`        | int    | `1`                | Dummy unit-device columns added on each side. Must be `>= 0`. |
| `flavor`       | string | `"nfet"`           | Device flavor: `"nfet"` (no well drawn) or `"pfet"` (unit devices enclosed in a well on PDK families that check one). Must be `"nfet"` or `"pfet"`. |
| `gate_contact` | bool   | `false`            | Draw a contact + local-metal pad on each gate landing pad and report `U<i>_G` on the `metal` role instead of `poly` — see above. Grows the unit device by `0.4` µm to keep the gate metal clear of the S/D pads. |

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
device additionally requires), so `klt gen res_array`'s output is directly
recognised as a resistor device by `klt extract --deck <pdk>` rather than
being absorbed into ordinary poly interconnect as a short (issue #369).
Neither curated *DRC* deck checks any of these layers, so drawing them never
affects `klt drc` status.

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
`res_generic_po`. gf180mcu exposes only its single `"generic"` flavour;
requesting a sky130-only flavour there raises a clear error. As with the
marker, no curated *DRC* deck checks these masks, so they never affect
`klt drc` status.

| `params` field | Type   | Default | Description |
| -------------- | ------ | ------- | ----------- |
| `length_um`    | double | `2.0`   | Unit resistor body length (µm). Must be `> 0`. |
| `width_um`     | double | `0.42`  | Unit resistor width (µm). Must be `>= 0.42`. |
| `spacing_um`   | double | `0.5`   | Spacing between unit resistors (µm). Must be `>= 0`; below `0.4`um risks violating a target PDK's minimum same-layer spacing rule (flagged via `drc_hints.notes`, not rejected). |
| `num`          | int    | `4`     | Number of matched unit resistors. Must be `>= 1`. |
| `dummy`        | int    | `1`     | Dummy unit resistors added at each end. Must be `>= 0`. |
| `rows`         | int    | `1`     | Fold the `num` unit resistors into this many parallel rows (boustrophedon order) instead of one long row. Must be `>= 1`. |
| `flavor`       | string | `"generic"` | Poly-resistor flavour / recognised device class: `"generic"` (base sheet-rho — `res_generic_po` on sky130, `ppolyf_u` on gf180mcu) or, on sky130 only, `"high"` (`res_high_po`) / `"xhigh"` (`res_xhigh_po`) for the higher-sheet-rho flavours. Must be a flavour the resolved PDK family exposes. |

### `guard_ring` (family 3: substrate/well tap ring)

A tap ring (drawn as a single unbroken outer-box-minus-inner-box polygon, so
there is no same-layer spacing violation between "segments") with
`contacts_per_side` contacts evenly spaced along each side and a
local-metal ring on top, optionally enclosed by a well tie (`add_well`) on
PDK families whose curated deck checks a well layer (gf180mcu's `Nwell`
`(21, 0)`; sky130's `nwell.drawing` `(64, 20)`, matching
`klayout_tools.decks.sky130.EXTRACTION_DECK.nwell`). Both supported PDK
families draw the well tie by default — sky130's curated *DRC* deck happens
to check no rule against that layer (so drawing it there never affects DRC
status), which is different from "the layer doesn't exist"; a family with
neither a well layer nor a well rule would still get the documented no-op,
reported via `drc_hints.notes`. Four ports —
`TAP_N`/`TAP_S`/`TAP_E`/`TAP_W` — sit at the midpoint of each ring side.
`device_count` is the number of tap contacts actually drawn
(`contacts_per_side * 4`, less any the ring opening below clipped away).
**`drc_hints.matched_group_id` is always `null`** — a guard ring has no
matching concept (it is excluded from the matched-device generators: 1, 2,
and 4).

| `params` field      | Type   | Default | Description |
| -------------------- | ------ | ------- | ----------- |
| `inner_width_um`     | double | `3.0`   | Width of the protected inner area (µm). Must be `> 0`. |
| `inner_height_um`    | double | `3.0`   | Height of the protected inner area (µm). Must be `> 0`. |
| `ring_width_um`      | double | `0.42`  | Tap ring thickness (µm). Must be `>= 0.42`. |
| `contacts_per_side`  | int    | `4`     | Tap contacts evenly spaced along each ring side. Must be `>= 1`, and must fit without overlapping (a `contacts_per_side` too large for `inner_width_um`/`inner_height_um` is rejected outright — a structural error, not a DRC-adjacent one). |
| `ring_gap_side`      | string | `""`    | Cut one routing opening through the ring on this side: `""` (a closed ring), `"N"`, `"S"`, `"E"` or `"W"`. See "Ring routing openings" below. |
| `ring_gap_um`        | double | `0.0`   | Length of the opening along its side (µm). Required (and must be `>= 0.4`, the minimum same-layer spacing) when `ring_gap_side` is set; must be `0` otherwise. |
| `ring_gap_offset_um` | double | `0.0`   | Slide the opening off its side's midpoint (µm): `+x` on `"N"`/`"S"`, `+y` on `"E"`/`"W"`. The opening must stay inside the side's straight run. |
| `add_well`           | bool   | `true`  | Enclose the ring in a well tie when the resolved PDK family checks one. |

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
(on its `add_guard_ring` ring) and `bjt_array` (on its `add_collector_ring`
ring). Openings are opt-in: omitting them draws exactly the closed ring
these generators always drew.

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

`flavor="pfet"` (composable with `mirror` — a mirror-labelled `pfet` pair is
a PMOS current mirror) encloses the device pair's own active footprint in a
well, independent of `add_guard_ring` (a PMOS pair with no automatic ring
still needs its own well). `flavor="nfet"` (the default) draws no additional
well shape — the default case's output is unchanged. Note that
`add_guard_ring`'s own well tie (see `guard_ring` above) is orthogonal to
`flavor`: on a PDK family whose curated deck checks a well layer, the
automatically-sized ring already draws its own well tie regardless of
`flavor`, same as the standalone `guard_ring` generator's `add_well` default.

| `params` field    | Type   | Default | Description |
| ------------------ | ------ | ------- | ----------- |
| `w_um`             | double | `0.42`  | Unit device width (µm). Must be `>= 0.42` (the smallest width that fits an enclosed contact -- a generator-side structural floor, not a target PDK's own diffusion-width minimum). |
| `l_um`             | double | `0.28`  | Gate length (µm). Must be `> 0`. |
| `splits`           | int    | `2`     | Interleaved sub-instances per device (cross-quad splits). Must be `>= 1`. |
| `add_guard_ring`   | bool   | `true`  | Enclose the pair in an automatically-sized guard ring. |
| `ring_gap_side`    | string | `""`    | Cut one routing opening through the guard ring on this side (`""`/`"N"`/`"S"`/`"E"`/`"W"`) — see `guard_ring`'s "Ring routing openings" above. |
| `ring_gap_um`      | double | `0.0`   | Length of that opening along its side (µm). Required (`>= 0.4`) with `ring_gap_side`, `0` otherwise. |
| `ring_gap_offset_um` | double | `0.0` | Slide the opening off its side's midpoint (µm) — e.g. onto the row of device ports a route needs to reach. |
| `ring_padding_um`  | double | `0.5`   | Padding between the device core and the guard ring's inner edge (µm), when `add_guard_ring` is set. Must be `>= 0`. Widening this grows the band between the outermost active edge and the ring — the only room available for a gate contact's routing stub (issue #484). |
| `row_spacing_um`   | double | `0.4`   | Spacing between the two interleaved device rows (µm). Must be `>= 0`. Widening this grows the inter-row band both matched devices' gate contacts share (issue #484). |
| `mirror`           | bool   | `false` | Label devices `M1`/`M2` (current mirror) instead of `Q1`/`Q2` (differential pair) — naming only. |
| `flavor`           | string | `"nfet"`| Device flavor: `"nfet"` (no additional well drawn) or `"pfet"` (device pair enclosed in a well on PDK families that check one). Must be `"nfet"` or `"pfet"`. |
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
| `name` | string | Stable port/pin name — see each generator's section above for its naming convention (`P1`/`P2` for `resistor_strip`, `U<i>_S`/`_D`/`_G` for `mos_array`, `Q<i>_E`/`_B` for `bjt_array`, etc). |
| `net` | string \| null | Caller-supplied net label; always `null` — no request field feeds it yet. |
| `layer` | object | `{ layer, datatype, name }` — the same triple `klt layers` reports, resolved against the *actual* PDK-family layer for the four phase-2 generators (see `klayout_tools.gen._PDK_ROLE_LAYERS`); `resistor_strip` always reports its fixed placeholder. `name` is always `null` (no per-PDK layer-*name* lookup is wired up yet). |
| `x_um`/`y_um` | number | Port location in micrometres, relative to the cell origin. |
| `width_um` | number | Port width. |
| `direction_deg` | number | Outward-facing direction in degrees (`0`/`90`/`180`/`270`). |

#### `drc_hints` fields

| Field | Type | Description |
| ----- | ---- | ----------- |
| `min_spacing_um` | number | The tightest design-rule spacing the generator actually used (or its own safe-margin constant, for a generator with no single caller-supplied spacing param — see each generator's section above). |
| `matched_group_id` | string \| null | Identifier tying together instances that must remain matched. Non-null for the array/matched-device generators (`mos_array`, `res_array`, `diff_pair`, `bjt_array`); always `null` for `resistor_strip` and `guard_ring`, neither of which has a matching concept. |
| `snapped_to_grid` | boolean | Whether any requested dimension was rounded to the technology grid (`true`) or used exactly as given (`false`). |
| `notes` | array\<string\> | Free-form, generator-specific DRC-adjacent notes — e.g. a `params` value that is legal but risks violating the target PDK's DRC deck (the spike's "advisory, not authoritative" semantics: such a value is *not* rejected, only flagged here). Always present, empty when there is nothing to report. |

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
