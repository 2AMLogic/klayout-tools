# `klt extract`

Extract a **schematic-equivalent** netlist (devices + connectivity) from a
GDSII or OASIS layout stream, headless, and write it as a SPICE circuit body
plus a structured summary. An opt-in `--parasitics` flag additionally extracts
first-order lumped RC interconnect parasitics (see "Parasitic (RC)
extraction" below).

```
klt extract <file> --deck sky130|gf180mcu [-o|--output <netlist.spice>] [--top <cell>] [--pdk <variant>] [--pdk-root <root>] [--parasitics] [--top-cell-pins] [--pins <A,B,VDD,VSS>] [--format text|json]
```

This is phase 2 of Epic #153 (`klt lvs`/`klt extract`), the build carried by
the accepted spike,
[`docs/design/lvs-extraction-spike.md`](../design/lvs-extraction-spike.md)
(section 2a) — read it first for the engine survey and the reasoning behind
the contract shape below. This document is the shipped contract; where the
two disagree, this document (and the code) win.

- `<file>` — path to a GDSII (`.gds`) or OASIS (`.oas`) file. KLayout
  auto-detects the stream format on read (same as `klt drc`); the extension
  is not authoritative.
- `--deck` — required. The connectivity + device-extraction deck to run.
  Currently: `sky130`, `gf180mcu`.
- `--output` / `-o` — path to write the extracted SPICE netlist. Defaults to
  `<file>` with its extension replaced by `.spice`, next to the input (the
  "next to the input" convention `klt render`/`klt sim` already use). The
  output path's parent directory is created automatically if it does not
  already exist (including any missing intermediate directories), matching
  `klt render`/`klt lvs`.
- `--top` — top cell to extract when the stream has more than one (required
  in that case; optional otherwise, and must name the sole top cell if
  given).
- `--pdk` / `--pdk-root` — optional. See "PDK resolution" below.
- `--parasitics` — optional, off by default. Additionally extract first-order
  lumped RC interconnect parasitics (one series R + one ground C per net) as
  extra `R`/`C` cards in the written netlist and a `parasitics` block in the
  JSON. When omitted, the output is byte-identical to a schematic-equivalent
  extraction. See "Parasitic (RC) extraction" below.
- `--top-cell-pins` — optional, off by default. Promote **only** labels drawn
  directly in the top cell to top-level pins. Because extraction is flat
  (`begin_shapes_rec`), the default behaviour promotes *every* named net to a
  pin — including nets that are named only because a label sits inside an
  instanced sub-cell, which are ordinary internal nodes once instanced (issue
  #291). With this flag such a below-top label keeps its net *name* but is not
  forced to a pin, so instancing a verified sub-cell no longer leaks its port
  labels out as spurious parent pins. Independent of the flag, a `warnings`
  entry names any net whose promotion came from a label found below the top
  cell — so the promotion is always visible rather than silently inferred from
  an unexpected pin count. A flat layout (no instances), or any layout whose
  pin labels all live in the top cell, is byte-for-byte unchanged. See
  "Top-cell-only pin promotion" below.
- `--pins` — optional, unset by default. Comma-separated declared pin set
  (e.g. `A,B,VDD,VSS`) — a per-*net* interface declaration, orthogonal to
  `--top-cell-pins`'s per-*cell* one (issue #514). Every named net not in
  this set keeps its name but is demoted to an internal node instead of
  being promoted to a top-level pin — use this to name an internal node of
  a lumped schematic device (e.g. one tap of a metal-option ladder) for
  documentation without blocking `klt lvs`'s `options.combine_devices` from
  folding the series chain. When unset, every named net still promotes to a
  pin, byte-for-byte unchanged. See "Declared pin set" below.
- `--format` — `text` (default, a human-readable summary) or `json`. The
  extracted **netlist** always goes to `--output`; `--format` governs only
  the summary report.

## Engine

`klt extract` runs fully headless via the pip `klayout` package's native
`klayout.db.LayoutToNetlist` (connectivity + device extraction) and
`klayout.db.NetlistSpiceWriter` (SPICE serialisation) — the same wrapped
dependency `klt drc` and `klt render` already use, verified live in the phase
1 spike. There is no dependency on the standalone `klayout` application
binary or any second geometry engine — only `pip install klayout` (already
this repo's sole runtime dependency).

Extraction is **flat**, not hierarchical: every deck layer is a single
flattened `Region`/`Texts` collection over the selected top cell (via
`Cell.begin_shapes_rec`), the same whole-layout flattening idiom `klt drc`
uses. Device recognition splits NMOS (`active - nwell`) from PMOS
(`active & nwell`) and runs KLayout's native `DeviceExtractorMOS4Transistor`
for each — one generic `nfet`/`pfet` device class per deck (no
voltage-flavor distinction). A deck may additionally declare one or more
vertical-BJT device-recognition entries, run through KLayout's native
`DeviceExtractorBJT3Transistor` — see "Bipolar (BJT) device recognition"
below — one or more drawn MiM-capacitor device-recognition entries, run
through KLayout's native `DeviceExtractorCapacitor` — see "MiM capacitor
device recognition" below — and one or more drawn precision-resistor
device-recognition entries, run through KLayout's native
`DeviceExtractorResistor`/`DeviceExtractorResistorWithBulk` — see "Drawn
resistors" under Coverage.

**Because extraction is flat, it does not stop at a macro's instance
boundary.** Issue #456 (Epic #393 Phase 3) confirmed this concretely
against a real mixed layout: a `klt gen diff_pair` analog macro (2 nfet
devices when extracted standalone) placed via `klt place-and-route`'s
`request.macros` field alongside ~15 real `sky130_fd_sc_hd` standard cells.
Extracting the *merged* layout in one `klt extract` run produced **256**
devices (129 nfet + 127 pfet) — since the macro alone contributes only
`nfet`, every one of the 127 `pfet` devices (and 254 of the 256 total) must
have been pulled from the standard-cell region, in the same flat pass as
the macro's own 2 transistors. No code change was needed for this — it
follows directly from `begin_shapes_rec` flattening every instance,
regardless of which verb (or how many process boundaries) placed it. See
[`docs/cli/drc.md`](drc.md) → "Mixed sky130_fd_sc_hd + analog-macro layout"
and [`docs/cli/lvs.md`](lvs.md) → "Mixed sky130_fd_sc_hd + analog-macro
netlist" for the same artifact's DRC/LVS findings.

## Deviation from the spike

The spike's proposed invocation is flag-only (`klt extract <file> --deck
sky130|gf180mcu`), with no PDK-resolver involvement. This command keeps
`--deck` as the required selector of the curated deck (self-contained,
exactly like `klt drc`'s decks — no PDK install is required to run it), and
additionally accepts optional `--pdk`/`--pdk-root` flags resolved through
the one shared resolver every other PDK-aware verb uses (`klt pdk find`'s
resolver, [`docs/cli/pdk.md`](pdk.md)), mirroring `klt sim`'s optional
`models.pdk`/`models.pdk_root` resolution. See "PDK resolution" below.

## Coverage

The `sky130` and `gf180mcu` decks are **curated starter subsets**, the
extraction analogue of `klt drc`'s curated rule decks (see
[`docs/cli/drc.md`](drc.md) → "Coverage"): a two-terminal-well CMOS stack
(one drawn well layer splitting NMOS/PMOS, contact/local-interconnect up
through the PDK's metal stack — sky130's `li1`/`met1`/`met2` and gf180mcu's full
`Metal1`–`Metal5` with `Via1`–`Via4` between them, so a block routed on any
declared level extracts as connected nets, not a pile of disconnected
ones), not a full PDK's device zoo. Both decks
are defined in `src/klayout_tools/decks/sky130.py` /
`src/klayout_tools/decks/gf180mcu.py` as an `ExtractionDeck` (layer roles:
`active`, `poly`, `nwell`, optional `tap`, `contact`, an ordered `metals`
stack with matching `metal_labels`/`vias`, an optional `well_label`, and an
optional `dummy` marker layer for drawn dummy devices — see "Dummy devices:
the `dummy` marker layer") —
each field's exact layer numbers and provenance are documented in the deck
module's own docstring, verified against this repo's real corpus fixtures
(`tests/corpus/sky130/`, `tests/corpus/gf180mcu/`).

Two known connectivity-fidelity limitations, both documented in the deck
modules and deliberate (not oversights):

- **NMOS body.** Neither curated deck draws a separate substrate/pwell
  layer. On **sky130**, `tap.drawing` is reused for both purposes: a shape
  drawn inside an `nwell` is a PMOS well tie (see below), a shape drawn
  outside every `nwell` is a genuine, drawable P-substrate tie — when a
  layout draws one and contacts it up to a named net, the NMOS body
  terminal (and the identically-modelled `bulk_to_substrate` resistor bulk
  and collector-less bipolar collector terminals) resolve to that real net
  (issue #490). Only a layout with **no** such ring falls back to a global
  net (`vsubs` by default) via KLayout's `connect_global`. **gf180mcu** has
  no distinct tap layer at all (`Comp` is shared with transistor active), so
  its NMOS body always falls back to the global net.
- **PMOS body (gf180mcu only).** sky130's curated deck draws well taps on a
  *distinct* layer from transistor active (`tap.drawing`), so the well body
  net picks up its real name via that tap + an `nwell` pin label (verified
  against the sky130 corpus: the PMOS body of a real inverter cell extracts
  to the correct `VPB` pin). gf180mcu's curated layer set has no distinct
  tap layer (`Comp` is shared with the transistor active layer) and no
  well-label layer, so its PMOS body terminal is a floating, anonymous net.

Connecting a well region to *every* contact inside it (rather than only a
genuinely distinct tap region) is deliberately **not** done — the well is a
background region spanning the whole PMOS area, so a blanket rule like that
shorts every transistor terminal inside the well together. See
`ExtractionDeck`'s docstring in `src/klayout_tools/decks/__init__.py` for
the full reasoning.

### Reserved annotation layer

Recording a floorplan reservation, an out-of-scope region, or a black-box
sub-cell placeholder in the GDS stream needs a layer number guaranteed to
stay outside both decks' connectivity graph as their coverage grows — not
just a layer number that happens to be unclaimed by today's deck code (both
decks are curated starter subsets, per "Coverage" above).

**GDS layers 990-999 (any datatype; `(994, 0)` as the single canonical
pair) are reserved for this purpose**, verified against each PDK's full
official layer table — not just the numeric range `ExtractionDeck`'s
`sky130`/`gf180mcu` instances currently declare. The full cross-check
against each PDK's official KLayout technology files and the rationale for
the specific range is in [`docs/cli/drc.md`](drc.md) → "Reserved annotation
layer" (the same reservation applies to both `klt drc` and `klt extract`,
since both read `(layer, datatype)` pairs out of the same PDK layer space).

The marker layer itself carries no connectivity meaning: a shape drawn on it
shows up in `ignored_layers` (see below) with its stream shape count exactly
like any other deck-unclaimed layer, and is never registered with the
connectivity graph. What geometry drawn *underneath* a marker shape does is
covered next.

### Black-box / abstract regions (issue #293)

A shape drawn on a reserved layer (see above) marks a **black-box/abstract
region**: everything drawn geometrically inside it — on any conductor or
label layer this deck's connectivity graph reads (`active`, `poly`, `nwell`,
`tap`, `contact`, `metals`, `vias`, and the `well_label`/`poly_label`/
`metal_labels` label layers) — is excluded from connectivity **before** any
device extractor runs, rather than merely left unread. This is the mechanism
for two situations that previously had no way to record themselves in the
GDS stream itself:

- **A sub-cell that will be drawn later.** Its hierarchy/area needs to exist
  now (so downstream floorplanning/DRC/placement tooling sees it), but its
  content doesn't yet — drawing a black-box marker over the reserved area
  records the placeholder instead of leaving the area undrawn (which loses
  the hierarchy/area record) or documenting the omission only in prose
  outside the GDS.
- **A drawn region deliberately out of scope for a compare.** Geometry that
  exists in the layout but should not participate in this run's `klt
  extract`/`klt lvs` connectivity (e.g. a region carrying non-functional test
  structures) can be marked out without deleting it from the layout.

Resolved *before* every other device-recognition step (drawn resistors,
bipolar, MiM capacitor, and the NMOS/PMOS split itself) — see
`extract.py`'s `_resolve_black_box_regions()` — so device-recognition
geometry that happens to sit inside a black-box region (e.g. a resistor
marker) is excluded outright, not "found" by its own extractor first and
only then short-circuited.

`klt extract`'s JSON response reports every black-box region it excluded in
a new `black_box_regions` field (see "JSON schema" below): one entry per
geometrically separate marker shape (two non-touching marker shapes are
always reported as two entries, never merged into one bbox), each carrying
its bounding box in micrometres and the count of conductor/label shapes the
exclusion actually removed. Always present as a list; empty when the layout
draws no reserved-layer geometry, in which case the rest of the response is
byte-identical to a run before this feature existed. Verifying a black-box
region works therefore needs no new tooling: draw a marker rectangle over a
device, re-run `klt extract --format json`, and confirm that device is
absent from `devices[]`/its nets from `nets[]` while `black_box_regions`
reports the rectangle's bbox with a non-zero `shapes_excluded` — and that
the marker layer itself still shows up in `ignored_layers` exactly as before
(it is never registered with the connectivity graph either way).

**Out of scope**: this mechanism is region-granular — it excludes everything
geometrically inside its bbox, indiscriminately. It is not a substitute for
marking an individual *device* (e.g. a dummy MOS device interleaved with the
functional devices it surrounds) as non-functional; that needs a
device-granular marker, tracked separately.

### Bipolar (BJT) device recognition

Both curated decks additionally declare one vertical-BJT device-recognition
entry (`ExtractionDeck.bipolars`, a tuple of `BipolarDevice` — see its
docstring in `src/klayout_tools/decks/__init__.py`), run through KLayout's
native `DeviceExtractorBJT3Transistor`:

- **sky130**: one `pnp` entry — base = `nwell.drawing` (64/20), emitter =
  `diff.drawing` (65/20), marker = `pnp.drawing` (82/44, the layer
  `sky130_fd_pr__pnp_05v5`-style device-cell instances draw over themselves
  for recognition).
- **gf180mcu**: one generic `bjt` entry (the DRM's `DRC_BJT` mark layer
  covers both NPN and PNP polarities with no single named device cell to
  attribute one to, unlike sky130's `pnp_05v5`) — base = `Nwell` (21/0),
  emitter = `Comp` (22/0), marker = `DRC_BJT` (127/5).

Both entries reuse the deck's own MOS-recognition `nwell`/`active` layers for
`base`/`emitter` rather than introducing dedicated bipolar-only masks — the
`marker` layer is what scopes recognition to genuine device-cell instances:
`extract.py` intersects `base` with `marker` before extraction, so an
ordinary PMOS-only `nwell` drawn elsewhere in the layout is never
misrecognised as a bipolar base. Neither curated deck draws a distinct
collector layer (the vertical bipolar's collector is the substrate itself),
so the collector terminal is tied to the deck's global substrate net
(`vsubs` by default), mirroring the NMOS-body wiring above.

**Base-terminal net resolution — inherits the "PMOS body (gf180mcu only)"
limitation above.** Because `BipolarDevice.base` reuses the deck's own
`nwell` layer (the very layer used for PMOS body recognition), the base
terminal's net is only as resolvable as that `nwell` node is —
`extract.py` wires `l2n.connect(bipolar_base, nwell)`, so the base
inherits whatever name `nwell` picks up. Whether that `nwell` node
resolves through to contact/metals therefore depends entirely on the
deck's `tap`/`well_label` mechanism (`extract.py` only wires
`nwell → tap → contact` when `deck.tap is not None`, and names the node
via `nwell → well_label`):

- **gf180mcu** declares *neither* `tap` *nor* `well_label` (`Comp` is
  shared with transistor active, and there is no well-label layer), so its
  `nwell` node — and hence its BJT base terminal — is a floating, anonymous
  net. This is the **exact same gap** as the "PMOS body (gf180mcu only)"
  limitation documented under "Coverage" above; the drawn BJT's base can no
  more be LVS'd or simulated against a schematic base net than gf180mcu's
  PMOS body can.
- **sky130** *does* declare `tap` (65/44, distinct from `diff.drawing`) and
  `well_label` (64/5), so its base terminal resolves correctly through
  `nwell → tap → contact → metals` and picks up its real pin name via the
  `nwell` label — sky130's BJT base is **not** floating, *provided the input
  geometry actually draws a `tap` shape over the base tie*. `klt gen
  bjt_array`'s own sky130 output now does (issue #432 — previously it drew
  the base-tie pad on `diff.drawing` only, with no `tap` shape, leaving its
  own base terminal an isolated node with no path to the recognised `nwell`
  base region despite this deck-level mechanism existing).

A code fix — adding `base_via`/`base_via_metal` fields to `BipolarDevice`
(analogous to the `CapacitorDevice.top_plate_via`/`top_plate_via_metal`
connectivity fix from issue #314) so a gf180mcu base tie could reach the
metal stack directly — is a **possible follow-up but is out of scope for
this issue**, pending confirmation that the gf180mcu DRM defines a legal
base-tie via/metal stack. File it separately if that confirmation lands.

`devices[].nets` for a bipolar device uses `"c"`/`"b"`/`"e"` terminal keys
(collector/base/emitter) instead of MOS's `"s"`/`"g"`/`"d"`/`"b"`, and
`devices[].params` is empty (KLayout's `DeviceClassBJT3Transistor` reports
area/perimeter parameters named `AE`/`AB`/`AC`/`PE`/`PB`/`PC`/`NE`, none of
which match the `W`/`L` parameter names `devices[].params` extracts — see
"JSON response" below).

### MiM capacitor device recognition

Both curated decks additionally declare one or more drawn MiM
(Metal-Insulator-Metal) capacitor device-recognition entries
(`ExtractionDeck.capacitors`, a tuple of `CapacitorDevice` — see its
docstring in `src/klayout_tools/decks/__init__.py`), run through KLayout's
native `DeviceExtractorCapacitor`. A MiM cap is two conductor plates
separated by a thin dielectric, drawn as **two independent layers** — a
purpose-drawn top plate and a bottom plate on an ordinary conductor — rather
than one marked-up conductor the way a drawn resistor or the bipolar entries
above reuse existing layers:

| Deck | `devices[].class` | Top plate | Bottom plate | Area capacitance | Perimeter capacitance |
| ---- | ------------------ | --------- | ------------ | ------------------ | ---------------------- |
| `gf180mcu` | `cap_mim_2f0_m4m5_noshield` | `FuseTop` (75/0), requires `CAP_MK` (117/5) + `MIM_L_MK` (117/10), excludes `efuse_mk`/`plfuse` | "Virtual bottom plate": `Metal4` (46/0) clipped to `FuseTop` sized (oversized) by 1.06µm | **1.99 fF/µm²** | **0.2383 fF/µm** |
| `sky130` | `sky130_fd_pr__model__cap_mim` | `capm.drawing` (89/44) | `met3.drawing` (70/20), unfiltered | **2.0 fF/µm²** | **0.19 fF/µm** |
| `sky130` | `sky130_fd_pr__model__cap_mim_m4` | `capm2.drawing` (97/44) | `met4.drawing` (71/20), unfiltered | **2.0 fF/µm²** | **0.19 fF/µm** |

KLayout computes `C = A * area_cap` (`area_cap_f_um2`) from the two plates'
actual geometric overlap area and perimeter, corrected by `klt extract`
itself (issue #512) to `C = A * area_cap + P * perim_cap`
(`perim_cap_f_um`) once KLayout's own already-computed `A`/`P` device
parameters are read back — so the two coefficients together *are* the
accuracy of the extracted value — the capacitor analogue of a drawn
resistor's sheet resistance. Both coefficients are transcribed from each
PDK's own **simulation model** (a two-term area-plus-perimeter/fringe law
every MiM device model publishes; a different source than the DRC rule
tables the rest of each deck module cites, and than the official KLayout
**LVS** deck's own single-term, rounded restatement — sky130's
`sky130_fd_pr__cap_mim_m3_1`/`_m3_2` SPICE model cards, gf180mcu's
`sm141064.ngspice` `cap_mim_2f0fF`) and cross-checked against independent
sources in the same PDK install — see the per-deck module docstrings
(`src/klayout_tools/decks/sky130.py`, `.../gf180mcu.py`) for the exact
source files, the derivation each is transcribed from, and every
approximation taken relative to it. gf180mcu's stack additionally needs a
"virtual bottom plate" derivation (its bottom plate is ordinary `Metal4`
routing, not a purpose-drawn cap layer the way sky130's `capm`/`capm2`
are) — clipping the bottom conductor to the sized top-plate outline so an
unrelated `Metal4` trace 1.06µm away from a real MiM cap is not swept in.

The correction is applied to the extracted device itself (issue #521), not
only to this command's JSON report: the `C` card in the written `.spice`
netlist — the file `klt sim` consumes — carries the same corrected value
`devices[].params.c_f` reports, and so does the netlist `klt lvs`'s
inline-extraction path hands to its comparer. A deck that leaves
`perim_cap_f_um` at its `0.0` default is unaffected in every one of those
places.

Two consequences worth knowing:

- **Unmarked conductor is never reclassified.** An ordinary
  `Metal4`-`Metal3` (or, on sky130, plain `met3`/`met4`) overlap with no
  `MIM_L_MK`/`capm`/`capm2` marker drawn stays ordinary connectivity, never
  a capacitor — mirroring the "unmarked conductor is never reclassified"
  guarantee drawn resistors give.
- **Capacitor plate nets are wired into the rest of this deck's metal stack
  wherever the deck declares how (issue #314).** A recognised bottom-plate
  region is tied into the deck's own `metals[]` connectivity node whenever
  `CapacitorDevice.bottom_plate` matches one of the deck's tracked `metals`
  layers, and a recognised top-plate region is tied in the same way through
  an optional declared `top_plate_via`/`top_plate_via_metal` pair — so
  ordinary contact/via/metal routing to either plate's conductor reaches
  that capacitor terminal, and `klt lvs` can match it against a schematic
  net the way it does an ordinary MOS terminal. Neither is a given for every
  deck/plate combination, though:
  - **gf180mcu**: both plates are wired. `bottom_plate` is `Metal4`, one of
    the deck's own `Metal1`–`Metal5` `metals` entries, so the bottom
    terminal is tied straight in. `top_plate_via`/`top_plate_via_metal`
    declare `Via4`/`Metal5` (the official deck's own `main.drc` connectivity,
    `top_via = via4` landing on `top_metal = metal5` for this stack), so the
    top terminal is tied in the same way.
  - **sky130**: neither plate is wired. `met3`/`met4` (the two stacks'
    `bottom_plate` layers) are not among this curated deck's own `metals`
    (`li1`/`met1`/`met2` only), and while the real PDK's MiM stacks do have a
    landing via for the top plate too (`sky130.lvs`'s `connect(capm, via3)`
    / `connect(capm2, via4)`), those land on `met4`/`met5` — also above this
    curated deck's `metals` stack — so `CapacitorDevice.top_plate_via` is
    left unset here. Both of sky130's plate terminals therefore remain their
    own isolated, self-connected connectivity nodes exactly as before #314:
    multiple plate polygons that touch merge into one net (e.g. a shared
    bottom plate across several caps), but neither plate's net extends into
    any real routing.

  In every case the *device* itself (a capacitor of the correct value
  between two correctly-shaped plates) is recognised identically; only an
  unwired plate's *net name/connectivity* carries the documented
  approximation above. A layout with no MiM-cap marker anywhere extracts
  bit-for-bit as it did before this feature existed — the capacitor
  extractor is never even invoked for an entry whose plate regions come out
  empty.
  - **A DRM-legal `top_plate_via` does not short the two plates (issue
    #364).** gf180mcu's top-plate-via minimum-overlap rule *requires* the
    bottom plate to enclose/overlap the via — the via and the bottom plate's
    conductor necessarily touch in plan view for a correctly-drawn cap. That
    overlap is excluded from the deck's generic `vias[]` layer before its
    generic per-layer `metals[i]`/`vias[i]` connectivity loop runs, so the
    via still ties the top plate to `top_plate_via_metal` as described above
    without also being read as an ordinary via onto the bottom plate
    beneath it.
- **gf180mcu's stack models only the default of three selectable
  densities.** The DRM/LVS deck's MiM cap supports 1.0/1.5/2.0 fF/µm²
  dielectric-thickness options selected as a foundry-side runset option, not
  something a drawn layout's own geometry can distinguish; this deck models
  only the LVS runset's own default (2.0 fF/µm²) — see
  `decks/gf180mcu.py`'s provenance note.
- **gf180mcu models only Option B, not Option A, of the DRM's two
  mutually-exclusive MiM stacks.** The DRM's "10.4 MIM Capacitor" section
  defines Option A (`MIM.*`, bottom plate `Metal2`, for a 3-metal-layer
  process variant) and Option B (`MIMTM.*`, bottom plate `Metal(n-1)` of an
  n-metal stack — `Metal4` on the 5-metal-layer variant this deck models);
  a PDK is wired for one or the other, never both. This deck's `capacitors`
  entry transcribes only Option B, mirroring the fixed 5-metal-layer
  `metals`/`vias` connectivity this whole deck models throughout — not an
  oversight — so Option A is out of scope here for the same reason as the
  rest of this deck's DRC/connectivity coverage; see `decks/gf180mcu.py`'s
  module docstring (its "10.4 MIM Capacitor" note) for the full derivation.

**sky130 gap**: neither MoM (Metal-on-Metal, interdigitated-finger)
capacitors nor any voltage-flavor/size variant beyond the two curated
`capm`/`capm2` stacks above are modelled — out of scope for this curated
starter subset, not a silent omission.

### Drawn resistors

Both decks additionally recognise **drawn precision-resistor device
classes** (issue #222, extended to cover each deck's other selectable
sheet-rho flavour by issue #299). A drawn resistor is a deliberately-marked
segment of an ordinary conductor: the designer draws poly, then covers the
resistive part with the PDK's resistor-ID layer. Without this recognition
that segment extracts as a plain conductor — a **short** between the
resistor's two heads — so a resistor drawn at the wrong length or width
passes LVS silently.

| Deck | `devices[].class` | Models | Body | Marker | Also requires | Sheet resistance | Fixed head/end offset |
| ---- | ----------------- | ------ | ---- | ------ | ------------- | ---------------- | ----------------------- |
| `sky130`   | `res_generic_po` | `sky130_fd_pr__res_generic_po` | `poly.drawing` 66/20 | `poly.res` 66/13 | — | **48.2 Ω/□** | — |
| `sky130`   | `res_high_po` | `sky130_fd_pr__res_high_po_*` | `poly.drawing` 66/20 | `poly.res` 66/13 | `psdm` 94/20, `rpm` 86/20 | **324.827244 Ω/□** | **379.705147 Ω** |
| `sky130`   | `res_xhigh_po` | `sky130_fd_pr__res_xhigh_po_*` | `poly.drawing` 66/20 | `poly.res` 66/13 | `psdm` 94/20, `urpm` 79/20 | **2000 Ω/□** | — |
| `gf180mcu` | `ppolyf_u` | `gf180mcu_fd_pr__ppolyf_u` (P+ poly, unsalicided) | `Poly2` 30/0 | `RES_MK` 110/5 | `Pplus` 31/0, `SAB` 49/0 | **350 Ω/□** | — |
| `gf180mcu` | `ppolyf_u_1k` | `gf180mcu_fd_pr__ppolyf_u_1k` (`POLY_RES='1k'` default) | `Poly2` 30/0 | `RES_MK` 110/5 | `SAB` 49/0, `Resistor` 62/0 | **1000 Ω/□** | — |

KLayout computes `R = L / W * sheet_rho` from the recognised segment's own
geometry, corrected by `klt extract` itself (issue #518) to
`R = L / W * sheet_rho + fixed_offset_ohm` once KLayout's own
already-computed `L`/`W` device parameters are read back — so the two
coefficients together *are* the accuracy of the extracted value, the
resistor analogue of a MiM capacitor's area/perimeter pair (issue #512).
The sheet-resistance figures are transcribed from that PDK's **own official
KLayout LVS deck** and cross-checked against a second independent source
(open_pdks' magic technology file) in the same PDK install; sky130's
`res_high_po` additionally carries a **fixed head/end-effect offset**,
because its real PDK **simulation model**
(`sky130_fd_pr__res_high_po`'s SPICE `.subckt`) composes a length-scaling
`rbody` term with a fixed-length `rhead` contact term the official LVS
deck's own single-term restatement drops — measured via ngspice against
that model, not transcribed from the LVS deck. See the per-deck module
docstrings (`src/klayout_tools/decks/sky130.py`, `.../gf180mcu.py`) for the
exact source lines/files, the derivation each is transcribed from, and
every approximation taken relative to it. A deck entry that leaves
`fixed_offset_ohm` at its `0.0` default (every row above except
`res_high_po`) reports `r_ohm` exactly as `L / W * sheet_rho` — the
pre-#518 formula, bit-for-bit.

As with the MiM capacitor's perimeter term, the correction is applied to the
extracted device itself (issue #521): the `R` card in the written `.spice`
netlist and the netlist `klt lvs` compares both carry the corrected
resistance, not just `devices[].params.r_ohm`.

Three consequences worth knowing:

- **Unmarked conductor is never reclassified.** A resistor-*shaped* poly bar
  with no resistor-ID layer over it stays ordinary routing, and a marked
  segment over diffusion stays a transistor gate (both decks exclude their
  own active/COMP layer from the resistor body, mirroring the PDKs' own LVS
  derivations).
- **A resistor flavour these curated decks do not model is left as a short,
  not extracted with the wrong value.** sky130's `rpm`/`urpm` precision
  implant masks each select their *own* wired device class above (so a
  segment marked with one is never mistaken for the other, nor for the
  48.2 Ω/□ generic flavour); gf180mcu's `Resistor` 62/0 high-sheet-rho mark
  selects `ppolyf_u_1k` (only the PDK's own `POLY_RES='1k'` default — its
  `_2k`/`_3k` siblings share *identical* drawn geometry with `_1k`,
  selected only by a build-time option no drawn layer distinguishes, so
  they remain deliberately unmodelled). gf180mcu's `SAB` is *required* on
  both `ppolyf_u` entries (without it the real device is the ~48×
  lower-resistance salicided `ppolyf_s`). A wrong resistance passing LVS
  with high confidence is worse than a known-unmodelled short.
- **A "deck-coverage gap" warns differently from unmarked geometry.** A
  segment that carries a resistor-marker layer this deck knows about, but
  whose `requires`/`excludes` conditions it does not satisfy (e.g. a
  gf180mcu poly segment marked `RES_MK` without the `Pplus`/`SAB` combination
  either wired flavour above needs), still extracts as an unintended short
  — but is flagged by a **different** `warnings[]` string than fully
  unmarked geometry; see "Known limitation: unmodelled device geometry"
  below.

### Junction diodes (issue #542)

The `gf180mcu` deck additionally recognises **junction-diode device classes**
(`ExtractionDeck.diodes`, a tuple of `DiodeDevice` — see its docstring in
`src/klayout_tools/decks/__init__.py`), run through KLayout's native
`DeviceExtractorDiode`. A discrete PN diode is the simplest ESD-clamp
primitive and the one every general-purpose I/O library ships as a baseline
— gf180mcu's own `gf180mcu_fd_io__asig_5p0` analogue-signal pad cell is a
plain dual-diode clamp (`D0 DVSS DVDD diode_nd2ps_06v0`,
`D3 ASIG5V DVDD diode_pd2nw_06v0` in its CDL). Without this recognition that
geometry extracts as **no device at all**, so `klt lvs` cannot verify any
diode-based clamp.

| Deck | `devices[].class` | Models | Anode | Cathode | Marker | Also requires |
| ---- | ----------------- | ------ | ----- | ------- | ------ | ------------- |
| `gf180mcu` | `diode_nd2ps_06v0` | `gf180mcu_fd_pr__diode_nd2ps_06v0` (n+ diffusion in p-substrate) | *substrate* (no drawn mask — tied to the deck's `substrate_net`) | `Comp` 22/0 | `diode_mk` 115/5 | `Nplus` 32/0, `Dualgate` 55/0; excludes `Nwell` 21/0, `DNWELL` 12/0 |
| `gf180mcu` | `diode_pd2nw_06v0` | `gf180mcu_fd_pr__diode_pd2nw_06v0` (p+ diffusion in Nwell) | `Comp` 22/0 | `Nwell` 21/0 | `diode_mk` 115/5 | `Pplus` 31/0, `Dualgate` 55/0; excludes `DNWELL` 12/0 |

KLayout forms the device from the two terminal regions' **geometric
overlap** and reports that overlap's area (`A`) and perimeter (`P`), which
`klt extract` surfaces as `devices[].params.area_um2`/`perimeter_um`. Both
entries are transcribed from gf180mcu's own official KLayout LVS deck
(`klayout/lvs/rule_decks/diode_derivations.lvs` /
`diode_extraction.lvs`) — see `decks/gf180mcu.py`'s provenance note for the
exact derivations and every approximation taken relative to them.

Points worth knowing:

- **Not an I-V model.** A recognised diode is emitted as a
  schematic-equivalent `D` card whose model token is the deck entry's own
  name (`D$1 vsubs CATH diode_nd2ps_06v0 A=1P P=4U`), exactly the fidelity
  level the MOS/bipolar recognisers already provide — a consumer simulating
  the netlist supplies a matching `.model`. `--pdk` model binding
  (see "SPICE model binding" below) does **not** bind diode classes; like
  gf180mcu's `bjt`, they keep the bare primitive form.
- **Unmarked diffusion is never reclassified.** Both terminal regions are
  intersected with the PDK's `diode_mk` device mark before recognition, the
  same guard the bipolar block applies to its base — so an ordinary PMOS's
  p+-in-Nwell source/drain, or a substrate/well tap, is never misrecognised
  as a diode. A layout with no `diode_mk` drawn anywhere extracts
  bit-for-bit as it did before this feature existed (the diode extractor is
  never even invoked for an entry whose terminal regions come out empty).
- **The substrate-side anode inherits the "NMOS body" limitation above.**
  gf180mcu draws no p-substrate mask, so `diode_nd2ps_06v0`'s anode is tied
  to the deck's `substrate_net` global (`vsubs` by default) rather than to a
  drawn, routable tie — the same wiring the NMOS body and the collector-less
  bipolar collector already use.
- **The `Nwell`-side cathode inherits the "PMOS body (gf180mcu only)"
  limitation above.** `diode_pd2nw_06v0`'s cathode shares the deck's own
  `nwell` connectivity node, and gf180mcu's curated deck declares neither
  `tap` nor `well_label` — so that terminal is a floating, anonymous net
  unless the well node picks up a name some other way. Exactly the same gap
  as the BJT base terminal's, documented above.
- **Deliberately unmodelled flavours.** Only the two 6V (`Dualgate`-marked)
  flavours are wired. The 3.3V (`_03v3`) variants, both `_dn` (deep-nwell)
  variants, and the `diode_nw2ps_*`/`diode_pw2dw_*`/`diode_dw2ps_*`/
  `sc_diode` families key off layers this curated deck does not model
  (`v5_xtor`, `dnwell`, `lvpwell`, `well_diode_mk`, `schottky_diode`);
  geometry they would claim stays **unrecognised** (a loud LVS mismatch)
  rather than being extracted as the wrong device. See
  `decks/gf180mcu.py`'s provenance note for the full list.

**sky130 gap**: sky130's own `diode_pw2nd`/`diode_pd2nw` families are not
modelled — out of scope for this first cut, consistent with how the
bipolar/capacitor/resistor families each landed with one deck first, not a
silent omission.

### Known limitation: unmodelled device geometry (issue #288, #324)

Every layer this deck reads (`active`, `poly`, `nwell`, `contact`, `metals`,
...) is a **connectivity layer**, wired up unconditionally by
`_extract_netlist`'s connectivity block regardless of whether any device
extractor claims the geometry drawn on it. If a layout contains geometry
drawn for a device class the active deck does not (yet) implement — today,
anything beyond `nfet`/`pfet` plus each deck's curated
resistor/bipolar/MiM-cap/diode entries above — that geometry is **not skipped**. It is absorbed into
ordinary interconnect exactly like a routing shape: the device's two (or
more) terminals extract as a **single shorted net** instead of the distinct
nets a schematic keeps them as. `klt extract` still exits `0`; nothing in the
JSON response says "this geometry was not recognised as a device" — the only
symptom is a `klt lvs` net/topology mismatch downstream that reads exactly
like an ordinary routing bug, with nothing pointing at the real cause. The
same failure mode reproduces for the *next* unmodelled device class even
after every device class on today's roadmap lands — it is a property of
"connectivity layer with no matching extractor", not of any specific missing
class.

`warnings[]` now carries one narrowly-scoped heuristic diagnostic for the
most common shape of this problem: a **poly resistor body** drawn without
(or excluded from) a resistor-ID marker layer, or any other conductor shape
sharing poly + contact that no MOS gate extractor claims. Specifically,
`klt extract` flags a `poly` connected component when **both**:

- it does not touch any extracted `nfet`/`pfet` gate region anywhere (a real
  MOS gate, including a legitimate poly-contacted gate strap, and ordinary
  poly routing between two recognised gates — poly needs no via to route on
  itself, so both are one merged polygon *with* the gates — are excluded
  unconditionally), **and**
- it touches `contact` at two or more geometrically separate locations (the
  resistor-body signature: a two-terminal segment contacted at each end,
  distinct from a routing run with a single landing pad), **and**
- it does not touch a region this deck's own drawn-resistor recognition
  (`_resolve_resistors`) already **recognised** as a resistor body on this
  layout (issue #324). Once a resistor body is recognised, its two heads
  survive as separate `poly` components with the body cut out from between
  them — a head with an ordinary (2+) contact array on a wide terminal
  therefore abuts the very body region the deck just extracted correctly,
  and is that resistor's own terminal, not a candidate unmodelled-device
  body, no matter how many contacts land on it.

Since issue #299, a flagged component is further split by whether it
overlaps *any* of the active deck's declared `ResistorDevice.marker` layers
(the raw marker geometry, not narrowed by that entry's own
`requires`/`excludes`) — producing up to two separate warning strings in
`warnings[]`:

- **"…carry no resistor-marker layer at all"** — the original #288 signature:
  no declared resistor marker covers this shape anywhere. Either an entirely
  undeclared device class (this deck has no `ResistorDevice`/other extractor
  for it at all) or a resistor drawn with no marker.
- **"…carry a resistor-marker layer, but do not match any of this deck's
  declared `ResistorDevice` requires/excludes conditions"** — a **deck-
  coverage gap**: the shape *is* marked with a resistor-ID layer this deck
  recognises in principle, but the specific combination of
  `requires`/`excludes` layers actually drawn on it does not match any
  declared flavour (e.g. gf180mcu's `RES_MK` present without `SAB`, or
  present with a fourth, still-unmodelled `POLY_RES` value gf180mcu's
  drawn geometry cannot distinguish from `ppolyf_u_1k` in the first place —
  see "Drawn resistors" above). This case means the deck's *authors* already
  know this marker layer exists and chose not to (or cannot yet) model this
  particular combination — a narrower, more actionable signal than "unmarked
  geometry" for deciding whether to file a coverage-gap issue.

Both strings are reported as warnings pointing back at this section — they
are a **diagnostic, not a device extractor**: neither ever identifies
*which* device class the geometry is (the deck still has no
device-extraction logic for it), and both are deliberately conservative
rather than exhaustive. Neither catches every possible unmodelled-device
shape — e.g. a device that never touches `poly`/`contact` at all, or one
whose body has only a single contact cluster, produces no warning. Treat a
non-empty match here as a strong signal to investigate, and treat its
absence as "nothing matched this specific signature", not as a general
guarantee that every device in the layout was recognised.

The only complete workaround today, for a device class this heuristic does
not catch: do not draw geometry for a device class the active deck cannot
yet extract, and track the omission separately, until the device class is
added to the deck. (The narrower case of a *deliberately non-functional*
device — a drawn dummy — is now handled directly by the `dummy` marker layer
described next, rather than left to this heuristic.)

**Enumerating the flagged shapes (issue #324).** Alongside the prose
`warnings[]` strings, the JSON response's `unmodelled_poly[]` field (see
"JSON schema" above) lists the exact flagged shapes — one entry per
component, each `{"bbox_um", "reason"}` — so a consumer can enumerate and
triage the flagged set once (e.g. "these N are known routing tracks, assert
the count doesn't grow") instead of re-deriving it by re-implementing this
heuristic against the stream.

**Remaining known limitation: ordinary poly routing (issue #324).** The
heuristic still has no signal distinguishing a resistor-shaped **routing
track** from an actual unmodelled resistor body: on a layout whose signal
routing is deliberately drawn on `poly` (e.g. a one-metal-level analog cell),
every track contacted at both ends and touching no gate trips the same
signature as a real missing device, and the false-positive count can
dominate the true-positive count. There is no server-side fix for this today
— tightening the signature (e.g. an aspect-ratio floor, or a "no more than 2
contact clusters" ceiling to separate a two-terminal body from a track that
fans out to every device on its net) is deliberately deferred pending
validation against more than one deck/layout. The interim workaround is
client-side: use `unmodelled_poly[]`'s bounding boxes to filter out shapes
already known to be routing (by inspection, by contact-cluster count, or by
aspect ratio) and assert only on what remains, or track the *change* in the
filtered count across revisions rather than gating on its absolute value.

### Merged net labels (issue #470)

Two *different* net labels can land on the same electrical net — for
example, a `klt gen-compose` `pins[]` entry names a port that other
connectivity (drawn metal, or a second `pins[]`/`connectivity[]` entry)
already reaches, silently renaming the node that owns the pad rather than
naming the caller's intended net. KLayout does not treat this as an error:
`LayoutToNetlist` simply joins every distinct label text found on one net
with a comma in `Net.expanded_name()` — two labels `Y` and `OUT` shorted
together come back as the single net name `Y,OUT` (three or more labels
join the same way, e.g. `Y,OUT,FOO`). That joined name flows into `nets[]`
and the written netlist unremarked; nothing about the string itself says
"these were two different names before extraction merged them."

`klt extract` detects this heuristically: any net whose name splits into 2+
parts on `,` is treated as a label collision. Each match produces two things:

- A structured entry in the response's `merged_net_labels[]` array (see "JSON
  schema" above): `{ "net": "<full joined name>", "labels": ["Y", "OUT"] }`
  — `labels` is the joined name pre-split, so a consumer does not have to
  re-derive the label list by re-implementing the `,`-scan against
  `nets[].name` itself.
- A matching prose entry in `warnings[]`, so a caller that only checks
  `warnings[]` (the documented, minimal self-check every `klt` command
  supports) still sees the collision rather than needing to know about
  `merged_net_labels[]` specifically.

`status` stays `"extracted"` and `net_count`/`nets[]`/`pin_count` are
unaffected — this is a diagnostic layered on top of the existing net, not a
rejection of the extraction. The collision is invisible to `klt drc` (the
shapes involved are legal, well-formed, and often not even touching — the
collision is between *labels* on a shared pad, not between wires), so this
is currently the only place in the toolchain that surfaces it.

**Known limitation (false positives).** The heuristic is a substring split,
not a provenance check: KLayout's `Net.expanded_name()` does not record
*why* a net carries a `,`-containing name, only the final joined string. A
label that legitimately contains a literal comma in its own text (unusual,
but not disallowed by any layer's text-shape format) is indistinguishable
from a genuine two-label collision by this heuristic — it will be reported
as a false-positive entry in both `merged_net_labels[]` and `warnings[]`.
There is no server-side fix for this today: a caller that intentionally
uses comma-containing label text should expect (and can safely ignore) a
`merged_net_labels[]` entry whose `labels` do not actually correspond to
independent naming intents.

### Voltage-domain markers (issue #552)

Some PDKs draw **two gate-oxide/voltage domains** on the same wafer, selected
by a marker layer — e.g. gf180mcu's `Dualgate` (55/0) selects its 5V/6V
thick-oxide domain, whose DRM publishes a distinct set of MOS models with
materially different characteristics from the default (thin-oxide) ones.
This curated deck derives MOS flavour from the well layer alone
(`nfet_active = active - nwell`) and never reads such a marker, so a
transistor drawn entirely inside `Dualgate` still extracts bound to the
deck's single (default) model name — e.g. `nfet_03v3` even for a device that
is actually 5V/6V, once `--pdk` resolves a subcircuit binding. Nothing about
the JSON response says the binding might be wrong.

`klt extract` flags this rather than silently emitting a plausible-looking
wrong model: whenever a deck-registered voltage-domain marker (today, only
gf180mcu's `Dualgate`) is present in the input stream and its geometry
overlaps extracted MOS device geometry (the deck's `active` region), two
things are produced:

- A structured entry in the response's `voltage_domain_warnings[]` array
  (see "JSON schema" above): `{ "marker": "55/0", "description": str }` —
  the same registry entry (and description text) `klt drc`'s
  `coverage.voltage_domain_warnings` surfaces for the same deck, so the
  wording matches across both commands for the same layout.
- A matching prose entry in `warnings[]`.

**What this field does and does not guarantee**: it flags that the bound
model name may be wrong for this device. It does **not** correct the
binding — that would require a per-flavour MOS marker field this deck's
`ExtractionDeck` does not have yet (a separate, larger follow-on). The
device still extracts, and still binds to the same (default) model name, as
it did before this field existed; only the signal is new.

### Dummy devices: the `dummy` marker layer (issue #295, extended to resistors/bipolars in #462 and to junction diodes in #542)

Analog matching practice puts **dummy devices** on the edges of a matched
pair or array so every functional device sees the same lithographic/stress
neighbourhood — the same technique applies to matched MOS pairs, resistor
arrays/ladders, and bipolar arrays alike. A dummy is drawn geometry that is
deliberately *not* part of the circuit: its terminals are tied off to a rail,
and it contributes nothing to the schematic. Because a drawn dummy device is
otherwise ordinary device geometry, it used to land in the extracted netlist
as a real device that the schematic-derived reference has no counterpart
for — so `klt lvs` reported a spurious `device.unmatched` (plus the usual net
cascade) for every dummy drawn, forcing an unlayoutable choice between
matching quality and a clean compare.

A deck may now declare an optional `dummy` marker layer (see the deck-schema
table below). Any MOS gate, drawn-resistor body, bipolar unit, or recognised
diode junction lying under a shape on that layer is **dropped before device
recognition**: `_extract_netlist` subtracts the marker region from the
NMOS/PMOS gate regions, each drawn resistor's candidate body region, each
bipolar's base (and, transitively, its emitter/collector) region, and each
diode's two terminal regions before handing any of them to KLayout's device
extractors, so geometry fully covered by the marker is
never recognised as a device at all. The dummy therefore does not appear in
`devices[]`, `device_count`, or `device_counts`, and `klt lvs` no longer sees
a phantom device to mismatch.

Only the **recognition-input region** is cut, never the whole device
footprint. For a MOS gate, the dummy's diffusions (source/drain) and its gate
poly remain in the connectivity graph; for a resistor, the marker-covered
segment of the body layer remains ordinary conductor rather than becoming a
resistive hole; for a bipolar, the parts of its base/emitter/collector
outside the marker (if any) are unaffected; for a diode, the parts of its
anode/cathode regions outside the marker are unaffected (a diode covered by
several declared flavours sharing one device-mark layer is counted **once**,
because the count is taken against the recognised junction rather than the
shared marker). Either way the shape still
extracts as ordinary interconnect and ties off to the rail exactly as drawn —
consistent with a dummy being "tied off to a rail". A marker that only
partially covers a device's recognition-input region is a clean geometric cut
(the same subtraction precedent drawn resistors already use for
`requires`/`excludes`), not an all-or-nothing reclassification: the remaining
area still extracts as a device, and only a component the marker fully
consumes is counted as dropped.

For visibility (rather than a silent drop — the failure mode issue #288 was
filed about), the JSON response carries a `dummy_devices_dropped` count of how
many devices — MOS gates, drawn resistors, bipolars, and junction diodes
alike — the marker suppressed. It is `0` when the active deck declares no `dummy` layer or the
layout draws no dummy geometry. Declaring `dummy` is fully opt-in and
additive: a deck that does not set it extracts exactly as it did before the
field existed, byte-for-byte.

sky130's curated deck (`klayout_tools.decks.sky130`) declares this layer as
of issue #491 — sky130 has no native per-device dummy-marker GDS layer of its
own (dummy fill in the real PDK is a density-rule/DRC concept, not a
device-recognition mark), so it reserves a curated, extraction-only marker on
an unused datatype (see `EXTRACTION_DECK.dummy`'s own comment for the exact
layer number and why it was chosen). `klt gen`'s `mos_array`/`res_array`/
`bjt_array` draw that marker over their `dummy_cells`' footprint on sky130,
so this section's suppression actually fires for arrays generated by those
commands — see each generator's own docs above. gf180mcu's curated deck
declares no `dummy` layer yet, so this mechanism stays unreachable there
until a follow-on issue wires it up.

## Top-cell-only pin promotion (`--top-cell-pins`, #291)

Extraction ends by turning named nets into the top circuit's `.SUBCKT` pins
(KLayout's `Netlist.make_top_level_pins()`). Because extraction is flat, "named
net" includes any net that is named only because a label sits inside an
**instanced sub-cell** — labels that were that sub-cell's own port names when it
was checked standalone, and are ordinary internal nodes once it is instanced.
Left unqualified, those sub-cell labels leak out as spurious **parent** pins, so
composing a verified sub-cell into a larger cell (the normal hierarchical-reuse
story) forces the reference netlist in a downstream `klt lvs` to declare those
internal nodes as ports just to make pin counts match.

`--top-cell-pins` changes which labels are allowed to promote: only labels drawn
**directly in the top cell** become pins. A net named solely by a label found
below an instance boundary keeps its net *name* (still useful in the written
SPICE and in `nets[]`) but is **not** promoted to a pin — it stays the internal
node it actually is once instanced.

How the two sets are distinguished: the strings on each label layer drawn
directly in the top cell (`Cell.shapes`) versus the full recursive flatten
(`Cell.begin_shapes_rec`); a string present recursively but never directly in
the top cell is a below-top label. A net's global/substrate name (from
`connect_global`, not a drawn text) is never a below-top label, so the substrate
pin is never affected.

**Always** — with or without the flag — a `warnings` entry names any net whose
pin promotion came from a below-top label, so the behaviour is visible rather
than something you deduce from an unexpected pin count. Without the flag the
warning reports the promotion (default behaviour is unchanged, so existing
output is byte-for-byte identical); with the flag it reports that the net was
kept internal.

A flat layout (no instances), or any layout whose pin labels all live in the top
cell, has an empty below-top set: `--top-cell-pins` is then a no-op and no
warning fires. `klt lvs` exposes the same control as the `layout.top_cell_pins`
request field (see [`docs/cli/lvs.md`](lvs.md)).

## Declared pin set (`--pins`, #514)

`--top-cell-pins` filters labels by **which cell** they were drawn in — the
right axis when interface and internal labels live at different hierarchy
levels. It does nothing when they are all drawn in the same cell, which is
the normal shape for a hand-routed overlay: naming an **internal node** of a
lumped schematic device (e.g. one tap of a metal-option ladder the reference
netlist models as a single series device, wired for documentation) still
promotes it to a top-level pin. A pinned internal node blocks `klt
lvs`'s `options.combine_devices` from folding the series chain through it —
the reference's one device becomes several unpairable extracted devices, and
`klt lvs` reports `device.unmatched` mismatches whose cause is not otherwise
attributed anywhere in the report.

`--pins A,B,VDD,VSS` (a comma-separated list of net names) declares the
**intended interface** explicitly, per net rather than per cell: every named
net **not** in the declared set keeps its name (still visible in the written
SPICE and in `nets[]`) but is demoted to an internal node instead of being
promoted to a pin. A declared name that matches no promoted net is reported
in `warnings` rather than silently ignored (a likely typo). Applied *after*
`--top-cell-pins`'s own reconciliation — it can only further restrict the
promoted set, never re-promote a net `--top-cell-pins` already kept
internal.

Omitting `--pins` (the default) skips this reconciliation entirely: every
named net still promotes to a pin, byte-for-byte identical to extraction
before this flag existed. `klt lvs` exposes the same control as the
`layout.declared_pins` request field (a JSON array of net name strings —
see [`docs/cli/lvs.md`](lvs.md)).

## PDK resolution

`--pdk`/`--pdk-root` are **optional** and resolved through
`klayout_tools.pdk.find_pdk` — the same resolver `klt pdk find`/`klt pdk
env` use ([`docs/cli/pdk.md`](pdk.md)):

- Omit both — the default — and extraction runs entirely from the curated
  `--deck` table; no PDK install is touched or required. This matches `klt
  drc`'s CI posture: both commands are runnable with nothing installed
  beyond `pip install klayout`.
- Give either — an explicit `--pdk <variant>` and/or `--pdk-root <root>` —
  and the PDK is resolved before extraction runs; an unresolvable PDK
  (nothing found, or the named variant is absent) is an application error
  (exit 1), and the resolved `variant`/`root`/`version` are echoed in the
  response's `pdk` field for provenance.

### SPICE model binding (`--pdk` given + resolvable)

Before this behavior existed, `--pdk`/`--pdk-root` only affected the JSON
response's `pdk` provenance field — the *written netlist* was identical
either way, using the curated deck's bare device-class label
(`nfet`/`pfet`) as an `M`-card model name:

```
M$1 Y A VGND vsubs nfet L=0.15U W=0.65U AS=0.234P AD=0.234P PS=1.6U PD=1.6U
```

`nfet` is the deck's own class label, not a model any real PDK ships —
sky130 and gf180mcu both ship their primitive MOS device as a SPICE
`.subckt` (taking `d g s b` terminals plus `l`/`w` geometry), never a
built-in `nmos`/`pmos` model — so this `M` card cannot bind a real PDK model
library at all.

**When a PDK resolves now**, each extracted device is written as an `X`
subcircuit call against the resolved PDK's real device library instead:

```
X$1 Y A VGND vsubs sky130_fd_pr__nfet_01v8 L=0.15U W=0.65U
X$7 RA RB sky130_fd_pr__res_generic_po l=6U w=1U
X$8 net1 net2 sky130_fd_pr__cap_mim_m3_1 l=10U w=5U
X$9 vsubs BASE EMIT vsubs sky130_fd_pr__pnp_05v5_W0p68L0p68
```

The device is bound via small curated per-class tables in
`src/klayout_tools/pdk_models.py` (see that module's docstring for the exact
provenance of every bound subcircuit name and parameter spelling, each read
off a real fetched PDK install rather than assumed) and a
`kdb.NetlistSpiceWriterDelegate` subclass that overrides KLayout's default
primitive-card writer only for classes present in the resolved tables.

**Coverage** (issue #339 extended #209's MOS-only binding to the other
recognised analog device classes):

| Device class | sky130 | gf180mcu | Geometry on the `X` card |
|---|---|---|---|
| MOS (`nfet`/`pfet`) | ✅ | ✅ | `L`/`W`, read off the device |
| Resistor | ✅ | ✅ | `l`/`w` (sky130) or `r_length`/`r_width` (gf180mcu), read off the device |
| Capacitor (MiM) | ✅ | ✅ | `l`/`w` (sky130) or `c_length`/`c_width` (gf180mcu), derived from the extracted plate area+perimeter |
| Bipolar | ✅ (`pnp`) | ❌ (carve-out) | none — a geometry-named variant selected by emitter area |

Bipolar note: sky130's `pnp_05v5` ships as discrete geometry-named cells
(`…_W0p68L0p68`, `…_W3p40L3p40`), not one parameterized cell, so the writer
selects the variant whose nominal emitter area is nearest the device's
measured `AE` and emits its four `c b e s` terminals (the collector net,
tied by extraction to the substrate, is repeated for the substrate pin).

**Scope limits** (deliberately narrower than the general PDK-device-metadata
resolver `docs/design/pdk-device-corner-metadata-spike.md` proposes as a
future epic):

- **One voltage/flavor per class**, the only flavor the curated extraction
  decks distinguish (see the module docstring): e.g. sky130's `01v8` MOS core
  devices and gf180mcu's `03v3` MOS core devices (gf180mcu has no
  `gf180mcu_fd_pr__`-prefixed naming convention the way sky130 does), and the
  specific resistor/capacitor device names each deck already declares.
- **gf180mcu bipolar is deliberately left unbound** — its recognised `bjt`
  device stays a bare `Q` card under `--pdk`, **not** a subcircuit call. This
  is a *documented carve-out*, not an oversight: the gf180mcu deck itself has
  no positively-identified single device-cell name to bind against — an
  existing, already-documented data gap in that deck
  (`src/klayout_tools/decks/gf180mcu.py:557-559`), which this binding cannot
  resolve without guessing a subcircuit name and polarity. sky130's `pnp` has
  a positively-identified cell family, so it *is* bound. This is the design
  choice for an un-bindable combination: a **documented bare-primitive
  carve-out** rather than a hard error, so a gf180mcu layout containing a
  bipolar still extracts under `--pdk` (its MOS/resistor/capacitor devices
  bind; only the `bjt` stays a bare card). Any other recognised device class
  with no curated binding entry is likewise written as its bare primitive card
  rather than a guessed subcircuit call.
- **Two curated decks only** (`sky130`, `gf180mcu`); a resolved PDK whose
  family has no curated MOS table entry for the requesting `--deck` (e.g. the
  `sky130` deck against a resolved `gf180mcuA` install, or a variant name
  matching no known PDK family at all) is an application error (exit 1)
  naming what was tried — **never** a silent fallback to the bare primitive
  form. (This up-front deck/PDK-mismatch guard keys off the MOS table, which
  every deck has; it is distinct from the per-class carve-out above.)
- Geometry values on every `X` card use an explicit micrometre unit suffix
  (e.g. `L=0.15U`, `l=6U`, `r_length=6U` — the same convention `klt extract`'s
  `M`-card form already uses, unambiguous regardless of any `.option scale` a
  caller's testbench may or may not set) and rely on the resolved
  subcircuit's own defaults for everything else (`nf`/`mult`/`par`/`m`, all
  confirmed `1`-equivalent in the fetched real installs each table was
  verified against). MOS source/drain area+perimeter (`AS`/`AD`/`PS`/`PD`,
  present on the bare `M`-card form) are **not** carried onto the `X` card —
  consistent with this command's documented schematic-equivalent,
  no-parasitics scope (see "Out of scope" below).
- **The JSON response is unaffected**: `device_counts`/`devices[].class`
  always report the deck's own class label (`nfet`/`pfet`/`res_generic_po`/
  `cap_mim_2f0_m4m5_noshield`/`pnp`/`bjt`/…), regardless of `--pdk`. Model
  binding is a SPICE-serialization concern only.

## Parasitic (RC) extraction (`--parasitics`)

`--parasitics` is an **opt-in, additive** mode implementing the interface
decision recorded in
[`docs/design/lvs-extraction-spike.md`](../design/lvs-extraction-spike.md) →
"Addendum (#216): parasitic (RC) extraction interface decision" (the
implementation is issue #217). When the flag is omitted the parasitics path is
skipped entirely and the written SPICE and JSON are byte-identical to a
schematic-equivalent extraction.

### Engine: a first-order lumped reduction (not a wrapped PEX engine)

The addendum deferred the engine choice to this implementation. KLayout's pip
`db` module — this repo's sole runtime dependency — has **no built-in
interconnect-mesh RC extraction call**: its `DeviceExtractorResistor` /
`...Capacitor` recognize *explicitly drawn* R/C devices (a precision poly
resistor, a MiM cap), not the parasitic R/C of ordinary wiring. Surveying the
open-source alternative (wrapping an external PEX layer) found no
suitably-licensed (MIT/Apache/BSD), headless, KLayout-scriptable
interconnect-PEX engine that works from a GDS/OASIS stream without pulling in a
**second geometry backend** and a non-pip toolchain — the same "two backends"
cost the spike's §1/§3 rejected for the LVS engine choice. So, matching the
addendum's lean and the DRC-deck curation pattern, `--parasitics` builds a
**first-order, lumped reduction** on top of the per-net/per-layer geometry
KLayout's `LayoutToNetlist` already tracks (`polygons_of_net`), using curated
per-PDK sheet-resistance / area-perimeter-capacitance coefficient tables.

### The model

For each net (except the deck's substrate/ground net, and nets with no
eligible interconnect geometry) the pass emits a single lumped-RC **Γ-section**:

```
net --R--> net__par --C--> <substrate_net>
```

- **C to ground** (femtofarads) = Σ over conductor roles of
  `area_um2 * cap_area + perimeter_um * cap_perim`, the net's lumped ground
  capacitance. The reference node is the deck's `substrate_net` (`vsubs`),
  which a `klt sim` testbench ties to the AC ground.
- **series R** (ohms) = Σ over conductor roles of `sheet_res * n_squares`,
  where `n_squares` is estimated per layer from the net's area `A` and
  perimeter `P` by modelling its copper as one equivalent rectangle
  (`L`,`W` = roots of `t² − (P/2)t + A = 0`; squares = `L/W`, clamped ≥ 1;
  exact for a single rectangular wire, → 1 for a square). This biases series
  resistance conservatively high for L-shaped or fragmented nets.

Conductor roles map to the deck's geometry layers: `poly` and each
`metals[i]` metal-stack layer. Two roles are deliberately excluded because
their capacitance is already captured by the extracted device models, and
counting them here would double-count it (#226):

- **Transistor gate poly** is subtracted from each net's poly shapes before
  measuring — the gate sits over the channel, not the substrate the
  coefficients describe, and its capacitance is in the device model. Only the
  parasitic measurement subtracts it; device connectivity is untouched.
- **Source/drain diffusion** carries no parasitic role: the extracted `M`
  cards already emit `AS`/`AD`/`PS`/`PD`, from which the device model derives
  the junction capacitance. Both PDKs' own magic tech files comment their
  active-layer parasitic caps out for the same reason.

The coefficients are curated per-PDK-family in each deck module's `PARASITICS`
table (`src/klayout_tools/decks/sky130.py` / `gf180mcu.py`), **transcribed with
citations from each PDK's public magic technology file** (`sky130.tech` /
`gf180mcu.tech` in fossi-foundation/open-pdks, GPLv3) — sheet resistances from
its `resist` entries, area/fringe capacitances from its `defaultareacap` /
`defaultperimeter` entries — never NDA'd, the same public-source curation
pattern as the DRC decks and the SPICE model-binding table (#214). Each changed
coefficient carries an inline citation (source file + field name) in its deck
comment.

Even so, the R/C values remain **order-of-magnitude and uncalibrated to
silicon**: while now sourced and re-verifiable against the published process
data, calibrating parasitic-extraction *accuracy* against silicon is an
explicit non-goal of this first cut (#216 "Non-goals"). The model is fixed,
not tunable — there is no `fast`/`accurate` mode selector.

### Coverage: every net with interconnect, not just labelled pins (#283)

Every net with eligible interconnect geometry gets a `parasitics.nets[]`
entry — including purely **internal, unlabelled** nets (e.g. a local driver's
output inside a larger block, which is only a pin one hierarchy level down).
A net does not need a layout label or a `.SUBCKT` pin to be measured: the pass
reads geometry per net object directly from `LayoutToNetlist`, independent of
whether that net happens to carry a name. This matters because the internal
nodes are usually the ones a post-layout resimulation cares about most (DAC
bottom plates, comparator regeneration nodes) — a supply pin's lumped C is
comparatively uninteresting.

A net is omitted from `parasitics.nets[]` only when it is the deck's
substrate/ground net, or when it has genuinely **zero** eligible interconnect
geometry (e.g. a net whose only poly is a transistor gate, already excluded
above, and that carries no metal) — not because it lacks a label.

### What it does *not* do

- **Net-to-net coupling capacitance** is explicitly out of scope for this
  first increment (ground capacitance only). Coupling needs spacing-aware
  neighbor geometry the lumped-to-ground model does not capture; it is a
  credible second increment once a friction log demands it.
- **Device connectivity is untouched.** The parasitic elements are additive:
  no existing device instance, net name, or pin is modified, and the internal
  `net__par` parasitic nodes never surface in `devices[]`/`nets[]` (see below)
  or on the `.SUBCKT` pin interface. The netlist stays a drop-in `klt sim`
  `netlist`.
- **Series R between device terminals is not modelled.** The emitted resistor
  is a shunt, not a through element: it runs from `net` to the internal
  `net__par` node, and the *only* thing attached to `net__par` is the grounded
  parasitic capacitor (`net --R--> net__par --C--> <substrate_net>`, per the
  topology above). Every device terminal on the net stays wired to the original
  `net` name, never to `net__par`, so the resistor carries no DC current and
  never appears in series between two terminals on the same net — not between a
  driver and its receivers, and not between two receivers. The practical
  consequence: `--parasitics` does **not** model IR drop on a shared
  supply/ground/bias rail, nor series resistance in a matched or
  high-impedance path, regardless of the computed R value.

### JSON `parasitics` block

`--parasitics` adds a top-level `parasitics` field (an additive, independently
optional field — `null` when the flag is omitted). The existing
`device_count` / `net_count` / `devices[]` / `nets[]` keep their exact
schematic-equivalent meaning whether or not the flag is given; the parasitic
R/C are **not** counted as devices and the internal nodes are **not** counted
as nets.

```json
"parasitics": {
  "r_count": 4,
  "c_count": 4,
  "total_resistance_ohm": 3050.7818,
  "total_capacitance_ff": 5.400116,
  "nets": [
    {
      "net": "Y",
      "resistance_ohm": 1169.7827,
      "capacitance_ff": 1.910204,
      "internal_node": "Y__par"
    }
  ],
  "metals_without_coefficient": []
}
```

| Field                  | Type            | Description                                                                                     |
| ---------------------- | --------------- | ------------------------------------------------------------------------------------------------- |
| `r_count` / `c_count`  | integer         | Number of parasitic resistors / capacitors emitted (one Γ-section per net, so these are equal).   |
| `total_resistance_ohm` | number          | Sum of the emitted series resistances (ohms).                                                     |
| `total_capacitance_ff` | number          | Sum of the emitted ground capacitances (femtofarads).                                             |
| `nets`                 | array\<object\> | One entry per net carrying parasitics, sorted by `net` for deterministic output. See below.       |
| `metals_without_coefficient` | array\<object\> | Metal stack levels the deck declares for connectivity but its `PARASITICS.metals` table has no coefficient for (issue #547). See below. Empty for both shipped decks. |

Each `nets[]` entry: `net` (the schematic-equivalent net name), `resistance_ohm`,
`capacitance_ff`, and `internal_node` (the injected internal parasitic node's
name, `<net>__par`, or a collision-suffixed variant).

### Curated-coefficient gaps: `metals_without_coefficient` (issue #547)

The per-deck `PARASITICS.metals` table (see "The coefficients are curated
per-PDK-family" above) is index-aligned with the extraction deck's own
`metals` stack, but the two tuples are curated independently — a deck can
declare more metal levels for connectivity than it has sourced RC
coefficients for. `_compute_parasitics` walks `PARASITICS.metals`
index-aligned against the extraction deck's `metals`, so any level past the
end of the shorter tuple (or an explicit `None` entry) silently contributes
zero resistance and capacitance to every net's reported parasitics — the
level is invisible in the number, not merely approximate. This was gf180mcu's
shape until issue #547: `PARASITICS.metals` had one entry (Metal1) against a
5-level `EXTRACTION_DECK.metals` stack, so `--parasitics` on gf180mcu reported
the R and C of Metal1 geometry only, with Metal2 through Metal5 contributing
exactly zero and no signal anywhere in the response saying so.

`metals_without_coefficient` surfaces this gap the same way `ignored_layers`
surfaces geometry the connectivity graph never reads: one entry per gap,
`{"metal_index": int, "layer": int, "datatype": int}` (`metal_index` is
0-based, matching both tuples' shared indexing — index 0 is the deck's
bottom-most metal level, e.g. gf180mcu's Metal1), sorted by `metal_index`.
Empty when every declared metal level has a coefficient — true for both
shipped decks today. A non-empty list is also mirrored as a prose entry in
top-level `warnings[]`, e.g. `"'gf180mcu' deck's PARASITICS.metals has no R/C
coefficient for Metal3, Metal4 -- ..."`, so a caller checking only
`warnings[]` still sees it.

### Parasitic R/C instance names are sanitized, not literal net names

The `R`/`C` cards `--parasitics` injects into the written SPICE are named
after each net's own `net` value above (e.g. net `Y` gets an `RY`/`CY` pair),
but with every character outside `[A-Za-z0-9_]` mapped to `_` first (issue
#312). This matters because a net's name can carry characters a SPICE reader
treats as syntax rather than an opaque token — `$` (KLayout's placeholder for
an unlabelled/anonymous net, e.g. `$12`) and `,` (KLayout's join character
when multiple text labels land on one net, e.g. `Y,Y2`). ngspice does not
reject either: it silently splits the comma-joined form at the comma into an
extra positional node, corrupting the card's arity and erroring against an
unrelated node instead of a clean syntax error.

The sanitization applies to the **instance name only** — a cosmetic handle
nothing downstream keys off of. The `net` field in the `parasitics.nets[]`
JSON above, the `internal_node` name, and the `.SUBCKT` pin interface are all
unaffected and still carry the literal net identity (further escaped by
KLayout's own `NetlistSpiceWriter` where node syntax requires it). If you need
to map an emitted `R`/`C` card back to the net it parasitizes, use
`parasitics.nets[].net` (or the netlist's own node names on that card), not
the sanitized instance name.

## Verified compatible with `klt sim`'s netlist convention

Hard acceptance bar (Epic #153: "`klt extract` output feeds `klt sim`
unmodified"), verified directly against
[`docs/cli/sim.md`](sim.md) → "Netlist convention: a circuit body, not a
full deck" rather than asserted: the written SPICE file is a
`.SUBCKT <top> <pins…> … .ENDS <top>` circuit body with **no top-level
`.control`/`.end` card** — confirmed directly against KLayout's
`NetlistSpiceWriter` output (it never emits a top-level `.END` for a
single-circuit netlist) and exercised by `tests/test_extract.py`.

An extracted netlist is a *DUT* with no stimulus (nothing in a layout says
"this rail is 1.8 V"); it is consumed by `klt sim` the way any DUT is — a
thin testbench `.include`s the extracted file, instantiates the `.subckt`,
and adds the sources, and *that* testbench is the `klt sim` `netlist`.
`tests/test_extract.py`'s `test_extracted_netlist_feeds_klt_sim_unmodified`
exercises exactly this against a real sky130 corpus cell end to end (skipped
when `ngspice` is not installed).

That testbench supplies the `.model` cards for the deck's device-class names
(e.g. `.model nfet nmos level=1`) — the same convention applies to a drawn
resistor, which is written as `R$1 A B 289.2 res_generic_po`: the extracted
resistance plus the device-class name as a model token, so the testbench adds
a matching `.model res_generic_po r`. (Unrelated to `--parasitics`, whose
injected R/C elements are deliberately emitted as *bare* `R`/`C` cards with
no model token.)

## JSON schema (the contract)

**JSON is the API.** Human-readable text output is a courtesy; the JSON
schema below is the stable contract. Per the project's rules, **breaking
(renaming, removing, or retyping) a field is a breaking change**. New fields
may be added without breaking the contract, so consumers should ignore
unknown fields. See [`docs/json-contract.md`](../json-contract.md) for the
envelope shared across all `klt` commands (`schema_version`, error shape,
exit codes).

```json
{
  "schema_version": 1,
  "file": "design.gds",
  "deck": "sky130",
  "top": "ota_5t",
  "dbu_um": 0.001,
  "netlist_path": "design.spice",
  "netlist_sha256": "4f1c...",
  "status": "extracted",
  "device_count": 2,
  "net_count": 6,
  "pin_count": 6,
  "device_counts": { "nfet": 1, "pfet": 1 },
  "ignored_layers": [{ "layer": 55, "datatype": 0, "shapes": 12 }],
  "device_classes": [
    "nfet",
    "pfet",
    "pnp",
    "sky130_fd_pr__model__cap_mim",
    "sky130_fd_pr__model__cap_mim_m4",
    "resistor"
  ],
  "devices": [
    {
      "name": "$1",
      "class": "nfet",
      "nets": { "s": "VGND", "g": "A", "d": "Y", "b": "vsubs" },
      "params": { "w_um": 0.65, "l_um": 0.15 }
    }
  ],
  "nets": [{ "name": "A", "pin": true, "device_count": 2 }],
  "warnings": [],
  "black_box_regions": [],
  "unmodelled_poly": [],
  "merged_net_labels": [],
  "voltage_domain_warnings": [],
  "pdk": null,
  "parasitics": null,
  "provenance": {
    "klt_version": "0.4.2",
    "klayout_version": "0.29.8",
    "pdk": null,
    "deck": { "name": "sky130", "content_hash": "sha256:<hex>" },
    "input": { "content_hash": "sha256:<hex>" }
  }
}
```

### Top-level fields

| Field              | Type                       | Description                                                                                          |
| ------------------ | -------------------------- | ------------------------------------------------------------------------------------------------------ |
| `schema_version`   | integer                    | Version of this command's JSON shape (starts at `1`; per-command, per `docs/json-contract.md`).        |
| `file`             | string                     | The input path exactly as provided on the command line.                                                |
| `deck`             | string                     | Extraction deck used (`"sky130"` / `"gf180mcu"`).                                                      |
| `top`              | string                     | Top cell the netlist was extracted from.                                                               |
| `dbu_um`           | number (float)             | Database unit in micrometres, same semantics as `klt layers`/`klt drc`.                                |
| `netlist_path`     | string                     | Resolved path of the written SPICE netlist (echoes `--output` or the computed default).                |
| `netlist_sha256`   | string                     | SHA-256 hex digest of the written netlist file.                                                        |
| `status`           | `"extracted"`              | Never `"error"` — a failed run does not emit this envelope at all (see Exit codes).                    |
| `device_count`     | integer                    | `len(devices)`.                                                                                        |
| `net_count`        | integer                    | `len(nets)`.                                                                                           |
| `pin_count`        | integer                    | Number of `nets[]` entries with `pin: true`.                                                           |
| `device_counts`    | object\<string, int\>      | Per-device-class counts, keyed by `devices[].class`, keys sorted for determinism (`"nfet"`/`"pfet"`, and, on decks/layouts that have one, a bipolar class like `"pnp"`/`"bjt"`, a MiM-capacitor class like `"sky130_fd_pr__model__cap_mim"`/`"cap_mim_2f0_m4m5_noshield"`, and/or a drawn-resistor class like `"res_generic_po"`/`"ppolyf_u"`). What was actually **found**.  |
| `dummy_devices_dropped` | integer               | Number of devices suppressed by the deck's optional `dummy` marker layer — MOS gates (issue #295), drawn resistors and bipolars (both issue #462), and junction diodes (issue #542) alike — deliberately non-functional dummy devices excluded from `devices[]`/`device_counts` before recognition. `0` when the deck declares no `dummy` layer or the layout draws none. See "Dummy devices: the `dummy` marker layer". |
| `ignored_layers`   | array\<object\>            | `(layer, datatype)` pairs carrying shapes in the input stream that this `--deck`'s connectivity graph does **not** read, each `{ "layer": int, "datatype": int, "shapes": int }` with its stream shape count, sorted by `(layer, datatype)`. Empty when every shape-bearing layer is one the deck reads. Geometry on such a layer is invisible to extraction, so a block routed on an undeclared metal level silently extracts as disconnected nets — a non-empty list with a material shape count is the signal that a downstream `klt lvs` mismatch is a deck-coverage gap, not a layout bug. The extraction-side analogue of `klt drc`'s `coverage.layers_in_stream_without_rules`. |
| `device_classes`   | array\<string\>            | The device-class roles this `--deck` is structurally capable of recognising (`["nfet", "pfet"]` for a MOS-only deck; a deck that also declares a bipolar entry appends its class name, e.g. sky130's `[..., "pnp", ...]` or gf180mcu's `[..., "bjt", ...]` — see "Bipolar (BJT) device recognition" above; a deck that declares one or more MiM-capacitor entries likewise appends each one's class name, e.g. sky130's two `"sky130_fd_pr__model__cap_mim"`/`"sky130_fd_pr__model__cap_mim_m4"` or gf180mcu's one `"cap_mim_2f0_m4m5_noshield"` — see "MiM capacitor device recognition" above; a deck that declares one or more drawn resistors appends the single `"resistor"` role after those — see "Drawn resistors" above; and a deck that declares one or more junction diodes appends each one's class name last, e.g. gf180mcu's `"diode_nd2ps_06v0"`/`"diode_pd2nw_06v0"` — see "Junction diodes" above), independent of what this layout happens to contain. sky130 currently reports `["nfet", "pfet", <bipolar>, <capacitor…>, "resistor"]` and gf180mcu `["nfet", "pfet", <bipolar>, <capacitor>, "resistor", <diode…>]`. Note the `"resistor"` role token is not a `devices[].class` label string (a deck's resistor class is named after the PDK device it models). What the deck **can find**, not what it found — see `device_counts` for that. A consumer that needs to know ahead of time whether a deck supports a given device class (e.g. before pairing it with a reference netlist for `klt lvs`) reads this instead of inferring "unsupported" from a zero count. |
| `devices`          | array\<object\>            | One entry per extracted device, see below.                                                             |
| `nets`             | array\<object\>            | One entry per extracted net, see below.                                                                |
| `warnings`         | array\<string\>            | Non-fatal extraction notes (e.g. a gate shape touching no diffusion, the unmodelled-device-geometry heuristic below, or a top-level pin promoted from a label found below the top cell — see "Top-cell-only pin promotion"). Always present, empty when clean. |
| `black_box_regions` | array\<object\>           | One entry per black-box/abstract region excluded from connectivity (issue #293 — see "Black-box / abstract regions" above), each `{ "bbox_um": {"left", "bottom", "right", "top"}, "shapes_excluded": int }`. Always present, empty when the layout draws no reserved-annotation-layer (990-999) geometry — see "Reserved annotation layer" above. |
| `unmodelled_poly`  | array\<object\>           | One entry per `poly` shape the unmodelled-device diagnostic flagged (issue #324 — see "Known limitation: unmodelled device geometry" below), each `{ "bbox_um": {"left", "bottom", "right", "top"}, "reason": "unmarked" \| "marked_unrecognised" }`. `reason` mirrors the two `warnings[]` cases below without requiring a consumer to parse the prose string. Sorted by `(left, bottom)` for deterministic output. Always present, empty whenever `warnings[]` carries no unmodelled-device entry. |
| `merged_net_labels` | array\<object\>          | One entry per net whose KLayout-assigned name is a comma-joined merge of 2+ distinct labels (issue #470 — see "Merged net labels" below), each `{ "net": "<full joined name>", "labels": [str, ...] }` (`labels` is `net` split on `,`). A matching prose entry is also appended to `warnings[]` for every affected net. Always present, empty when no net carries multiple labels. |
| `voltage_domain_warnings` | array\<object\>     | One entry per voltage-domain marker layer (issue #552 — see "Voltage-domain markers" below) whose geometry overlaps extracted MOS device geometry, each `{ "marker": "<layer>/<datatype>", "description": str }`. A matching prose entry is also appended to `warnings[]`. Always present, empty for a deck that registers no such marker or a layout that draws none of it overlapping MOS geometry. |
| `pdk`              | object \| `null`           | `{"variant", "root", "version"}` when `--pdk`/`--pdk-root` were given and resolved; `null` otherwise.   |
| `parasitics`       | object \| `null`           | Lumped RC summary when `--parasitics` was given; `null` otherwise. See "Parasitic (RC) extraction".     |
| `provenance`       | object                     | Shared reproducibility block (`klt_version`, `klayout_version`, `pdk`, `deck`, `input`) defined once in [`docs/json-contract.md`](../json-contract.md). Its `pdk` mirrors the resolved PDK as `{name, source, version}` (the richer `pdk` field above carries `root`); `deck` pins the extraction deck by name and `sha256:` content hash; `input` pins the input layout file (`path`, distinct from `netlist_sha256`, which hashes the *written* netlist) by `sha256:` content hash. |

The `devices[]`/`nets[]` report is a *convenience view* for agents that want
structure without re-parsing SPICE; the **netlist file at `netlist_path` is
the authoritative artifact**, and it is what a future `klt lvs` and `klt sim`
consume.

### `devices[]` entries

| Field    | Type                        | Description                                                                                    |
| -------- | --------------------------- | ------------------------------------------------------------------------------------------------ |
| `name`   | string                      | The device's instance name in the written netlist (e.g. `"$1"`, matching the `M$1 ...` line).    |
| `class`  | string                      | The deck's device-class name (`"nfet"` / `"pfet"`, a declared bipolar class like `"pnp"` / `"bjt"`, a declared MiM-capacitor class like `"sky130_fd_pr__model__cap_mim"` / `"cap_mim_2f0_m4m5_noshield"`, a declared drawn-resistor class like `"res_generic_po"` on sky130 / `"ppolyf_u"` on gf180mcu — see "Drawn resistors" — or a declared junction-diode class like `"diode_nd2ps_06v0"` on gf180mcu, see "Junction diodes"). |
| `nets`   | object\<string, string\|null\> | Terminal → net-name map. MOS: `"s"`, `"g"`, `"d"`, `"b"`. Bipolar: `"c"`, `"b"`, `"e"` (collector/base/emitter — see "Bipolar (BJT) device recognition" above). MiM capacitor: `"a"`, `"b"` (the two plates — see "MiM capacitor device recognition" above). Drawn resistor: `"a"`, `"b"` (the two heads), plus `"w"` for a resistor with a bulk terminal (gf180mcu's `ppolyf_u`, tied to the deck's substrate global — see "Drawn resistors"). Junction diode: `"a"`, `"c"` (anode/cathode — see "Junction diodes" above). `null` only if a terminal has no connected net at all (never observed for `s`/`g`/`d` in this deck's extraction; MOS `b`, bipolar `b` and a diode's `Nwell`-side terminal can be `null`-free but anonymous, see "Coverage"). |
| `params` | object\<string, number\>    | MOS: `"w_um"` / `"l_um"`, the extracted gate width/length in micrometres. Bipolar: empty (KLayout's `DeviceClassBJT3Transistor` reports area/perimeter parameters this field does not extract). MiM capacitor: `"c_f"` (extracted capacitance, in **Farads**), `"area_um2"` (the plates' overlap area, in square micrometres), and `"perimeter_um"` (the plates' overlap perimeter, in micrometres, issue #512) — `c_f = area_um2 * area_cap + perimeter_um * perim_cap`, see "MiM capacitor device recognition" above (`perim_cap` defaults to `0.0` for a deck that has not set `CapacitorDevice.perim_cap_f_um`, reproducing the pre-#512 area-only formula bit-for-bit). Drawn resistor: `"w_um"` / `"l_um"` for the resistive segment's own width/length, plus `"r_ohm"` — the extracted resistance, `l_um / w_um * sheet_rho + fixed_offset_ohm` (issue #518; `fixed_offset_ohm` defaults to `0.0` for a deck that has not set `ResistorDevice.fixed_offset_ohm`, reproducing the pre-#518 `l_um / w_um * sheet_rho`-only formula bit-for-bit — see "Drawn resistors" above). Junction diode: `"area_um2"` / `"perimeter_um"`, the recognised junction's own area/perimeter (issue #542) — no I-V model is extracted, see "Junction diodes" above. |

`devices` is sorted by `name` for deterministic, diff-clean output.

### `nets[]` entries

| Field          | Type    | Description                                                                 |
| -------------- | ------- | ------------------------------------------------------------------------------ |
| `name`         | string  | The net's name in the written netlist (a labelled name, or an anonymous `$N`). |
| `pin`          | boolean | Whether this net is promoted to a top-cell pin (a named net at the top level). |
| `device_count` | integer | Number of device terminals connected to this net.                             |

`nets` is sorted by `name` for deterministic, diff-clean output.

## Exit codes

| Code | Meaning                                                                                          |
| ---- | --------------------------------------------------------------------------------------------------- |
| `0`  | Extraction succeeded, netlist written.                                                              |
| `1`  | Failed to run — bad file, unknown `--deck`, unresolvable PDK (when `--pdk`/`--pdk-root` given), a resolved PDK with no curated model-binding table entry for `--deck` (see "SPICE model binding" above), missing/ambiguous top cell, or an engine error (e.g. device recognition producing a device with an unconnected terminal — most commonly a bipolar device-mark drawn exactly coincident with, rather than strictly enclosing, the terminal geometry it scopes; confirmed clean by this module's own test suite, issue #432). |
| `2`  | Usage error (missing argument, bad `--format` value) — from argparse.                               |

There is no exit code `3` — unlike `klt drc`/`klt lvs`, there is no "ran but
found problems" outcome for extraction; it either produces a netlist or it
fails (matching `klt gen`'s reasoning for omitting a `3`).

On error (exit 1), a concise message is written to **stderr** and nothing is
written to stdout. No Python traceback is printed.

- `--format text` (default): a plain-text line prefixed `klt extract:`.
- `--format json`: the documented JSON error envelope (see
  [`docs/json-contract.md`](../json-contract.md)):

  ```json
  { "schema_version": 1, "error": { "command": "extract", "message": "unknown deck 'nope' (available: gf180mcu, sky130)" } }
  ```

## Out of scope

First-order lumped RC parasitics are available behind `--parasitics` (see
above, issue #217). The following remain out of scope:

- **Net-to-net coupling capacitance.** `--parasitics` models lumped
  capacitance to ground only; coupling between neighboring nets is
  deliberately deferred (it needs spacing-aware neighbor geometry the
  lumped-to-ground model does not capture) and is a credible second
  increment. Without `--parasitics`, no interconnect R/C is extracted at all.
- **Series resistance between device terminals.** `--parasitics` emits each
  net's resistor as a shunt to a grounded capacitor, not as a through element
  between terminals (see "Parasitic (RC) extraction" → "What it does *not*
  do"); every device terminal stays on the original net, so the emitted R
  never carries DC current between two terminals on the same net. It therefore
  does not model IR drop on a shared supply/ground/bias rail, nor series
  resistance in a matched or high-impedance path, regardless of the computed R
  value.
- **Parasitic-extraction accuracy calibration.** The `--parasitics`
  coefficients are now sourced and cited from each PDK's public magic
  technology file (see "Parasitic (RC) extraction"), but they remain
  order-of-magnitude and uncalibrated to silicon; calibrating them against
  silicon is an explicit non-goal (#216 "Non-goals").
- **Resistor flavours beyond what's wired.** Each deck now declares more
  than one drawn-resistor device class (issue #222, extended by #299 --
  see "Drawn resistors"), but not every PDK flavour: sky130 models its
  generic, `rpm`, and `urpm` poly resistors (each with a *single* flat
  sheet-rho, not the official deck's five-way per-length device-class split
  — see the `res_high_po`/`res_xhigh_po` provenance note in
  `decks/sky130.py`), but not its diffusion or metal resistors; gf180mcu
  models its unsalicided p+ poly resistor at both its base sheet-rho and its
  PDK-default `POLY_RES='1k'` high-sheet-rho variant (the `_2k`/`_3k`
  siblings are geometrically indistinguishable from `_1k` in a drawn
  layout — see `decks/gf180mcu.py`), but not its salicided, N+ poly,
  diffusion, well, or metal resistors. These remaining flavours are
  deliberately excluded rather than approximated — see "Drawn resistors".

Netlist comparison (`klt lvs`) is a separate command; this command only
produces the layout-side netlist half of that comparison.
