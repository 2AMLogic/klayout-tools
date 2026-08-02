# `klt extract`

Extract a **schematic-equivalent** netlist (devices + connectivity) from a
GDSII or OASIS layout stream, headless, and write it as a SPICE circuit body
plus a structured summary. An opt-in `--parasitics` flag additionally extracts
first-order lumped RC interconnect parasitics (see "Parasitic (RC)
extraction" below).

```
klt extract <file> --deck sky130|gf180mcu [-o|--output <netlist.spice>] [--top <cell>] [--pdk <variant>] [--pdk-root <root>] [--parasitics] [--top-cell-pins] [--format text|json]
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
through the PDK's metal stack — sky130's `li1`/`met1` and gf180mcu's full
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
  layer, so there is no drawn tap geometry to derive a real net name from.
  The NMOS body terminal is tied to a global net (`vsubs` by default) via
  KLayout's `connect_global` instead of a real substrate-tap extraction.
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

| Deck | `devices[].class` | Top plate | Bottom plate | Capacitance density |
| ---- | ------------------ | --------- | ------------ | -------------------- |
| `gf180mcu` | `cap_mim_2f0_m4m5_noshield` | `FuseTop` (75/0), requires `CAP_MK` (117/5) + `MIM_L_MK` (117/10), excludes `efuse_mk`/`plfuse` | "Virtual bottom plate": `Metal4` (46/0) clipped to `FuseTop` sized (oversized) by 1.06µm | **2.0 fF/µm²** |
| `sky130` | `sky130_fd_pr__model__cap_mim` | `capm.drawing` (89/44) | `met3.drawing` (70/20), unfiltered | **2.0 fF/µm²** |
| `sky130` | `sky130_fd_pr__model__cap_mim_m4` | `capm2.drawing` (97/44) | `met4.drawing` (71/20), unfiltered | **2.0 fF/µm²** |

KLayout computes `C = A * area_cap` from the two plates' actual geometric
overlap area, so the capacitance-density number *is* the accuracy of the
extracted value — the capacitor analogue of a drawn resistor's sheet
resistance. Each is transcribed from that PDK's own official KLayout **LVS**
deck (a different source than the DRC rule tables the rest of each deck
module cites — sky130's `sky130.lvs`, gf180mcu's `mimcap_derivations.lvs` /
`mimcap_extraction.lvs`) and cross-checked against independent sources in
the same PDK install — see the per-deck module docstrings
(`src/klayout_tools/decks/sky130.py`, `.../gf180mcu.py`) for the exact
source files, the derivation each is transcribed from, and every
approximation taken relative to it. gf180mcu's stack additionally needs a
"virtual bottom plate" derivation (its bottom plate is ordinary `Metal4`
routing, not a purpose-drawn cap layer the way sky130's `capm`/`capm2`
are) — clipping the bottom conductor to the sized top-plate outline so an
unrelated `Metal4` trace 1.06µm away from a real MiM cap is not swept in.

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
    (`li1`/`met1` only), and while the real PDK's MiM stacks do have a
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

| Deck | `devices[].class` | Models | Body | Marker | Also requires | Sheet resistance |
| ---- | ----------------- | ------ | ---- | ------ | ------------- | ---------------- |
| `sky130`   | `res_generic_po` | `sky130_fd_pr__res_generic_po` | `poly.drawing` 66/20 | `poly.res` 66/13 | — | **48.2 Ω/□** |
| `sky130`   | `res_high_po` | `sky130_fd_pr__res_high_po_*` | `poly.drawing` 66/20 | `poly.res` 66/13 | `psdm` 94/20, `rpm` 86/20 | **319.8 Ω/□** |
| `sky130`   | `res_xhigh_po` | `sky130_fd_pr__res_xhigh_po_*` | `poly.drawing` 66/20 | `poly.res` 66/13 | `psdm` 94/20, `urpm` 79/20 | **2000 Ω/□** |
| `gf180mcu` | `ppolyf_u` | `gf180mcu_fd_pr__ppolyf_u` (P+ poly, unsalicided) | `Poly2` 30/0 | `RES_MK` 110/5 | `Pplus` 31/0, `SAB` 49/0 | **350 Ω/□** |
| `gf180mcu` | `ppolyf_u_1k` | `gf180mcu_fd_pr__ppolyf_u_1k` (`POLY_RES='1k'` default) | `Poly2` 30/0 | `RES_MK` 110/5 | `SAB` 49/0, `Resistor` 62/0 | **1000 Ω/□** |

KLayout computes `R = L / W * sheet_rho` from the recognised segment's own
geometry, so the sheet-resistance number *is* the accuracy of the extracted
value. Each is transcribed from that PDK's **own official KLayout LVS deck**
and cross-checked against a second independent source (open_pdks' magic
technology file) in the same PDK install — see the per-deck module docstrings
(`src/klayout_tools/decks/sky130.py`, `.../gf180mcu.py`) for the exact source
lines, the derivation each is transcribed from, and every approximation taken
relative to it.

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

### Known limitation: unmodelled device geometry (issue #288)

Every layer this deck reads (`active`, `poly`, `nwell`, `contact`, `metals`,
...) is a **connectivity layer**, wired up unconditionally by
`_extract_netlist`'s connectivity block regardless of whether any device
extractor claims the geometry drawn on it. If a layout contains geometry
drawn for a device class the active deck does not (yet) implement — today,
anything beyond `nfet`/`pfet` plus each deck's curated resistor/bipolar/MiM-
cap entries above — that geometry is **not skipped**. It is absorbed into
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
  distinct from a routing run with a single landing pad).

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

### Dummy devices: the `dummy` marker layer (issue #295)

Analog matching practice puts **dummy devices** on the edges of a matched
pair or array so every functional device sees the same lithographic/stress
neighbourhood. A dummy is drawn geometry that is deliberately *not* part of
the circuit: its gate and both diffusions are tied off to a rail, and it
contributes nothing to the schematic. Because a drawn dummy MOS is an
otherwise-ordinary `nfet`/`pfet`, it used to land in the extracted netlist as
a real device that the schematic-derived reference has no counterpart for —
so `klt lvs` reported a spurious `device.unmatched` (plus the usual net
cascade) for every dummy drawn, forcing an unlayoutable choice between
matching quality and a clean compare.

A deck may now declare an optional `dummy` marker layer (see the deck-schema
table below). Any MOS gate lying under a shape on that layer is **dropped
before device recognition**: `_extract_netlist` subtracts the marker region
from the NMOS/PMOS gate regions, so a gate fully covered by the marker is
never handed to KLayout's device extractor and never becomes a device at all.
The dummy therefore does not appear in `devices[]`, `device_count`, or
`device_counts`, and `klt lvs` no longer sees a phantom device to mismatch.

Only the **gate** is cut. The dummy's diffusions (source/drain) and its gate
poly remain in the connectivity graph, so they still extract as ordinary
interconnect and tie off to the rail exactly as drawn — consistent with a
dummy being "gate and both diffusions tied off to a rail". A marker that only
partially covers a gate is a clean geometric cut (the same subtraction
precedent drawn resistors use), not an all-or-nothing reclassification: the
remaining gate area still extracts as a device, and only a gate the marker
fully consumes is counted as dropped.

For visibility (rather than a silent drop — the failure mode issue #288 was
filed about), the JSON response carries a `dummy_devices_dropped` count of
how many gates the marker suppressed. It is `0` when the active deck declares
no `dummy` layer or the layout draws no dummy geometry. Declaring `dummy`
is fully opt-in and additive: a deck that does not set it extracts exactly as
it did before the field existed, byte-for-byte.

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

**When a PDK resolves now**, each extracted MOS device is written as an `X`
subcircuit call against the resolved PDK's real device library instead:

```
X$1 Y A VGND vsubs sky130_fd_pr__nfet_01v8 L=0.15U W=0.65U
```

The device is bound via a small curated
`(deck_name, pdk_variant_family) -> {"nfet": <subckt>, "pfet": <subckt>}`
table (`src/klayout_tools/pdk_models.py`; see that module's docstring for
the exact provenance of each bound subcircuit name, verified against a real
fetched PDK install rather than assumed) and a
`kdb.NetlistSpiceWriterDelegate` subclass that overrides KLayout's default
`M`-card device writer only for classes present in the resolved table.

**Scope limits** (deliberately narrower than the general PDK-device-metadata
resolver `docs/design/pdk-device-corner-metadata-spike.md` proposes as a
future epic):

- **MOS family only**, one voltage flavor per PDK family — the only flavor
  the curated extraction decks distinguish (see this module's own docstring):
  sky130's `01v8` core devices (`sky130_fd_pr__nfet_01v8` /
  `sky130_fd_pr__pfet_01v8`) and gf180mcu's `03v3` core devices (`nfet_03v3`
  / `pfet_03v3` — gf180mcu has no `gf180mcu_fd_pr__`-prefixed naming
  convention the way sky130 does).
- **Two curated decks only** (`sky130`, `gf180mcu`); a resolved PDK whose
  family has no curated table entry for the requesting `--deck` (e.g. the
  `sky130` deck against a resolved `gf180mcuA` install, or a variant name
  matching no known PDK family at all) is an application error (exit 1)
  naming what was tried — **never** a silent fallback to the bare `M`-card
  form.
- The written `X` card carries only `L`/`W` (both with an explicit
  micrometre unit suffix, e.g. `L=0.15U` — the same convention `klt
  extract`'s `M`-card form already uses, unambiguous regardless of any
  `.option scale` a caller's testbench may or may not set) and relies on
  the resolved subcircuit's own defaults for everything else (`nf`/`mult`/
  `par`, all confirmed `1`-equivalent in the fetched real installs this
  table was verified against — this deck's device extractor never models
  multi-finger/multiplied devices either). Source/drain area+perimeter
  (`AS`/`AD`/`PS`/`PD`, present on the bare `M`-card form) are **not**
  carried onto the `X` card — consistent with this command's documented
  schematic-equivalent, no-parasitics scope (see "Out of scope" below).
- **The JSON response is unaffected**: `device_counts`/`devices[].class`
  always report the deck's own class label (`nfet`/`pfet`), regardless of
  `--pdk`. Model binding is a SPICE-serialization concern only.

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
  ]
}
```

| Field                  | Type            | Description                                                                                     |
| ---------------------- | --------------- | ------------------------------------------------------------------------------------------------- |
| `r_count` / `c_count`  | integer         | Number of parasitic resistors / capacitors emitted (one Γ-section per net, so these are equal).   |
| `total_resistance_ohm` | number          | Sum of the emitted series resistances (ohms).                                                     |
| `total_capacitance_ff` | number          | Sum of the emitted ground capacitances (femtofarads).                                             |
| `nets`                 | array\<object\> | One entry per net carrying parasitics, sorted by `net` for deterministic output. See below.       |

Each `nets[]` entry: `net` (the schematic-equivalent net name), `resistance_ohm`,
`capacitance_ff`, and `internal_node` (the injected internal parasitic node's
name, `<net>__par`, or a collision-suffixed variant).

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
  "pdk": null,
  "parasitics": null,
  "provenance": {
    "klt_version": "0.4.2",
    "klayout_version": "0.29.8",
    "pdk": null,
    "deck": { "name": "sky130", "content_hash": "sha256:<hex>" }
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
| `dummy_devices_dropped` | integer               | Number of MOS gates suppressed by the deck's optional `dummy` marker layer (issue #295) — deliberately non-functional dummy devices excluded from `devices[]`/`device_counts` before recognition. `0` when the deck declares no `dummy` layer or the layout draws none. See "Dummy devices: the `dummy` marker layer". |
| `ignored_layers`   | array\<object\>            | `(layer, datatype)` pairs carrying shapes in the input stream that this `--deck`'s connectivity graph does **not** read, each `{ "layer": int, "datatype": int, "shapes": int }` with its stream shape count, sorted by `(layer, datatype)`. Empty when every shape-bearing layer is one the deck reads. Geometry on such a layer is invisible to extraction, so a block routed on an undeclared metal level silently extracts as disconnected nets — a non-empty list with a material shape count is the signal that a downstream `klt lvs` mismatch is a deck-coverage gap, not a layout bug. The extraction-side analogue of `klt drc`'s `coverage.layers_in_stream_without_rules`. |
| `device_classes`   | array\<string\>            | The device-class roles this `--deck` is structurally capable of recognising (`["nfet", "pfet"]` for a MOS-only deck; a deck that also declares a bipolar entry appends its class name, e.g. sky130's `[..., "pnp", ...]` or gf180mcu's `[..., "bjt", ...]` — see "Bipolar (BJT) device recognition" above; a deck that declares one or more MiM-capacitor entries likewise appends each one's class name, e.g. sky130's two `"sky130_fd_pr__model__cap_mim"`/`"sky130_fd_pr__model__cap_mim_m4"` or gf180mcu's one `"cap_mim_2f0_m4m5_noshield"` — see "MiM capacitor device recognition" above; and a deck that declares one or more drawn resistors appends the single `"resistor"` role after those — see "Drawn resistors" above), independent of what this layout happens to contain. Both registered decks currently report `["nfet", "pfet", <bipolar>, <capacitor…>, "resistor"]`. Note the trailing `"resistor"` is a role token, not a `devices[].class` label string (a deck's resistor class is named after the PDK device it models). What the deck **can find**, not what it found — see `device_counts` for that. A consumer that needs to know ahead of time whether a deck supports a given device class (e.g. before pairing it with a reference netlist for `klt lvs`) reads this instead of inferring "unsupported" from a zero count. |
| `devices`          | array\<object\>            | One entry per extracted device, see below.                                                             |
| `nets`             | array\<object\>            | One entry per extracted net, see below.                                                                |
| `warnings`         | array\<string\>            | Non-fatal extraction notes (e.g. a gate shape touching no diffusion, the unmodelled-device-geometry heuristic below, or a top-level pin promoted from a label found below the top cell — see "Top-cell-only pin promotion"). Always present, empty when clean. |
| `black_box_regions` | array\<object\>           | One entry per black-box/abstract region excluded from connectivity (issue #293 — see "Black-box / abstract regions" above), each `{ "bbox_um": {"left", "bottom", "right", "top"}, "shapes_excluded": int }`. Always present, empty when the layout draws no reserved-annotation-layer (990-999) geometry — see "Reserved annotation layer" above. |
| `pdk`              | object \| `null`           | `{"variant", "root", "version"}` when `--pdk`/`--pdk-root` were given and resolved; `null` otherwise.   |
| `parasitics`       | object \| `null`           | Lumped RC summary when `--parasitics` was given; `null` otherwise. See "Parasitic (RC) extraction".     |
| `provenance`       | object                     | Shared reproducibility block (`klt_version`, `klayout_version`, `pdk`, `deck`) defined once in [`docs/json-contract.md`](../json-contract.md). Its `pdk` mirrors the resolved PDK as `{name, source, version}` (the richer `pdk` field above carries `root`); `deck` pins the extraction deck by name and `sha256:` content hash. |

The `devices[]`/`nets[]` report is a *convenience view* for agents that want
structure without re-parsing SPICE; the **netlist file at `netlist_path` is
the authoritative artifact**, and it is what a future `klt lvs` and `klt sim`
consume.

### `devices[]` entries

| Field    | Type                        | Description                                                                                    |
| -------- | --------------------------- | ------------------------------------------------------------------------------------------------ |
| `name`   | string                      | The device's instance name in the written netlist (e.g. `"$1"`, matching the `M$1 ...` line).    |
| `class`  | string                      | The deck's device-class name (`"nfet"` / `"pfet"`, a declared bipolar class like `"pnp"` / `"bjt"`, a declared MiM-capacitor class like `"sky130_fd_pr__model__cap_mim"` / `"cap_mim_2f0_m4m5_noshield"`, or a declared drawn-resistor class like `"res_generic_po"` on sky130 / `"ppolyf_u"` on gf180mcu — see "Drawn resistors"). |
| `nets`   | object\<string, string\|null\> | Terminal → net-name map. MOS: `"s"`, `"g"`, `"d"`, `"b"`. Bipolar: `"c"`, `"b"`, `"e"` (collector/base/emitter — see "Bipolar (BJT) device recognition" above). MiM capacitor: `"a"`, `"b"` (the two plates — see "MiM capacitor device recognition" above). Drawn resistor: `"a"`, `"b"` (the two heads), plus `"w"` for a resistor with a bulk terminal (gf180mcu's `ppolyf_u`, tied to the deck's substrate global — see "Drawn resistors"). `null` only if a terminal has no connected net at all (never observed for `s`/`g`/`d` in this deck's extraction; MOS `b` and bipolar `b` can be `null`-free but anonymous, see "Coverage"). |
| `params` | object\<string, number\>    | MOS: `"w_um"` / `"l_um"`, the extracted gate width/length in micrometres. Bipolar: empty (KLayout's `DeviceClassBJT3Transistor` reports area/perimeter parameters this field does not extract). MiM capacitor: `"c_f"` (extracted capacitance, in **Farads**) and `"area_um2"` (the plates' overlap area, in square micrometres — `c_f = area_um2 * area_cap`, see "MiM capacitor device recognition" above). Drawn resistor: `"w_um"` / `"l_um"` for the resistive segment's own width/length, plus `"r_ohm"` — the extracted resistance, `l_um / w_um * sheet_rho`. |

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
| `1`  | Failed to run — bad file, unknown `--deck`, unresolvable PDK (when `--pdk`/`--pdk-root` given), a resolved PDK with no curated model-binding table entry for `--deck` (see "SPICE model binding" above), missing/ambiguous top cell, or an engine error. |
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
