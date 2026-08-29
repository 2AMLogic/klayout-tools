# `klt extract`

Extract a **schematic-equivalent** netlist (devices + connectivity) from a
GDSII or OASIS layout stream, headless, and write it as a SPICE circuit body
plus a structured summary. An opt-in `--parasitics` flag additionally extracts
first-order lumped RC interconnect parasitics (see "Parasitic (RC)
extraction" below).

```
klt extract <file> --deck sky130|gf180mcu|sg13g2|sg13cmos5l [-o|--output <netlist.spice>] [--top <cell>] [--pdk <variant>] [--pdk-root <root>] [--parasitics] [--top-cell-pins] [--pins <A,B,VDD,VSS>] [--deck-option <key>=<value> ...] [--defer-resistor-fixed-offset] [--abstract-cells <glob> ...] [--abstract-cell-lef <path> ...] [--format text|json]
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
  Currently: `sky130`, `gf180mcu`, `sg13g2`, `sg13cmos5l`. `sg13g2`'s device
  coverage is MOS (thin-oxide `sg13_lv_*` plus, as of issue #1231, the
  thick-oxide `sg13_hv_*` flavour scoped to `ThickGateOx` 44/0), three drawn
  poly resistors (`rsil`, `rppd`, and — as of issue #1235, its upstream
  sheet-rho ambiguity resolved against a third citable source — `rhigh`),
  and two drawn metal resistors (`res_metal1`, `res_metal2`, also issue
  #1235); it has **no** curated capacitor, bipolar (HBT) or diode entries
  yet, and deliberately omits the remaining metal resistors
  (`res_metal3`..`res_topmetal2`, above this deck's curated Metal2 stack)
  — see `src/klayout_tools/decks/sg13g2.py`'s own docstring for each gap
  and why. `sg13cmos5l`'s device coverage is MOS (both the thin-oxide
  `sg13_lv_*` flavour and, as of issue #1416, the thick-oxide `sg13_hv_*`
  flavour scoped to the same `ThickGateOx` 44/0) plus the same three drawn
  poly resistors (`rsil`, `rppd`, `rhigh`, added by issue #1415); it has
  **no** curated capacitor, bipolar or diode entries yet, and — although
  issue #1417 has since extended this deck's metal stack to the full
  `Metal1`-`TopMetal1` stack those bodies sit on — the `res_metal1`..
  `res_topmetal1` metal-resistor family is not transcribed by this deck yet
  — see `src/klayout_tools/decks/sg13cmos5l.py`'s own docstring for each gap
  and why.
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
- `--deck-option` — optional, unset by default, repeatable. `<key>=<value>`
  selects which caller-visible flavour of a deck's shared-geometry device
  family this run wires (issue #595) — today's only recognised key is
  gf180mcu's `poly_res` (values `1k` (the PDK's own default), `2k`, `3k`).
  An unrecognised key or value is an application error, not a silently-kept
  default. Omitting the flag resolves every deck exactly as before it
  existed. See "Selecting a shared-geometry resistor flavour" below.
- `--defer-resistor-fixed-offset` — optional, off by default. Omit each
  opted-in resistor device class's `ResistorDevice.fixed_offset_ohm`
  head/end-resistance term from the extracted `R`, leaving only the raw
  per-primitive body resistance in both the written SPICE and the JSON
  `devices[].params.r_ohm` (issue #588). Use this when the netlist will be
  read back through `klt lvs`'s pre-extracted `layout.netlist` +
  `layout.deck` + `options.combine_devices: true` path, which applies the
  offset once per *post-combine* logical device — baking it in per drawn
  primitive here first would double-count it across a series fold. When
  omitted, the correction is applied at extraction time exactly as before,
  byte-for-byte unchanged; and for a deck with no `fixed_offset_ohm`-opted-in
  resistor class (everything except sky130's `res_high_po` today) the flag is
  a no-op either way. See "Deferring the fixed resistor offset" below.
- `--abstract-cells` — optional, unset by default, repeatable. An `fnmatch`
  glob (e.g. `'sky130_fd_sc_hd__*'`) naming instantiated cell types to
  extract as opaque, pinned black boxes instead of flattening them to their
  own devices (issue #620); repeatable flags OR together. Pins are resolved
  per distinct cell type: from that type's own `metal_labels`/`well_label`/
  `poly_label` text drawn directly in its own definition when present, else
  from `--abstract-cell-lef`. A matched type with neither pin source is an
  application error. Everything not matched by a pattern extracts exactly as
  today. See "Cell-level (black-box + pins) abstraction" below.
- `--abstract-cell-lef` — optional, unset by default, repeatable. A LEF file
  (or a directory of `*.lef`/`*.tlef` files) to resolve pins from for an
  `--abstract-cells`-matched cell type that draws no in-cell pin label — the
  `MACRO`/`PIN`/`PORT` block whose name matches the cell type; first match
  across repeated flags wins. Has no effect (and is an application error) if
  given without `--abstract-cells`. See "Cell-level (black-box + pins)
  abstraction" below.
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
uses. One consequence (issue #1085): a `klt extract`-derived layout netlist
can never structurally match a **hierarchical** reference netlist (one leaf
`.subckt` plus N instance calls of it — the shape a macro built by tiling
one verified leaf cell naturally takes) in `klt lvs`, since the comparer
pairs circuits one-for-one and the flat layout side simply has no
subcircuit-call circuit to pair against the reference's — see
[`docs/cli/lvs.md`](lvs.md) → `options.flatten_reference` for the fix (an
in-process flatten of the *reference* netlist before comparing, so both
sides are flat) rather than a hierarchy-preserving extraction mode, which
this command does not have. Device recognition splits NMOS (`active - nwell`) from PMOS
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
`active`, `poly`, `nwell`, optional `tap` (plus the optional
`tap_nplus`/`tap_pplus` implant pair a deck with no distinct `tap` layer can
declare instead — issue #1084, see below), `contact`, an ordered `metals`
stack with matching `metal_labels`/`vias`, an optional `well_label`, and an
optional `dummy` marker layer for drawn dummy devices — see "Dummy devices:
the `dummy` marker layer") —
each field's exact layer numbers and provenance are documented in the deck
module's own docstring, verified against this repo's real corpus fixtures
(`tests/corpus/sky130/`, `tests/corpus/gf180mcu/`).

**NMOS/PMOS body resolution**, both documented in the deck modules:

- **NMOS body.** Neither curated deck draws a separate substrate/pwell
  layer. On **sky130**, `tap.drawing` is reused for both purposes: a shape
  drawn inside an `nwell` is a PMOS well tie (see below), a shape drawn
  outside every `nwell` is a genuine, drawable P-substrate tie — when a
  layout draws one and contacts it up to a named net, the NMOS body
  terminal (and the identically-modelled `bulk_to_substrate` resistor bulk
  and collector-less bipolar collector terminals) resolve to that real net
  (issue #490). Only a layout with **no** such ring falls back to a global
  net (`vsubs` by default) via KLayout's `connect_global`. **gf180mcu** has
  no distinct tap layer at all (`Comp` is shared with transistor active),
  but declares `tap_nplus`/`tap_pplus` (issue #1084, see below) so the same
  fallback/resolution split applies there too, derived rather than drawn.
- **PMOS body.** sky130's curated deck draws well taps on a *distinct*
  layer from transistor active (`tap.drawing`), so the well body net picks
  up its real name via that tap + an `nwell` pin label (verified against
  the sky130 corpus: the PMOS body of a real inverter cell extracts to the
  correct `VPB` pin). **gf180mcu**'s curated layer set has no distinct tap
  layer (`Comp` is shared with the transistor active layer) and no
  well-label layer, but (issue #1084) derives an equivalent tap region from
  its already-declared `Nplus`/`Pplus` implant layers: a layout that draws
  an `Nplus`-covered `Comp` shape inside the `Nwell`, contacted up to a
  named net, resolves the PMOS body to that real net the same way sky130's
  drawn tap does. A layout that draws **no** such tie still leaves the
  PMOS body terminal a floating, anonymous net, exactly as before #1084 —
  strictly additive, not a behaviour change for a tie-less layout. This is
  also a **simulation** caveat when it does happen, not only an
  LVS-comparison one: an anonymous body net has no DC bias path at all,
  which corrupts a direct resimulation of the extracted netlist — see
  "Parasitic (RC) extraction" → "Known gap: an anonymous PMOS body net has
  no DC bias path" below for the full consequence and how the JSON response
  surfaces it.

### Deriving a tap region with no dedicated tap layer (issue #1084)

Some PDK families (gf180mcu) draw a well/substrate tie as ordinary `active`
diffusion covered by an implant mask rather than on a dedicated tap layer —
exactly how the PDK's own official LVS deck itself recognises a tie with no
dedicated tap mask. `ExtractionDeck.tap_nplus`/`tap_pplus` let a deck declare
that pair of implant layers instead of (or in addition to leaving) `tap`
unset; `klt extract` then derives an equivalent `tap` region:

- a **well tie** is `tap_nplus`-covered diffusion *inside* the well (n+
  diffusion tied to the n-type well — opposite doping from an ordinary PMOS
  source/drain, which is `tap_pplus`-covered there, so the two can never
  collide);
- a **substrate tie** is `tap_pplus`-covered diffusion *outside* every well
  (p+ diffusion tied to the p-type substrate — opposite doping from an
  ordinary NMOS source/drain, which is `tap_nplus`-covered there).

The derived region feeds the exact same tap/well/substrate connectivity
machinery a directly-drawn `tap` layer already uses (issue #490) — a well
tie ties the PMOS body via `nwell`, a substrate tie ties the NMOS body via
the `substrate_net` global — so `klt lvs` against a reference netlist whose
bulk terminals name the real supply rails can reach a clean match on a
layout that draws such a tie, with no caller-side netlist rewriting. Either
field left unset simply contributes nothing to the derived region (a deck
may declare only one side); both default to `None`, so a deck that declares
neither derives no tap at all — byte-for-byte the same extraction as before
these fields existed. Ignored outright when `tap` itself is set: a
genuinely distinct drawn tap layer always wins.

Connecting a well region to *every* contact inside it (rather than only a
genuinely distinct tap region) is deliberately **not** done — the well is a
background region spanning the whole PMOS area, so a blanket rule like that
shorts every transistor terminal inside the well together. See
`ExtractionDeck`'s docstring in `src/klayout_tools/decks/__init__.py` for
the full reasoning.

### Per-isolated-region NMOS substrate scoping (issue #1128)

Before this feature, **every** recognised NMOS body (and any substrate-tie
tap geometry, drawn or derived) in a layout shared the *one* deck-wide
`substrate_net` global (`vsubs` by default) unconditionally — even when a
layout drew two physically separate, deep-nwell/isolated-p-well NMOS
domains intended to be tied to two different real nets. The PMOS side of
this problem does not exist: a PMOS's own body ("W") terminal is the drawn
`nwell` region itself, which is already naturally scoped per physically
disjoint well island; NMOS had no equivalent mechanism (see issue #1084's
`tap_nplus`/`tap_pplus` above, which fixes tap-layer *existence* for
gf180mcu but not this per-region-scoping gap).

`ExtractionDeck.substrate_isolation` is an optional isolation/deep-well
layer (gf180mcu declares `DNWELL`, 12/0). When set, an NMOS device's
recognised active-diffusion island — or a substrate-tie tap's slice of
geometry — that overlaps a connected component of this layer resolves to a
*per-island* synthesized identity (`f"{substrate_net}_iso{n}"`, `n`
assigned in deterministic bounding-box order) instead of the single
deck-wide `substrate_net` global. Geometry that does not overlap any
component of this layer at all keeps today's single shared identity,
matching real silicon (an un-isolated p-substrate genuinely is one
continuous node) — this is a per-*isolated-region* split, not a full
per-instance one. A real, drawn/derived substrate-tie tap inside one
isolated region absorbs that region's synthesized identity exactly the way
a tap absorbs the single global identity today (issue #490/#1084) — so two
distinct, correctly-tied ties on two DNWELL islands extract as two
genuinely distinct nets, letting `klt lvs` match a reference netlist that
names them separately.

`None` (the default — every deck as of this field's introduction until
gf180mcu's own module sets it) disables this scoping entirely: a deck that
declares no `substrate_isolation` extracts exactly as it did before this
field existed, and gf180mcu's own existing (non-isolated) corpus fixtures
are unaffected either way, since none of them draw `DNWELL`. Applies only
to a deck's *default* (non-`mos_flavours`) NMOS device recognition and to
the `tap`/derived-tap substrate-tie slice — a flavoured NMOS device
(`mos_flavours`), a `bulk_to_substrate` resistor's `W` terminal, or a
collector-less bipolar's collector, whose own geometry happens to sit
inside an isolated region, still resolves to the single global
`substrate_net` identity: a known, documented residual gap left for a
follow-up.

### Anonymous net numbering (`$N`) is NOT a stable cross-platform contract (issue #1063)

A net with no drawn label writes as `Net.expanded_name()`'s own
placeholder, `"$" + cluster_id` (e.g. `$171`) — a *device instance* name
built off it (e.g. `M$171`, or an unlabelled net's synthesized `Device`
name generally) keeps this bare spelling verbatim, but the written
`.spice` netlist's *node* syntax (an `M`/`X`/`R`/`C` card's own net
references, and the `.SUBCKT` pin list) backslash-escapes the leading `$`
(`\$171`, never bare `$171` — issue #1162, "Anonymous nets are
backslash-escaped" above) since ngspice treats a token that *starts with*
`$` as an inline-comment marker. The JSON `nets[].name` field (and
`devices[].nets[...]`, `parasitics.nets[].net`/`.hub_net`/
`.terminals[].leg_net`) carries this same escaped `\$171` node spelling,
not the bare instance-name form (`net_id` is the same underlying
`cluster_id`, as an integer — see "`nets[]` entries" below). `cluster_id`
is assigned once, internally, inside the native `l2n.extract_netlist()`
call — a single opaque KLayout/`kdb` (compiled C++) routine. This repo
only *reads* that string; it never assigns, sorts, or otherwise influences
it.

**This numbering is deterministic within one run and one environment
(re-running extraction on the same machine against the same pinned
`klayout` version reproduces the identical `$N` labels every time — see
`test_parasitics_deterministic_across_runs` in `tests/test_extract.py`),
but it is explicitly NOT a stable/guaranteed contract across different
`klayout` point releases or host platforms.** `pyproject.toml` pins only
`klayout>=0.30` (no upper bound), and KLayout documents no guarantee that
`cluster_id` assignment order survives a version bump or a differing
platform's native shape-clustering traversal. In practice, extracting the
byte-identical GDS with the byte-identical deck on two different
environments has been observed to swap which anonymous net gets which
number (e.g. `$171`/`$172` trading places) while every *named* net and
every device stays byte-identical.

**This does not affect `klt lvs`.** `klt lvs` compares with KLayout's
native `NetlistComparer`, a topological/graph-isomorphism comparator that
pairs nets and devices by connectivity structure, never by name or number
(`src/klayout_tools/lvs.py`'s `same_circuits()` call is deliberately
pinned away from `NetlistComparer`'s default name-based top-circuit
matching — see issue #231) — so an anonymous-net-label swap changes
nothing about a `klt lvs` match/mismatch verdict.

**What this means for callers**: do not diff two `klt extract` netlists
(or two JSON reports' `nets[]`/`devices[]`) *by raw text* across different
machines, CI runners, or resolved `klayout` pip versions and expect zero
diff — a byte-for-byte difference confined to swapped `$N`/`$M` anonymous
net labels, with every named net and device otherwise identical, is a
numbering artifact, not a connectivity regression. Compare with `klt lvs`
instead (see [`docs/cli/lvs.md`](lvs.md)) to verify connectivity
equivalence between two extractions — it is unaffected by this numbering
and gives a definitive match/mismatch verdict rather than a fragile text
diff. If a caller genuinely needs byte-for-byte netlist reproducibility
across environments, pin the exact resolved `klayout` version (reported in
the JSON `provenance.klayout_version` field — see "JSON schema (the
contract)" below) on every environment that produces or compares extracted
netlists.

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

### Cell-level (black-box + pins) abstraction (`--abstract-cells`, issue #620)

`black_box_regions` above excludes geometry by *region*, with no concept of a
pin — the excluded area's contents simply disappear from connectivity.
`--abstract-cells` is the complementary, **cell**-granular operation: every
*instantiated cell* whose name matches a given `fnmatch` glob pattern is
extracted as an opaque black box **with pins**, wired into the parent's net
graph by name — a hierarchical SPICE subcircuit, not a flattened pile of
devices. This is the mode a gate-level LVS needs: comparing an
OpenROAD-produced, placed-and-routed GDS against its synthesized gate-level
netlist requires extracting *at* the standard-cell boundary, not the
transistor level (see
[`2AMLogic/sky130-modexp#8`](https://github.com/2AMLogic/sky130-modexp/issues/8)).

**Additive, off by default.** `--abstract-cells` unset is byte-for-byte
identical to today's behaviour; only instantiated cells matching a given
pattern are affected, and everything else in the same layout — routing
metal, vias, fill, unmatched devices — extracts exactly as it does today,
in the same run.

**Pin resolution**, once per *distinct* matched cell type (cached across
every occurrence of that type):

1. **In-cell labels** (preferred). If the cell's own definition draws text
   directly on one of the deck's own label layers (`metal_labels[i]`,
   `well_label`, `poly_label`) — not promoted from a nested sub-cell, only
   text drawn in the cell itself — each distinct label names a pin, and its
   own footprint's centre is the pin's access point, probed on the specific
   conductor layer the label was drawn on.
2. **LEF fallback** (`--abstract-cell-lef`). When a matched cell type draws
   no such label, each `--abstract-cell-lef` path (a LEF file, or a
   directory of `*.lef`/`*.tlef` files) is searched for a `MACRO` block of
   the same name; that macro's `PIN`/`PORT` geometry supplies the pin names
   and access points (each port's bounding-box centre, in the macro's own
   local micrometre frame — the standard convention every real PDK
   standard-cell LEF follows, `ORIGIN 0 0` matching the cell's own drawn GDS
   origin). The LEF's own layer name is not translated to a GDS layer, so a
   LEF-resolved pin is probed against the deck's conductor stack bottom-up
   instead of one specific layer.
3. **Neither resolves.** A matched cell type with no in-cell label and no
   matching LEF macro is an application error naming the cell type — never a
   silently dropped pin or an unconnected instance.

**What gets erased vs. preserved inside an abstracted cell.** Every
device-recognition layer this deck's connectivity graph reads
(`ExtractionDeck.connectivity_layers` — MOS-recognition layers, and any
declared resistor/bipolar/diode marker or capacitor plate layer) is erased
from the matched cell type's own definition (and everything it in turn
instantiates) before extraction runs — so no transistor, drawn resistor,
bipolar, or MiM capacitor inside an abstracted cell is ever recognised as a
device. `contact`/`metals`/`vias` (routing/interconnect) and the label
layers are deliberately left intact: the parent's own routing lands on and
passes through an abstracted cell's own local interconnect mesh exactly as
drawn, which is what makes the pin access point reachable at all, and the
label layers are what pin resolution itself reads. A cell type matched by
the pattern is assumed to be used **exclusively** as a black box wherever it
is instantiated in the stream — erasure applies to every instance of that
cell type, not just the ones reachable from the extraction top cell.

**Known, documented gap**: a resistor or capacitor whose recognition layer
happens to be one of the deck's own `metals` (a real but rare deck
configuration — no PDK shipped in this repo does this) is not erased, since
that layer is routing. Neither `sky130` nor `gf180mcu` declares a
resistor/capacitor body on a `metals[]` layer today.

**Output.** Every distinct matched cell type becomes its own
`.SUBCKT <cell type> <pins...> ... .ENDS` block in the written SPICE (empty
body — a black box declares no devices), and every matched instance becomes
an `X<instance>` card in the parent's own `.SUBCKT` block, connected to the
same layout-derived net names the un-abstracted portion of the circuit
already uses. This falls directly out of KLayout's native netlist model
(`kdb.SubCircuit`/`kdb.NetlistSpiceWriter`) — every circuit, including the
flat top-level one, was already written as its own `.SUBCKT` block before
this feature existed, so hierarchy here is a purely additive extension of
the same writer, not a new SPICE-emission code path.

```
* cell TOP
.SUBCKT TOP IN NET1 OUT
* cell instance BUF_0 r0 *1 0,0
XBUF_0 NET1 OUT BUF
* cell instance BUF_1 r0 *1 0,0
XBUF_1 IN NET1 BUF
.ENDS TOP

* cell BUF
.SUBCKT BUF A Y
.ENDS BUF
```

The JSON response's `abstracted_cells` field (see "JSON schema" below)
reports one entry per distinct matched cell type: instance count, resolved
pin count, and how its pins were resolved
(`"in_cell_labels"` / `"lef_abstract"`, plus the specific LEF path for the
latter) — mirroring the audit-coverage style of `black_box_regions[]`/
`ignored_layers[]`. Always a list, empty unless `--abstract-cells` matched
at least one instantiated cell.

**A pin whose resolved access point lands on no conductor at all** (e.g. an
unrouted design, or a LEF-fallback coordinate outside the drawn footprint)
does not fail the run: that specific instance's pin gets a fresh,
unconnected net instead, and a `warnings[]` entry names the instance and
pin — the same "warn on a per-instance geometric miss, don't hard-fail the
whole extraction" convention this module already uses elsewhere (e.g.
`unbiased_pmos_body_nets`).

**A pin with several candidate access points** — a LEF `PIN` declaring more
than one disjoint `PORT` rectangle, a pin name labelled more than once, or a
label whose own metal fragment is tied to a second fragment only through the
cell's own (abstraction-erased) poly/diffusion — is probed at *every*
candidate, and the results are ranked. The ranking is deliberately narrow
(issue #1366):

- a **named** net beats an unnamed one, since by this point extraction (and
  `--def-net-names`, if given) has already named every externally-routed
  net, while an unrouted in-cell fragment is a fresh island that earns no
  name from either source;
- among unnamed candidates only, one carrying more **device terminals**
  wins — a fixed property of the drawn layout;
- **everything else ties**, and a tie is resolved in favour of the *earlier*
  candidate, which is always the pin's own primary access point (its drawn
  label, or its first-declared LEF `PORT`) rather than a fragment discovered
  by walking the cell's internal connectivity.

In particular, the ranking never counts how many abstracted-cell pins have
already been wired onto a candidate net. That count grows as the wiring pass
itself runs, so ranking on it made the outcome a function of the order pins
happened to be resolved in rather than of the drawn layout — a
rich-get-richer loop that, on a dense fully-routed block, bound ground-role
pins to the power net (`VDD` is wired before `VSS` on every instance, so the
power rail is always a pin ahead) and output pins to one of their own
instance's input nets (the input is wired first, for the same reason). Two
*named* candidates are likewise never ranked against each other by
connectivity: both are already real, externally-routed nets, and which one is
larger says nothing about which one this pin's geometry belongs to.

**Self-check: two declared pins on one net.** After wiring, any abstracted
instance that resolved two or more of its *separately declared* pins onto
the same net produces one aggregated `warnings[]` entry naming the instance,
the pins and the net (up to five instances spelled out, then a count). This
is a warning, never an error — a deliberately tied-off pin is legal — but on
a black box the caller has no interior to inspect and no devices to
cross-check against, so the condition is worth surfacing: a ground pin
resolved onto the power net and an output resolved onto one of its own
instance's inputs both show up as exactly this shape (issue #1366).

Expect this entry to appear — legitimately — on any library whose cells
declare separate body-tie pins that the layout ties to the rails, e.g.
sky130's `VPB`/`VPWR` and `VNB`/`VGND` pairs. That is the design's own
intent, not a fault; the entry is aggregated into a single `warnings[]`
string (with a count for the remainder) precisely so a whole-block flow that
trips it everywhere stays readable.

**Mirrored/rotated instances** resolve their pins correctly: each
occurrence's own instance transform (rotation, mirroring, array
displacement) is applied to the cell-local access point before probing, so
two differently-oriented instances of the same abstracted cell type wire up
to the correct parent nets independently.

**Scope note**: this mode emits a hierarchical **SPICE subcircuit**
netlist only. A gate-level **Verilog** netlist (module instantiations,
port-connected by name) is a deliberately deferred follow-up, tracked
separately — see issue #620's discussion for the rationale (this keeps the
initial delivery to a single bounded change, reusing KLayout's existing
`NetlistSpiceWriter` machinery rather than adding a new output-format code
path).

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

**Base-terminal net resolution — inherits the "PMOS body" limitation
above.** Because `BipolarDevice.base` reuses the deck's own `nwell` layer
(the very layer used for PMOS body recognition), the base terminal's net is
only as resolvable as that `nwell` node is — `extract.py` wires
`l2n.connect(bipolar_base, nwell)`, so the base inherits whatever name
`nwell` picks up. Whether that `nwell` node resolves through to
contact/metals therefore depends on the deck's tap mechanism (drawn `tap`,
or — issue #1084 — a derived one from `tap_nplus`/`tap_pplus`) and/or
`well_label` (`extract.py` only wires `nwell → tap → contact` when a tap
mechanism is declared, and names the node via `nwell → well_label`):

