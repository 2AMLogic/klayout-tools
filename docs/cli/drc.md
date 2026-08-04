# `klt drc`

Run a headless DRC rule deck against a GDSII or OASIS layout stream and
report violations as structured data.

```
klt drc <file> --deck sky130|gf180mcu [--format text|json]
```

- `<file>` — path to a GDSII (`.gds`) or OASIS (`.oas`) file. KLayout
  auto-detects the stream format on read; the extension is not authoritative.
- `--deck` — required. The DRC deck to run. Currently: `sky130`, `gf180mcu`.
- `--format` — `text` (default, a human-readable summary) or `json`.

## Engine

`klt drc` runs fully headless via the pip `klayout` package's native
`klayout.db.Region` check primitives (`width_check`, `space_check`,
`separation_check`, `enclosing_check`, `enclosed_check`, `notch_check`,
`overlap_check`) — the same C++ polygon-processing engine that backs
KLayout's higher-level DRC-DSL scripts, invoked directly instead of through
the script runner. There is **no dependency on the standalone `klayout`
application binary or its `.drc`/`.lydrc` script runner** — only
`pip install klayout` (already this repo's sole runtime dependency), so the
command runs anywhere that already runs in CI.

A "deck" is our own declarative rule table (`DrcRule`: rule id, layer,
check kind, threshold, optional second layer) that drives those check
primitives — not the official sky130 `.lydrc` script executed verbatim. See
"Coverage" below for what that means for rule fidelity.

### `"enclosing"` / `"enclosed"` also catch zero-overlap escapes

`Region.enclosing_check`/`enclosed_check` only measure the *facing edges* of
shapes that already face each other — a shape (or part of a shape) of the
"enclosed" layer that has **no** spatial overlap with the "enclosing" layer
produces no facing edges at all, so the raw primitive reports nothing for it.
That is the *worst* case an enclosure rule exists to catch (zero enclosure,
not just insufficient margin), so `klt drc` does not rely on the primitive
alone: for every `check: "enclosing"`/`"enclosed"` rule, it additionally
flags whatever part of an otherwise-interacting enclosed shape escapes the
enclosing layer entirely — under the *same* rule id, alongside the
`enclosing_check`/`enclosed_check` edge-pair violations (see
`violations[].check` below; both violation kinds share the same `"check"`
string).

This is scoped to shapes of the enclosed layer that overlap the enclosing
layer *somewhere* (i.e. `other_layer.interacting(layer) - layer`, not a
blanket `other_layer - layer`) rather than every zero-overlap shape
unconditionally: some rules approximate an official DRM rule that is really
scoped to one sub-population of a shared layer (e.g. gf180mcu's
`poly2.enclosing.contact.1` and `comp.enclosing.contact.1` both check the
*same* `Contact` layer, against Poly2 for gate contacts and Comp for
diffusion contacts respectively — an ordinary diffusion contact has zero
overlap with Poly2 by design, not by defect, and a plain not-inside check
over the whole layer would flag every contact in every real layout against
whichever rule doesn't apply to it). Scoping to shapes that already interact
with this rule's enclosing layer keeps the fix targeted at genuine escapes
of a feature the rule already covers, at the cost of not detecting a shape
that is 100% disjoint from the enclosing layer everywhere (no part of it
interacts at all) — a residual gap tracked as a known limitation, since
closing it fully requires compound/connectivity-scoped layer expressions
this engine does not evaluate (see "Coverage" below).

### Sized/derived-layer rules (`DerivedLayer`)

Some DRM rules are defined against a derived geometry rather than a single
drawn `(layer, datatype)` — e.g. gf180mcu's `MIMTM.2` scopes to the MiM
stack's "virtual bottom plate": its purpose-drawn top-plate layer (`FuseTop`)
oversized by a fixed margin, restricted to wherever the bottom-plate
conductor (`Metal4`) already comes near it. `DrcRule`'s optional
`derived_layer` field (a `DerivedLayer(base, sized_by_um, intersect_with)`,
issue #345) expresses exactly this: the checked region becomes
`intersect_with.interacting(base) & base.sized(sized_by_um)` — only shapes
of `intersect_with` that already touch the *unsized* `base` region
somewhere, clipped to `base`'s outline oversized by `sized_by_um` — instead
of `layer`'s own raw drawn shapes. `layer` stays required and continues to
name the rule's reporting identity (`violations[].layer`, `coverage`),
independent of `derived_layer`'s two input layers.

This exists because an *unscoped* version of some rules would be actively
wrong, not merely conservative: gf180mcu's `mim.enclosing.via4.1`
(`MIMTM.2`) checks a `Metal4`-derived region's enclosure of `Via4` by
0.4um — a blanket "`Metal4` must enclose every `Via4` by 0.4um" check with
no scoping would flag ordinary `Metal4`-`Via4`-`Metal5` routing throughout
*any* layout using that stack, since routing vias use a much smaller
enclosure than a MiM cap's virtual bottom plate requires. Scoping the
checked region to `Metal4` that already overlaps a `FuseTop` shape keeps
ordinary routing (no MiM structure nearby) out of the derived region
entirely, so it never trips the rule — see
`test_run_drc_gf180mcu_mim_enclosing_via4_ordinary_routing_clean` in
`tests/test_drc.py` for the regression fixture proving this, and
`DerivedLayer`'s own docstring (`src/klayout_tools/decks/__init__.py`) for
the full derivation. `derived_layer` is a general mechanism, not specific to
MIM capacitors or gf180mcu — any future deck (this one or sky130) can use it
for another rule whose official scope needs a sized/boolean layer
expression.

## Coverage

The `sky130` deck is a **curated starter subset**, not the full sky130
design rule manual (which spans hundreds of rules). It currently covers 16
rules — width, spacing, and enclosure checks across the `poly`, `diff`,
`li1`, `met1`, `licon1`, `mcon`, `met2`, and `via` (met1&lt;-&gt;met2 via1)
layers — transcribed directly from the official community sky130 KLayout
DRC deck
([`fossi-foundation/open-pdks`](https://github.com/fossi-foundation/open-pdks),
`sky130/klayout/sky130.lydrc` and `sky130.lyt`; GPLv3). The `met2`/`via`
rules (issue #513) were cross-checked against a real sky130A PDK install's
own current deck (`libs.tech/klayout/drc/sky130A_mr.drc`), whose rule
ids/values for every rule already curated here (`poly.1a`, `li.1`, `li.3`,
`m1.1`, `m1.2`, `m1.4`, `licon.5`, `licon.8`, `ct.2`) match exactly,
confirming it carries the same content. Each rule in
`src/klayout_tools/decks/sky130.py` cites the exact source rule id (e.g.
`"poly.1a"`) and comment it was transcribed from.

Two of the sixteen rules approximate an official rule defined on a
*compound* layer expression (a boolean union of two mask layers, e.g.
`diff.or(tap)`) as a check against a single drawn layer, because the native
`Region` check primitives check one layer, or one layer against one other
layer, at a time — they do not evaluate arbitrary layer expressions the way
the DRC-DSL script runner does. Four more (`met2.width.1`, `via.width.1`,
`met1.enclosing.via.1`, `met2.enclosing.via.1`) approximate an official rule
that additionally bounds a max size, length, or a periphery-scoped/
corner-relaxed refinement our single-layer/two-layer check primitives don't
support — the same class of approximation `met1.enclosing.mcon.1` and
gf180mcu's `contact.width.1` already make. Every approximation is called
out explicitly in its rule's docstring; the threshold *values* used are
always the real, unmodified source values. The official deck's `m2.6`
(minimum met2 area, 0.0676 um²) is **not** transcribed: no `"area"` check
primitive exists in `DrcRule`'s vocabulary today (only
`width`/`space`/`notch`/`separation`/`enclosing`/`enclosed`/`overlap`) —
tracked as a candidate follow-on rather than silently dropped.

The `gf180mcu` deck is likewise a **curated starter subset**: 24 rules —
width, spacing, and enclosure checks across the `Poly2`, `Comp`
(diffusion/active), `Contact`, `Metal1`-`Metal3`, `Metal5`, and `MetalTop`
layers, plus a first increment of well/substrate-tap coverage (`Nwell`
spacing and Nwell-tap enclosure), one bipolar (BJT)-specific device rule
(`DRC_BJT` mark-layer separation), and the MiM capacitor stack
(`Metal4`/`FuseTop` bottom-/top-plate spacing and overlap, plus the virtual
bottom plate's `Via4` overlap, see "Sized/derived-layer rules" above) —
transcribed from the published GlobalFoundries 180nm MCU **Design Rule
Manual**
([`google/gf180mcu-pdk`](https://github.com/google/gf180mcu-pdk),
`docs/physical_verification/design_manual/`; Apache License 2.0),
specifically the "7.4 Nwell" (`NW.*`), "7.5 Comp" (`DF.*`), "7.7 Poly2"
(`PL.*`), "7.12 Contact" (`CO.*`), "7.13 Metaln" (`Mn.*`, extended to
`n = 2..5`), "7.15 MetalTop" (`MT.*`), "10.4.2 MIM Capacitor, Option B"
(`MIMTM.*`), and "10.7 DRC_BJT Mark Layer" (`BJT.*`) sections. Unlike
sky130 (transcribed from a live, KLayout-runnable `.lydrc` script), the
companion KLayout DRC-deck repo
([`google/globalfoundries-pdk-libs-gf180mcu_fd_pv`](https://github.com/google/globalfoundries-pdk-libs-gf180mcu_fd_pv))
does not yet open-source the core FEOL/BEOL width/space/enclosure checks as
executable rule-deck code, so `src/klayout_tools/decks/gf180mcu.py` instead
cites the DRM's own published rule ids (e.g. `"DF.1a"`, `"PL.1"`, `"CO.1"`,
`"Mn.1"`, `"MT.1"`, `"MIMTM.1"`, `"NW.2a"`, `"DF.4d"`, `"BJT.3"`) and
numeric values directly.

Eight of the twenty-four gf180mcu rules approximate an official DRM rule in
some way — either a compound-layer context our single/two-layer check
primitives can't isolate (`comp.space.1`, `poly2.space.1`, `poly2.width.1`,
`nwell.enclosing.comp.1`), a bound our primitives don't support
(`contact.width.1`'s fixed-size square, approximated as a minimum only), a
sized/derived-layer context our primitives can't isolate (`mim.space.1`,
approximated as a general `Metal4`-to-`Metal4` spacing check that may
over-flag ordinary `Metal4` routing unrelated to a MiM capacitor), or
context our engine has no data for at all — net-potential (`nwell.space.1`)
or device connectivity (`bjt.separation.comp.1`), both of which require
netlist/connectivity information the geometry-only check primitives don't
have. Each is called out explicitly in its rule's docstring in
`gf180mcu.py`; the threshold values used are always the real, unmodified
DRM values.

The DRM's MiM capacitor rule `MIMTM.2` (minimum bottom-plate overlap of
`Via4`) is transcribed as `mim.enclosing.via4.1` (issue #345) using the
`derived_layer` primitive described above, rather than approximated against
raw `Metal4` — see "Sized/derived-layer rules (`DerivedLayer`)" above for
why an unscoped version of this particular rule would have been actively
wrong, not merely conservative.

Coverage does **not** yet include: `Pplus`/`Nplus` implant-specific rules
(width/space/enclosure of the implant layers themselves), `LVPWELL` or
`DNWELL`, the remaining `BJT.*` rules (`BJT.1`/`BJT.2`, which key off
`DNWELL`), the MIM Option-A (`MIM.*`) rule set (a different, 3-metal-layer
process variant this deck doesn't model — see `gf180mcu.py`'s docstring), or
5V/6V high-voltage variants — left for follow-on issues.

Coverage is expected to grow incrementally in follow-on issues, for both
decks.

## Macro-scale, machine-generated (standard-cell) layout

`klt drc` was first exercised against hand-drawn analog layout only — a
handful of shapes, one or two drawn layers, shallow hierarchy. Issue #436
ran it against the first machine-generated, macro-scale target: a real
`sky130_fd_sc_hd` standard-cell GCD macro produced end to end by
`klt synthesize` + `klt place-and-route` (real Yosys + real OpenROAD, see
`tests/corpus/README.md`'s provenance note) — thousands of
instances, one level of real hierarchy, and routing-layer usage across
`met1`-`met5` plus vias, none of which the analog fixtures exercise. `klt
drc` itself required **no code changes**: it ran cleanly (no crash,
`status`/`violation_count`/`coverage` all well-formed) in about two seconds
against this layout, confirming the whole-layout-flattened design (see
"Limitation: whole-layout, flattened" below) scales to macro-scale,
deep-instance-count input without a special case.

The run did surface a handful of genuine `diff.enclosing.licon.1`
violations, though — worth understanding as a property of the **input**,
not a `klt drc` defect. They cluster at row boundaries between adjacent
standard-cell instances (confirmed by inspecting the merged `diff` region
around each violation: the flagged shape spans two neighboring cell
instances, not one). `klt place-and-route` does not yet run a filler-cell
insertion stage (its stage set is `floorplan` → `place` → `cts` → `route`
only, per `docs/cli/place-and-route.md`) — a full ORFS-style flow inserts
filler cells to close exactly these row gaps before signoff DRC, which
would very likely absorb this class of violation. Until filler-cell
insertion exists, a `klt drc` run against `klt place-and-route` output
directly (skipping filler insertion) can legitimately show a small number
of row-gap-adjacent enclosure violations — this is the curated `sky130`
deck correctly reporting real, if flow-artifact-caused, geometry, not a
false positive to suppress.

## Mixed sky130_fd_sc_hd + analog-macro layout (Epic #393 Phase 3, #456)

Extending the macro-scale check above, issue #456 ran `klt drc` against a
genuinely **mixed** layout: a real `klt gen diff_pair` analog block,
LEF-abstracted (`klt lef-abstract`, #438/PR #448) and placed as a hard
macro via `klt place-and-route`'s `request.macros` field (#438/PR #448)
alongside ~15 real `sky130_fd_sc_hd` standard-cell instances — real Yosys
0.67 + real OpenROAD `26Q3-771-g7cfb2105c9`, merged into one top cell.

Because the whole-layout-flattened design ("Limitation: whole-layout,
flattened" below) has no region concept at all, a rule deck cannot be
scoped to "see" only one of the two domains — every rule already runs
against every shape in the flattened `Region`, standard-cell or macro
alike, by construction. This was confirmed two ways, not just asserted:

1. **A real, pre-existing violation** (`diff.enclosing.licon.1`, the same
   class documented above) was found in the standard-cell region of the
   unmodified layout — direct evidence the deck was already evaluating
   that domain.
2. **A known violation (`poly.width.1`, an undersized `poly.drawing` shape)
   was injected at a caller-chosen location inside each region separately**
   — once inside the macro's own placed footprint, once inside the
   standard-cell core area, well outside the macro — and `klt drc` reported
   both, each with a `bbox` matching its injection point exactly (plus a
   third, incidental `poly.enclosing.licon.1` hit where the injected macro-
   region shape landed adjacent to the macro's own real `licon1` contact —
   further evidence the check engine is reasoning about real macro
   geometry, not skipping it).

No code change was needed for either domain to be checked — this is a
verification finding, not a fix. See #456 for the full transcript
(descriptor, LEF, synthesized netlist, and injection script).

## Reserved annotation layer

Both decks read a fixed, enumerable set of `(layer, datatype)` pairs (a
deck's `coverage.deck_layers`, below). Any GDS layer number outside that set
is, by construction, invisible to `klt drc` and `klt extract` today — but
"invisible today" is an emergent property of the current deck tables, not a
guarantee, since both decks are curated starter subsets that are expected to
grow (see "Coverage" above). Recording a floorplan reservation, an
out-of-scope region, or a black-box sub-cell placeholder in the GDS stream
needs a layer number that stays unclaimed as that coverage grows — not a
number picked only because it happens to be unused by today's deck code.

**Reserved for annotation: GDS layer numbers 990-999, any datatype.** Use
`(994, 0)` as the single canonical pair when one value is wanted. This was
verified against each PDK's **full official layer table**, not just the
numeric range the curated decks currently read (sky130's 64-97, gf180mcu's
21-127 — see "Coverage" above for why relying on that narrower range would
be unsafe: neither deck yet covers every layer its own PDK defines, e.g.
gf180mcu's implant/`LVPWELL`/`DNWELL`/high-voltage gaps noted above):

- **sky130**: cross-checked against KLayout's own bundled `sky130`/`sky130A`
  technology's full named `layer_map(...)` (the `.lyt`/`.lyp` triad KLayout
  ships for this PDK; 90 distinct layer numbers spanning drawing, pin,
  label, net, blockage, and metal-option/PSA sublayers) plus the SkyWater
  `open_pdks`-adjacent Mabrains KLayout PCell layer constants
  (`layers_def.py`, which additionally names `rpm_high`/`urpm` on layer 79 —
  a real sky130 device layer, used by this repo's own `res_generic_po`
  exclusion list in `sky130.py`, that is absent from the display `.lyp` but
  present in the process). Every sky130 layer number found across those
  sources is `<= 235` (`prBoundary.boundary`, `235/4`, the highest by a wide
  margin — the rest top out at `127`).
- **gf180mcu**: cross-checked against GlobalFoundries' own KLayout
  technology layer-properties file (`gf180mcu.lyp`, published in the
  `google/gf180mcu-pdk` repo and byte-identical across the `gf180mcuA`-`D`
  process-corner variants; 117 named entries covering every physical mask
  plus the DRC-result marker layers KLayout's ruledeck runner uses). The
  highest layer number present is `241` (a text/marker layer); the highest
  drawn-mask layer is `227`.

990-999 sits roughly 750 layer numbers above the highest number found in
either PDK's full official layer table. That gap is deliberate headroom for
"a second PDK family" whose own layer-numbering convention is unknown
today: every published open-source PDK layer table checked for this project
so far stays in the low hundreds, so a three-digit reservation starting at
990 is safe against any PDK that follows the same convention, with margin
to spare even if a given deck's *own* coverage keeps growing toward its
PDK's full layer set.

**Verifying the reservation stays inert.** Draw the annotation on a layer in
the 990-999 range, run `klt drc --deck <deck> <file> --format json` against
it, and confirm `coverage.layers_in_stream_without_rules` includes the
`"994/0"`-style entry for that layer while `violation_count` is unaffected
by it (see "`coverage`" below). The extraction-side analogue,
`ignored_layers`, provides the same check for `klt extract` — see
[`docs/cli/extract.md`](extract.md) → "Reserved annotation layer". Both
fields already exist and need no new code for this purpose: they are
exactly the "still inert" signal a future deck increment would have to
break for the reservation to stop being safe, and that break would be
visible in these fields the moment it happened.

`klt layers` and `klt stats` also **name** shapes on a reserved layer rather
than reporting them as ordinary, unrecognised geometry: each `layers[]`
entry carries an `annotation: true` field when its `(layer, datatype)` falls
in the 990-999 range — see [`docs/cli/layers.md`](layers.md) → "Semantics
and guarantees" and [`docs/cli/stats.md`](stats.md) → "Semantics and
guarantees".

## Limitation: whole-layout, flattened

Each rule is checked against the **whole layout**, flattened per top cell
(via `Cell.begin_shapes_rec`) — there is no `--top <cell>` filter to scope
the check to a single cell in this version. If a layout has multiple top
cells, each is checked independently and the `cell` field reports the top
cell a violation was found under.

Flattening the geometry does not, however, mean the report is blind to
hierarchy: see "Per-instance attribution" below for how each violation is
additionally mapped back to the originating placed instance.

## Per-instance attribution (`source_cell` / `source_path`)

For a hierarchical, machine-generated layout — an OpenROAD place-and-route
run with hundreds of placed standard cells, say — knowing only that a
violation is "somewhere under the top cell" is rarely actionable. So, after
each violation's `bbox` is computed on the flattened geometry, `klt drc`
maps that bbox back to the **innermost placed instance whose own bounding
box fully contains it** (via `Cell.begin_instances_rec_touching`, spatially
restricted so it stays cheap even for very large macros):

- `source_cell` — the cell-definition name of that innermost instance (e.g.
  `"sky130_fd_sc_hd__inv_2"`).
- `source_path` — the chain of cell names from the top cell's direct child
  down to `source_cell`, inclusive (e.g. `["block_a",
  "sky130_fd_sc_hd__inv_2"]`).

Both are `null` when the violation is contained in **no single** instance —
top-level routing/geometry, or a `bbox` that straddles an instance boundary
(a spacing or enclosure violation *between* two adjacent placements belongs
to neither, so the top `cell` remains the only honest attribution). A cell
placed more than once is **not conflated**: each placement's violations fall
inside only that placement's world bounding box, so they attribute to the
correct occurrence — the shared `source_cell`/`source_path` name the reused
definition, while the distinct `bbox` locates the specific placement.

These two fields are **purely additive** to the JSON contract — the existing
`cell` field keeps its meaning (the top cell) and the sort key is unchanged
— so this did not bump `drc`'s `schema_version` (see
[`docs/json-contract.md`](../json-contract.md)).

## Database units (dbu)

Each deck's `DrcRule` thresholds are authored in database units against a
fixed **nominal dbu** (currently `0.001` µm — 1 nm per unit — for both
`sky130` and `gf180mcu`; see each deck's `NOMINAL_DBU_UM` constant in
`src/klayout_tools/decks/`). A GDSII/OASIS stream's own `Layout.dbu` is not
required to match that nominal value — foundry PCell libraries, GDS written
by other tools, and older flows commonly use a different dbu (5 nm, 10 nm,
etc.), and that is an ordinary input, not an edge case.

`klt drc` handles this automatically: before running any check, every
threshold is rescaled by `nominal_dbu_um / <the input file's own dbu>`, so
the same physical geometry produces the identical `status` /
`violation_count` / `violations[]` regardless of the input stream's database
unit. You do not need to pre-convert or normalize a layout's dbu before
running `klt drc` against it. The `dbu_um` field in the JSON output (below)
simply echoes the input layout's own database unit for reference — bounding
boxes and polygon coordinates in `violations[]` are reported in that same
(input) database unit, not the deck's nominal one.

## JSON schema (the contract)

**JSON is the API.** Human-readable text output is a courtesy; the JSON
schema below is the stable contract. Per the project's rules, **breaking
(renaming, removing, or retyping) a field is a breaking change**, and rule
`id` values are part of that contract — a rule id is never renumbered or
repurposed once shipped. New fields may be added without breaking the
contract, so consumers should ignore unknown fields. See
[`docs/json-contract.md`](../json-contract.md) for the envelope shared across
all `klt` commands (`schema_version`, error shape, exit codes).

```json
{
  "schema_version": 1,
  "file": "design.gds",
  "deck": "sky130",
  "dbu_um": 0.001,
  "status": "clean",
  "violation_count": 0,
  "rule_counts": {},
  "violations": [],
  "coverage": {
    "deck_layers": ["22/0", "30/0", "33/0", "34/0"],
    "layers_checked": ["22/0", "30/0"],
    "layers_in_stream_without_rules": ["46/0", "75/0"],
    "rules_skipped": ["metal1.width.1", "metal1.space.1"]
  },
  "provenance": {
    "klt_version": "0.4.2",
    "klayout_version": "0.29.8",
    "pdk": null,
    "deck": { "name": "sky130", "content_hash": "sha256:<hex>" },
    "input": { "content_hash": "sha256:<hex>" }
  }
}
```

A `"clean"` `status` with a non-empty `coverage.layers_in_stream_without_rules`
means "clean, and here is exactly what was not looked at" — geometry drawn
entirely on layers the selected deck has no rules for reports `"clean"` (the
engine correctly found no violations among the rules it *could* run), but
`coverage` is what lets a consumer distinguish that from a genuinely
fully-checked pass. See "Coverage" above for why this is common, not an edge
case: both shipped decks cover a deliberate layer subset of their PDK.

On a run with findings:

```json
{
  "schema_version": 1,
  "file": "design.gds",
  "deck": "sky130",
  "dbu_um": 0.001,
  "status": "violations",
  "violation_count": 1,
  "rule_counts": { "poly.width.1": 1 },
  "violations": [
    {
      "rule": "poly.width.1",
      "description": "minimum poly width",
      "check": "width",
      "layer": "poly.drawing",
      "cell": "TOP",
      "source_cell": "sky130_fd_sc_hd__inv_2",
      "source_path": ["sky130_fd_sc_hd__inv_2"],
      "bbox": { "left": 0, "bottom": 0, "right": 100, "top": 2000 },
      "polygon": [[0, 0], [0, 2000], [100, 2000], [100, 0]]
    }
  ],
  "coverage": {
    "deck_layers": ["65/20", "66/20", "66/44", "68/20"],
    "layers_checked": ["65/20", "66/20", "66/44"],
    "layers_in_stream_without_rules": [],
    "rules_skipped": []
  }
}
```

### Top-level fields

| Field             | Type                     | Description                                                             |
| ----------------- | ------------------------ | ------------------------------------------------------------------------ |
| `schema_version`  | integer                  | Version of this command's JSON shape (starts at `1`; per-command).       |
| `file`            | string                   | The input path exactly as provided on the command line.                  |
| `deck`            | string                   | The deck name used (`"sky130"` or `"gf180mcu"`).                         |
| `dbu_um`          | number (float)           | The input layout's database unit in micrometres, same semantics as `klt layers`. See "Database units (dbu)" above — rule thresholds are rescaled to this value automatically, so it need not match any deck's nominal dbu. |
| `status`          | `"clean"` \| `"violations"` | Never `"error"` — a failed run does not emit this envelope at all (see Exit codes). |
| `violation_count` | integer                  | `len(violations)`.                                                       |
| `rule_counts`     | object\<string, int\>    | Per-rule-id violation counts; keys sorted for determinism.               |
| `violations`      | array\<object\>          | One entry per violating geometry, see below.                             |
| `coverage`        | object                   | What was actually checked vs. what's present in the input stream, see below. |
| `provenance`      | object                   | Shared reproducibility block (`klt_version`, `klayout_version`, `pdk`, `deck`, `input`) defined once in [`docs/json-contract.md`](../json-contract.md). `pdk` is always `null` (`klt drc` resolves no PDK); `deck` pins the selected rule deck by name and `sha256:` content hash; `input` pins the input layout file (`path`) by `sha256:` content hash. |

### `violations[]` entries

| Field         | Type                | Description                                                                 |
| ------------- | ------------------- | ----------------------------------------------------------------------------- |
| `rule`        | string               | Stable rule id (e.g. `"poly.width.1"`) — never renumbered once shipped.       |
| `description` | string               | Human-readable rule description.                                              |
| `check`       | string               | The check kind (`"width"`, `"space"`, `"enclosing"`, etc.). For `"enclosing"`/`"enclosed"`, `check` does not distinguish a marginal-distance violation from a zero-overlap escape violation — both are reported under the same `check` string; see "`\"enclosing\"`/`\"enclosed\"` also catch zero-overlap escapes" above. |
| `layer`       | string               | The deck's own layer name (e.g. sky130's `"poly.drawing"` or gf180mcu's `"Poly2"`) if the deck names the layer, else `"<layer>/<datatype>"`. |
| `cell`        | string               | Name of the top cell the violation was found under. (The check is flattened per top cell — see "Limitation: whole-layout, flattened" — so this is always a top cell, never a sub-instance.) |
| `source_cell` | string \| null       | Cell-definition name of the *innermost placed instance* whose bounding box fully contains this violation's `bbox` — the originating standard cell for a hierarchical, machine-generated layout. `null` when the violation is contained in no single instance: top-level geometry, or a `bbox` that straddles an instance boundary (a violation *between* two placements belongs to neither). See "Per-instance attribution" below. |
| `source_path` | array\<string\> \| null | The instance chain from the top cell's direct child down to `source_cell` (inclusive), e.g. `["block_a", "sky130_fd_sc_hd__inv_2"]`; a single-element list for a directly-placed leaf. `null` exactly when `source_cell` is `null`. |
| `bbox`        | object (dbu ints)    | `{"left", "bottom", "right", "top"}`, in database units.                     |
| `polygon`     | array\<[x,y]\> \| null | Vertices in database units, or `null` if the check produced a degenerate edge pair that could not be converted to a polygon. |

`violations` is sorted by
`(rule, cell, bbox.left, bbox.bottom, bbox.right, bbox.top)` so repeated runs
against the same input produce identical, diff-clean output. The full bbox is
part of the key (not just the lower-left corner) so violations sharing a corner
are still totally ordered — output stays canonical across platforms and KLayout
builds regardless of the engine's internal shape-enumeration order.

### `coverage`

Additive field (see [`docs/json-contract.md`](../json-contract.md) — no
`schema_version` bump): what the run actually checked, distinct from what it
found. `status: "clean"` alone cannot tell a consumer apart "genuinely
checked and passed" from "the deck had no rules for anything this layout
draws" — `coverage` closes that gap. `status`'s own two-value contract
(`"clean"` / `"violations"`) is unchanged; a non-empty
`layers_in_stream_without_rules` does not change `status`.

| Field                             | Type            | Description                                                                 |
| ---------------------------------- | --------------- | ----------------------------------------------------------------------------- |
| `deck_layers`                      | array\<string\> | Every `"<layer>/<datatype>"` the selected deck's rules reference — a static property of the deck, independent of the input stream. Sorted ascending by `(layer, datatype)`. |
| `layers_checked`                   | array\<string\> | The subset of `deck_layers` actually present in this stream (i.e. found via `Layout.find_layer(...)`), matching what the per-rule check loop actually ran against. Sorted ascending by `(layer, datatype)`. |
| `layers_in_stream_without_rules`   | array\<string\> | `"<layer>/<datatype>"` pairs present in the input stream that no active rule in the selected deck references at all — the load-bearing field: turns `"clean"` into "clean, and here is exactly what was not looked at." Sorted ascending by `(layer, datatype)`. |
| `rules_skipped`                    | array\<string\> | Rule ids silently skipped because their `layer`/`other_layer` was absent from this stream. Sorted alphabetically. |

`layers_checked` and `layers_in_stream_without_rules` are computed from the
input stream's own layer table (reusing the same per-layer enumeration
`klt layers` uses — see [`klt layers`](layers.md)), not from shape counts: a
layer present in the stream's layer table with zero shapes still counts as
"in the stream" for this purpose, matching `Layout.find_layer(...)`'s own
semantics.

## Exit codes

| Code | Meaning                                                     |
| ---- | ------------------------------------------------------------ |
| `0`  | Ran clean — no violations.                                   |
| `1`  | Failed to run — bad file, unknown `--deck`, or engine error. |
| `2`  | Usage error (missing argument, bad `--format` value) — from argparse. |
| `3`  | Ran successfully, violations found.                          |

`2` is deliberately **not** reused for "violations found" even though some
DRC tools use a 2-way error/warning split on that code: `2` is already
spoken for by argparse in every other `klt` subcommand, and a script
depending on `klt drc`'s exit code must be able to tell "you typed something
wrong" apart from "the deck ran and found problems" apart from "the tool
itself failed."

On error (exit 1), a concise message is written to **stderr** and nothing is
written to stdout. No Python traceback is printed — including for an unknown
`--deck` name.

- `--format text` (default): a plain-text line prefixed `klt drc:`.
- `--format json`: the documented JSON error envelope (see
  [`docs/json-contract.md`](../json-contract.md)):

  ```json
  { "schema_version": 1, "error": { "command": "drc", "message": "unknown deck 'nope' (available: gf180mcu, sky130)" } }
  ```

Note that exit code `3` is a per-command extension of the shared exit-code
table: unlike `0`/`1`/`2`, it is specific to `klt drc` and means the deck ran
successfully and the documented success payload *is* on stdout.

## Worked example

See `examples/drc/`: `generate.py` builds `example.gds` (a poly bar
narrower than the minimum width, and a diff shape under-enclosing a licon1
contact — two seeded violations — plus one clean, wide met1 shape), and
`example.drc.json` is the exact expected output of:

```
klt drc examples/drc/example.gds --deck sky130 --format json
```