- **gf180mcu** declares no `well_label` and no drawn `tap`, but does declare
  `tap_nplus`/`tap_pplus` (issue #1084), so its `nwell` node — and hence its
  BJT base terminal — resolves through `nwell → tap (derived) → contact →
  metals` exactly when the layout draws an `Nplus`-covered `Comp` well tie
  inside that same `Nwell`, contacted up to a named net; absent such a tie
  it is still a floating, anonymous net. This is the **exact same
  condition** as the "PMOS body" limitation documented under "Coverage"
  above; the drawn BJT's base can no more be LVS'd or simulated against a
  schematic base net than a tie-less gf180mcu PMOS body can.
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
  - **sky130**: both plates are wired. `met3`/`met4` (the two stacks'
    `bottom_plate` layers) became two of this curated deck's own `metals`
    connectivity levels when `metals` grew from `li1`/`met1`/`met2` to the
    full `li1`-through-`met5` stack (issue #619, closing the gap that left
    `_ROUTING_LAYER_RANGE`'s promised `met1-met5` signal-routing range one
    level short of what this deck could actually merge), so each bottom
    plate ties straight into the real met3/met4 routing net around it — the
    same "bottom plate matches a tracked `metals` layer" mechanism
    gf180mcu's `Metal4` bottom plate already used. The real PDK's MiM stacks
    also have a landing via for the top plate (`sky130.lvs`'s `connect(capm,
    via3)` / `connect(capm2, via4)`); once #619 made `via3`/`via4`/`met4`/
    `met5` tracked `vias`/`metals` entries, issue #775 set
    `top_plate_via`/`top_plate_via_metal` on both curated entries
    (`via3`→`met4` for the met3/`capm` stack, `via4`→`met5` for the
    met4/`capm2` stack) — the same `top_plate_via`/`top_plate_via_metal`
    mechanism gf180mcu's stack already used. (A layer serving both a
    `metals` connectivity role and a device-recognition role simultaneously
    — as met3/met4 now do — is not a conflict; see "Device-recognition-only
    layers" below for the reporting distinction this dual role motivated.)

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
- **gf180mcu's stack recognizes only one density by default; all three
  selectable densities are now caller-selectable (issue #1151).** The
  DRM/LVS deck's MiM cap supports 1.0/1.5/2.0 fF/µm² dielectric-thickness
  options selected as a foundry-side runset option, not something a drawn
  layout's own geometry can distinguish — the three densities share
  **byte-identical drawn mask geometry** per metal pair (same `Metal4`
  bottom plate, same `FuseTop` top-plate inset, same `CAP_MK`/`MIM_L_MK`
  markers, same `Via4`/`Metal5` up-hop). Before issue #1151, a design
  drawing a DRM-legal MiM cap for the 1.0/1.5 fF/µm² options had no way to
  get an accurate extraction — worse, drawing that cap's top-plate via
  exactly per the PDK's own minimum-overlap rule (which requires the bottom
  plate to *enclose/overlap* the via, not clear it) is silently read as an
  **ordinary routing via**, merging the bottom-plate net and top-plate net
  into one false short in the extracted netlist, with no diagnostic — not
  merely "unrecognized," a wrong result that can still pass DRC. This deck
  still models only the LVS runset's own default (2.0 fF/µm²,
  `cap_mim_2f0_m4m5_noshield`) when `--deck-option mim_cap=...` is omitted —
  see `decks/gf180mcu.py`'s provenance note — but a design that has
  deliberately committed to the 1.0/1.5 fF/µm² option can now select it
  explicitly via `--deck-option mim_cap=cap_mim_1f0_m4m5_noshield` (or
  `...1f5...`), getting an accurately-valued extraction (right
  `area_cap_f_um2`/`perim_cap_f_um`, right reported device name) and no
  false short. See "Selecting a shared-geometry MiM capacitor density
  flavour" below.
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

### Device-recognition-only layers (issue #619)

`ignored_layers` (see "JSON schema" below) only distinguishes "shape-bearing
layer this deck reads" from "shape-bearing layer this deck never reads at
all" — it cannot tell "read as a `metals`/`vias` connectivity level" from
"read only for a `bipolars`/`capacitors`/`resistors`/`diodes`
device-recognition role." That ambiguity let a real routing-connectivity gap
hide behind a clean-looking `ignored_layers` report: sky130's `met3.drawing`/
`met4.drawing` were already read as `capacitors[].bottom_plate` (see above)
well before they became `metals` connectivity levels, so a layout with two
nets joined only through a met3/met4 segment extracted as two disconnected
nets with `ignored_layers: []` — nothing in the response said why, because
met3/met4 genuinely were not "ignored."

`device_recognition_only_layers` (see "JSON schema" below) closes that gap:
it lists every shape-bearing layer the deck reads for device recognition
that is **not** also one of its `metals`/`vias` connectivity levels *and*
**not** one of the deck's own MOS-core layers (`active`/`poly`/`nwell`/
`contact`, plus optionals) — the "read but not merged" counterpart to
`ignored_layers`'s "never read at all." A layer can legitimately appear in
*neither* list (a `metals`/`vias` level that also happens to carry a
device-recognition role, e.g. sky130's met3/met4 after issue #619's `metals`
extension — see "MiM capacitor device recognition" above) — that dual role
is not itself a problem.

Unlike `ignored_layers`, a non-empty `device_recognition_only_layers` does
**not** append to `warnings[]`: a deck's own marker/mask geometry (a drawn
resistor's ID layer, a bipolar's marker, a MiM cap's top-plate mark) is
expected to be device-recognition-only by PDK design — it was never a
candidate connectivity level, so its presence is routine, not a
correctness gap. Warning on every occurrence would make `warnings[]` fire
on nearly any layout that uses one of these device classes, defeating the
signal-to-noise this field is meant to provide. The MOS-core exclusion
exists for the same low-noise reason: sky130's vertical-PNP bipolar reuses
the deck's own `nwell`/`diff` layers as its `base`/`emitter` (see "Bipolar
(BJT) device recognition" above), so without excluding MOS-core layers,
*any* ordinary PMOS/NMOS layout — with no bipolar device drawn at all —
would populate this field purely from that coincidental layer-number
overlap.

### Drawn resistors

Both decks additionally recognise **drawn precision-resistor device
classes** (issue #222, extended to cover each deck's other selectable
sheet-rho flavour by issue #299, and to make gf180mcu's shared-geometry
flavours caller-selectable via `--deck-option` by issue #595). A drawn
resistor is a deliberately-marked
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
| `gf180mcu` | `ppolyf_u_1k`/`_2k`/`_3k` | `gf180mcu_fd_pr__ppolyf_u_{1k,2k,3k}` (`POLY_RES` default `1k`, caller-selectable via `--deck-option poly_res=`) | `Poly2` 30/0 | `RES_MK` 110/5 | `SAB` 49/0, `Resistor` 62/0 | **1000/2000/3000 Ω/□** | — |

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
resistance, not just `devices[].params.r_ohm`. (`klt lvs`'s
`options.combine_devices` is the one exception: when set, it defers this
correction until after combining series-connected primitives, so a folded
logical device gets the fixed offset once rather than once per primitive —
see `docs/cli/lvs.md`'s `options.combine_devices` entry, issue #559. `klt
extract` itself — no combine step involved — applies the correction unless
`--defer-resistor-fixed-offset` asks it not to; see "Deferring the fixed
resistor offset" below.)

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
  selects one of `ppolyf_u_1k`/`_2k`/`_3k` — all three share *identical*
  drawn geometry, disambiguated only by the upstream PDK's build-time
  `POLY_RES` option, which `--deck-option poly_res=<value>` now lets a
  caller select explicitly (issue #595; `1k`, the PDK's own default, when
  the flag is omitted) — see "Selecting a shared-geometry resistor flavour"
  below. gf180mcu's `SAB` is *required* on both `ppolyf_u` entries (without
  it the real device is the ~48× lower-resistance salicided `ppolyf_s`). A
  wrong resistance passing LVS with high confidence is worse than a
  known-unmodelled short.
- **A "deck-coverage gap" warns differently from unmarked geometry.** A
  segment that carries a resistor-marker layer this deck knows about, but
  whose `requires`/`excludes` conditions it does not satisfy (e.g. a
  gf180mcu poly segment marked `RES_MK` without the `Pplus`/`SAB` combination
  either wired flavour above needs), still extracts as an unintended short
  — but is flagged by a **different** `warnings[]` string than fully
  unmarked geometry; see "Known limitation: unmodelled device geometry"
  below.

#### Deferring the fixed resistor offset (`--defer-resistor-fixed-offset`, issue #588)

The `fixed_offset_ohm` term is a **per-logical-device** head/end effect, not a
per-square one: a resistor drawn as N series-connected primitives has one pair
of heads, not N. `klt extract` cannot know which drawn primitives a downstream
consumer will fold together, so by default it applies the correction where it
can see it — once per drawn primitive, at extraction time.

`klt lvs`'s `options.combine_devices: true` is the consumer that *does* know:
it folds the series chain and must add the offset exactly once afterwards. For
the **inline** shape (`layout.file` + `layout.deck`) it handles both halves
itself — it extracts with the correction deferred internally and re-applies it
post-combine. For the **pre-extracted** shape (`layout.netlist` +
`layout.deck` + `combine_devices: true`, issue #585) it can only do the second
half; the netlist it is handed must already have been written with the
correction deferred, or the two additions double-count (the per-primitive
offsets are summed by the fold and cannot be un-summed afterwards).

`--defer-resistor-fixed-offset` is the extraction-time half of that contract,
for a flow that drives `klt` as a subprocess and never imports the package (it
is exactly `run_extract(..., apply_resistor_fixed_offset=False)`):

```sh
# Write a body-R-only netlist for klt lvs's post-combine correction.
klt extract ladder.gds --deck sky130 --defer-resistor-fixed-offset -o ladder.spice

# ... then compare it with the correction applied once per folded device:
#   {"layout": {"netlist": "ladder.spice", "deck": "sky130", "top": "RES"},
#    "reference": {...}, "options": {"combine_devices": true}}
klt lvs request.json --format json
```

Rules of thumb:

- Pair `--defer-resistor-fixed-offset` with `combine_devices: true` **and** a
  `layout.deck`. Deferring without a `layout.deck` (the bare
  `{"netlist": ..., "top": ...}` shape, which attempts no correction at all)
  leaves the offset missing entirely.
- Do **not** defer for any other consumer. A netlist headed for `klt sim`, or
  for `klt lvs` without `combine_devices`, wants the corrected `R` — that is
  the default, so simply omit the flag.
- The flag changes only `R` on `fixed_offset_ohm`-opted-in resistor classes
  (today: sky130's `res_high_po`). Every other device, net, pin, and warning
  in the report — and every byte of the written netlist outside those `R`
  values — is identical either way; on a layout with no such resistors, the
  two outputs are byte-for-byte equal.

#### Selecting a shared-geometry resistor flavour (`--deck-option`, issue #595)

Some drawn-resistor families have two or more sheet-rho **flavours that share
identical recognition geometry** — the same body/marker/`requires`/`excludes`
region — disambiguated only by a build-time variable in the *official* PDK
LVS deck this tool's deck is transcribed from, not by any drawn layer. There
is nothing a curated deck could key off to tell them apart on its own, so it
recognises exactly one flavour by default (the PDK's own default) and leaves
the others unmodelled — a design actually built against a different flavour
of that same geometry previously had **no way to select it**: its resistor
still extracted, but as the wrong device (a 2x/3x-off resistance), or (if the
default entry were narrowed away for any reason) as an unmodelled short.

`--deck-option <key>=<value>` (repeatable) picks the flavour explicitly for
this run. Today's resistor case is gf180mcu's `poly_res`, the curated-deck
counterpart of the upstream `POLY_RES` deck variable cited in
`decks/gf180mcu.py`'s own `ppolyf_u_1k` provenance note (a second,
capacitor-shaped case, `mim_cap`, is documented separately below):

| Deck | Key | Values | Selects |
| ---- | --- | ------ | ------- |
| `gf180mcu` | `poly_res` | `1k` (default), `2k`, `3k` | Which of `ppolyf_u_1k`/`ppolyf_u_2k`/`ppolyf_u_3k` (1000/2000/3000 Ω/□) a `Resistor` (62/0)-marked poly segment extracts as. |

```sh
# A design drawn against gf180mcu's 2000-ohm/sq POLY_RES='2k' flavour:
klt extract cell.gds --deck gf180mcu --deck-option poly_res=2k -o cell.spice --format json
```

Rules of thumb:

- Selecting the flavour that already matches the deck's default (`poly_res=1k`
  today) is byte-for-byte identical to omitting `--deck-option` entirely,
  other than the additive `provenance.deck.options` echo below.
- An unrecognised key (one no resistor entry in this deck declares) or an
  unrecognised value (not one of the entry's declared flavours) is an
  application error (exit 1), never a silently-kept default and never a
  guessed resistance — the same "known-unmodelled short beats a silently
  wrong value" discipline the deck's own `requires`/`excludes` narrowing
  already applies.
- The resolved mapping is echoed verbatim as `provenance.deck.options` in the
  JSON response (present only when `--deck-option` was given), so a record
  can pin exactly which flavour a run selected alongside the deck's own
  `content_hash`.
- This changes only *which* device class/sheet-rho a matched resistor segment
  is reported as — never *whether* a segment is recognised, and never how
  many devices a drawn segment produces (still exactly one, whichever flavour
  is selected).
- Combining `--deck-option` with `--pdk` binds the *selected* flavour's own
  simulation subcircuit (`X … ppolyf_u_2k r_length=… r_width=…`), not the
  default one and not a bare `R` card — every value the key accepts has a
  curated model-binding entry. See "SPICE model binding" below.
- `klt lvs` accepts the same override as `request.layout.deck_options` (issue
  #600) — the JSON-request-document counterpart of this flag, since `klt lvs`
  takes a request document rather than per-flag CLI args. A 2k/3k design's
  layout-side extraction is resolved with the matching flavour instead of
  silently falling back to the deck's default. See
  [`docs/cli/lvs.md`](lvs.md)'s `layout.deck_options` field.

#### Selecting a shared-geometry MiM capacitor density flavour (`--deck-option`, issue #1151)

gf180mcu's MiM cap has the same "one drawn geometry, several PDK-offered
electrical variants" shape as the resistor flavours above, one level up in
consequence: the 1.0/1.5/2.0 fF/µm² density options share **byte-identical
drawn mask geometry** on the same `Metal4`/`FuseTop`/`Metal5` stack (see "MiM
capacitor device recognition" above), disambiguated only by the upstream PDK
LVS deck's build-time `MIM_CAP` variable, not by any drawn layer. Before this
option existed, a design drawn against the 1.0/1.5 fF/µm² options had no way
to select it: the cap still extracted as `cap_mim_2f0_m4m5_noshield`
regardless, and — because the density options' `Via4` top-plate-via geometry
is DRM-legal for all three, not just the recognized one — a correctly-drawn
1.0/1.5 fF/µm² cap's top-plate via is silently read by the deck's generic
per-layer connectivity loop as an ordinary routing via, **shorting the
bottom-plate and top-plate nets together** in the extracted netlist with no
diagnostic — the top-plate-via/`vias[]`-overlap exclusion issue #364 gives
the *recognized* density (see "MiM capacitor device recognition" above)
never fired for a density this deck could not yet recognize as a capacitor
at all.

| Deck | Key | Values | Selects |
| ---- | --- | ------ | ------- |
| `gf180mcu` | `mim_cap` | `cap_mim_1f0_m4m5_noshield`, `cap_mim_1f5_m4m5_noshield`, `cap_mim_2f0_m4m5_noshield` (default) | Which of the three densities (1.0/1.5/2.0 fF/µm² area, plus the matching perimeter/fringe coefficient) a marked `FuseTop`-over-`Metal4` MiM cap extracts as. |

```sh
# A design drawn against gf180mcu's 1.0 fF/um^2 MIM_CAP='1' flavour:
klt extract cell.gds --deck gf180mcu --deck-option mim_cap=cap_mim_1f0_m4m5_noshield -o cell.spice --format json
```

Rules of thumb:

- Unlike `poly_res`'s short `1k`/`2k`/`3k` spelling, `mim_cap`'s values are
  the PDK's own full LVS device-class names (there is no equally short
  upstream token for a density option to mirror) — `cap_mim_1f0_m4m5_noshield`,
  `cap_mim_1f5_m4m5_noshield`, `cap_mim_2f0_m4m5_noshield`.
- Selecting the flavour that already matches the deck's default
  (`mim_cap=cap_mim_2f0_m4m5_noshield` today) is byte-for-byte identical to
  omitting `--deck-option` entirely, other than the additive
  `provenance.deck.options` echo below.
- An unrecognised key or an unrecognised value is an application error (exit
  1), never a silently-kept default and never a guessed capacitance — the
  same discipline `poly_res` above already applies. A `deck_options` mapping
  that names both `poly_res` and `mim_cap` together resolves both
  independently in the same call.
- The resolved mapping is echoed verbatim as `provenance.deck.options` in the
  JSON response, exactly as for `poly_res` above.
- This changes only *which* device class/area-and-perimeter coefficients a
  matched MiM overlap is reported as — never *whether* it is recognised, and
  never how many devices a drawn overlap produces (still exactly one,
  whichever flavour is selected).
- **Unlike `poly_res`, combining `--deck-option mim_cap=...` with `--pdk`
  does not yet bind a curated simulation subcircuit for the two
  non-default flavours** (`cap_mim_1f0_m4m5_noshield`/
  `cap_mim_1f5_m4m5_noshield` have no `_CAPACITOR_MODEL_TABLE` entry as of
  this option's introduction) — the extracted `C` card is written unbound
  (the same documented carve-out an unbound device class anywhere else in
  this deck gets, e.g. gf180mcu's `bjt`), not a wrong subcircuit call. See
  "SPICE model binding" below.

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
- **The substrate-side anode inherits the "NMOS body" resolution above.**
  gf180mcu draws no p-substrate mask, so `diode_nd2ps_06v0`'s anode is tied
  to the deck's `substrate_net` global (`vsubs` by default) — the same
  wiring the NMOS body and the collector-less bipolar collector already use.
  As with the NMOS body, a layout that *does* draw a substrate tie the
  deck's tap derivation claims (a `Pplus`-covered `Comp` shape outside every
  `Nwell`, contacted up to a named net — issue #1084) resolves the anode to
  that real, drawn net instead: `connect_global` merges the tie into the
  same node and KLayout names the result from the drawn label.
- **A drawn tie the tap derivation does *not* claim is now reported, not
  silently dropped (issue #1196).** When contacted, labelled geometry sits
  inside the diode's own device-mark footprint — physically the same region
  the substrate-formed terminal is derived from — but is not covered by the
  deck's tap mechanism (e.g. the tie carries no `Pplus` implant), nothing
  joins the two: the terminal keeps the synthesized `substrate_net` name
  while the drawn net exists beside it in the netlist. `warnings[]` now
  carries one aggregate entry per (diode class, terminal) naming the device
  class, the terminal, the drawn label(s) that did **not** name it, and the
  synthesized net substituted for them, e.g.:

  ```
  1 diode_nd2ps_06v0 'a' terminal(s) resolved to the deck-synthesized
  'vsubs' substrate net, but drawn, labelled tie geometry inside the same
  device footprint resolves to a different net (VSS) -- ...
  ```

  Connectivity is unchanged (this is a disclosure, not a re-wiring). No
  warning fires for the common case of a diode with no drawn tie at all —
  landing on the synthesized global is the documented fallback there — nor
  when the tie is unlabelled, nor when its label happens to be spelled
  exactly like the deck's own `substrate_net`.
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

**Remaining known limitation: ordinary poly routing (issue #324, escape hatch
added in #1425).** The heuristic still has no *geometric* signal
distinguishing a resistor-shaped **routing track** — most commonly a
**deliberate poly underpass**: a poly strip contacted to metal at each end,
used to route one net beneath another on a deck with too few metal levels to
stay planar — from an actual unmodelled resistor body: on a layout whose
signal routing is deliberately drawn on `poly` (e.g. a one-metal-level analog
cell), every track contacted at both ends and touching no gate trips the same
signature as a real missing device, and the false-positive count can dominate
the true-positive count. A caller who can mark the intentional shapes in the
GDS itself should use the `poly_interconnect` deck field below rather than
filter the JSON client-side — it is precise (per-shape) and travels with the
layout. Tightening the *heuristic itself* (e.g. an aspect-ratio floor, or a
"no more than 2 contact clusters" ceiling to separate a two-terminal body from
a track that fans out to every device on its net) remains deliberately
deferred pending validation against more than one deck/layout, and would
silently change reports for every caller, marked or not — the interim
workaround for a shape that is *not* (or cannot yet be) annotated is still
client-side: use `unmodelled_poly[]`'s bounding boxes to filter out shapes
already known to be routing (by inspection, by contact-cluster count, or by
aspect ratio) and assert only on what remains, or track the *change* in the
filtered count across revisions rather than gating on its absolute value.

### Declaring intentional poly interconnect: the `poly_interconnect` marker layer (issue #1425)

A deliberate **poly underpass** — a poly strip contacted to metal at each end,
carrying one net beneath another where the deck has too few metal levels to
stay planar — matches the unmodelled-device signature above exactly (no MOS
gate, contact at 2+ separate points, no resistor marker), even though it is
correct, intentional geometry and the extracted netlist is already right.
Before this field there was no way to tell `klt extract` that a given `poly`
shape is intentional interconnect rather than a missing device: the report
carried a `warnings[]` entry and an `unmodelled_poly[]` record for correct
geometry, and a genuine unmodelled resistor body elsewhere in the same cell
was harder to spot in the same list.

A deck may declare an optional `poly_interconnect` marker layer (a `(layer,
datatype)` pair, like `dummy` above — any layer the deck's PDK does not
otherwise use for connectivity; it never participates in ordinary net
extraction, only in this exclusion check). A `poly` connected component that
overlaps `poly_interconnect` **anywhere** is excluded from the
unmodelled-device diagnostic entirely: it is never counted, never appears in
`unmodelled_poly[]`, and never contributes to `warnings[]`. This is a
per-shape, whole-component exclusion — the same "any overlap counts" idiom
this deck already uses for a recognised MOS gate or resistor body — not a
narrowing of the geometric signature itself, so it never affects a shape the
caller has not annotated. The strip's own extraction is unaffected either
way: it was already correct ordinary poly interconnect before this field
existed; declaring the marker only silences the diagnostic for it.

`poly_interconnect` is opt-in and additive, `None` by default: no shipped
deck (sky130, gf180mcu, sg13g2, sg13cmos5l) declares it as of this field's
introduction, so every deck extracts exactly as it did before the field
existed unless a caller configures it (e.g. via a project-local deck
override). **A shape that goes unannotated still lands in
`unmodelled_poly[]`** — including a genuine poly underpass in a layout whose
deck has not declared this marker, or whose caller has not drawn it — so a
non-empty `unmodelled_poly[]` is not on its own proof of a defect; a caller
reading the report should check whether each flagged shape is a deliberate,
unannotated underpass before assuming it is a missing device.

### Merged net labels (issue #470)

Two *different* net labels can land on the same electrical net — for
example, a `klt gen-compose` `pins[]` entry names a port that other
connectivity (drawn metal, or a second `pins[]`/`connectivity[]` entry)
already reaches, silently renaming the node that owns the pad rather than
naming the caller's intended net. KLayout does not treat this as an error:
`LayoutToNetlist` simply joins every distinct label text found on one net
into its `Net.expanded_name()` — two labels `Y` and `OUT` shorted together
come back as one joined net name (three or more labels join the same way).

**One spelling everywhere (issue #696).** KLayout's own `Net.expanded_name()`
joins labels with a comma (`Y,OUT`) — but a SPICE node token cannot contain
a comma, so KLayout's `NetlistSpiceWriter` writes the *same* net using `|`
instead (`Y|OUT`) wherever it appears as an actual node reference in the
written netlist (its `.SUBCKT` pin list and every instance line that
connects to it — only its leading `* pin ...` comment keeps the raw,
comma-joined form). `klt extract` renders every net name it reports —
`nets[].name`, `devices[].nets[...]`, `merged_net_labels[].net`, and
`parasitics.nets[].net` — using that same `|`-joined spelling, so a merged
net's name is byte-identical across the written netlist and every JSON field
that names it; `klt lvs`'s `net_correspondence[]` and `mismatches[].net`
follow the same convention (see `docs/cli/lvs.md`). Before this, the JSON
carried the raw, un-escaped comma spelling while the netlist carried the
escaped pipe spelling — the same net, spelled two different ways depending
on which artifact you read it from, with no documented way to join them by
name short of hard-coding the `,` -> `|` substitution yourself.

`klt extract` detects a merge heuristically: any net whose (already
`|`-joined) name splits into 2+ parts on `|` is treated as a label
collision. Each match produces two things:

- A structured entry in the response's `merged_net_labels[]` array (see "JSON
  schema" above): `{ "net": "<full joined name>", "labels": ["Y", "OUT"] }`
  — `net` uses the same `|`-joined spelling as `nets[].name` and the written
  netlist, so it is a usable key into the netlist rather than a separately
  spelled alias of it; `labels` is the joined name pre-split, so a consumer
  does not have to re-derive the label list by re-implementing the `|`-scan
  against `nets[].name` itself.
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
*why* a net carries a multi-label name, only the final joined string. A
label that legitimately contains a literal `|` in its own text (unusual, but
not disallowed by any layer's text-shape format) is indistinguishable from a
genuine two-label collision by this heuristic — it will be reported as a
false-positive entry in both `merged_net_labels[]` and `warnings[]`. There is
no server-side fix for this today: a caller that intentionally uses
`|`-containing label text should expect (and can safely ignore) a
`merged_net_labels[]` entry whose `labels` do not actually correspond to
independent naming intents.

### Voltage-domain markers (issue #552) and per-flavour MOS binding (issue #1111)

Some PDKs draw **two gate-oxide/voltage domains** on the same wafer, selected
by a marker layer — e.g. gf180mcu's `Dualgate` (55/0) selects its 5V/6V
thick-oxide domain, whose DRM publishes a distinct set of MOS models with
materially different characteristics from the default (thin-oxide) ones.
This curated deck derives MOS flavour from the well layer alone
(`nfet_active = active - nwell`) plus, as of issue #1111, any additional
marker declared by the deck's `ExtractionDeck.mos_flavours` field: a
transistor whose active-diffusion geometry overlaps such a marker at all
(the whole island, source *and* drain — not split mid-device even when the
marker only partially covers it, since the DRM does not contemplate a
transistor legally straddling a voltage-domain boundary) is claimed entirely
by that flavour. gf180mcu declares one such entry keyed on `Dualgate`, so a
transistor drawn (fully or partially) inside it now extracts bound to
`nfet_06v0`/`pfet_06v0` under `--pdk`, instead of the default
`nfet_03v3`/`pfet_03v3` — the structural `devices[].class`/`device_counts`
labels are unaffected either way (every MOS device, flavoured or not, still
reports the deck's ordinary `"nfet"`/`"pfet"` class; only the *bound SPICE
model name* differs).

`sg13g2` declares one such entry too (issue #1231), keyed on its
`ThickGateOx` (44/0) marker: a transistor drawn inside it binds the real
thick-oxide `sg13_hv_nmos`/`sg13_hv_pmos` models under `--pdk` instead of
the default `sg13_lv_nmos`/`sg13_lv_pmos` — the split the PDK's own
`general_derivations.lvs` makes (`ngate_hv_base = ngate.and(thickgateox_drw)`
vs. `ngate_lv_base = ngate.not(thickgateox_drw)`). `sky130` declares one too
(issue #1369), keyed on its `hvi` (75/20) marker: a transistor drawn inside
it binds the real 5V-gate/10.5V-drain `sky130_fd_pr__nfet_g5v0d10v5`/
`sky130_fd_pr__pfet_g5v0d10v5` models under `--pdk` instead of the default
`sky130_fd_pr__nfet_01v8`/`sky130_fd_pr__pfet_01v8` — the split the PDK's own
`sky130.lvs` makes (`ngate_high_voltage = ngate.and(hvi)...` vs.
`ngate_low_voltage = ngate.not(hvi)...`). A deck-registered voltage-domain
marker with **no** matching `mos_flavours` entry still only produces the
diagnostic below, never a corrected binding:

- A structured entry in the response's `voltage_domain_warnings[]` array
  (see "JSON schema" above): `{ "marker": "44/0", "description": str }` —
  the same registry entry (and description text) `klt drc`'s
  `coverage.voltage_domain_warnings` surfaces for the same deck, so the
  wording matches across both commands for the same layout. Because the
  description is shared, it also names what the *DRC* side of the same deck
  does and does not model (as of issue #1110 gf180mcu's `DF.1a`/`DF.3a`
  `Comp` width/space rules are `Dualgate`-scoped, see
  [`klt drc`](drc.md#voltage-domain-rule-pairs-_lv_mv-issue-1110)); none of
  that changes extraction, which still ignores the marker entirely.
- A matching prose entry in `warnings[]`.

**What this field does and does not guarantee**: for a marker with no
`mos_flavours` coverage, it only flags that the bound model name may be
wrong — it does not correct the binding. For a marker `mos_flavours` *does*
cover (gf180mcu's `Dualgate` as of issue #1111, sg13g2's `ThickGateOx` as of
issue #1231, sky130's `hvi` as of issue #1369), the binding gap this field
exists to flag is closed, so `voltage_domain_warnings` no longer fires for
MOS device geometry inside it at all — the registry entry itself is
unaffected (gf180mcu's `Dualgate` entry stays registered for the DRC-rule
residue `klt drc`'s own coverage still tracks — `DF.6`, `PL.5a`/`PL.5b`, not
transcribed by that curated deck yet; sg13g2's `ThickGateOx` entry likewise
stays registered for its own DRC-rule residue, e.g. the `Gat.a1`/`Gat.a2`
channel-length-specific GatPoly widths; sky130's `hvi` entry stays
registered for the largest residue of the three — *no* rule in that curated
deck reads `hvi`, so e.g. its `diff.width.1` rule checks `difftap.1`'s
0.15 µm against geometry the real DRM's medium-voltage column
(`difftap.14`, 0.29 µm) governs, see `UNMODELED_VOLTAGE_MARKERS`'s own
description in `decks/sky130.py`).

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

## Device rule provenance (issues #868, #867)

`klt drc`'s deck rules gained a machine-readable, per-rule provenance
citation in issue #747 (see [`docs/cli/drc.md`](drc.md)'s "Rule provenance
and the golden-pair manifest") — Epic #711's Phase 1. This is the same
mechanism's extension to the LVS/device-recognition side (Phase 2a):
[`RuleProvenance`](../../src/klayout_tools/decks/__init__.py) — the exact
type `DrcRule.provenance` uses — is now also a field on
[`ResistorDevice`](../../src/klayout_tools/decks/__init__.py),
[`CapacitorDevice`](../../src/klayout_tools/decks/__init__.py),
[`BipolarDevice`](../../src/klayout_tools/decks/__init__.py), and
[`DiodeDevice`](../../src/klayout_tools/decks/__init__.py), and MOS
recognition gets its own `ExtractionDeck.nfet_provenance`/
`pfet_provenance` pair (MOS has no per-entry list the way
resistor/capacitor/bipolar/diode do — a deck declares exactly one NMOS and
one PMOS recognition rule via its own `active`/`poly`/`nwell` fields, so the
two citations live directly on the deck).

For a device-recognition entry, `source_path`/`rule_id` typically cite the
PDK's **KLayout LVS deck** (a different upstream file than the DRC-side
`.lydrc`/`.drc` script `DrcRule.provenance` cites — sky130's `sky130.lvs`,
from a *different* upstream repo, `efabless/sky130_klayout_pdk`, than the
`fossi-foundation/open-pdks` mono-repo the DRC rules above come from) and
its official device-class name (e.g. `"sky130_fd_pr__nfet_01v8"`,
`"sky130_fd_pr__res_generic_po"`, `"sky130_fd_pr__model__cap_mim"`) as
`rule_id` — the LVS analogue of a DRC rule id, naming *which specific
device* an entry's geometry/coefficients were transcribed from.

`provenance` is populated for sky130's curated MOS (`nfet`/`pfet`), all
three resistor entries, both capacitor entries, and the one bipolar entry —
8 device rules total — see `decks/sky130.py`'s `EXTRACTION_DECK` for the
concrete citations. **Issue #904 (Epic #711 Phase 3a) backfills the
gf180mcu counterpart**: MOS (`nfet`/`pfet`), both resistor entries, the one
capacitor entry, the one bipolar entry, and both diode entries — 8 device
rules total, mirroring sky130's own count — see `decks/gf180mcu.py`'s
`EXTRACTION_DECK` for the concrete citations. Unlike sky130's citations
(which name an official upstream device-class name for every entry), the
generic `bjt` bipolar entry has no positively-identified official LVS
device-class name upstream (see `decks/gf180mcu.py`'s own docstring note),
so its `provenance` instead cites the DRM rule (`BJT.3`) that defines the
`DRC_BJT` marker geometry it recognises on. Like `DrcRule.provenance`, this
field is not (yet) surfaced in `klt extract`'s JSON output; it is queryable
only by a caller that imports `klayout_tools.decks` directly (e.g. a
coverage-audit script).

Issue #868 (Phase 2a, the rule-model pilot) validated one entry per named
device *class* against a golden layout→netlist pair (MOSFET: `nfet` only;
resistor: `res_generic_po` only; capacitor: `sky130_fd_pr__model__cap_mim`
only). Issue #867 (Phase 2b) extends this to **every** provenance-cited
device rule — `pfet`, `res_high_po`, `res_xhigh_po`,
`sky130_fd_pr__model__cap_mim_m4`, and the `pnp` bipolar, on top of the
three #868 already covered — mirroring Phase 1's width/spacing golden-pair
discipline applied to the LVS side: each device rule ships a minimal, hand-
computed synthetic layout, run through `run_extract`, asserting the
extracted netlist's device parameters exactly match a value computed
independently from the deck's own provenance-cited coefficient (or, for the
bipolar — whose citation is a fixed device-class-name selection with no
per-rule numeric coefficient the way a resistor's `sheet_rho_ohm_sq` or a
capacitor's `area_cap_f_um2` is — the correct device class and net
resolution). See `tests/test_lvs_device_provenance.py`, whose own
`test_golden_pairs_cover_every_provenanced_device_rule` (parametrized per
deck) enforces this 1:1 coverage: it fails if a future provenance-backfilled
device rule ships without a matching golden pair, mirroring
`tests/golden_deck/`'s own coverage test for `klt drc`'s rules. **Issue #904
(Epic #711 Phase 3a) extends this same golden-pair discipline to all eight
of gf180mcu's provenance-backfilled device rules** (MOS x2, resistors x2,
the one capacitor, the one bipolar, diodes x2), in the same test module.
Epic #711's Phase 2c (cross-checking the compiled sky130 LVS device rules
against the hand-written deck on the #520 corpus) is the next section;
Phase 3a's own gf180mcu counterpart follows it.

## sky130 native-deck (`sky130.lvs`) LVS device-extraction cross-check (issue #869)

Phase 1 (issue #747) resolved `klt drc`'s "cross-check against the
hand-written deck" not by hand-authoring a second Python DRC deck, but by
running KLayout's own native rule-deck runner (`run_drc_klayout_engine`,
`--engine klayout`, issue #565) against the real upstream `sky130A.lydrc`
script and diffing its violations against the compiled `DrcRule` deck's own
results on the same golden layouts — see [`docs/cli/drc.md`](drc.md) →
"Engine" → `"klayout"`. This section is the LVS-device-extraction
counterpart: `klayout_tools.extract.run_extract_klayout_engine` (a Python
helper, not yet a `klt extract --engine` CLI flag — see "Scope" below) drives
`sky130.lvs` — sky130's single, directly-runnable native LVS/device-
extraction deck, resolved via `klayout_tools.pdk.lvs_deck_file` from the
same `libs.tech/klayout/lvs/` directory `klt pdk find` already discovers —
through the standalone `klayout` application binary and reads back the
device netlist it extracts, in a shape close enough to `run_extract`'s own
`devices[]`/`device_counts` to compare value-for-value.

**A native `.lvs` script is written for a complete LVS flow** (extract, then
`compare` against a reference schematic) and hard-requires a schematic to
exist at all — `run_extract_klayout_engine` always supplies a trivial
synthesized empty-`.SUBCKT` stub purely to satisfy that requirement; the
compare verdict itself is discarded (it is expected to report a mismatch
against the empty stub on every real fixture — that is not a failure, since
there is no trustworthy independent reference schematic to compare against
here; only the extracted netlist is used). Two `-rd` globals
(`net_only=true`, `top_lvl_pins=true`) disable the script's own default
`netlist.simplify` pass, which otherwise silently drops every extracted
device before the netlist is even written. Extracted parameter values are
corrected for a verified, fixed unit round-trip `kdb.NetlistSpiceReader()`
applies when re-reading `sky130.lvs`'s own SPICE writer output (length-typed
params `L`/`W`/`P`/`PS`/`PD` come back scaled ×1e6, area-typed `A`/`AS`/`AD`
×1e12) — see `run_extract_klayout_engine`'s own docstring for the full
mechanism.

**The "#520 corpus."** Issue #520 (Tiny Tapeout) is an open, unbuilt epic
with no vendored corpus anywhere in this repo (no `tt_corpus`-shaped fixture
directory, no vendored GDS). Following the same convention Phase 1's own
golden-pair manifest (`tests/golden_deck/`) and Phase 2a/2b's own golden
layout→netlist pairs (`tests/test_lvs_device_provenance.py`) already
established for this class of cross-check, `tests/test_lvs_native_extraction_
cross_check.py` reuses that synthesized-golden-layout convention as the
corpus stand-in — one minimal layout per device rule `EXTRACTION_DECK`
declares a `RuleProvenance` citation for (the same 8-rule set
`tests/test_lvs_device_provenance.py` already validates against the compiled
deck in isolation), extended where the native deck's own recognition
contract needed geometry (an implant layer, an exact SKU width) the existing
golden pairs did not draw.

**Results** (verified against a real, `volare`-fetched sky130A install,
`open_pdks c6d73a35f524070e85faff4a6a9eef49553ebc2b` — the same commit
`sky130.py`'s own provenance notes cite — and a real KLayout 0.28.16
binary): **7 of the 8 provenanced device rules were run; 4 agree exactly, 3
disagree for a documented, already-known reason, 1 is deferred** (`pnp` —
the native deck's exact-area/exact-edge-length three-terminal device
selection needs bespoke geometry construction not completed within this
issue's scope; investigated and documented, not silently dropped). Every
non-exact disagreement is a *deliberate, previously documented* refinement
this deck already carries a code-comment citation for — `res_high_po`'s
two-term fit (issue #518) and `cap_mim`/`cap_mim_m4`'s perimeter term (issue
#512) — discovered independently by this cross-check, not introduced by it.
No *undocumented* disagreement was found on the 7 rules actually run. See
`tests/test_lvs_native_extraction_cross_check.py`'s own module docstring for
the full per-rule table and each test's docstring for the underlying
geometry/coefficient detail.

**Scope.** `run_extract_klayout_engine` is a Python helper used by this
cross-check's own test module, not (yet) wired up as a `klt extract
--engine klayout` CLI flag the way `run_drc_klayout_engine` is for `klt
drc` — this issue's acceptance criteria ask for the cross-check to be run
and reported, not for a new user-facing engine flag; promoting it to a CLI
surface (JSON contract, docs, exit codes) is a separate, explicitly-scoped
follow-on if a caller ever needs to run this oracle outside of tests.
gf180mcu was out of scope for issue #869 (its device entries carried no
`RuleProvenance` citations yet to cross-check against at the time) — see the
next section for issue #904's own gf180mcu cross-check, once that
provenance backfill landed.

## gf180mcu native-deck (`gf180mcu.lvs`) LVS device-extraction cross-check (issue #904)

Epic #711 Phase 3a's own AC4: "if an existing hand-written gf180 deck exists
in klt, the compiled deck's agreement with it is measured on a corpus and
reported (not asserted)." This section is the gf180mcu counterpart of the
sky130 section immediately above, reusing the exact same infrastructure
(`run_extract_klayout_engine`) and reporting discipline.

**Unlike gf180mcu's own DRC deck** (which ships only as unassembled
per-feature fragments under `libs.tech/klayout/drc/rule_decks/*.drc` with no
single runnable file, per `docs/cli/drc.md`'s "Engine" → "klayout"
limitation — the reason gf180mcu's *DRC*-side native cross-check remains
deferred), **gf180mcu's native LVS deck ships as a single, directly-runnable
`libs.tech/klayout/lvs/gf180mcu.lvs`** — verified against a real
`volare`-fetched `gf180mcuD` install for this issue, resolved the same way
`sky130.lvs` is via `klayout_tools.pdk.lvs_deck_file` (whose own docstring
already flagged this exact possibility as of issue #869: "a future caller
is free to attempt it").

**A generalization `run_extract_klayout_engine` needed for a second PDK.**
sky130.lvs's own custom SPICE-writer delegate emits length/area parameters
as bare, suffix-less numbers, which `kdb.NetlistSpiceReader()` then
mis-reads as meters and rescales internally by a fixed, known factor
(1e6/1e12) that `run_extract_klayout_engine` was undoing unconditionally
(see "Unit round-trip" in that function's own docstring). gf180mcu.lvs uses
KLayout's own built-in `write_spice(...)` instead, which *does* write an
explicit engineering-notation unit suffix (`L=0.4U`, not a bare `L=0.4`) —
and the reader parses that suffix correctly, so the sky130-specific undo
would silently *mis*-scale every gf180mcu length/area parameter by a further
incorrect factor if applied unconditionally (verified empirically: a written
`L=0.4U` round-trips as plain `0.4`, not `400000.0`). Issue #904 adds a
`bare_length_area_units` parameter (default `True`, preserving pre-#904
sky130 behaviour exactly) so a caller can opt out for a native writer
confirmed to already emit unit-suffixed numbers. It also adds an `extra_rd`
parameter (a `{key: value}` mapping of additional `-rd` globals appended to
the invocation), needed below for gf180mcu's own MiM-capacitor variant
selection.

**Results** (verified against a real, `volare`-fetched gf180mcuD install and
a real KLayout binary): **5 of the 8 provenanced device rules were run; 4
agree exactly, 1 agrees with a documented refinement disagreement; 3 are
deferred** (investigated, not executed — the same "well-reasoned,
explicitly-documented scoping decision" issue #869 allowed for sky130's own
`pnp`).

- `nfet`/`pfet`: **exact** (L, W). The native deck's `mos4(...)` recognition
  needs an explicit `Nplus`/`Pplus` implant layer covering the active area
  that the compiled deck does not model — the gf180mcu counterpart of issue
  #869's own sky130 `nsdm`/`psdm` finding.
- `ppolyf_u`/`ppolyf_u_1k`: **exact** (R). Plain sheet-rho × squares on both
  sides; the native deck's default `$poly_res` (`'1k'`) already matches the
  compiled deck's own default flavour, so no `extra_rd` override is needed
  here.
- `cap_mim_2f0_m4m5_noshield`: **documented disagreement** — the native
  deck's raw single-term area coefficient (`2.0e-15` F/µm²) vs. the compiled
  deck's own refined two-term fit (`area_cap_f_um2=1.99e-15`,
  `perim_cap_f_um=2.383e-16`, issue #512's perimeter-term refinement) — the
  gf180mcu counterpart of issue #869's own `cap_mim`/`cap_mim_m4` finding.
  Needs `extra_rd={"metal_level": "5LM"}`: the native deck's own
  `$metal_level` global defaults to `'6LM'` (top-metal stack = Metal6/Via5),
  while `decks/gf180mcu.py`'s MiM capacitor models the 5LM variant (Metal4
  as the stack's bottom plate, per that module's own docstring) — without
  the override the native deck would not recognise a `Metal4` bottom plate
  as the MiM stack's conductor at all.
- `BJT.3` (the generic `bjt` bipolar) and both diodes
  (`gf180mcu_fd_pr__diode_nd2ps_06v0`, `...diode_pd2nw_06v0`): **deferred**.
  The native deck's bipolar recognition is a family of *exact-geometry*
  device classes gated on the emitter's drawn area **and** edge length
  simultaneously (e.g. an emitter within 0.5% of an exact 10×10µm square) —
  the same class of gap as sky130's own deferred `pnp`. The two diodes are
  topologically different devices than the compiled deck's simpler
  recognition: `diode_nd2ps_06v0`'s native `'P'` terminal is an `LVPWELL`
  region the compiled deck's own recognition never draws at all, and
  `diode_pd2nw_06v0`'s native `'N'` terminal requires an actual `Nwell`
  tap/tie region interacting with n+ diffusion, not just the bare `Nwell`
  polygon.

Every non-exact-agreement rule's disagreement/deferral is either a
*deliberate, previously documented* refinement this deck already carries a
code-comment citation for (issue #512, discovered independently by this
cross-check, not introduced by it), or a genuinely different native
device-recognition topology investigated and written up, not silently
dropped. See `tests/test_lvs_native_extraction_cross_check_gf180mcu.py`'s
own module docstring for the full per-rule table and each test's docstring
for the underlying geometry/coefficient detail.

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

### DEF→GDS-merged (`klt place-and-route`) layouts (#1385, #1390)

`--top-cell-pins`'s "only labels drawn directly in the top cell" heuristic is
sized for a **hand-drawn hierarchical** layout, where a sub-cell's own
internal pin labels genuinely live inside that instance's own cell view. A
layout produced by `klt place-and-route`'s own LEF/DEF → GDS merge is
structurally different: DEF's `NETS` section records every net's physical
pin connections as `(component, local-pin-name)` pairs, and every one of
those connection points ends up geometrically **in the top cell** once the
design is flattened by routing — indistinguishable, by cell-nesting depth
alone, from a genuine DEF `PINS`-section top-level port. `--top-cell-pins`
is a structural no-op against this case (the below-top set is always empty
for a fully-flattened layout — see above), and `make_top_level_pins()`
promotes every one of those DEF-`NETS` connection-point labels right
alongside the real top-level ports, including collided, comma-joined names
where two or more distinct labels land on the same electrical net (surfaced
separately as `merged_net_labels[]`, see
["Merged net labels"](#merged-net-labels-issue-470) above).

**`--def-pins` (issue #1390, below) is the automatic fix for this**: it
derives the design's genuine top-level port set directly from the routed
DEF's own `PINS` section — the same file `klt place-and-route` already wrote
— and applies it as a per-net reconciliation, no hand-derived `--pins` list
required. See ["DEF-derived declared pins"](#def-derived-declared-pins---def-pins-issue-1390)
below for the full mechanism and a worked example against a real macro-scale
corpus fixture.

What else is implemented, so this failure mode is never silent even without
`--def-pins`:

- If the layout carries **zero** text on any of the target `--deck`'s own
  label layers anywhere in the cell tree, `warnings[]` says so explicitly and
  points at `klt layers` and, for a `klt place-and-route` layout
  specifically, at `request.io.layer_h`/`io.layer_v` — the observed
  real-world trigger is an I/O-pin-layer choice that lands on a GDS layer the
  target deck never scans for pin-name text at all (issue #1385's root
  cause). This is a whole-layout check, independent of any flag.
- After every promotion/demotion pass above has run (`make_top_level_pins()`,
  `--top-cell-pins`, `--pins`/`declared_pins`, `--def-pins`), if the top
  circuit ends up with **zero** top-level pins for *any* reason, `warnings[]`
  says so — `klt lvs`'s `NetlistComparer` has no net/device anchor to seed
  correspondence with zero top-level pins, and otherwise reports a full
  mismatch with no hint the root cause is upstream pin promotion rather than
  device extraction disagreement.
- `merged_net_labels[]` (issue #470, see below) already reports every net —
  promoted to a pin or not — whose name is a multi-label collision, which is
  usually the first visible symptom of the DEF-`NETS`-vs-`PINS` ambiguity
  above.

`--pins`/`declared_pins` (below) remains the manual fallback when the DEF is
not available (or a caller wants a hand-curated interface list for some
other reason) — it is unchanged by `--def-pins`'s addition.

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

## DEF-derived declared pins (`--def-pins`, issue #1390)

`--pins` needs the caller to already have the design's port list in hand.
For a `klt place-and-route`-produced layout that list already exists — it is
the routed DEF's own `PINS` section, the same file `klt place-and-route`
wrote at `response.def_path`. `--def-pins <path-to-def>` parses that section
directly (`place_and_route.def_pin_names`, the same scan `klt
place-and-route`'s own internal post-route SPEF pipeline already relies on,
issue #961) and applies it as the declared pin set — no hand-derived `--pins`
list required. This is the automatic path issue #1385 left open (see
["DEF→GDS-merged layouts"](#defgds-merged-klt-place-and-route-layouts-1385-1390)
above): the practical fix for the "every DEF `NETS` connection point promotes
right alongside the real top-level ports" failure mode, scoped specifically
to a `klt place-and-route`-originated layout (it needs that DEF file to
exist).

`--def-pins` cannot reuse `--pins`'s plain exact-string match, though.
KLayout's flat extraction joins every distinct text label found on one
electrical net into a single, comma-separated net name (`Net.name` — see
["Merged net labels"](#merged-net-labels-issue-470) above), and in a
densely-routed DEF-merged layout *most* nets — port or not — carry two or
more such labels: a genuine top-level port's net carries both the DEF
`PINS`-declared label and whichever local instance pin label it physically
connects into, and an ordinary internal net carries a driver's local
output-pin label together with a receiver's local input-pin label. Measured
live against this repo's own routed `gcd` corpus fixture: of 752 pins the
unrestricted default promotes, 494 carry a joined name (e.g. `Y|clk` — `|`
here and in the written netlist, `Net.name`'s own internal spelling is
comma-joined, see `spice_safe_net_name`). So `--def-pins` keeps a promoted
net whenever **any** of its joined component labels is a declared DEF pin
name, not only when the whole joined name matches verbatim — every other
currently-promoted net is demoted, exactly as `--pins` demotes on a plain
miss. A `warnings` entry lists any net demoted this way, and a separate
entry lists any `--def-pins` name that matched no promoted net's label set.

```
$ klt place-and-route pnr_request.json --format json | jq -r .def_path
/path/to/gcd.def
$ klt extract /path/to/gcd.gds --deck sky130 --def-pins /path/to/gcd.def --format json
```

Applied *after* `--pins`/`declared_pins`'s own reconciliation when both are
given (an unusual combination — normally only one or the other is needed) —
it can only further restrict, never re-promote a net `--pins` already kept
internal. Omitting `--def-pins` (the default) skips this reconciliation
entirely, byte-for-byte identical to extraction before this flag existed. A
given path with no parseable DEF `PINS` section is a clean error (exit 1),
not a silent no-op — unlike `--def-net-connections`'s tolerant "absence is
not proof of zero" convention, a caller passing `--def-pins` has explicitly
asked for its restriction to apply, and silently skipping it would demote
*every* promoted pin instead of the intended subset.

`--top-cell-pins` and `--def-pins` are not mutually exclusive — combining
them is harmless (the below-top-label pass is a structural no-op on a
DEF-merged layout, see above) but does not need to be done, since
`--def-pins` alone already covers the DEF-merge case fully.

## Matched-device geometry check (`--matched-group`, issue #1018)

Standard analog-layout review practice treats a matched pair's post-layout
geometry as something that must be **verified, not assumed**: a differential
pair or current-mirror leg that ends up with unequal `W`/`L` after layout —
a hand-edit slip, or a mis-parameterized generator call — silently breaks
the matching assumption the sizing exercise was based on. `--matched-group
NAME=INST1,INST2[,...]` (repeatable, one group per flag) declares a set of
device instances (`devices[].name`, e.g. `"$1"`) expected to stay
geometrically matched, and checks — after extraction — that every parameter
every resolved member reports in common (`devices[].params`, e.g.
`w_um`/`l_um` for a MOS pair, `r_ohm` for a matched resistor pair) is
identical across the whole group:

```
klt extract mirror.gds --deck sky130 --matched-group mirror_leg=$1,$2 --format json
```

This is a **self-consistency check within one extracted netlist** — it does
not require a reference netlist. Comparing extracted geometry *against* a
golden schematic netlist is `klt lvs`'s job (`options.parameter_tolerance`,
see [`docs/cli/lvs.md`](lvs.md)); this flag catches the layout
contradicting *itself*.

- **Instance names, not net names.** A group member names a device
  (`devices[].name`, KLayout's synthesized `"$<n>"` instance name unless the
  extraction writer names it otherwise), not a net — run `klt extract`
  once without `--matched-group` first to read off the instance names to
  declare.
- **Only fields present on every resolved member are compared.** The
  comparison is the intersection of `params` keys across the group's
  resolved members — a group whose members share a device class compares
  every parameter that class reports (e.g. both `w_um` and `l_um` for a MOS
  pair); a group mixing incompatible device classes with no shared `params`
  key compares nothing and reports no mismatch.
- **Post-rounding equality, no numeric tolerance.** `devices[].params`
  values are already rounded to extraction's own precision
  (`_PARAM_PRECISION_UM` / `_PARAM_PRECISION_OHM`), which already clears
  floating-point noise — this feature does not introduce a separate
  tolerance concept.
- **An unresolved instance name is a warning, not an error.** A declared
  name matching no extracted device in this layout is reported in
  `matched_device_groups[].unresolved_instances` and `warnings`, mirroring
  `--critical-net`'s own tolerant "declared but absent" convention — a
  caller may legitimately reuse one group declaration across several
  layout variants.
- **A mismatch is a warning, not a hard failure.** Some intentional
  near-matches exist (e.g. a deliberately skewed mirror leg) — a divergent
  group does not fail the extraction, it is reported in
  `matched_device_groups[].mismatched_fields` and `warnings`, citing the
  group name, the mismatched field(s), and every resolved member's value.
- **A malformed declaration is an error.** A group name given fewer than
  two instance names, or repeated across two `--matched-group` flags,
  exits `1` with a clean message rather than silently keeping only the
  last occurrence or comparing a group of one.

Output, additive:

| Field | Meaning |
|---|---|
| `matched_device_groups[].name` | The declared group name. |
| `matched_device_groups[].instances` | The declared member list, echoed back verbatim (as given, not sorted/deduplicated — matches `parasitics.critical_nets`'s own echo convention). |
| `matched_device_groups[].unresolved_instances` | The subset of `instances` matching no extracted device, sorted. |
| `matched_device_groups[].mismatched_fields` | `[{"field": "<param name>", "values": {"<instance name>": <float>, ...}}, ...]` — one entry per parameter that diverges across the group's *resolved* members. Empty when every compared field agrees (which includes fewer than two resolved members — nothing to compare). |

`matched_device_groups` is always present, `[]` when `--matched-group` was
never given — byte-identical to extraction before this flag existed.

### Worked example

Given a layout with two NMOS devices `$1` and `$2` intended as a
current-mirror pair, but `$2`'s gate was drawn with twice `$1`'s channel
length:

```
$ klt extract mirror.gds --deck sky130 --matched-group mirror_leg=\$1,\$2 --format json
```

```json
{
  "matched_device_groups": [
    {
      "name": "mirror_leg",
      "instances": ["$1", "$2"],
      "unresolved_instances": [],
      "mismatched_fields": [
        {"field": "ad_um2", "values": {"$1": 0.8, "$2": 0.6}},
        {"field": "as_um2", "values": {"$1": 0.8, "$2": 0.6}},
        {"field": "l_um", "values": {"$1": 0.4, "$2": 0.8}},
        {"field": "pd_um", "values": {"$1": 3.6, "$2": 3.2}},
        {"field": "ps_um", "values": {"$1": 3.6, "$2": 3.2}}
      ]
    }
  ],
  "warnings": [
    "matched-group 'mirror_leg' declares $1, $2 as intentionally matched, but their extracted geometry diverges -- ad_um2: $1=0.8, $2=0.6; as_um2: $1=0.8, $2=0.6; l_um: $1=0.4, $2=0.8; pd_um: $1=3.6, $2=3.2; ps_um: $1=3.6, $2=3.2 -- this likely breaks the layout's matching assumption (a hand-edit slip or a mis-parameterized generator call). See docs/cli/extract.md's 'Matched-device geometry check' section."
  ]
}
```

(`l_um` diverges directly from the gate-length difference; `ad_um2`/`as_um2`/`pd_um`/`ps_um` diverge incidentally, since the drain/source diffusion pocket's own area/perimeter shift with the gate — a mismatched-field list is not limited to the one dimension a caller intentionally changed.)

A correctly matched pair (equal `l_um`/`w_um` on both instances) reports the
same `matched_device_groups[0]` shape with `"mismatched_fields": []` and no
matching `warnings` entry.

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
X$1 Y A VGND vsubs sky130_fd_pr__nfet_01v8 L=0.15 W=0.65 AS=0.234 AD=0.234 PS=1.6 PD=1.6
X$7 RA RB sky130_fd_pr__res_generic_po l=6 w=1
X$8 net1 net2 sky130_fd_pr__cap_mim_m3_1 l=10 w=5
X$9 vsubs BASE EMIT sky130_fd_pr__pnp_05v5_W0p68L0p68
```

The device is bound via small curated per-class tables in
`src/klayout_tools/pdk_models.py` (see that module's docstring for the exact
provenance of every bound subcircuit name and parameter spelling, each read
off a real fetched PDK install rather than assumed) and a
`kdb.NetlistSpiceWriterDelegate` subclass that overrides KLayout's default
primitive-card writer only for classes present in the resolved tables.

**Geometry units follow the resolved PDK's own convention** (issue #1396).
The unbound `M` card above keeps KLayout's explicit unit suffixes
(`L=0.15U`, `AS=0.234P`); the bound `X` card writes whichever form the target
vendor deck expects:

| PDK family | Bound `X`-card geometry | Why |
|---|---|---|
| sky130 | bare micrometres — `L=0.15 W=0.65 AS=0.234` | The vendor deck (`libs.tech/combined/corners/all.spice`) sets `.option scale=1.0u` and documents the convention itself: *"1 micron width is W=1, not W=1u."* |
| gf180mcu | explicit unit suffixes — `L=0.28U W=10U AS=0.5P` | No `.option scale` anywhere in `libs.tech/ngspice/`; its subcircuits declare raw-metre defaults (`.subckt nfet_03v3 d g s b w=1e-5 l=2.8e-7`), so an absolute literal is correct. |
| sg13g2 | explicit unit suffixes — `l=6U w=1U` | Confirmed (issue #1457) against a real fetched IHP-Open-PDK v0.3.0 install: no `.option scale` anywhere in `libs.tech/ngspice/`; its `sg13_lv_nmos`/`rsil`/`rppd`/`rhigh` subcircuits all declare raw-metre defaults, same convention as gf180mcu. |

This matters because `.option scale` is applied **on top of** the parsed
literal — ngspice multiplies a MOS card's `l`/`w`/`ps`/`pd` by `scale` and its
`as`/`ad` by `scale²`. Writing sky130's cards with unit suffixes therefore
made every geometry 10⁶× too small, and the resulting device matched no model
bin at all: ngspice rejected it with the generic `could not find a valid
modelname`. The same mistake in the other direction (bare numbers for
gf180mcu) would be wrong by the same factor, which is why the convention is
resolved per PDK family rather than picked globally.

**Coverage** (issue #339 extended #209's MOS-only binding to the other
recognised analog device classes):

| Device class | sky130 | gf180mcu | sg13g2 | Geometry on the `X` card |
|---|---|---|---|---|
| MOS (`nfet`/`pfet`) | ✅ (plus `hvi`-scoped `g5v0d10v5` flavour, issue #1369) | ✅ (plus `Dualgate`-scoped `06v0` flavour, issue #1111) | ✅ `sg13_lv_*` (plus `ThickGateOx`-scoped `sg13_hv_*` flavour, issue #1231) | `L`/`W`/`AS`/`AD`/`PS`/`PD`, read off the device (issue #695) |
| Resistor | ✅ | ✅ (all flavours) | ✅ `rsil`/`rppd`/`rhigh` (issue #1457); ❌ `res_metal1`/`res_metal2` (verified carve-out — no real subcircuit exists) | `l`/`w` (sky130 or sg13g2) or `r_length`/`r_width` (gf180mcu), read off the device |
| Capacitor (MiM) | ✅ | ✅ | — (no capacitor recognition in that deck yet) | `l`/`w` (sky130) or `c_length`/`c_width` (gf180mcu), derived from the extracted plate area+perimeter |
| Bipolar | ✅ (`pnp`) | ❌ (carve-out) | — (no bipolar recognition in that deck yet) | none — a geometry-named variant selected by emitter area |

MOS flavour note (issue #1111, extended to sky130 by issue #1369): gf180mcu's
and sky130's `nfet`/`pfet` bindings are no longer a single fixed subcircuit
name. A transistor drawn (fully or partially — see "Voltage-domain markers
and per-flavour MOS binding" above) inside `Dualgate` (55/0) binds
`nfet_06v0`/`pfet_06v0` instead of gf180mcu's default `nfet_03v3`/
`pfet_03v3`; a transistor drawn inside sky130's `hvi` (75/20) binds
`sky130_fd_pr__nfet_g5v0d10v5`/`sky130_fd_pr__pfet_g5v0d10v5` instead of
sky130's default `sky130_fd_pr__nfet_01v8`/`sky130_fd_pr__pfet_01v8`; a
transistor drawn entirely outside either marker is unaffected. Selecting a
flavour this way never changes `devices[].class` (every flavour reports the
deck's ordinary `"nfet"`/`"pfet"` class) or `device_counts` — only which real
subcircuit the `X` card names.

Resistor note: the binding is **flavour-complete** on gf180mcu — every value
`--deck-option poly_res=` accepts binds its own real subcircuit
(`ppolyf_u_1k` / `ppolyf_u_2k` / `ppolyf_u_3k`, all three confirmed in
`sm141064.ngspice` on the identical three-terminal `r_length`/`r_width`
convention), so combining `--pdk` with a non-default flavour emits
`X … ppolyf_u_2k r_length=… r_width=…`, not a bare `R` card. Selecting a
flavour changes *which* subcircuit is called; it never changes
`devices[].class` handling or the extracted resistance, which the deck
computes from that flavour's own sheet rho either way.

sg13g2 resistor note (issue #1457): `rsil`/`rppd`/`rhigh` bind to their real,
identically-named 3-terminal subcircuits (`X … rsil l=… w=…`, the bulk-tie
third terminal wired to `vsubs`) — a real fetched IHP-Open-PDK v0.3.0 install
confirms all three in `libs.tech/ngspice/models/resistors_mod.lib`.
`res_metal1`/`res_metal2` are a **verified carve-out**, not a "not implemented
yet" gap: the same install defines no `.subckt`/`.model` for either name
anywhere, so there is no real subcircuit for this table to bind to — they
keep the bare `R`-card form under `--pdk`, unchanged from today, matching the
"Scope limits" carve-out discipline below.

Bipolar note: sky130's `pnp_05v5` ships as discrete geometry-named cells
(`…_W0p68L0p68`, `…_W3p40L3p40`), not one parameterized cell, so the writer
selects the variant whose nominal emitter area is nearest the device's
measured `AE` and emits its three `c b e` terminals (the vendor subcircuit
declares only `c b e mult=1`; the extraction deck ties the collector net to
the substrate internally, but no separate substrate pin is emitted on the
`X` card).

**Scope limits** (deliberately narrower than the general PDK-device-metadata
resolver `docs/design/pdk-device-corner-metadata-spike.md` proposes as a
future epic):

- **One voltage/flavor per class, with documented exceptions.** sky130's
  `01v8` MOS core devices, sky130/gf180mcu's specific resistor/capacitor
  device names, and gf180mcu's *default* `03v3` MOS core devices (gf180mcu
  has no `gf180mcu_fd_pr__`-prefixed naming convention the way sky130 does)
  are each still the one *default* flavor those classes distinguish. All
  three decks' MOS classes now have a marker-scoped exception (issues
  #1111/#1231/#1369): a `Dualgate`-marked gf180mcu transistor binds
  `nfet_06v0`/`pfet_06v0`, a `ThickGateOx`-marked sg13g2 transistor binds
  `sg13_hv_nmos`/`sg13_hv_pmos`, and an `hvi`-marked sky130 transistor binds
  `sky130_fd_pr__nfet_g5v0d10v5`/`sky130_fd_pr__pfet_g5v0d10v5`, instead of
  their decks' defaults — see the "MOS flavour note" above.
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
- **`sg13g2`'s drawn metal resistors are a verified carve-out** (`res_metal1`/
  `res_metal2`, issue #1235's classes; the carve-out itself confirmed by
  issue #1457): a real fetched IHP-Open-PDK v0.3.0 install defines no
  `.subckt`/`.model` for either name anywhere under `libs.tech/ngspice/` or
  `libs.tech/xyce/`, so there is no real subcircuit for this binding to call
  — they stay bare `R` cards under `--pdk`, the same documented
  bare-primitive carve-out gf180mcu's `bjt` gets above, not an error. Their
  model token is already the PDK's own device-class name (`res_metal1`/
  `res_metal2`), so a consumer can supply the matching `.model` itself. Its
  drawn *poly* resistors (`rsil`/`rppd`/`rhigh`, issues #1231/#1235) **do**
  bind, as of issue #1457 — see the "Coverage" table and "sg13g2 resistor
  note" above.
- **Three curated decks** (`sky130`, `gf180mcu`, `sg13g2` — the last as of
  issue #1231, resolved from the `ihp-sg13g2` variant name IHP-Open-PDK
  installs use); a resolved PDK whose
  family has no curated MOS table entry for the requesting `--deck` (e.g. the
  `sky130` deck against a resolved `gf180mcuA` install, or a variant name
  matching no known PDK family at all) is an application error (exit 1)
  naming what was tried — **never** a silent fallback to the bare primitive
  form. (This up-front deck/PDK-mismatch guard keys off the MOS table, which
  every deck has; it is distinct from the per-class carve-out above.)
- Geometry values on every `X` card follow the resolved PDK family's own
  convention (see "Geometry units follow the resolved PDK's own convention"
  above): bare micrometres for sky130 (`L=0.15`, `l=6`, `AS=0.234`), an
  explicit unit suffix for gf180mcu and `sg13g2` (`L=0.28U`, `r_length=6U`
  for a length in micrometres, `AS=0.5P` for an area in square
  micrometres). Both forms rely on the resolved
  subcircuit's own defaults for everything else (`nf`/`mult`/`par`/`m`, all
  confirmed `1`-equivalent in the fetched real installs each table was
  verified against — the extractor has no opinion on multi-finger/multiplied
  devices, so there is nothing measured to carry there). MOS source/drain
  junction area+perimeter (`AS`/`AD`/`PS`/`PD`, present on the bare `M`-card
  form) **are** carried onto the `X` card (issue #695) — both curated PDKs'
  target subcircuits declare matching `as`/`ad`/`ps`/`pd` call-site
  parameters, so nothing is lost for a bound MOS device.
- **Not every dropped parameter has somewhere to go.** sky130's bound `pnp`
  is the one case today: `pnp_05v5_W0p68L0p68`/`_W3p40L3p40` are
  fixed-geometry cells selected by name (see `variants` above), so they take
  no per-instance base/collector-area/perimeter or emitter-count override at
  all — the extractor's measured `PE`/`AB`/`PB`/`AC`/`PC`/`NE` (KLayout's
  `DeviceClassBJT3Transistor`, present and non-zero on the bare `Q`-card
  form) have no parameter to land on. Rather than dropping them silently,
  `--pdk` binding emits one aggregate `warnings[]` entry per affected class,
  naming both the class and the specific parameters dropped (issue #695's
  acceptance criterion) — re-run without `--pdk` to recover them from the
  bare `Q`-card form.
- **The JSON response's `devices[].class`/`device_counts` are unaffected**:
  they always report the deck's own class label (`nfet`/`pfet`/
  `res_generic_po`/`cap_mim_2f0_m4m5_noshield`/`pnp`/`bjt`/…), regardless of
  `--pdk` — model binding rewrites the *written SPICE* only.
  `devices[].params`, by contrast, always carries every parameter the
  extractor measured (see "`devices[]` entries" below) — independent of
  `--pdk`, since it is built from the extracted device objects before the
  `--pdk`-bound netlist is even written.

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

### The model: a star topology (issue #592)

For each net (except the deck's substrate/ground net, and nets with no
eligible interconnect geometry) the pass computes one lumped `(R_total,
C_total)` from the net's own geometry, then distributes it as a **star**:
the net itself is the star's **hub**, every device terminal that was
connected directly to the net is moved onto its own per-terminal "leg" net,
and a series resistor bridges each leg back to the hub. The hub's total
ground capacitance still hangs off the hub as a single capacitor:

```
term_a --Ra--> net(hub) <--Rb-- term_b
                 |
                 Ctotal
                 |
           <substrate_net>
```

So two terminals on the same net now sit in series through two resistors
(`Ra + Rb`), not on one shared node with zero resistance between them — the
topology issue #338 documented as the pre-#592 gap (a resistor that carried
no DC current and never appeared in series between two terminals). A net
with a single terminal degenerates to exactly the pre-#592 **Γ-section**
(one resistor carrying the net's whole computed resistance, `term --R-->
net(hub) --C--> <substrate_net>`); a net with **no** device terminal at all
(routed geometry with nothing electrically attached) falls back to the same
shape, but through a fresh internal node rather than the net itself
(`net --R--> net__par --C--> <substrate_net>`), since there is no terminal
to make the net double as a hub for.

- **C to ground** (femtofarads) = Σ over conductor roles of
  `(area_um2 − coupled_area_um2) * cap_area + perimeter_um * cap_perim`, the
  net's lumped ground capacitance — unchanged by the star split, still one
  capacitor per net. The reference node is the deck's `substrate_net`
  (`vsubs`), which `--parasitics` gives a DC reference of its own (see
  "Substrate DC reference" below) and which a `klt sim` testbench ties to
  the AC ground.
  `coupled_area_um2` is the part of that role's area that sits directly over
  or under *another* net's conductor on an adjacent metal level, which is
  charged between the two nets instead — see "Vertical-overlap coupling
  capacitance" below. It is zero for every non-metal role, zero for the
  perimeter/fringe term (only the *area* term moves), and zero on any deck
  that curates no overlap coefficients.
- **total series R** (ohms) = Σ over conductor roles of `sheet_res *
  n_squares`, where `n_squares` is estimated per layer from the net's area
  `A` and perimeter `P` by modelling its copper as one equivalent rectangle
  (`L`,`W` = roots of `t² − (P/2)t + A = 0`; squares = `L/W`, clamped ≥ 1;
  exact for a single rectangular wire, → 1 for a square). This biases series
  resistance conservatively high for L-shaped or fragmented nets.
- **per-terminal leg R** — the net's total series R distributed across its
  terminals, weighted by each terminal's Euclidean distance from the
  centroid of all of the net's terminal positions (a terminal farther from
  the net's other connections gets more of the total; an equal split when
  every terminal coincides). A terminal's position is read from
  `Device.trans` — KLayout records one placement transform per *device*
  (typically its recognition shape's center), not a distinct position per
  terminal, so this is a **coarse** proxy, not a true per-segment routing
  measurement (that is Option 2, still out of scope — see "What it does
  *not* do" below).

Conductor roles map to the deck's geometry layers: `poly` and each
`metals[i]` metal-stack layer. Two roles are deliberately excluded because
their capacitance is already captured by the extracted device models, and
counting them here would double-count it (#226):

- **Transistor gate poly** is subtracted from each net's poly shapes before
  measuring — the gate sits over the channel, not the substrate the
  coefficients describe, and its capacitance is in the device model. Only the
  parasitic measurement subtracts it; device connectivity is untouched.
- **Source/drain diffusion** carries no parasitic role: the extracted MOS
  card already emits `AS`/`AD`/`PS`/`PD` — the bare `M`-card form always has,
  and a `--pdk`-bound `X` card does too (issue #695) — from which the device
  model derives the junction capacitance. Both PDKs' own magic tech files
  comment their active-layer parasitic caps out for the same reason.

The coefficients are curated per-PDK-family in each deck module's `PARASITICS`
table (`src/klayout_tools/decks/sky130.py` / `gf180mcu.py` / `sg13g2.py`),
**transcribed with citations from each PDK's public magic-format technology
file** — `sky130.tech` / `gf180mcu.tech` in fossi-foundation/open-pdks
(GPLv3), and `libs.tech/magic/ihp-sg13g2-extract.tech` in
IHP-GmbH/IHP-Open-PDK (Apache-2.0) — sheet resistances from its `resist`
entries, area/fringe capacitances from its `defaultareacap` /
`defaultperimeter` entries, vertical-overlap coupling from its
`defaultoverlap` entries — never NDA'd, the same public-source curation
pattern as the DRC decks and the SPICE model-binding table (#214). Each changed
coefficient carries an inline citation (source file + field name) in its deck
comment.

`sg13g2`'s `PARASITICS` table covers its full seven-level metal stack
(Metal1-TopMetal2, `EXTRACTION_DECK.metals[0:7]`) and all six adjacent-pair
vertical-overlap coefficients (issue #1281, extending #1277's initial
Metal1/Metal2 curation) — see
`src/klayout_tools/decks/sg13g2.py`'s `PARASITICS` module comment for the
full rationale, including why `ihp-sg13g2-extract.tech` (not
`libs.tech/parasitics/itf/sg13g2_typ.itf`) is the source: the `.itf` file
carries only a raw process-stack description with no directly-transcribable
area/perimeter-capacitance table.

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
above, and that carries no metal) — not because it lacks a label. That test is
applied to the net's **pre-coupling** ground capacitance, so which nets appear
is unchanged by the coupling correction below: a net whose entire area term
moved to coupling still gets an entry (with `capacitance_ff` possibly `0.0`),
because its hub still has to exist for the coupling capacitor to attach to.

### Vertical-overlap coupling capacitance (issue #760)

Where one net's conductor on metal level `i` sits **directly under** a
*different* net's conductor on the adjacent level `i+1`, that overlap is a
parallel-plate capacitor between the two nets. `--parasitics` models it:

- **Geometry.** For each adjacent level pair, the two nets' already-extracted
  per-level regions are intersected (`Region & Region` on
  `LayoutToNetlist.polygons_of_net` output). This is a plain boolean AND on
  geometry the extraction already has — there is **no halo search, no
  neighbour-search structure, and no new dependency**, which is why the
  crossover term is affordable while the lateral term (Stage 2b) is not yet.
- **Coefficient.** `overlap_area_um2 × PARASITICS.metal_overlaps[i]`, the
  PDK's own `defaultoverlap` value for that level pair (fF/µm²), transcribed
  with a per-value citation exactly like the area/fringe coefficients.
- **Charge moves; it is not duplicated.** The overlap area is subtracted from
  **both** nets' ground *area* term on their own respective level, so the
  same area is never charged twice. Perimeter/fringe terms and series R are
  untouched — resistance is a property of a conductor's own geometry, not of
  what sits above or below it.
- **Same-net overlap is excluded by construction.** A net's own via stack (or
  its own li1 routed under its own met1 strap) is overlap between one net and
  itself; coupling is only ever computed between two *distinct* nets, so that
  area stays on the ground term. On the `gcd` corpus block this is the
  majority of all crossover area — 77.6 fF-equivalent of 146.3 fF-equivalent
  total, with 68.4 fF-equivalent genuinely inter-net.
- **Coupling is aggregated per net *name*, not per net object.** Where a
  layout gives several genuinely distinct nets the same label (`gcd` has 105
  separate, un-strapped `VGND` rail islands), every coupling pair naming that
  label attaches to a single (last-registered) island's hub node, because the
  pair list and the hub map are both keyed by name. Overlap between two such
  same-named nets is therefore skipped and left on the ground term: both
  terminals would land on that one hub, and a capacitor between a node and
  itself contributes nothing electrically while inflating the reported total.
  Overlap between two *different* names accumulates into a single pair,
  matching the one hub per name the card attaches to. Note the ground terms
  themselves are **not** aggregated this way — since issue #765 each
  `parasitics.nets[]` entry is a distinct net object with its own `net_id`,
  and KLayout's SPICE writer renames duplicates on write (`VGND`, `VGND$1`,
  …), so same-labelled islands stay separate nodes in the netlist. Per-net-
  object coupling is a named follow-on.

Output is additive:

| Field | Meaning |
|---|---|
| `parasitics.nets[].coupled[]` | Per net, its coupling counterparts: `{"net", "capacitance_ff", "levels", "lateral_levels"}`, sorted by counterpart name. `levels` is the list of `[lower_metal_index, upper_metal_index]` deck-`metals` index pairs that contributed vertical coupling; `lateral_levels` (issue #976, see below) is the list of same-`metal_index` levels that contributed lateral coupling — `capacitance_ff` is their sum when a pair has both. Each pair appears from **both** sides, so summing `coupled[].capacitance_ff` across `nets[]` double-counts — use `total_coupling_capacitance_ff`. |
| `parasitics.cc_count` | Number of coupled net **pairs** (equivalently, coupling `C` cards emitted) — vertical and lateral combined; a pair with both kinds still gets exactly one card. |
| `parasitics.total_coupling_capacitance_ff` | Sum over distinct pairs, each counted once. |
| `parasitics.overlap_pairs_without_coefficient[]` | Gap report — adjacent metal-level pairs the deck declares with no curated overlap coefficient. See below. |

`c_count` keeps its documented meaning — always one per `nets[]` entry, ground
capacitors only. Coupling capacitors are counted by `cc_count`, not folded
into it.

In the emitted SPICE, each coupled pair becomes one two-terminal `C` card
between the two nets' **hub** nodes (named `cc_<net_a>_<net_b>`, sanitized the
same way every other parasitic instance name is). Because both terminals sit
on real nets rather than on the substrate net, this is the first element in
`klt extract`'s output that can carry a disturbance from one net to another —
i.e. the first extracted netlist that can show crosstalk in `klt sim` at all.

**Still a fixed, quasi-static, uncalibrated model.** Adding this term makes
the crossover charge better *attributed* and better *sized*; it does not make
`--parasitics` a PEX tool. See "What it does *not* do" below.

### Lateral (same-layer, sidewall) coupling capacitance for critical nets (`--critical-net`, issue #976)

Vertical-overlap coupling above only ever fires when one net's conductor
sits directly over another's on an *adjacent level* — it says nothing about
two nets routed side by side on the *same* level, which is the dominant
same-layer crosstalk path on a crowded bus (`docs/design/
extract-fidelity-roadmap.md`'s Stage 2b: "the largest [fidelity gain] of any
stage in this roadmap"). `--critical-net <net>` (repeatable; requires
`--parasitics`) closes part of that gap, for the nets Epic #709's Phase 2
names as the ones that matter — a design's high-impedance nodes, a SAR
ADC's CDAC top plate, a PLL loop filter — **without** running a full-layout
lateral search, which the roadmap flags as real, unbounded cost on a routed
block:

```
klt extract cell.gds --deck sky130 --parasitics --critical-net VOUT --critical-net VCTRL --format json
```

- **Scope: a caller-declared net set, not the whole layout.** A same-layer
  net pair only gets lateral coupling computed when **at least one side** is
  named `--critical-net` — geometry between two nets neither of which was
  named is untouched, same as before this flag existed. This is a
  deliberate scope decision (see the roadmap's own Stage 2b cost estimate),
  not an accuracy compromise on the nets a caller actually names.
- **Geometry.** For each named net's shapes on a metal level, KLayout's own
  `Region.separation_check` (the same primitive `klt drc`'s `check:
  "separation"` rules already use) finds every same-layer neighbour within
  that level's own minimum-spacing DRC rule — the tightest legal routing
  configuration, and thus the dominant real same-layer coupling case. The
  summed facing-edge length becomes the coupling term.
- **Coefficient.** `facing_length_um × PARASITICS.metal_sidewalls[i]`, the
  PDK's own `defaultsidewall` value for that level (fF/µm), transcribed the
  same way `metal_overlaps` was. `metal_sidewalls`/its lookback distance are
  index-aligned with `metals` directly (one entry per level), unlike
  `metal_overlaps`' adjacent-*pair* indexing.
- **Additive, not deducted.** Unlike the vertical case, this charge is
  **not** subtracted from either net's substrate fringe term — a documented
  simplification, not an oversight: magic's own fringe-shielding model needs
  its `defaultsidewall` *second* parameter's semantics resolved first (an
  explicitly open question, see the roadmap doc), so this increment adds the
  coupling term without also attempting that deduction.
- **A name matching no net is a warning, not an error.** Unlike `--mom-net`,
  a `--critical-net` name that matches nothing in this particular layout is
  reported in `warnings` rather than raised — a caller may legitimately name
  several candidate nets across several blocks/runs.
- **Naming an anonymous net.** `--critical-net`/`--distributed-rc` match
  against the same spelling `nets[].name`/`parasitics.nets[].net` report
  (see "Anonymous nets are backslash-escaped" below) — an unlabelled net's
  KLayout-synthesized placeholder must be given backslash-escaped (e.g.
  `--critical-net '\$2'`), not bare (`--critical-net '$2'` matches no net,
  issue #1162).

Output, additive:

| Field | Meaning |
|---|---|
| `parasitics.critical_nets` | The `--critical-net` request, echoed back verbatim. `[]` when the flag was never given. |
| `parasitics.nets[].coupled[].lateral_levels` | Per coupling counterpart, the sorted list of same-`metal_index` levels that contributed lateral coupling to that pair — `[]` unless one side was named `--critical-net`. |

Both `sky130`'s deck curates `metal_sidewalls`/its lookback distance for
every metal level today; a deck that does not populates neither, so
`--critical-net` on it reports zero lateral coupling for every requested
net (flagged in `warnings`, mirroring `overlap_pairs_without_coefficient`'s
gap-reporting discipline rather than silently zeroing).

### Distributed (multi-segment) RC ladder for critical nets (`--distributed-rc`, issue #977)

Every net's parasitic resistance/capacitance above (including a
`--critical-net`-named one) is still a single **lumped** element: one star
hub, all of the net's capacitance at that one node, one resistor leg per
device terminal — a topology that overstates a net's own Elmore delay by
roughly 2x versus a genuinely distributed line (`docs/design/
extract-fidelity-roadmap.md`'s Stage 3, citing Elmore 1948 and Rubinstein/
Penfield/Horowitz 1983). `--distributed-rc` (requires `--critical-net`;
reuses that flag's own net set rather than a second net-classification
mechanism) replaces the star with a multi-segment ladder for the same
"nets that matter" Epic #709's Phase 2 names — high-impedance nodes, a SAR
ADC's CDAC top plate, a PLL loop filter:

```
klt extract cell.gds --deck sky130 --parasitics --critical-net MID --distributed-rc --format json
```

- **Scope: exactly the `--critical-net` set, and only where there is
  something to chain.** A named net with 2 or more device terminals gets
  the ladder; a named net with fewer than 2 (nothing to chain — the star
  and the ladder coincide at 0 or 1 terminal) silently keeps the star model
  and is reported in `warnings`, not an error — the same "declared but not
  fully applicable" tolerance `--critical-net` itself already has for a
  name matching no net.
- **Terminal ordering is a physical-spread proxy, not routed geometry.**
  Terminal positions (the same device-placement centroids the star topology
  already reads) are ordered along the axis between the two most distant
  terminals, giving a deterministic node sequence for the chain — still an
  approximation (terminal position is a device-placement proxy, not true
  per-segment routing geometry) but a strictly finer-grained one than a
  single hub.
- **Conservation.** The ladder redistributes the *same* `resistance_ohm`/
  `capacitance_ff` totals `--parasitics` already computes for that net — it
  changes **where** the R/C sits, not **how much** exists. `N` terminals
  become `N - 1` series segment resistors (summing back to the net's total
  resistance, split proportional to each segment's inter-terminal distance)
  and `N` per-terminal ground capacitors (summing back to the net's total
  capacitance; an interior node gets the average of its two adjacent
  segments' length share, an end node half of its one segment's — the
  standard "half the capacitance of each adjoining segment" lumped-element
  discretization of a distributed RC line).
- **The coupling-attachment point is the ladder's middle leg**, a
  coarse choice flagged rather than silently assumed — coupling geometry
  (vertical or `--critical-net`-scoped lateral) is not in general localized
  to one exact point on a real routed net. A net with both `--distributed-rc`
  and an inter-net coupling capacitor still gets exactly one coupling `C`
  card, attached to that middle node instead of the star's single hub.

Output, additive:

| Field | Meaning |
|---|---|
| `parasitics.distributed_rc` | `true` only when `--distributed-rc` was given. `false` for a plain `--critical-net`-only run (Phase 2a's own baseline) and whenever the flag was never given. |
| `parasitics.nets[].rc_model` | `"lumped"` (the star/Gamma-shunt model, every net unless this feature applies) or `"distributed"` (the ladder above). |
| `parasitics.nets[].segments[]` | The ladder's per-segment resistors, in node order: `{"net_a", "net_b", "resistance_ohm"}`. `[]` unless `rc_model == "distributed"`. |
| `parasitics.nets[].terminals[]` (distributed) | Same array as the star's, but each entry's shape differs for a distributed net: `{"device", "terminal", "leg_net", "order", "capacitance_ff"}` — `order` is the terminal's 0-based position in the ladder's node sequence, `capacitance_ff` its own per-node ground capacitance (there is no per-leg `resistance_ohm` here; resistance lives on `segments[]` instead, between adjacent nodes, not per terminal). |

`c_count`/`r_count` reflect actual emitted device counts either way: a
distributed net contributes `N` capacitor devices (one per terminal) instead
of the star's 1, and `N - 1` segment resistors instead of the star's `N` leg
resistors — `total_resistance_ohm`/`total_capacitance_ff` are unaffected,
since both models redistribute the identical per-net totals.

A `--distributed-rc` name matching a net with fewer than 2 device terminals
is reported in `warnings` (`"... matched a net with fewer than 2 device
terminals -- kept the star/Gamma-shunt model for them instead of a
distributed ladder"`), not an error — see "A name matching no net is a
warning, not an error" above for the same tolerance on `--critical-net`
itself. `--distributed-rc` given without `--critical-net` is a clean
`ExtractError` (`"--distributed-rc requires --critical-net"`) — there is no
net set to scope this onto otherwise.

### `klt mom` cross-check for one net (`--mom-net`, issue #798)

`--parasitics` (above) is a fast, curated-coefficient model — the same
lumped-RC table for every net on a given metal level, regardless of that
net's actual neighbours. Epic #701's Method-of-Moments field solver
([`klt mom`](mom.md)) is the opposite: a real per-shape electrostatic solve,
too slow to run for every net in a design but exact for the one you point it
at. `--mom-net <net>` wires the two together for exactly one net at a time —
Phase 1b of the epic, "prove the shipped MoM solver improves real extraction
fidelity, not just canonical benchmarks":

```
klt extract cell.gds --deck sky130 --parasitics --mom-net Y --format json
```

- **Requires `--parasitics`.** There is nothing to cross-check against
  otherwise; given alone, `--mom-net` is a clean error.
- **Naming an anonymous net.** `--mom-net` matches against the same
  spelling `nets[].name`/`parasitics.nets[].net` report (see "Anonymous
  nets are backslash-escaped" below) — an unlabelled net's KLayout-
  synthesized placeholder must be given backslash-escaped (e.g. `--mom-net
  '\$2'`), not bare (issue #1162).
- **Requires the `klt_mom_native` extension** to be built — see
  [`docs/cli/mom.md#building-the-native-extension`](mom.md#building-the-native-extension).
  An unbuilt extension, a `<net>` matching no net with ground-eligible
  parasitics geometry, or a solver-level failure (e.g. the panel-count guard)
  is a clean `ExtractError`, not a silent fallback to the lumped-RC value —
  this flag is an explicit request for a specific net's MoM-derived number,
  so silently substituting a different model would hide exactly the failure
  a caller invoking it wants to know about.
- **Genuinely replaces**, not merely reports alongside: the named net's
  ground capacitance in both the written SPICE `C` card and its
  `parasitics.nets[]` entry becomes the MoM-solved value. Every other net's
  parasitics are completely unaffected.
- **The comparison is also reported**, in a new `parasitics.mom_crosscheck`
  object — see "JSON `parasitics` block" below for the field list.
- **A name shared by several net islands solves the lowest-`net_id` one**
  (issue #811). A layout label is not a net identity: several genuinely
  distinct, electrically unconnected nets can carry the same one (the `gcd`
  corpus block has 105 separate un-strapped `VGND` islands, 88 `VPWR`).
  `--mom-net` picks the island with the lowest `net_id` — i.e. the first
  entry carrying that name in `parasitics.nets[]`, which is sorted by
  `(net, net_id)`, so the choice is reproducible run to run — reports which
  island it solved as `mom_crosscheck.net_id`, and emits a `warnings[]` entry
  naming the number of matching islands. Every other same-named island keeps
  its lumped-RC capacitance. The `parasitics.nets[]` entry that receives the
  MoM value is resolved from that same `net_id`, never re-matched by name, so
  the island measured and the island swapped are the same one by
  construction.

**Method.** The named net's geometry on each of the deck's `metals` roles
(`PARASITICS.metals` — see "The coefficients are curated per-PDK-family"
above) is read via the same `LayoutToNetlist.polygons_of_net` call
`--parasitics` itself uses, and each constituent shape's axis-aligned
bounding box becomes a `klt mom` conductor panel for that net (the same
bbox-based discretisation `klt mom`'s own GDS path uses — see
[`docs/cli/mom.md`](mom.md#spec-file)). A second conductor, a synthesized
ground plate, is placed directly beneath each such shape:

1. **z-gap.** `PARASITICS.metals[i].cap_area_ff_um2` is the PDK's own
   area-capacitance coefficient — inverting the parallel-plate formula
   (`C/A = ε₀·εᵣ/d`) at a fixed relative permittivity (εᵣ = 3.9, SiO₂ — the
   same value [`docs/cli/mom.md`](mom.md)'s own worked examples use) gives
   the z-gap `d` an idealised infinite parallel plate at that permittivity
   would need to reproduce the coefficient's area term. This does **not**
   claim to recover the real physical li1/met1-to-substrate distance — this
   repo's decks curate no such stackup height (`ParasiticsDeck` has no
   `z0_um`/`z1_um` field) — it is a modelling choice, stated plainly rather
   than left implicit.
2. **Ground plate size.** A plate exactly the same size as the net's own
   shape loses a large share of its edge field lines to open space instead
   of terminating them on the plate below (the deck's own coefficient
   implicitly assumes an effectively infinite plane — see
   `docs/design/extract-fidelity-roadmap.md` section 2.2). The synthesized
   plate is padded by 3× the implied z-gap in every direction, a factor
   chosen from a convergence sweep run during this feature's implementation
   (moving from 2× to 5× padding changed the answer by under 0.3% at a fixed
   panel size on a representative net).
3. **Panel size.** `min(0.5, gap / 4)` micrometers, keeping the solve inside
   [`docs/design/mom-validation.md`](../design/mom-validation.md)'s ~1%-accuracy band
   for a `gap`-scale panel while staying well under the 8000-panel guard for
   a single standard-cell-scale net.
4. **Solve.** `klt mom`'s native solver runs directly on this synthesized
   two-conductor request (no intermediate GDS/spec-file round trip — see
   `klayout_tools.mom.solve_capacitance_matrix`), and the returned matrix's
   `net`/`net` diagonal — the capacitance between the net and the ground
   plate in this isolated two-conductor system, the same reading
   [`docs/cli/mom.md`](mom.md#reading-the-matrix)'s own parallel-plate
   worked example uses — is the reported `mom_capacitance_ff`.

**Scope, stated plainly.** Only the deck's `metals` roles are modelled — a
net whose lumped-RC ground capacitance also draws on the `poly`/`diffusion`
roles (when a deck curates them; sky130 curates no `diffusion` role at all)
is not fully represented by the MoM side of the comparison, and
`mom_crosscheck.warnings` always says so explicitly. This is a genuine,
deliberate limitation — pick a net whose interconnect is entirely metal
(most output/routing nets) for the cleanest comparison; a gate net that also
draws poly will show a correspondingly larger, and honestly-flagged, delta.

**Worked example.** `tests/corpus/sky130/sky130_fd_sc_hd__inv_1.gds`'s output
net `Y` is exactly the net `docs/design/extract-fidelity-roadmap.md` section
4 already cites as this repo's committed schematic-vs-extracted
sensitivity-floor example, and its ground capacitance comes entirely from a
single li1 shape — the cleanest possible case (no poly/diffusion
contribution to exclude):

```
$ klt extract tests/corpus/sky130/sky130_fd_sc_hd__inv_1.gds \
    --deck sky130 --parasitics --mom-net Y --format json
```

Measured result (reproducible against the pinned corpus fixture and deck —
re-running the command above reproduces it exactly):

| Field | Value |
|---|---|
| `lumped_rc_capacitance_ff` | `0.23966` |
| `mom_capacitance_ff` | `0.17338` |
| `delta_ff` | `-0.06628` |
| `delta_pct` | `-27.656` |
| `panel_size_um` | `0.23332` |
| `panel_count` | `834` |

MoM's ab-initio electrostatic solve reports about 28% *less* capacitance for
this net than the lumped-RC coefficient table's area+fringe sum — the
lumped model's `cap_perim_ff_um` fringe term (itself a PDK-curated,
field-solver-derived approximation for an *arbitrary* neighbouring
environment) does not, for this specific net's actual isolated geometry,
match a direct solve as closely as the area term alone would suggest. This
is exactly Epic #701's stated Phase 1b goal: a **measured**, not asserted,
fidelity delta on a real corpus net — read as "these two models disagree by
this much on this net," not as a verdict that either number is the "true"
capacitance (silicon or a full non-idealised field solve would be needed for
that).

### Substitute a caller-supplied `klt mom` R/L/C for a critical net (`--mom-rlc-net`, issue #988, Epic #709 Phase 3a)

`--mom-net` above drives its *own* MoM solve (an idealised, synthesized
ground plate) and reports the comparison. `--mom-rlc-net <net>
--mom-rlc-resistance-ohm <r> --mom-rlc-capacitance-ff <c>
[--mom-rlc-inductance-nh <l>]` is the mechanical complement: it substitutes a
caller-supplied R/L/C — e.g. the output of a *separate*, real [`klt
mom`](mom.md) run against that net's actual geometry (arbitrary stackup, not
an idealised infinite-plane assumption) — for that one named net's Phase 1/2
lumped-RC/coupling-C value, so the net a caller has singled out as critical
carries a MoM-grade parasitic in the extracted netlist a later
[`klt sim`](sim.md)/[`klt pex`](pex.md) re-simulation actually uses.

```
klt extract cell.gds --deck sky130 --parasitics \
    --mom-rlc-net CRIT --mom-rlc-resistance-ohm 42.5 \
    --mom-rlc-capacitance-ff 3.1 --mom-rlc-inductance-nh 0.8 \
    --format json
```

- **This command never calls `klt mom` itself.** Unlike `--mom-net`, the
  three numeric values are opaque caller input — where they came from (a
  `klt mom` spec-file run, hand computation, a third-party field solver) is
  none of this flag's concern. Run `klt mom` separately (see
  [`docs/cli/mom.md`](mom.md)) and pass its numbers through.
- **Requires `--parasitics`** and at least one of
  `--mom-rlc-resistance-ohm`/`--mom-rlc-capacitance-ff`/
  `--mom-rlc-inductance-nh` (and vice versa: any of the three given without
  `--mom-rlc-net` is also an error). Each of the three is independently
  optional — a caller trusting MoM for capacitance only, say, can omit
  `--mom-rlc-resistance-ohm` and keep the lumped-RC value for that
  component.
- **A `<net>` matching no net with ground-eligible parasitics geometry is a
  clean `ExtractError`** — the same "an explicit request for a specific
  net's value should never silently fall back" convention `--mom-net`
  follows, not `--critical-net`'s tolerant `warnings`-only convention (a
  caller supplying a real measured value for a specific net expects it
  applied).
- **Naming an anonymous net.** `--mom-rlc-net` matches against the same
  spelling `nets[].name`/`parasitics.nets[].net` report (see "Anonymous
  nets are backslash-escaped" below) — an unlabelled net's KLayout-
  synthesized placeholder must be given backslash-escaped (e.g.
  `--mom-rlc-net '\$2'`), not bare (issue #1162).
- **Mutually exclusive with `--distributed-rc` naming the same net** (via
  `--critical-net`) — a caller-supplied lumped R/L/C total and a
  multi-segment ladder derived from the deck's own coefficient table cannot
  both describe one net's model at once; combining them for the same net is
  an `ExtractError`.
- **`--mom-rlc-resistance-ohm`/`--mom-rlc-capacitance-ff` genuinely
  replace**, not merely report alongside: the named net's series
  resistance/ground capacitance in both the written SPICE `R`/`C` card(s)
  and its `parasitics.nets[]` entry become the substituted values. Every
  other net's parasitics — JSON and SPICE — are completely unaffected
  (net-scoped).
- **`--mom-rlc-inductance-nh` is purely additive**, not a substitution —
  this command's default model (`PARASITIC_MODEL_SCOPE`) has no inductance
  term for anything to replace. When given, one series inductor is spliced
  into the named net's star/Gamma-shunt hub, in henries, in the written
  SPICE `L` card: `hub --L--> <fresh node> --C--> ground` (the ground
  capacitor moves from the hub to the inductor's far end, rather than
  sitting in parallel with it).
- **Resolved by net name**, not `net_id` (matching `--critical-net`'s own
  convention, not `--mom-net`'s) — there is no single solved net object here
  to key on, since the R/L/C values are opaque caller input rather than the
  output of a solve this flag ran itself. Every distinct net object sharing
  this net name (e.g. several un-strapped islands with the same layout
  label) is substituted the same way.
- **The substitution is also reported**, in a new
  `parasitics.mom_rlc_override` object — see "JSON `parasitics` block"
  below for the field list.
- **`klt pex`** ([`docs/cli/pex.md`](pex.md)) forwards all four flags
  straight through to its own extraction step, so the re-simulated
  extracted-side testbench (and the resulting `delta[]` rows) reflect the
  substituted net's MoM-grade parasitic.

### SPEF export (`--spef`, issue #948, Epic #700 Phase 3)

`--spef <path>` writes `--parasitics`'s existing per-net R/C model as a
Standard Parasitic Exchange Format file (SPEF, `*SPEF "IEEE 1481-1999"`) at
`<path>`, alongside the existing JSON/SPICE outputs — for `read_spef`-style
STA consumption (e.g. [`klt place-and-route`](place-and-route.md)'s `route`
stage, via its own `post_route_spef` request field). Requires
`--parasitics`; given without it, `--spef` is a clean `ExtractError`, the
same "a flag naming something invalid is an error, not a silent no-op"
convention `--mom-net` above already follows.

```
klt extract routed.gds --deck sky130 --parasitics --spef routed.spef --format json
```

**A format translation of already-computed data, not a new extraction
pass** (`docs/design/post-route-sta-survey.md` §3.1/§4.1): every SPEF `*CAP`/
`*RES` value is copied verbatim from `parasitics.nets[]` — the same
star-topology model documented above, re-expressed as SPEF's `*D_NET`/
`*CONN`/`*CAP`/`*RES` block syntax instead of KLayout SPICE `R`/`C` device
cards. Units are declared (`*C_UNIT 1 FF`, `*R_UNIT 1 OHM`) to match
`parasitics`'s own units exactly, so no femtofarad/ohm conversion — and no
conversion error — is introduced.

**Net-name correlation by default; optional device-terminal correlation via
`--def-net-connections` (issue #948, extended by #961).** Each `*D_NET`
block is keyed by `parasitics.nets[].net` — the same layout-label-derived
name the survey's §2.1 flags as the one identifier a downstream STA tool
needs to resolve against its own (Verilog-derived) flat net list. `*CONN`
entries always name a net's own **port** membership (`*P <name> B`, when
the schematic-equivalent extraction's `nets[].pin` marks it a top-level
pin); device-terminal (`*I <inst>:<pin>`) connectivity is **not** emitted by
default, because the per-device names inside `parasitics.nets[].terminals[]`
are this repo's own layout-driven `Device` naming (`$1`, `$2`, …) — never
asserted to correlate with a digital flow's own linked-design instance
names. `*CONN` is optional per the SPEF grammar, so this narrower,
net-name-only form is still a syntactically valid file.
`docs/cli/place-and-route.md`'s `spef_sta` field is where that net-name
correlation is actually *checked* (an explicit "N of M nets annotated"
count against the linked design), not merely assumed here.

**`--def-net-connections <def_path>` (issue #961)** parses a routed DEF's
own `NETS` section for each net's real `(instance, pin)` connections — in
the same instance/pin spelling the linked gate-level design already uses,
since DEF preserves the flow's original names verbatim (unlike this repo's
own `Device` naming). For every net whose name appears exactly once in this
extraction (see the duplicate-name caveat below), this emits one additional
`*I <inst>:<pin> B` `*CONN` entry per connection and wires it into the RC
network with a zero-ohm `*RES` leg from the net's own primary internal node
— this does **not** model resistance *between* DEF-level pins (this repo's
extracted parasitics have no notion of which pin drives a net or the
physical wire path to each load), only that each real design pin sits at
the net's own lumped potential. This is what lets a real OpenSTA
`read_spef` session actually attach a net's total capacitance to a
driver/load pin instead of discarding the whole `*D_NET` block as
unconnected. Requires `--spef`; a net name that appears more than once in
the extraction (e.g. several un-strapped `VGND` islands sharing one label)
is skipped rather than asserting connectivity no single island actually
has — the DEF's own `NETS` section declares each logical net name once,
with every one of its real connections, so attaching that full list to
every same-named `*D_NET` block would overclaim. `klt place-and-route`'s
own `post_route_spef` pass does this automatically, sourcing the DEF path
from the same routed DEF `declared_pins` already reads.

**Every two-terminal `*RES`/coupling `*CAP` endpoint is a colon-scoped
internal node, a `*CONN`-declared instance-pin identifier, or (for an
unambiguously-named port net) the port's own bare name — never a bare
*non-port* net name (PR #984's root-cause topology fix, plus issue #961's
own residual fix, both live-verified 2026-08-14).** OpenSTA's SPEF reader
only accepts a bare *non-port* net name as a **single-node**
self-capacitance-to-ground reference — never as one of the two endpoints of
a `*RES` or coupling `*CAP` entry. Only a colon-scoped, two-part identifier
resolves there for a non-port net: `*I <inst>:<pin>` (a real DEF-derived
instance pin) or `<net>:<N>` (a properly-scoped internal SPEF node, IEEE
1481-1999's own convention for a net's non-pin internal nodes — and,
live-verified, resolvable even when referenced *across* `*D_NET` blocks by
a coupling `*CAP` entry, contrary to what a purely-per-block reading of the
grammar might suggest).

**A `*CONN`-declared port name is a different story, live-verified against
OpenSTA's own shipped `examples/gcd_sky130hd.spef` test fixture and against
a fresh `gcd` SPEF built by this repo:** a bare port name *does* resolve as
a two-node `*RES`/`*CAP` endpoint — through OpenSTA's own pin lookup
(`findParasiticNode`'s bare-token branch calls `findPortPinRelative`, a
`Network::findPin` lookup on the design's own top-level pin), not through
any net-name lookup, so the earlier PR #984 finding that "even a
`*P`-declared bare port name never resolves" was correct for the general
bare-*net*-name defect it fixed but incomplete for *ports* specifically. A
**bus-indexed** port (`a_in[0]`) needs one more adjustment on top: its
brackets must stay **un-escaped** (`a_in[0]`, not `a_in\[0\]`) — an escaped
bracket tells the reader "this is a literal backslash-bracket character,
not a bus index" per this file's own `*BUS_DELIMITER [ ]` declaration, so
`findPin` looks for a pin that does not exist and warns `pin a_in\[0\] not
found`, live-reproduced isolating this exact mechanism. This is the *one*
identifier position this writer does not run through the general escaping
helper (see "Identifiers are escaped" below).

Given that, every `*D_NET` block plans its own `net_node` — a unique
port's own bare name (brackets un-escaped) when it qualifies, else the
internal `<net>:1` node PR #984 planned, where every device-terminal leg,
`*I <inst>:<pin>` connectivity leg, and the no-device-terminal Gamma-shunt
resistor all originate — and `hub_node` (a further internal `<net>:2` node
only in that Gamma-shunt case, otherwise the same as `net_node` — where the
self- and every coupling `*CAP` attach). `*P <port> B` is still emitted for
a declared port as pure block-level "this net is also a port" metadata
(OpenSTA's own SPEF grammar, `*P`, is a documentation-only production with
no connectivity effect of its own — the electrical tie-in comes entirely
from `net_node`/`hub_node` being the port's own bare name, not from the
`*P` line).

**Measured live end to end on the routed `gcd` corpus fixture (2026-08-14,
`openroad/orfs:latest`):** `report_parasitic_annotation` went from **533**
partially-unannotated drivers (the pre-#984 net-name-only baseline) to
**52** after PR #984's topology rework, to **0** after this residual fix —
and `spef_sta.worst_slack_ns` reads more pessimistically than the top-level
(rung 2) value across `read_spef` for the first time, unaffected by this
residual fix (it was already correctly wired for the paths it changed; the
residual only affected `report_parasitic_annotation`'s own completeness
bookkeeping for port-adjacent connections, not the delay values already
attached through the rest of the RC network). See
[`docs/cli/place-and-route.md`](place-and-route.md)'s "`*CONN`
device-terminal pin correlation" subsection for the full live-verification
log, including the isolated single-net reproductions (against OpenSTA's own
`gcd_sky130hd.spef` example and against this repo's `gcd` SPEF) that pinned
down the pin-lookup-vs-net-lookup and bracket-escaping mechanisms.

Direction on every declared `*PORTS` entry is always `B`
(bidirectional/unspecified) — a GDS text label carries no I/O-direction
metadata, so this is a declared "unknown," never a guessed `I`/`O`.

**Pair `--spef` with `--pins` whenever you also pass `--def-net-names`
(issue #961).** Flat extraction promotes *every named* net to a top-level
pin, and `--def-net-names` names every routed net — so together they declare
the design's ordinary internal nets (`_019_`, …) as `*PORTS`/`*P` entries, a
claim a reading STA tool will try, and fail, to resolve against its own
port list. `--pins` (issue #514) is the existing per-net declared-interface
restriction: every promoted pin outside the set is demoted back to an
internal net, and only real design ports survive into `*PORTS`. On the
routed `gcd` corpus fixture this is 463 declared ports before, 54 after,
with the `*D_NET` set unchanged. `klt place-and-route`'s own
`post_route_spef` pass does this automatically, sourcing the port list from
the routed DEF's `PINS` section — see
[`docs/cli/place-and-route.md`](place-and-route.md)'s "`*PORTS` lists only
real design ports" subsection.

**Identifiers are escaped, and that is load-bearing — with one deliberate
exception.** SPEF's own (IEEE 1481-1999) identifier grammar admits only
`[A-Za-z0-9_]` bare; every other character is a *special character* that
must carry a leading backslash. KLayout's extracted names hit that
constantly — an unlabelled net is named `$<n>`, a net carrying several
layout labels is named by joining them with `|`, and a bussed pin label
keeps its `[`/`]` — so every identifier this writer emits (`*PORTS`
entries, `*D_NET` names, `*CONN`/`*P` entries, and each non-port
`*CAP`/`*RES` node) is escaped: `$1009` → `\$1009`, `A|A2|Y` → `A\|A2\|Y`,
`a_in[13]` (as a `*D_NET`/`*PORTS`/`*P` name) → `a_in\[13\]`. Without it a
real OpenSTA `read_spef` aborts on the *first* `*D_NET` line
(`[ERROR STA-1670] … syntax error`, reproduced on the routed `gcd` corpus
fixture against `openroad/orfs:latest`); with it the identical file parses
cleanly. Reading tools strip the backslashes back off for *net*-name
matching, so the name matched against a netlist's own nets is the unescaped
one either way.

**The one exception: a unique-named port's own bare-name `*RES`/`*CAP` node
reference leaves `[`/`]` un-escaped** (see "Every two-terminal `*RES`/
coupling `*CAP` endpoint" above) — `a_in[13]` as that specific node, not
`a_in\[13\]`, live-verified as the one shape OpenSTA's *pin*-lookup path
(as opposed to the net-name-matching path the previous paragraph covers)
actually resolves for a bus-indexed port. Every other identifier position
for that same net (`*D_NET a_in\[13\] …`, `*PORTS`/`*P a_in\[13\] B`) still
escapes it — the exception is narrowly scoped to the specific `*RES`/`*CAP`
node text, not the net's identity everywhere else in the file.

**Duplicate net names are a known, inherited limitation**, not something
this writer resolves: a layout label shared by several distinct, un-strapped
net islands (e.g. the `gcd` corpus's 105 separate `VGND` islands, see
"Coverage" above) emits one independent `*D_NET` block per island under the
identical name — SPEF's own net-name-keyed grammar has no per-island
qualifier the way this response's own `net_id` field does. Immaterial for
genuine signal nets (never un-strapped this way in practice).

Additive field: `spef_path` (top-level, alongside `netlist_path`) — the
resolved SPEF path, or `null` when `--spef` was omitted (byte-identical to
before this feature existed).

### DEF-derived net names (`--def-net-names`, issue #951, Epic #700 Phase 3)

`--def-net-names` names each routed net from the **DEF net name** rather
than from GDS text labels.

**Why it exists.** Extraction's default naming source is text labels
(see "Coverage" above). On a routed GDS produced by
[`klt place-and-route`](place-and-route.md), the DEF→GDS merge emits label
texts for **top-level pins only** — 52 of them on the `gcd` corpus fixture —
so every *internal* routed net is named either by KLayout's synthesized
`$<id>` placeholder or by joining whatever standard-cell pin labels happen to
touch it (`A,X`). Neither is what the design calls that net (`_019_`,
`req_msg[3]`, …), so the `--spef` file above named nothing an STA tool could
resolve: correlation measured **0 of 981 / 1904 / 449** nets on
`gcd`/`modexp`/`mult8` when `--spef` shipped.

**Where the real names come from.** KLayout's LEF/DEF reader records each
routed-net shape's DEF net name as a **GDS shape property** —
`LEFDEFReaderConfiguration.net_property_name`, whose default is property
`1` — and GDS `PROPATTR`/`PROPVALUE` round-trips it. A routed GDS therefore
already carries the design's own net names; nothing about the artifact
needed to change to recover them. (The committed
`tests/corpus/place_and_route/gcd.gds.gz` fixture carries all **458** of
that design's DEF net names on its routed metal — which is why closing this
gap required no fixture regeneration.) This flag reads that property off the
routed-metal shapes, resolves each name to its extracted net, and renames
it, overriding the label-derived name.

```
klt extract routed.gds --deck sky130 --def-net-names \
  --parasitics --spef routed.spef --format json
```

**Opt-in, deliberately.** Property `1` carries no guaranteed meaning in a
GDS that did *not* come from a LEF/DEF merge, so this is never inferred:
without the flag, every layout's output is byte-identical to before the flag
existed. `klt place-and-route`'s own `post_route_spef` path passes it
internally, since it knows its GDS came from its own DEF→GDS merge.

**Never a silent no-op.** A run that opts in and finds no such property on
any routed-metal shape says so in `warnings` ("`--def-net-names` found no DEF
net-name shape property (1) …"), rather than quietly returning
default-named output that a caller might read as DEF-named. A run where some
DEF names resolve to no extracted net (geometry the deck's connectivity
graph does not join) reports those names too.

**One net per name.** A DEF net that extraction splits into several
electrically disconnected islands names one island and leaves the rest
alone, rather than minting duplicate net names into the SPICE/SPEF output —
the same posture as the duplicate-net-name limitation noted above.

### What it does *not* do

- **Lateral (same-layer, sidewall) coupling capacitance and fringe
  shielding** are out of scope. Two wires running side by side on the *same*
  level couple to each other in the real layout; this model gives that
  exactly zero, and additionally charges each of their facing edges the full
  isolated-edge `defaultperimeter` fringe term to substrate as though the
  neighbour were not there. Both need spacing-aware neighbour geometry (a
  halo search) that the crossover pass below deliberately does not do. They
  are Stage 2b/2c of `docs/design/extract-fidelity-roadmap.md`, named
  follow-ons rather than open-ended future work. This scope is self-declared
  in the output — see "Parasitic model scope (`parasitics.model`)" below
  (issue #728).

  *Vertical* (crossover) coupling **is** modelled as of issue #760 — see
  "Vertical-overlap coupling capacitance" below.
- **Device connectivity, as reported in `devices[]`/`nets[]`, is untouched.**
  Those two fields are built from the schematic-equivalent netlist *before*
  parasitics injection and carry their exact documented meaning whether or
  not `--parasitics` was given — no existing device instance is removed, no
  net name a caller already sees is renamed, and no pin is added or dropped.
  The netlist stays a drop-in `klt sim` `netlist`. This is **not** a claim
  that the injected SPICE leaves each device's own terminal-to-node wiring
  untouched (see below — as of issue #592 it does not, by design).
- **Per-terminal resistance is coarse, not a true routing measurement
  (issue #592).** The star topology above puts real, non-zero resistance
  in series between any two terminals on the same net — the practical gap
  #338 first documented (a driver-to-receiver or receiver-to-receiver path
  through a shared net previously carried exactly zero series resistance,
  regardless of the net's drawn geometry). What it does *not* do is
  approximate the *routed path* between two specific terminals: each leg's
  resistance is the net's total computed R weighted by one coarse per-device
  position (`Device.trans`, not a true terminal- or segment-level location),
  not a measurement of the actual copper between those two points. A full
  distributed, per-segment RC ladder (the standard PEX-style network) is a
  strictly more accurate but substantially larger undertaking, deliberately
  deferred as issue #592's own "Option 2".

### Known gap: an anonymous PMOS body net has no DC bias path (issue #555)

This is a **simulation**, not just an LVS-comparison, caveat — read it before
resimulating a `--parasitics`-extracted netlist directly, not only if you are
comparing it against a schematic reference.

"Coverage" above documents when this happens: a PMOS device's body terminal
extracts onto a **floating, anonymous net** (a KLayout-synthesized `$5`-style
name) rather than a real, named net whenever no well tie — drawn (sky130's
`tap.drawing`) or derived (gf180mcu's `tap_nplus`/`tap_pplus`, issue #1084) —
reaches that device's `nwell` island. On **gf180mcu**, before #1084 this hit
*every* PMOS unconditionally (no tap mechanism existed at all); since #1084 a
layout that draws an `Nplus`-covered `Comp` well tie, contacted to a named
net, resolves the PMOS body to that real net instead — a layout with no such
tie still hits this gap exactly as before. This is unlike the NMOS body,
which always resolves to the deck's synthesized global substrate net
(`vsubs`) via `connect_global` absent a drawn/derived substrate tie, and
unlike a PMOS body reached by a real well tie (sky130's drawn `tap.drawing` +
`well_label` pin, e.g. `VPB`, or gf180mcu's derived `tap_nplus` tie), which
resolves to that real net instead of this floating one.

The consequence for `--parasitics` (and for any direct resimulation of the
extracted netlist, with or without `--parasitics`): that anonymous net has
**no DC bias path at all** — not even the substrate/ground shunt every other
net gets. For a real PDK MOS subcircuit model (which implements its own
body-diode network, not a bare terminal), the body node's DC operating point
is left to float to whatever the source/drain-body junction diodes balance
to, rather than sitting at the real supply rail (e.g. `vdd`) every real
single-well gf180mcu design ties its PMOS wells to. Concretely: a lone PMOS
with its source driven to 0 V, resimulated directly against the real
gf180mcu model library, comes back with its anonymous body net at ~0 V DC,
not `vdd` — a full supply rail's worth of `Vsb` error, which moves every
device's threshold voltage (body effect) and, at the wrong bias point, can
forward-bias a source/drain-body junction outright. This makes a full-circuit
resimulation of an extracted gf180mcu netlist with more than one PMOS
source/drain voltage **physically wrong**, not merely imprecise — the
netlist "simulates" (ngspice converges) but the numbers it produces are not
comparable to a schematic-level netlist's.

The anonymous net's synthesized name is not left to grepping the written
SPICE body for a `$`-prefixed node: it is surfaced structurally in the JSON
response two ways —

- Every affected PMOS device's `devices[].nets["b"]` already carries the
  exact net name (e.g. `"\$5"`, backslash-escaped per "Anonymous nets are
  backslash-escaped" above, issue #1162), same as any other terminal.
- The top-level `unbiased_pmos_body_nets[]` array (see "JSON schema" below)
  flags it explicitly, one `{"device", "net"}` entry per affected PMOS
  device, plus a single aggregate prose `warnings[]` entry with the affected
  device count baked in (e.g. `"148 PMOS devices tie their body to an
  anonymous net with no DC bias path..."`, issue #599) — not one line per
  device, so `warnings[]` does not scale with the device count on a large
  design — so a caller does not have to independently discover KLayout's
  `$<n>` anonymous-net convention to detect the gap. Present (and non-empty
  when applicable) whether or not `--parasitics` was given, since the
  DC-bias gap exists either way.

**This issue does not re-bias the net.** No device-physics change is made:
the anonymous net's connectivity, and the fact that it carries no bias, are
unchanged — this is a *reporting* fix, not a fix to the underlying gap. An
opt-in flag that would actually tie the anonymous net to a named rail at
extraction time (e.g. a `--tie-well-to=<net>`-style hint, mirroring how
`klt lvs`'s `hints` accommodate deck-coverage gaps for LVS-comparison) is a
**known, deliberately deferred follow-up** — a real API design decision
(new CLI flag semantics plus extraction-time net-merging logic) intentionally
out of scope here. File a follow-up issue if your workflow needs the net
actually re-biased rather than merely flagged.

### Single-device-terminal nets (issue #596)

A net that touches exactly one device terminal — most often an MOS gate with
no driver — has no DC path from anywhere else in the netlist. Nothing about
this is invisible in the layout: it is DRC-clean, and `klt lvs` can report
`status: "match"` against a reference that carries the identical defect (a
generator-driven flow that draws the layout and writes the reference from the
same buggy source draws the same unconnected net on both sides). The failure
only shows up several stages downstream, when a transient solver reports
`singular matrix: check node <net>` against an anonymous node name — or,
worse, converges to a self-consistent but physically wrong operating point.

`klt extract --format json` detects this directly from the already-extracted
netlist: every `nets[]` entry already carries `device_count`
(`Net.terminal_count()`) and `pin` (see "JSON schema" below), so "exactly one
device terminal, not a declared pin" is a structural condition, not a
heuristic. Surfaced two ways:

- The top-level `single_terminal_nets[]` array (see "JSON schema" below)
  flags every match, `{"net", "device", "terminal", "terminal_kind"}` — one
  entry per affected net, naming the exact device and terminal that owns it.
- Up to two aggregate prose `warnings[]` entries, each with its bucket's
  count baked in (issue #599 — not one line per net, so `warnings[]` does
  not scale with the affected net count on a large design): one for every
  `"gate"` hit combined, phrased as "almost certainly an unconnected input",
  since an undriven MOS gate is essentially never intentional; and one for
  every other terminal kind combined (`"source"`/`"drain"`/`"body"`, or the
  literal terminal letter for a drawn resistor/capacitor/diode/bipolar
  device), phrased as a weaker "confirm these nets have no other intended
  connectivity", since a single-terminal source/drain/body tie can be a
  legitimate deliberately-unterminated dummy's diffusion tie.

`terminal_kind` is derived from the owning device's own terminal set: a
device with a `"g"` terminal (the one terminal name no other recognised
device class in this repo's decks uses) is treated as MOS-like, and its
`"g"`/`"s"`/`"d"`/`"b"` terminals map to `"gate"`/`"source"`/`"drain"`/
`"body"`; every other device (drawn resistor/capacitor `"a"`/`"b"`/`"w"`,
diode `"a"`/`"c"`, bipolar `"c"`/`"b"`/`"e"`) reports the literal terminal
key verbatim.

**Known non-detection**: this only flags a net with **exactly one** device
terminal. A gate driven by nothing but tied to one *other* gate (two
floating gates shorted together, `device_count == 2`) is not detected —
that pattern still passes `klt drc` and this diagnostic alike, since neither
tool traces DC bias paths transitively across the whole netlist.

### Dead metal (issue #676)

Geometry on the deck's routing stack — its `metals` and `vias` levels — that
joins **no** extracted net. No via lands on it, no same-layer wiring touches
it, so nothing in `nets[]` mentions it and it is invisible to this report, to
`klt lvs`, and to a resimulation of the written netlist. The only way to find
it before this diagnostic existed was to render the raw geometry and eyeball
it against the extracted nets.

It cuts both ways, which is why it is reported rather than warned about
quietly:

- **Deliberate** dead metal is common and legitimate — artwork, a logo, a
  fill or density trick, a bond-pad blank. A reviewer should be *told* that
  non-functional geometry is present, not have to discover it.
- **Accidental** dead metal is the symptom of a connection you meant to make
  and did not (or of an extraction that quietly dropped it): a routing stub
  that never reached its via, an island left behind by an edit.

Surfaced two ways:

- The top-level `dead_metal[]` array (see "JSON schema" below), one entry per
  connected **cluster** — not per drawn polygon — with the layer, bounding
  box, drawn-shape count and area, so a human can go look at it.
- A single aggregate prose `warnings[]` entry with the cluster and shape
  counts baked in (issue #599 — not one line per cluster, so `warnings[]`
  does not scale with the amount of dead geometry).

**XY overlap between adjacent layers is not connection.** The netted side of
the comparison comes from the extracted connectivity graph, which joins two
metal levels only through a declared via layer — so a met2 wire passing
directly over a live met1 wire, with no via between them, is still dead metal.
A naive `Region.interacting()` check across adjacent layers gets exactly this
wrong.

**A labelled floating cluster is not dead.** A power strap, seal ring, bond
pad or RDL segment that carries a pin label survives extraction as a real,
named, pinned net (see "Device-free labelled nets" behaviour in issue #539) —
its geometry *does* join an extracted net, so it never appears here, however
few devices it touches.

**Known interaction — dummy devices.** A device suppressed by the deck's
`dummy` marker (see "Dummy devices: the `dummy` marker layer") produces no
device, so its source/drain routing joins no net unless the layout ties it
off to a rail. A dummy left deliberately untied therefore *does* show up here
— correctly, since that geometry genuinely is absent from the netlist — but
cross-check `dummy_devices_dropped` before treating it as a routing bug.

**Scope: the routing stack only.** Device-forming layers (poly, diffusion,
well, tap) are deliberately out of scope — unnetted geometry there has its own
sharper diagnostics (`unmodelled_poly`, `single_terminal_nets`,
`unbiased_pmos_body_nets`), and the deck's own well/substrate regions are not
"routed" in a sense this comparison could judge. Geometry on a layer the deck
does not read at all is a different failure class again — see
`ignored_layers` above.

### Substrate DC reference (issue #1263)

Every ground capacitor `--parasitics` injects hangs off the deck's
`substrate_net` (`vsubs` for both sky130 and gf180mcu). That net is
*synthesized* — KLayout's `connect_global` mints it, and any
`<substrate_net>_iso<n>` per-isolated-region variant (see
"Per-isolated-region NMOS substrate scoping"), out of nothing; no layout can
draw a label for it. So
nothing in the layout gives it a defined DC value, and a testbench that
`.include`s the extracted file and `X`-instantiates its `.SUBCKT` (the
convention documented under "Verified compatible with `klt sim`'s netlist
convention" below) used to hand ngspice a node whose only connections were
capacitors:

```
Warning: singular matrix:  check node xdut.vsubs
Warning: Dynamic gmin stepping failed
Warning: True gmin stepping failed
Warning: source stepping failed
```

ngspice's gmin/source-stepping recovery sometimes still returns *a* number,
but not a reproducible one — so the failure mode was a silently
untrustworthy post-layout result rather than a hard error. Being a promoted
pin did not help: `make_top_level_pins()` promotes the substrate net like
any other named net, but a pin wired to an equally-undriven node in the
caller's testbench floats just the same.

**`--parasitics` now writes the tie itself.** For every synthesized
substrate identity in the extraction — the deck's `substrate_net` *and*
each `_iso<n>` variant — one large shunt resistor to SPICE's global ground
node `0` is emitted **inside** the `.SUBCKT` body:

```spice
.SUBCKT sky130_fd_sc_hd__inv_1 A VGND VPB VPWR Y vsubs
...
CY Y vsubs 2.3966e-16
Rvsubs_dctie vsubs 0 1e+12
.ENDS sky130_fd_sc_hd__inv_1
```

Design notes, and what this does *not* change:

- **Node `0`, not `.global`.** Node `0` is SPICE's global ground: it names
  the same node inside a subcircuit body as at the top level, with no
  `.global` card (conventionally a top-level card, which an extracted
  circuit body must not emit) and no cooperation from the instantiating
  testbench. The tie therefore travels with the extracted artifact and
  behaves identically whether the file came from `klt extract --parasitics`
  directly or from [`klt pex`](pex.md)'s own orchestration.
- **The pin interface is untouched.** No pin is created, promoted, or
  demoted; `pin_count`, the `.SUBCKT` header, `net_count`, `device_count`,
  `devices[]` and `nets[]` are all byte-identical to what they were before.
  In particular `klt pex`'s `pin_count_mismatch` / `flat_dut_mismatch`
  diagnostics read exactly the same interface they always did.
- **It is idempotent with a hand-authored tie.** A testbench that already
  supplies `.global vsubs` + `Vsubs vsubs 0 DC 0` (or ngspice's blanket
  `.options rshunt=1e12`) keeps working unchanged — 1 Tohm in parallel with
  an ideal source draws ~1.8 pA at a 1.8 V rail, and the card lives in the
  subcircuit's own namespace so there is no instance-name collision.
  Existing testbenches do **not** need to remove their workaround.
- **It is harmless where nothing floats.** On a node with a real DC path a
  1 Tohm leak is below every simulator tolerance — the same property that
  makes ngspice's own `.options rshunt` safe. Verified by
  `tests/test_extract.py::test_parasitics_substrate_dc_tie_is_harmless_where_nothing_floats`,
  which A/Bs the identical extraction with and without the card.
- **It is a DC anchor, not an AC ground.** 1 Tohm against a fraction of a
  femtofarad is an RC time constant of ~0.1 s, so within any realistic
  transient the substrate node still *floats in AC*. A measurement that
  needs `vsubs` held at a real 0 V reference (e.g. any coupling-capacitance
  measurement, where a floating substrate node would create its own
  aggressor→victim path through two series capacitors) must still tie it
  explicitly in the testbench — `Vvsubs vsubs 0 DC 0` on the promoted pin,
  as this page's own examples do.
- **It is not a parasitic.** `parasitics.r_count` /
  `parasitics.total_resistance_ohm` count *extracted* resistance only and do
  not include these cards; they are reported separately in
  `parasitics.substrate_dc_tie` (below). A consumer that cross-checks
  "`R` cards written" against `r_count` must exclude the devices named
  there.

### JSON `parasitics` block

`--parasitics` adds a top-level `parasitics` field (an additive, independently
optional field — `null` when the flag is omitted). The existing
`device_count` / `net_count` / `devices[]` / `nets[]` keep their exact
schematic-equivalent meaning whether or not the flag is given; the parasitic
R/C are **not** counted as devices and the internal/leg nodes are **not**
counted as nets.

**Breaking shape change, `schema_version` 1 -> 2 (issue #592):** each
`nets[]` entry's `internal_node` field is replaced by `hub_net` and a new
`terminals[]` array, and `r_count` now counts every emitted resistor (one or
more per net) rather than always equalling `c_count`. See
`docs/json-contract.md`.

**Additive fields, no `schema_version` bump (issue #760):**
`nets[].coupled[]`, `cc_count`, `total_coupling_capacitance_ff`, and
`overlap_pairs_without_coefficient[]` are new; no documented field is renamed
or retyped, and `c_count` keeps its meaning. The `model.coupling` *value*
changes (it no longer reads `"not modelled"`) — an additive behavior change
per `docs/json-contract.md`'s pre-1.0 caveat, recorded in `CHANGELOG.md`
rather than versioned, the same treatment issue #547's material R/C value
change got. Values of `nets[].capacitance_ff` and `total_capacitance_ff` move
(crossover charge relocates to the new coupling term) without their
definitions changing.

**Additive field, no `schema_version` bump (issue #798):** `mom_crosscheck`
is new (`null` unless `--mom-net` was given). When `--mom-net` names a net,
that net's `capacitance_ff` (in both `nets[]` and the written SPICE, and
therefore also in `total_capacitance_ff`) becomes the `klt mom`-solved value
instead of the lumped-RC one — a material value change, but strictly
opt-in and per-net, and recorded in `CHANGELOG.md` per the same
pre-1.0-caveat precedent issue #547's R/C value change and issue #760's
`model.coupling` value change already established. `--mom-net` omitted (the
default) leaves every field byte-identical to before this feature existed.

**Additive field, no `schema_version` bump (issue #811):**
`mom_crosscheck.net_id` is new — it names the net *object* the solve
measured, so a `--mom-net` label shared by several distinct islands is no
longer ambiguous in the report. No documented field is renamed or retyped,
and single-island nets (every `--mom-net` value with one match) report
exactly the same numbers as before.

**Additive fields, no `schema_version` bump (issue #976):** `critical_nets`
and `nets[].coupled[].lateral_levels` are new; no documented field is
renamed or retyped. `--critical-net` omitted (the default) leaves every
field byte-identical to before this feature existed — `critical_nets` is
simply `[]` and every `lateral_levels` is `[]`. The `model.coupling` *value*
changes again (it now also describes the `--critical-net`-scoped lateral
case), the same "additive behavior change, recorded in `CHANGELOG.md`"
treatment issue #760's own `model.coupling` change got.

**Additive fields, no `schema_version` bump (issue #977):**
`distributed_rc` and `nets[].rc_model`/`nets[].segments[]` are new; no
documented field is renamed or retyped. `--distributed-rc` omitted (the
default) leaves every field byte-identical to before this feature existed —
`distributed_rc` is simply `false`, every `rc_model` is `"lumped"`, and
every `segments` is `[]`. **Given**, a distributed net's own
`nets[].terminals[]` entry *shape* changes (see "Distributed (multi-segment)
RC ladder for critical nets" above) — a per-net, per-flag shape difference
rather than a global rename, so `nets[].terminals[]`'s documented meaning is
otherwise unchanged for every non-distributed net (still the overwhelming
majority of any run). The `model.resistance` *value* changes (it now also
describes the `--distributed-rc` ladder case), the same "additive behavior
change, recorded in `CHANGELOG.md`" treatment `model.coupling`'s own changes
got.

**Additive fields, no `schema_version` bump (issue #988):** `l_count`,
`total_inductance_nh`, `nets[].inductance_nh`, and `mom_rlc_override` are
new; no documented field is renamed or retyped. `--mom-rlc-net` omitted (the
default) leaves every field byte-identical to before this feature existed —
`l_count` is simply `0`, `total_inductance_nh` is `0.0`, every
`nets[].inductance_nh` is `0.0`, and `mom_rlc_override` is `null`. **Given**
`--mom-rlc-resistance-ohm`/`--mom-rlc-capacitance-ff`, the named net's
`resistance_ohm`/`capacitance_ff` (in `nets[]`, the written SPICE, and
therefore also `total_resistance_ohm`/`total_capacitance_ff`) become the
substituted values instead of the lumped-RC ones — a material value change,
but strictly opt-in and net-scoped, recorded in `CHANGELOG.md` per the same
pre-1.0-caveat precedent issue #798's own `--mom-net` swap already
established.

**Additive field, no `schema_version` bump (issue #1263):**
`substrate_dc_tie` is new — no documented field is renamed or retyped, and
every existing count/total keeps its exact meaning (the tie cards are *not*
counted in `r_count`/`total_resistance_ohm`). The written SPICE gains one
`R<net>_dctie <net> 0 1e+12` card per synthesized substrate net inside the
`.SUBCKT` body — an additive behavior change recorded in `CHANGELOG.md`
rather than versioned, per the same pre-1.0 caveat issue #547's R/C value
change established. See "Substrate DC reference" above.

```json
"parasitics": {
  "r_count": 6,
  "c_count": 4,
  "cc_count": 1,
  "l_count": 0,
  "total_resistance_ohm": 3050.7818,
  "total_capacitance_ff": 5.400116,
  "total_coupling_capacitance_ff": 0.171301,
  "total_inductance_nh": 0.0,
  "nets": [
    {
      "net": "Y",
      "net_id": 42,
      "resistance_ohm": 1169.7827,
      "capacitance_ff": 1.910204,
      "inductance_nh": 0.0,
      "hub_net": "Y",
      "terminals": [
        {
          "device": "$1",
          "terminal": "D",
          "leg_net": "Y__t0",
          "resistance_ohm": 526.291
        },
        {
          "device": "$2",
          "terminal": "D",
          "leg_net": "Y__t1",
          "resistance_ohm": 643.4917
        }
      ],
      "coupled": [
        {
          "net": "A",
          "capacitance_ff": 0.171301,
          "levels": [[0, 1]],
          "lateral_levels": []
        }
      ]
    }
  ],
  "metals_without_coefficient": [],
  "overlap_pairs_without_coefficient": [],
  "critical_nets": [],
  "distributed_rc": false,
  "mom_rlc_override": null,
  "substrate_dc_tie": {
    "resistance_ohm": 1000000000000.0,
    "nets": [{ "net": "vsubs", "device": "Rvsubs_dctie" }]
  },
  "model": {
    "capacitance": "net-to-ground for every net's own (non-coupled) area/perimeter, plus net-to-net for the vertical-overlap coupling `coupling` describes below -- a coupled net pair gets a direct capacitor between their two hub nodes, not just capacitors to the deck's ground/substrate net",
    "coupling": "vertical overlap (crossover) unconditionally -- where one net's conductor on an adjacent metal level sits directly over another *distinct* net's conductor, that overlap area is charged between the two nets instead of to ground (issue #760) -- plus lateral (same-layer, sidewall) coupling, but only for a net pair naming one of the caller's declared `--critical-net` nets (issue #976): facing-edge length within that layer's own minimum-spacing lookback is charged between the two nets, *additively* (not deducted from either net's substrate fringe term, unlike the vertical case -- a known simplification). Any same-layer pair with neither side named `--critical-net`, and fringe shielding in general, are still not modelled",
    "resistance": "single lumped series resistance per net, distributed as a star across that net's device terminals (issue #592) -- not a per-segment, distributed RC ladder, *unless* `--distributed-rc` names this net via `--critical-net` (issue #977, Epic #709 Phase 2b): then the net's terminals are ordered along their approximate physical spread and its total R/C is broken into a chain of per-segment resistors (segment length proportional to inter-terminal distance) with a ground capacitor at each terminal node (proportional to its adjacent segment length/2), instead of one star hub -- still an approximation (terminal position is a device-placement proxy, not true per-segment routing geometry), but a strictly finer-grained one than the single-hub star",
    "frequency": "quasi-static -- one frequency-independent R and C per net; no skin effect, no distributed transmission-line behavior"
  }
}
```

| Field                  | Type            | Description                                                                                     |
| ---------------------- | --------------- | ------------------------------------------------------------------------------------------------- |
| `r_count`              | integer         | Total number of parasitic resistors emitted across every net — one per `terminals[]` entry (star topology, issue #592), or exactly one for a net with no device terminal (the pre-#592 Γ-shunt fallback). `>= c_count` whenever any net has more than one terminal. Does **not** include the substrate DC-tie resistors (issue #1263) — those are counted in `substrate_dc_tie.nets[]` instead. |
| `c_count`              | integer         | Number of **ground** capacitors emitted — always one per `nets[]` entry, unchanged since issue #216. Coupling capacitors are *not* counted here; see `cc_count`. |
| `cc_count`             | integer         | Number of **coupling** capacitors emitted (issue #760) — one per distinct coupled net pair. `0` when the layout has no inter-net vertical overlap, or the deck curates no overlap coefficients. |
| `l_count`              | integer         | Additive field (issue #988). Number of series inductors emitted — `0` unless `--mom-rlc-net`/`--mom-rlc-inductance-nh` was given, in which case `1` (this feature adds at most one net's worth). |
| `total_resistance_ohm` | number          | Sum of each net's *total* series resistance (ohms) — the same value as summing `nets[].resistance_ohm`, not `nets[].terminals[].resistance_ohm` (which sums back to the same per-net total, modulo the negligible per-leg minimum-resistance clamp). |
| `total_capacitance_ff` | number          | Sum of the emitted ground capacitances (femtofarads).                                             |
| `total_coupling_capacitance_ff` | number | Sum of the emitted coupling capacitances (femtofarads), each pair counted **once** (issue #760). Summing `nets[].coupled[].capacitance_ff` instead double-counts, since every pair is reported from both endpoints. |
| `total_inductance_nh`  | number          | Additive field (issue #988). Sum of `nets[].inductance_nh` (nanohenries) — `0.0` unless `--mom-rlc-net`/`--mom-rlc-inductance-nh` was given. |
| `nets`                 | array\<object\> | One entry per net carrying parasitics, sorted by `net` for deterministic output. See below.       |
| `metals_without_coefficient` | array\<object\> | Metal stack levels the deck declares for connectivity but its `PARASITICS.metals` table has no coefficient for (issue #547). See below. Empty for both shipped decks. |
| `overlap_pairs_without_coefficient` | array\<object\> | Adjacent metal-level pairs the deck declares but its `PARASITICS.metal_overlaps` table has no vertical-overlap coefficient for (issue #760). See below. Empty for both shipped decks. |
| `critical_nets`        | array\<string\> | The `--critical-net` request, echoed back verbatim (issue #976). `[]` when the flag was never given. |
| `distributed_rc`       | boolean         | `true` only when `--distributed-rc` was given (issue #977). `false` otherwise, `--critical-net`-only runs included. |
| `model`                | object          | Machine-readable declaration of the parasitic model's own scope — static text, the same regardless of the file/deck (issue #728). See "Parasitic model scope (`parasitics.model`)" below. |
| `mom_crosscheck`       | object \| null  | Additive field (issue #798). `null` unless `--mom-net <net>` was given, in which case it is the swap-and-measure report for that one net — see "`klt mom` cross-check for one net" above and the field list below. |
| `mom_rlc_override`     | object \| null  | Additive field (issue #988). `null` unless `--mom-rlc-net <net>` was given, in which case it is the substitution report for that one net — see "Substitute a caller-supplied `klt mom` R/L/C for a critical net" above and the field list below. |
| `substrate_dc_tie`     | object          | Additive field (issue #1263). The DC reference written for every synthesized substrate net — see "Substrate DC reference" above and the field list below. Always present (never `null`); `nets` is `[]` when this extraction synthesized no substrate identity at all. |

`substrate_dc_tie`:

| Field | Type | Description |
|---|---|---|
| `resistance_ohm` | number | The shunt resistance used for every tie card (`1e12`). Fixed, not deck- or net-dependent. |
| `nets` | array\<object\> | One entry per tied net, sorted by net name: `{"net", "device"}` — `net` is the substrate net's node name exactly as the written netlist spells it (the deck's `substrate_net`, or a `<substrate_net>_iso<n>` variant), and `device` is the emitted card's full instance name (e.g. `"Rvsubs_dctie"`), so a consumer cross-checking written `R` cards against `r_count` can exclude them by name. |

`mom_crosscheck` (present only when `--mom-net` was given):

| Field | Type | Description |
|---|---|---|
| `net` | string | The `--mom-net` value, echoed back. |
| `net_id` | integer | Additive field (issue #811). Which net *object* was solved — the same `net_id` (KLayout's `Net.cluster_id`) the matching `nets[]` entry carries. `net` alone does not identify one: a label shared by several un-strapped islands resolves to the lowest-`net_id` island, and this field says which. |
| `lumped_rc_capacitance_ff` | number | The net's ground capacitance **before** the swap — what `--parasitics` alone would have reported. |
| `mom_capacitance_ff` | number | The `klt mom`-solved value — also what `nets[].capacitance_ff` for this net (and the written SPICE `C` card) now carry. |
| `delta_ff` | number | `mom_capacitance_ff - lumped_rc_capacitance_ff`. |
| `delta_pct` | number \| null | `100 * delta_ff / lumped_rc_capacitance_ff`; `null` in the degenerate case `lumped_rc_capacitance_ff == 0.0`. |
| `background_permittivity` | number | The relative permittivity assumed for the z-gap derivation (3.9, SiO₂ — see the method description above). |
| `panel_size_um` | number | The discretisation panel size the solve used. |
| `panel_count` | integer | Total panel count across both synthesized conductors (informational, mirrors `klt mom`'s own `panel_count`). |
| `ground_pad_factor` | number | How far beyond each net shape's own bbox the synthesized ground plate extends, as a multiple of that shape's implied z-gap (`3.0` — see the method description above). |
| `method` | string | One-line prose description of the cross-check method, for a human reading the JSON without this doc open. |
| `warnings` | array\<string\> | Cross-check-specific caveats (always includes the metals-only scope note above); also mirrored into the top-level `warnings[]`. |

`mom_rlc_override` (present only when `--mom-rlc-net` was given):

| Field | Type | Description |
|---|---|---|
| `net` | string | The `--mom-rlc-net` value, echoed back. |
| `matched_net_count` | integer | How many net *objects* sharing this net name were substituted (usually `1`; `> 1` only for un-strapped same-labelled islands). |
| `previous_resistance_ohm` | number | The sum of the matched net object(s)' series resistance **before** the swap — what `--parasitics` alone would have reported. |
| `previous_capacitance_ff` | number | The sum of the matched net object(s)' ground capacitance **before** the swap. |
| `resistance_ohm` | number \| null | The `--mom-rlc-resistance-ohm` value, echoed back — `null` when that flag was omitted (this component kept its lumped-RC value). |
| `capacitance_ff` | number \| null | The `--mom-rlc-capacitance-ff` value, echoed back — `null` when that flag was omitted. |
| `inductance_nh` | number \| null | The `--mom-rlc-inductance-nh` value, echoed back — `null` when that flag was omitted (no inductor was added). |
| `method` | string | One-line prose description of the substitution, for a human reading the JSON without this doc open. |

Each `nets[]` entry:

| Field            | Type            | Description                                                                                          |
| ---------------- | --------------- | ------------------------------------------------------------------------------------------------------ |
| `net`            | string          | The schematic-equivalent net name. **Not guaranteed unique across `nets[]`** — two electrically distinct nets can carry the identical layout label (e.g. separate un-strapped `VGND` islands nothing straps together); use `net_id` to disambiguate (issue #765). |
| `net_id`         | integer         | Additive field (issue #765). A stable identifier, unique across every entry in this response, that disambiguates same-named entries — the net object's own KLayout cluster id. Build `{entry["net_id"]: entry}` instead of `{entry["net"]: entry}` if the layout may have same-labelled distinct nets (real layouts routinely do — 105 out of 908 distinct `VGND`/`VPWR`/... labels on the `gcd` corpus). |
| `resistance_ohm` | number          | The net's total computed series resistance (ohms) — the star's total "budget", distributed across `terminals[]`. |
| `capacitance_ff` | number          | The net's total lumped **ground** capacitance (femtofarads), hung off `hub_net` — with any area coupled to another net on an adjacent metal level already removed (issue #760, see `coupled[]`). Can be `0.0` for a net whose whole area term moved to coupling; the entry (and its hub) still exists. |
| `inductance_nh`  | number          | Additive field (issue #988). This net's series inductor (nanohenries) — `0.0` unless `--mom-rlc-net`/`--mom-rlc-inductance-nh` named this net. |
| `hub_net`        | string          | The star's hub node name (or, for a distributed net, the ladder's middle leg — see "Distributed (multi-segment) RC ladder for critical nets" above). Equal to `net` itself whenever the net has at least one device terminal (the common case — the pin/subcircuit connectivity that already lived on `net` stays there, at zero resistance from the hub). Only a fresh `<net>__par`-style node (or a collision-suffixed variant) when the net has **no** device terminal to fan a star out to. |
| `rc_model`       | string          | Additive field (issue #977). `"lumped"` (the star/Gamma-shunt model above, every net unless the next field applies) or `"distributed"` (the `--distributed-rc` ladder — see "Distributed (multi-segment) RC ladder for critical nets" above). |
| `segments`       | array\<object\> | Additive field (issue #977). The ladder's per-segment resistors, in node order: `{"net_a", "net_b", "resistance_ohm"}`. `[]` unless `rc_model == "distributed"`. |
| `terminals`      | array\<object\> | One entry per device terminal moved onto its own leg net. For `rc_model == "lumped"`: `{"device", "terminal", "leg_net", "resistance_ohm"}` — `device` is the owning device's `expanded_name()`, `terminal` its terminal name (e.g. `"D"`, `"G"`, `"A"`), `leg_net` the fresh internal node that terminal now connects to (`<net>__t<i>`, or a collision-suffixed variant), and `resistance_ohm` that leg's own series resistance back to `hub_net`. For `rc_model == "distributed"` the shape differs (issue #977): `{"device", "terminal", "leg_net", "order", "capacitance_ff"}` — `order` is the terminal's 0-based position in the ladder's node sequence and `capacitance_ff` its own per-node ground capacitance; resistance lives on `segments[]` instead (between adjacent nodes), not per terminal. **Empty** for the no-device-terminal fallback case above. |
| `coupled`        | array\<object\> | This net's vertical-overlap coupling counterparts (issue #760), `{"net", "capacitance_ff", "levels"}`, sorted by counterpart `net`. `capacitance_ff` is the pair's total coupling capacitance summed over every contributing level pair; `levels` lists the contributing `[lower_metal_index, upper_metal_index]` deck-`metals` index pairs. **Empty** when the net has no inter-net crossover. Each pair appears on both endpoints' lists — see `total_coupling_capacitance_ff`. |

Two device terminals on the same net now sit in series through their two
`terminals[]` legs (`leg_a --Ra--> hub_net <--Rb-- leg_b`) — the
terminal-to-terminal resistance is `Ra + Rb`, strictly positive whenever
both legs are, unlike the pre-#592 topology where it was always exactly
zero.

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

### Curated-coefficient gaps: `overlap_pairs_without_coefficient` (issue #760)

The vertical-overlap coefficient family (`PARASITICS.metal_overlaps`) gets the
same treatment, for the same reason: entry `i` is the coefficient between
`metals[i]` and `metals[i+1]`, so a fully-populated table has `len(metals) - 1`
entries, and a shorter tuple (including the empty-tuple default a deck starts
from) or an explicit `None` entry means that adjacent pair silently
contributes **zero** coupling capacitance. Unlike the `metals` gap, the area
is not lost — it stays charged to ground exactly as it was before this
feature existed — but the crossover between those two levels is invisible in
the output, and a caller reading `model.coupling` would otherwise reasonably
assume it had been accounted for.

`overlap_pairs_without_coefficient` reports one entry per gap,
`{"lower_metal_index", "upper_metal_index", "lower_layer", "lower_datatype",
"upper_layer", "upper_datatype"}` (0-based indices into the deck's `metals`
tuple; pair index 0 is between the bottom two levels), sorted by
`lower_metal_index`. Empty for both shipped decks — each curates one
`defaultoverlap` coefficient per adjacent pair its stack declares — and
trivially empty for a deck with fewer than two metal levels. A non-empty list
is mirrored into top-level `warnings[]` the same way.

### Parasitic model scope (`parasitics.model`)

Before issue #728, nothing in the emitted netlist or JSON declared the
parasitic model's boundaries. A consumer could only infer scope by noticing
that every `C_*` card's second terminal was the same ground net, which reads
back indistinguishable from "the tool looked for coupling here and found
none." As of issue #760 that inference would also be *wrong* — some cards now
connect two signal nets — which is exactly why the declaration is a field
rather than a doc paragraph.

`parasitics.model` states the model's own boundaries machine-readably instead
— a fixed, four-key object present on every `parasitics` block (including the
all-zero case, when no net had eligible interconnect geometry to parasitize):

| Key           | Value (verbatim)                                                                                                                                          |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `capacitance` | `"net-to-ground for every net's own (non-coupled) area/perimeter, plus net-to-net for the vertical-overlap coupling `coupling` describes below -- a coupled net pair gets a direct capacitor between their two hub nodes, not just capacitors to the deck's ground/substrate net"` |
| `coupling`    | `"vertical overlap (crossover) unconditionally -- where one net's conductor on an adjacent metal level sits directly over another *distinct* net's conductor, that overlap area is charged between the two nets instead of to ground (issue #760) -- plus lateral (same-layer, sidewall) coupling, but only for a net pair naming one of the caller's declared `--critical-net` nets (issue #976): facing-edge length within that layer's own minimum-spacing lookback is charged between the two nets, *additively* (not deducted from either net's substrate fringe term, unlike the vertical case -- a known simplification). Any same-layer pair with neither side named `--critical-net`, and fringe shielding in general, are still not modelled"` |
| `resistance`  | `"single lumped series resistance per net, distributed as a star across that net's device terminals (issue #592) -- not a per-segment, distributed RC ladder, *unless* `--distributed-rc` names this net via `--critical-net` (issue #977, Epic #709 Phase 2b): then the net's terminals are ordered along their approximate physical spread and its total R/C is broken into a chain of per-segment resistors (segment length proportional to inter-terminal distance) with a ground capacitor at each terminal node (proportional to its adjacent segment length/2), instead of one star hub -- still an approximation (terminal position is a device-placement proxy, not true per-segment routing geometry), but a strictly finer-grained one than the single-hub star"` |
| `frequency`   | `"quasi-static -- one frequency-independent R and C per net; no skin effect, no distributed transmission-line behavior"`                                       |

The text is static — identical across every deck, cell, and run — so a
downstream flow can assert the exact model it is relying on instead of a human
inferring the model's limits by grepping capacitor terminals. Issue #728
anticipated exactly one use of that: "fail the run if
`parasitics.model.coupling != 'not modelled'`" once a coupling-aware increment
lands. Issue #760 **is** that increment, so such an assertion now starts
firing by design — the value no longer reads `"not modelled"`. A consumer that
wants "no coupling at all" must now either pin the full string or omit
`--parasitics`. The same four lines are also written as `*`-commented
header lines at the top of the SPICE netlist whenever `--parasitics` was
given, immediately after the existing `* extracted by klt extract --deck
<name>` line:

```
* extracted by klt extract --deck sky130
* parasitic model (--parasitics):
* - capacitance: net-to-ground for every net's own (non-coupled) area/perimeter, plus net-to-net for the vertical-overlap coupling `coupling` describes below -- a coupled net pair gets a direct capacitor between their two hub nodes, not just capacitors to the deck's ground/substrate net
* - coupling: vertical overlap (crossover) unconditionally -- where one net's conductor on an adjacent metal level sits directly over another *distinct* net's conductor, that overlap area is charged between the two nets instead of to ground (issue #760) -- plus lateral (same-layer, sidewall) coupling, but only for a net pair naming one of the caller's declared --critical-net nets (issue #976): facing-edge length within that layer's own minimum-spacing lookback is charged between the two nets, additively (not deducted from either net's substrate fringe term, unlike the vertical case -- a known simplification). Any same-layer pair with neither side named --critical-net, and fringe shielding in general, are still not modelled
* - resistance: single lumped series resistance per net, distributed as a star across that net's device terminals (issue #592) -- not a per-segment, distributed RC ladder, *unless* --distributed-rc names this net via --critical-net (issue #977, Epic #709 Phase 2b): then the net's terminals are ordered along their approximate physical spread and its total R/C is broken into a chain of per-segment resistors (segment length proportional to inter-terminal distance) with a ground capacitor at each terminal node (proportional to its adjacent segment length/2), instead of one star hub -- still an approximation (terminal position is a device-placement proxy, not true per-segment routing geometry), but a strictly finer-grained one than the single-hub star
* - frequency: quasi-static -- one frequency-independent R and C per net; no skin effect, no distributed transmission-line behavior
```

so a reader of the raw netlist alone (not just the JSON) can see the model's
scope without cross-referencing `klt extract`'s JSON output. Omitted (no
header lines beyond the existing `* extracted by ...` line) whenever
`--parasitics` was not given, matching `parasitics` itself being `null` in
that case. Declaring the model's scope was item (1) of the near-term half of
the Method-of-Moments epic (#701), laid out in issue #728; issue #760 is the
next item, changing what the model computes rather than only what it declares.
*Lateral* coupling remains "Out of scope" below.

### Parasitic R/C instance names are sanitized, not literal net names

The ground `C` card (one per net) `--parasitics` injects into the written
SPICE is named after that net's own `net` value above (e.g. net `Y` gets a
`CY`), each coupling `C` card (one per coupled pair, issue #760) is named
`Ccc_<net_a>_<net_b>` from the same values (e.g. nets `A`/`Y` get a
`Ccc_A_Y`), and each of its star-leg `R` cards is named the same way with a `_t<i>` suffix
(e.g. `RY_t0`, `RY_t1` — one per `parasitics.nets[].terminals[]` entry; a net
with no device terminal falls back to a single unsuffixed `RY`, matching the
capacitor's naming) — but with every character outside `[A-Za-z0-9_]` mapped
to `_` first (issue #312). This matters because a net's name can carry
characters a SPICE reader treats as syntax rather than an opaque token — `$`
(KLayout's placeholder for an unlabelled/anonymous net, e.g. `$12`) and `|`
(this field's own join character when multiple text labels land on one net,
e.g. `Y|Y2` — see "Merged net labels" above, issue #696). ngspice does not
reject either: it silently splits the joined form into an extra positional
node, corrupting the card's arity and erroring against an unrelated node
instead of a clean syntax error.

The sanitization applies to the **instance name only** — a cosmetic handle
nothing downstream keys off of. The `net` field in the `parasitics.nets[]`
JSON above, the `hub_net` / `terminals[].leg_net` node names, and the
`.SUBCKT` pin interface are all unaffected and already carry the literal net
identity as the written netlist itself spells it (the same `\|`-joined form
KLayout's own `NetlistSpiceWriter` writes for merged-label node syntax, issue
#696, and the same backslash-escaped `\$N` form it writes for an anonymous
net's node syntax, issue #1162 — see "Merged net labels" above and
"Anonymous nets are backslash-escaped" below, respectively). If you need to
map an emitted `R`/`C` card back to the net it parasitizes, use
`parasitics.nets[].net` (or the netlist's own node names on that card), not
the sanitized instance name.

### Anonymous nets are backslash-escaped

KLayout's own `NetlistSpiceWriter` backslash-escapes a leading `$` (`\$2`,
never a bare `$2`) wherever it writes an anonymous, unlabelled net's
`Net.expanded_name()` placeholder as an actual SPICE node reference — a
node token that merely *starts with* `$` is inline-comment syntax to ngspice
(and the wider SPICE3/HSPICE-descended dialect family), so a bare `$2` would
silently truncate the rest of its card. This escaping applies to **net-name
fields only**: `nets[].name`, `devices[].nets[...]`, and
`parasitics.nets[].net`/`.hub_net`/`.terminals[].leg_net` all carry this
same escaped `\$N` spelling for an anonymous net (issue #1162) — matching
the written netlist byte-for-byte, exactly like the `\|`-joined spelling
"Merged net labels" above already carries for a label-merged net. **Not
`devices[].name`** — a synthesized device instance name is never the first
token on its SPICE instance line (always preceded by a class letter like `M$1774`),
so the inline-comment hazard does not apply there; device names remain bare
`$<n>` without backslash escaping. **Before this fix, these JSON fields reported the *bare*, unescaped `$N` form** — a
caller that copied one of these fields verbatim into a hand-authored SPICE
card (a testbench, a `.save`/probe directive, a manual instantiation)
reproduced the exact silent-truncation hazard `NetlistSpiceWriter` itself
already avoids internally. A `--critical-net`/`--distributed-rc`/
`--mom-rlc-net`/`--mom-net` argument naming an anonymous net must use this
same escaped `\$N` spelling (matching what these fields now report) — an
unescaped `$N` argument matches no net.

**Disambiguated across same-labelled nets (issue #765).** Since `net` is not
guaranteed unique (see `net_id` above), two entries can sanitize to the
identical instance-name base — e.g. two un-strapped `VGND` islands both want
`RVGND`/`CVGND`. The first entry to reach a given base name keeps it
unsuffixed; every later entry sharing that base gets a `_dup<n>` suffix
(`RVGND`, `RVGND_dup1`, `RVGND_dup2`, ...), so every emitted `R`/`C` card has
a distinct instance name even when many nets share a label. This suffix is
assigned in `parasitics.nets[]` order (sorted by `(net, net_id)`), so it is
deterministic across runs of the same layout/deck but is **not** meaningful
on its own — use `net_id`, not the `_dup<n>` count, to identify which net an
instance name's card belongs to.

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

**No substrate tie needs hand-authoring (issue #1263).** A `--parasitics`
extraction carries its own DC reference for the deck's synthesized substrate
net(s) — a `R<net>_dctie <net> 0 1e+12` card inside the `.SUBCKT` body — so a
testbench written from scratch around the extracted file no longer has to add
`.global vsubs` / `Vsubs vsubs 0 DC 0` / `.options rshunt=1e12` just to get a
non-singular DC solve. A testbench that *already* has one keeps working
unchanged (the tie is idempotent with it). Two things this does **not**
cover, which are still the testbench's job:

- an **AC** ground for the substrate net — 1 Tohm is a DC anchor, not a low
  impedance, so a measurement whose result depends on the substrate node being
  held at 0 V (any coupling measurement) must still drive it explicitly;
- any *other* floating node — an unconnected gate, a `klt gen-compose`
  generator port never wired through `connectivity[]` — for which ngspice's
  blanket `.options rshunt=1e12` remains the right remedy (see
  `examples/design-pipeline/09-sim.testbench.spice`).

See "Substrate DC reference" under `--parasitics` above for the full rules.

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
  "device_recognition_only_layers": [],
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
      "params": {
        "w_um": 0.65,
        "l_um": 0.15,
        "as_um2": 0.234,
        "ad_um2": 0.234,
        "ps_um": 1.6,
        "pd_um": 1.6
      }
    }
  ],
  "nets": [{ "name": "A", "pin": true, "device_count": 2 }],
  "warnings": [
    "1 layer (55/0) carrying 12 shapes is outside 'sky130' deck's connectivity graph -- this geometry is invisible to extraction, so a net routed only through it extracts as multiple disconnected nets instead of one, which will silently mismatch a downstream `klt lvs` reference netlist -- see ignored_layers[] for the full per-layer shape counts. See docs/cli/extract.md's 'ignored_layers' field documentation."
  ],
  "black_box_regions": [],
  "abstracted_cells": [],
  "unmodelled_poly": [],
  "merged_net_labels": [],
  "voltage_domain_warnings": [],
  "unbiased_pmos_body_nets": [],
  "single_terminal_nets": [],
  "dead_metal": [],
  "pdk": null,
  "parasitics": null,
  "provenance": {
    "klt_version": "0.4.2",
    "klayout_version": "0.29.8",
    "pdk": null,
    "deck": { "name": "sky130", "content_hash": "sha256:<hex>", "released": true },
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
| `ignored_layers`   | array\<object\>            | `(layer, datatype)` pairs carrying shapes in the input stream that this `--deck`'s connectivity graph does **not** read, each `{ "layer": int, "datatype": int, "shapes": int }` with its stream shape count, sorted by `(layer, datatype)`. Empty when every shape-bearing layer is one the deck reads. Geometry on such a layer is invisible to extraction, so a block routed on an undeclared metal level silently extracts as disconnected nets — a non-empty list with a material shape count is the signal that a downstream `klt lvs` mismatch is a deck-coverage gap, not a layout bug. The extraction-side analogue of `klt drc`'s `coverage.layers_in_stream_without_rules`. Does **not** catch a layer that is read for device recognition only, never as a `metals`/`vias` connectivity level — see `device_recognition_only_layers` below (issue #619). Every entry here already carries a material (`shapes > 0`) count — empty-layer entries are dropped before they reach this field — so a non-empty `ignored_layers` also appends a single aggregate prose entry to `warnings[]` (issue #666), naming the affected layer(s) and their total shape count, so a caller checking only `warnings[]` still sees it. |
| `device_recognition_only_layers` | array\<object\> | `(layer, datatype)` pairs carrying shapes in the input stream that this `--deck` **does** read (so they never appear in `ignored_layers` above) but only for a bipolar/capacitor/resistor/diode device-recognition role, never as a `metals`/`vias` connectivity level and never one of the deck's own MOS-core layers either (issue #619 — see "Device-recognition-only layers" below), each `{ "layer": int, "datatype": int, "shapes": int }` with its stream shape count, sorted by `(layer, datatype)`. Two nets joined only through such a layer will not merge, and — unlike a layer the deck never reads at all — this gap is invisible to `ignored_layers`, which can only tell "read" from "not read," not "read for connectivity" from "read for device recognition only." This is diagnostic context, not a warning: unlike `ignored_layers`, a non-empty list does **not** append to `warnings[]` (a deck's own marker/mask geometry is expected to be device-recognition-only by PDK design, not a coverage gap). Empty when every device-recognition layer is also a `metals`/`vias` level or one of the deck's own MOS-core layers, or the deck declares no `bipolars`/`capacitors`/`resistors`/`diodes` entries at all. |
| `device_classes`   | array\<string\>            | The device-class roles this `--deck` is structurally capable of recognising (`["nfet", "pfet"]` for a MOS-only deck; a deck that also declares a bipolar entry appends its class name, e.g. sky130's `[..., "pnp", ...]` or gf180mcu's `[..., "bjt", ...]` — see "Bipolar (BJT) device recognition" above; a deck that declares one or more MiM-capacitor entries likewise appends each one's class name, e.g. sky130's two `"sky130_fd_pr__model__cap_mim"`/`"sky130_fd_pr__model__cap_mim_m4"` or gf180mcu's one `"cap_mim_2f0_m4m5_noshield"` — see "MiM capacitor device recognition" above; a deck that declares one or more drawn resistors appends the single `"resistor"` role after those — see "Drawn resistors" above; and a deck that declares one or more junction diodes appends each one's class name last, e.g. gf180mcu's `"diode_nd2ps_06v0"`/`"diode_pd2nw_06v0"` — see "Junction diodes" above), independent of what this layout happens to contain. sky130 currently reports `["nfet", "pfet", <bipolar>, <capacitor…>, "resistor"]` and gf180mcu `["nfet", "pfet", <bipolar>, <capacitor>, "resistor", <diode…>]`. Note the `"resistor"` role token is not a `devices[].class` label string (a deck's resistor class is named after the PDK device it models). What the deck **can find**, not what it found — see `device_counts` for that. A consumer that needs to know ahead of time whether a deck supports a given device class (e.g. before pairing it with a reference netlist for `klt lvs`) reads this instead of inferring "unsupported" from a zero count. |
| `devices`          | array\<object\>            | One entry per extracted device, see below.                                                             |
| `nets`             | array\<object\>            | One entry per extracted net, see below.                                                                |
| `warnings`         | array\<string\>            | Non-fatal extraction notes (e.g. a gate shape touching no diffusion, the unmodelled-device-geometry heuristic below, or a top-level pin promoted from a label found below the top cell — see "Top-cell-only pin promotion"). Always present, empty when clean. |
| `black_box_regions` | array\<object\>           | One entry per black-box/abstract region excluded from connectivity (issue #293 — see "Black-box / abstract regions" above), each `{ "bbox_um": {"left", "bottom", "right", "top"}, "shapes_excluded": int }`. Always present, empty when the layout draws no reserved-annotation-layer (990-999) geometry — see "Reserved annotation layer" above. |
| `abstracted_cells` | array\<object\>            | One entry per distinct cell type matched by `--abstract-cells` (issue #620 — see "Cell-level (black-box + pins) abstraction" above), each `{ "cell": string, "instance_count": int, "pin_count": int, "resolution_source": "in_cell_labels" \| "lef_abstract", "lef_path": string \| null }` (`lef_path` names the specific `--abstract-cell-lef` file for `"lef_abstract"`, `null` for `"in_cell_labels"`). Always present, empty unless `--abstract-cells` matched at least one instantiated cell. |
| `unmodelled_poly`  | array\<object\>           | One entry per `poly` shape the unmodelled-device diagnostic flagged (issue #324 — see "Known limitation: unmodelled device geometry" below), each `{ "bbox_um": {"left", "bottom", "right", "top"}, "reason": "unmarked" \| "marked_unrecognised" }`. `reason` mirrors the two `warnings[]` cases below without requiring a consumer to parse the prose string. Sorted by `(left, bottom)` for deterministic output. Always present, empty whenever `warnings[]` carries no unmodelled-device entry. A deliberate poly underpass lands here too unless the deck declares `poly_interconnect` and the shape is marked with it — see "Declaring intentional poly interconnect" below (issue #1425). |
| `merged_net_labels` | array\<object\>          | One entry per net whose name is a merge of 2+ distinct labels (issue #470 — see "Merged net labels" below), each `{ "net": "<full joined name>", "labels": [str, ...] }` (`net` uses the same `\|`-joined spelling as `nets[].name` and the written netlist, issue #696; `labels` is `net` split on `\|`). A matching prose entry is also appended to `warnings[]` for every affected net. Always present, empty when no net carries multiple labels. |
| `voltage_domain_warnings` | array\<object\>     | One entry per voltage-domain marker layer with **no** matching `mos_flavours` coverage (issue #552, narrowed by issue #1111 — see "Voltage-domain markers and per-flavour MOS binding" below) whose geometry overlaps extracted MOS device geometry, each `{ "marker": "<layer>/<datatype>", "description": str }`. A matching prose entry is also appended to `warnings[]`. Always present, empty for a deck that registers no such marker, a marker fully covered by `mos_flavours` (e.g. gf180mcu's `Dualgate` as of issue #1111 — see the linked section for what "covered" means), or a layout that draws none of the remainder overlapping MOS geometry. |
| `unbiased_pmos_body_nets` | array\<object\>  | One entry per extracted PMOS device whose body (`"b"`) terminal ties to an anonymous, KLayout-synthesized net rather than a real, named one (issue #555 — see "Known gap: an anonymous PMOS body net has no DC bias path" above), each `{ "device": "<device name>", "net": "<anonymous net name>" }`. A single aggregate prose entry (count baked in, e.g. `"148 PMOS devices tie their body to..."`) is also appended to `warnings[]` when this field is non-empty — not one line per device (issue #599). Always present, empty when no PMOS device's body net is anonymous — i.e. every device whose `nwell` island a drawn or derived well tie reaches (issue #1084). Present regardless of `--parasitics`/`--pdk`. |
| `single_terminal_nets` | array\<object\>    | One entry per net with `device_count == 1` and `pin: false` (issue #596 — see "Single-device-terminal nets" above), each `{ "net": "<net name>", "device": "<owning device name>", "terminal": "<lower-cased terminal key>", "terminal_kind": "gate" \| "source" \| "drain" \| "body" \| "<literal terminal key>" }`. Up to two aggregate prose entries (one per `terminal_kind` bucket — `"gate"` vs. everything else — each with its bucket's count baked in) are also appended to `warnings[]`, phrased more strongly for the `"gate"` bucket — not one line per net (issue #599). Always present, empty when every net either has zero or 2+ device terminals, or is a declared pin. |
| `dead_metal`       | array\<object\>            | One entry per connected cluster of routing-stack (`metals`/`vias`) geometry that joins no extracted net (issue #676 — see "Dead metal" above), each `{ "role": "metal<i>" \| "via<i>", "layer": int, "datatype": int, "bbox_um": {"left", "bottom", "right", "top"}, "shapes": int, "area_um2": number }`, sorted by `(layer, datatype, left, bottom)`. `role`'s `<i>` indexes the deck's own `metals`/`vias` tuple (`0` = bottom-most level); `shapes` counts the drawn shapes on that stream layer the cluster covers (one entry per *cluster*, not per polygon). XY overlap between adjacent metal levels is **not** connection — only a same-layer touch or a via landing joins two shapes, so a wire passing over another with no via between them is still dead. A labelled floating cluster (power strap, seal ring, bond pad) survives as a real named net and never appears here. A non-empty list also appends a single aggregate prose entry to `warnings[]` (count baked in, issue #599). Always present, empty when every metal/via shape joins a net. |
| `matched_device_groups` | array\<object\>       | One entry per `--matched-group` declaration, in the order given (issue #1018 — see "Matched-device geometry check" above), each `{ "name": string, "instances": [string, ...], "unresolved_instances": [string, ...], "mismatched_fields": [{"field": string, "values": {"<instance name>": number, ...}}, ...] }`. `instances` echoes the declared member list verbatim; `unresolved_instances` (sorted) is the subset matching no extracted device; `mismatched_fields` lists every parameter that diverges across the group's resolved members (post-rounding equality — no numeric tolerance). A matching prose entry is also appended to `warnings[]` for a group with unresolved members and/or mismatched fields. Always present, empty (`[]`) when `--matched-group` was never given. |
| `pdk`              | object \| `null`           | `{"variant", "root", "version"}` when `--pdk`/`--pdk-root` were given and resolved; `null` otherwise. `root` is `{"path", "scope"}` (issue #1376, schema_version 3) -- not the raw `--pdk-root` argument -- via the same `{path, scope}` shape `klt env-provenance` and `klt pex`/`klt sim`/`klt size` use: `scope: "repo"` with a repo-relative `path` when the PDK install lives inside the invoking repo, `scope: "external"` with `path: null` otherwise (a PDK install almost always is). Committing this response as evidence (the normal use of `--format json`) no longer bakes the resolving machine's absolute PDK install path (and possibly a username) into the record; `provenance.pdk` below already carries the same PDK's reproducible identity (name/source/version) without a path. |
| `parasitics`       | object \| `null`           | Lumped RC summary when `--parasitics` was given; `null` otherwise. See "Parasitic (RC) extraction".     |
| `spef_path`        | string \| `null`           | Additive field (issue #948). Resolved path of the written SPEF file when `--spef` was given; `null` otherwise. See "SPEF export".                       |
| `provenance`       | object                     | Shared reproducibility block (`klt_version`, `klayout_version`, `pdk`, `deck`, `input`) defined once in [`docs/json-contract.md`](../json-contract.md). Its `pdk` mirrors the resolved PDK as `{name, source, version}` (the richer `pdk` field above carries `root`); `deck` pins the extraction deck by name and `sha256:` content hash, plus a `released` tri-state signal (issue #1193, non-fatal) for whether that hash ships in any released `klayout-tools` version -- `false` flags an unreleased/dev-edited deck, `null` when unresolvable (e.g. the generated deck history table is missing) -- and an `options` key (issue #595) echoing `--deck-option`'s resolved mapping when non-empty (omitted entirely otherwise) -- see "Selecting a shared-geometry resistor flavour" below; `input` pins the input layout file (`path`, distinct from `netlist_sha256`, which hashes the *written* netlist) by `sha256:` content hash. |

The `devices[]`/`nets[]` report is a *convenience view* for agents that want
structure without re-parsing SPICE; the **netlist file at `netlist_path` is
the authoritative artifact**, and it is what a future `klt lvs` and `klt sim`
consume.

### `devices[]` entries

| Field    | Type                        | Description                                                                                    |
| -------- | --------------------------- | ------------------------------------------------------------------------------------------------ |
| `name`   | string                      | The device's instance name in the written netlist (e.g. `"$1"`, matching the `M$1 ...` line). **This field is deliberately NOT backslash-escaped** (contrast `nets[].name` below): a KLayout-synthesized `$<n>` anonymous-device name is never the first token on an instance line — always preceded by a class-letter prefix (`M$1774`, `R$22`, etc.) — so the leading-`$` ngspice inline-comment hazard that `spice_safe_net_name()` guards against in net-name fields does not apply to device names. See "Anonymous nets are backslash-escaped" for the full rationale. |
| `class`  | string                      | The deck's device-class name (`"nfet"` / `"pfet"`, a declared bipolar class like `"pnp"` / `"bjt"`, a declared MiM-capacitor class like `"sky130_fd_pr__model__cap_mim"` / `"cap_mim_2f0_m4m5_noshield"`, a declared drawn-resistor class like `"res_generic_po"` on sky130 / `"ppolyf_u"` on gf180mcu — see "Drawn resistors" — or a declared junction-diode class like `"diode_nd2ps_06v0"` on gf180mcu, see "Junction diodes"). |
| `nets`   | object\<string, string\|null\> | Terminal → net-name map (same `\|`-joined spelling as `nets[].name` for a label-merged net, issue #696). MOS: `"s"`, `"g"`, `"d"`, `"b"`. Bipolar: `"c"`, `"b"`, `"e"` (collector/base/emitter — see "Bipolar (BJT) device recognition" above). MiM capacitor: `"a"`, `"b"` (the two plates — see "MiM capacitor device recognition" above). Drawn resistor: `"a"`, `"b"` (the two heads), plus `"w"` for a resistor with a bulk terminal (gf180mcu's `ppolyf_u`, tied to the deck's substrate global — see "Drawn resistors"). Junction diode: `"a"`, `"c"` (anode/cathode — see "Junction diodes" above). `null` only if a terminal has no connected net at all (never observed for `s`/`g`/`d` in this deck's extraction; MOS `b`, bipolar `b` and a diode's `Nwell`-side terminal can be `null`-free but anonymous, see "Coverage"). |
| `params` | object\<string, number\>    | MOS: `"w_um"` / `"l_um"`, the extracted gate width/length in micrometres, plus `"as_um2"` / `"ad_um2"` (source/drain junction area, square micrometres) and `"ps_um"` / `"pd_um"` (source/drain junction perimeter, micrometres) — the same measured junction geometry a `--pdk`-bound `X` card's own `AS`/`AD`/`PS`/`PD` carry (issue #695), present here regardless of `--pdk` since `devices[]` is built from the extracted device objects before the netlist is written. Bipolar: empty (KLayout's `DeviceClassBJT3Transistor` reports area/perimeter parameters this field does not extract — see "SPICE model binding" above for how those measured values are still surfaced, via a `warnings[]` entry, when `--pdk` binds a `pnp` device onto a fixed-geometry target subcircuit). MiM capacitor: `"c_f"` (extracted capacitance, in **Farads**), `"area_um2"` (the plates' overlap area, in square micrometres), and `"perimeter_um"` (the plates' overlap perimeter, in micrometres, issue #512) — `c_f = area_um2 * area_cap + perimeter_um * perim_cap`, see "MiM capacitor device recognition" above (`perim_cap` defaults to `0.0` for a deck that has not set `CapacitorDevice.perim_cap_f_um`, reproducing the pre-#512 area-only formula bit-for-bit). Drawn resistor: `"w_um"` / `"l_um"` for the resistive segment's own width/length, plus `"r_ohm"` — the extracted resistance, `l_um / w_um * sheet_rho + fixed_offset_ohm` (issue #518; `fixed_offset_ohm` defaults to `0.0` for a deck that has not set `ResistorDevice.fixed_offset_ohm`, reproducing the pre-#518 `l_um / w_um * sheet_rho`-only formula bit-for-bit — see "Drawn resistors" above). Junction diode: `"area_um2"` / `"perimeter_um"`, the recognised junction's own area/perimeter (issue #542) — no I-V model is extracted, see "Junction diodes" above. |

`devices` is sorted by `name` for deterministic, diff-clean output.

### `nets[]` entries

| Field          | Type    | Description                                                                 |
| -------------- | ------- | ------------------------------------------------------------------------------ |
| `name`         | string  | The net's name, byte-identical to how the written netlist's `.SUBCKT`/instance lines reference it as a node — a labelled name, or an anonymous net's KLayout-synthesized `$N` placeholder backslash-escaped to `\$N` (issue #1162 — see "Anonymous nets are backslash-escaped" below) — a net two labels merged is `\|`-joined (e.g. `"Y\|Y2"`), not KLayout's own un-escaped, comma-joined `Net.expanded_name()` form (issue #696 — see "Merged net labels" below). |
| `pin`          | boolean | Whether this net is promoted to a top-cell pin (a named net at the top level). |
| `device_count` | integer | Number of device terminals connected to this net.                             |

`nets` is sorted by `name` for deterministic, diff-clean output.

## `--check` / `--rerun`

A `klt extract --format json` report is often committed as evidence
alongside a design (e.g. as a `klt signoff` manifest citation, or as the
netlist `klt lvs` compared against a reference), but nothing previously let
a consumer *verify* that a committed report still reproduces against the
current input/deck/tool version without hand-rolling a normalize-and-diff.
This matters in particular because a curated deck (e.g. `decks/gf180mcu.py`)
is a plain Python module, not a data file — *any* byte change to it,
including a device-recognition change (e.g. to the substrate/well-tap
derivation described above), changes its content hash with no separate
"semantic vs. cosmetic" signal. `klt extract --check <report.json>` (issue
#1149) closes that gap, mirroring `klt drc --check`/`klt lvs --check`
(issue #1106) exactly:

```
klt extract --check design.extract.json                  # cheap mode (default)
klt extract --check design.extract.json --rerun          # full mode
```

`--check` is mutually exclusive with the positional `<file>` argument (and
with `--deck`/`--top`/etc., which are ignored) — the input path and deck are
both read from `<report.json>` itself, not given again on the command line.

### Cheap mode (default)

Re-hashes the input layout stream and the deck named in the committed
report's own `provenance` block (reusing
`klayout_tools._provenance.sha256_file`, never reimplementing hashing) and
compares each against the `sha256:`-prefixed digest the report already
recorded — **no extraction engine re-run**. Response shape:

```json
{
  "schema_version": 1,
  "mode": "check",
  "report": "design.extract.json",
  "status": "match",
  "checks": [
    {
      "field": "provenance.input.content_hash",
      "expected": "sha256:<hex>",
      "actual": "sha256:<hex>",
      "match": true
    },
    {
      "field": "provenance.deck.content_hash",
      "expected": "sha256:<hex>",
      "actual": "sha256:<hex>",
      "match": true
    }
  ]
}
```

`status` is `"match"` only when every `checks[]` entry's `match` is `true`.
A report with no recorded hash to compare against (e.g. one produced before
`provenance.input`/`provenance.deck.content_hash` existed) always renders
that check `match: false` — a missing baseline is never treated as a pass
(mirrors `klt signoff`'s `_grade_evidence()` "never a false pass"
discipline). A `provenance.deck.content_hash` mismatch here is exactly the
signal issue #1149 was filed to add: it means the curated deck's own source
changed since this report was committed, so the previously-recorded
`devices`/`nets` (and any downstream `klt lvs` comparison against them) can
no longer be trusted to still reproduce without re-running.

### Full mode (`--rerun`)

Actually re-runs the extraction (`run_extract`) against the `file`/`deck`/
`top` the committed report itself names, plus `provenance.deck.options`
(`--deck-option`) when present, writing the fresh netlist back to the same
`netlist_path` the committed report recorded. Diffs the fresh report against
the committed one, field by field. `provenance.klt_version`/
`provenance.klayout_version`/`provenance.pdk.version` are excluded from the
diff — these legitimately vary between two runs of identical inputs on
different tool installs/PDK snapshots; every other field (including
`device_count`, `devices`, `nets`, and the hashes cheap mode also checks) is
load-bearing. Response shape:

```json
{
  "schema_version": 1,
  "mode": "rerun",
  "report": "design.extract.json",
  "status": "drifted",
  "drift": [
    { "field": "devices.0.nets.b", "committed": "vsubs", "fresh": "STIE" }
  ],
  "fresh": { "...": "the freshly produced klt extract report, full shape" }
}
```

`status` is `"match"` only when `drift` is empty. `fresh` embeds the
complete report `--rerun` just produced, so a consumer can inspect the
current state without a second invocation.

**Known limitation**: only `file`/`deck`/`top`/`provenance.deck.options` are
reconstructed from the committed report. Every other optional flag
(`--parasitics`, `--mom-net`, `--spef`, `--critical-net`,
`--distributed-rc`, `--def-net-names`, `--def-net-connections`,
`--mom-rlc-*`, `--top-cell-pins`, `--pins`,
`--defer-resistor-fixed-offset`, `--abstract-cells`, `--abstract-cell-lef`,
`--matched-group`, `--pdk`/`--pdk-root`) is never echoed anywhere in the
response, so a report produced with any of them will legitimately (and
unhelpfully) show drift in the corresponding fields under `--rerun`. Use
cheap mode instead when any of these apply.

### Exit codes for `--check` / `--rerun`

Both modes use the same 0/3 split described below: `0` when `status` is
`"match"`, `3` when it is `"drifted"`. A missing or unparseable
`<report.json>` exits `1` with a clean error message, same as any other
application-level failure.

## Exit codes

| Code | Meaning                                                                                          |
| ---- | --------------------------------------------------------------------------------------------------- |
| `0`  | Extraction succeeded, netlist written. Under `--check`: the committed report still holds (`status: "match"`). |
| `1`  | Failed to run — bad file, missing/unknown `--deck`, unresolvable PDK (when `--pdk`/`--pdk-root` given), a resolved PDK with no curated model-binding table entry for `--deck` (see "SPICE model binding" above), missing/ambiguous top cell, or an engine error (e.g. device recognition producing a device with an unconnected terminal — most commonly a bipolar device-mark drawn exactly coincident with, rather than strictly enclosing, the terminal geometry it scopes; confirmed clean by this module's own test suite, issue #432). Under `--check`: a missing/unparseable committed report, or `--rerun` given without `--check`. |
| `2`  | Usage error (missing argument, bad `--format` value, or combining `<file>` with `--check`) — from argparse. |
| `3`  | Under `--check`: the committed report drifted from the current deck/input (`status: "drifted"`) — see "`--check` / `--rerun`" above. |

Outside `--check`, there is no "ran but found problems" outcome for a fresh
extraction; it either produces a netlist or it fails (matching `klt gen`'s
reasoning for omitting a `3` there) — exit code `3` is reachable only via
`--check`.

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

- **Lateral (same-layer, sidewall) coupling capacitance.** `--parasitics`
  models capacitance to ground plus *vertical* (crossover) net-to-net
  coupling on adjacent metal levels (issue #760, see "Vertical-overlap
  coupling capacitance"). Two wires running side by side on the same level
  still couple by exactly zero, and each still charges its facing edge the
  full isolated-edge fringe term to substrate as though the neighbour were
  absent — both need the spacing-aware neighbour (halo) search the crossover
  pass deliberately avoids. Stage 2b/2c of
  `docs/design/extract-fidelity-roadmap.md`. Without `--parasitics`, no
  interconnect R/C is extracted at all. This gap is declared
  machine-readably in the output itself rather than only in this doc — see
  `parasitics.model` (issue #728, "Parasitic model scope").
- **Full distributed, per-segment RC (a PEX-style ladder).** `--parasitics`
  distributes each net's resistance as a coarse star from the net to each
  device terminal (issue #592 — see "Parasitic (RC) extraction" → "The
  model: a star topology"), so series resistance between two terminals on
  the same net now exists and is non-zero, modelling (coarsely) IR drop on a
  shared rail and series resistance in a matched or high-impedance path. It
  is not a per-segment measurement of the actual routed copper between two
  specific points — that full distributed model remains a credible future
  increment (issue #592's own deferred "Option 2").
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
- **Gate-level Verilog output.** `--abstract-cells` (issue #620) emits a
  hierarchical **SPICE subcircuit** netlist only. A gate-level Verilog
  netlist (module instantiations, port-connected by name) is a deliberately
  deferred follow-up — see "Cell-level (black-box + pins) abstraction". The
  *opposite* direction — converting a `klt place-and-route` `verilog_path`
  gate-level Verilog netlist into SPICE for use as `klt lvs`'s reference —
  is not deferred: see `docs/cli/lvs.md`'s `reference.form:
  "gate-level-verilog"` (issue #1336).

Netlist comparison (`klt lvs`) is a separate command; this command only
produces the layout-side netlist half of that comparison.
