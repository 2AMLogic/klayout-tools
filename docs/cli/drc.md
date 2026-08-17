# `klt drc`

Run a headless DRC rule deck against a GDSII or OASIS layout stream and
report violations as structured data.

```
klt drc <file> --deck sky130|gf180mcu|sg13g2 [--top <cell>] [--format text|json]
klt drc <file> --engine klayout [--deck-file <path> | --pdk <variant> [--pdk-root <path>]] [--timeout-s <seconds>] [--format text|json]
```

- `<file>` — path to a GDSII (`.gds`) or OASIS (`.oas`) file. KLayout
  auto-detects the stream format on read; the extension is not authoritative.
- `--deck` — the DRC deck to run for `--engine curated` (the default);
  required in that case, ignored for `--engine klayout`. Currently: `sky130`,
  `gf180mcu`, `sg13g2`.
- `--top` — top cell to check when the stream has more than one; omit to
  check every top cell (today's default, unchanged). `coverage` (see below)
  is scoped along with it — `layers_in_stream_without_rules`/`layers_checked`
  reflect only `<cell>`'s own hierarchy, not the whole stream. A named cell
  absent from the stream exits `1` with a clean error. **Not supported yet
  for `--engine klayout`** (see "Engine" → "klayout" below).
- `--engine` — `curated` (default) or `klayout` (issue #565, opt-in). See
  "Engine" below.
- `--deck-file` — explicit path to a KLayout DRC-DSL script (`.lydrc`/`.drc`)
  to run with `--engine klayout`, overriding `--pdk`/`--pdk-root` resolution.
- `--pdk` / `--pdk-root` — PDK variant/install-root to resolve the native
  deck script from, for `--engine klayout` (same resolution semantics as
  `klt lef-abstract`'s `--pdk`/`--pdk-root`, see [`klt pdk`](pdk.md)).
- `--timeout-s` — wall-clock budget in seconds for the `klayout` subprocess
  (`--engine klayout` only; default `300`).
- `--format` — `text` (default, a human-readable summary) or `json`.

## Engine

### `"curated"` (default)

`klt drc` runs fully headless via the pip `klayout` package's native
`klayout.db.Region` check primitives (`width_check`, `space_check`,
`separation_check`, `enclosing_check`, `enclosed_check`, `notch_check`,
`overlap_check`) — the same C++ polygon-processing engine that backs
KLayout's higher-level DRC-DSL scripts, invoked directly instead of through
the script runner. By default there is **no dependency on the standalone
`klayout` application binary or its `.drc`/`.lydrc` script runner** — only
`pip install klayout` (already this repo's sole runtime dependency), so the
command runs anywhere that already runs in CI.

A "deck" is our own declarative rule table (`DrcRule`: rule id, layer,
check kind, threshold, optional second layer) that drives those check
primitives — not the official sky130 `.lydrc` script executed verbatim. See
"Coverage" below for what that means for rule fidelity.

### `"klayout"` (issue #565 — opt-in, run the PDK's own native deck)

Wraps the standalone `klayout` application binary as a subprocess, the same
"wrap a proven engine" pattern `klt lvs`'s `"netgen"` engine uses (issue
#343) — see [`klt lvs`](lvs.md) → "Engine" → `"netgen"` for the sibling
writeup this one mirrors closely. Requires a `klayout` binary on `$PATH` —
not the pip `klayout` package this repo otherwise depends on exclusively; a
missing binary is a clear, actionable error (exit 1), never a traceback.

Invokes:

```
klayout -b -r <deck_file> -rd input=<file> -rd report=<tmp>.lyrdb
```

— the standard batch-mode DRC-DSL invocation KLayout's own tooling
documents, and the exact usage comment a real sky130A install's own
`libs.tech/klayout/drc/sky130A.lydrc` embeds. The report is always written
as a `.lyrdb` (RDB XML) file and parsed into the same `violations[]`/
`rule_counts` shape the curated engine produces — never trusting the
subprocess's exit code alone: `klayout -b -r` can exit `0` even when the
deck script itself errored out before reaching its own `report(...)` call,
so the *report file's own presence* is this engine's only trustworthy
completion signal (mirroring `_run_netgen_lvs`'s "no log file at all" check
in `lvs.py`). A timeout (`--timeout-s`, default `300`) or a malformed/
unparseable report both raise a clean error rather than guessing.

**Deck resolution.** `<deck_file>` is resolved, in order: `--deck-file` (an
explicit path) if given; otherwise `klayout_tools.pdk.drc_deck_file(variant=
--pdk, root=--pdk-root)` (issue #565), which resolves a PDK-native,
directly-runnable `<variant>.lydrc`/`.drc` script from the same
`libs.tech/klayout/drc/` directory `klt pdk find` already discovers —
mirroring `klt lvs`'s `netgen_setup_file()` PDK-asset resolver one asset
area over. Verified against a real, `volare`-fetched sky130A install: sky130
ships exactly this shape, a single self-contained `sky130A.lydrc`. A PDK
that instead ships its native deck as topic fragments under
`drc/rule_decks/*.drc` plus a Python assembly/CLI wrapper (`drc/run_drc.py`,
gf180mcu's real shape) has no single ready-to-run file this resolver can
find without either reassembling those fragments itself — the harder,
riskier "concatenate prologue/rule-tables/epilogue by hand" path this
engine's design deliberately avoids — or driving `run_drc.py`'s own bespoke
CLI contract, which is out of scope for this uniform `--engine klayout`
flag. For that shape, resolution returns nothing and `klt drc` exits with an
actionable error naming `--deck-file` as the way forward; a caller who has
already produced a merged deck (e.g. by running the PDK's own `run_drc.py
--macro_gen` ahead of time) can still reach it that way.

**Known limitations, relative to the curated engine:**

- **No `--top` support yet.** An arbitrary PDK-native DRC-DSL script has no
  standard `-rd`-settable "restrict to this one top cell" variable this
  engine can rely on generically — `sky130A.lydrc`, e.g., always reads the
  whole `$input` stream. Passing `--top` alongside `--engine klayout` is a
  clean error rather than a silently-ignored request.
- **No `coverage` population.** Unlike the curated engine's declarative
  `DrcRule` table, an external deck's rule set is opaque to `klt drc` — every
  `coverage` sub-field (`deck_layers`, `layers_checked`,
  `layers_in_stream_without_rules`, `rules_skipped`, `voltage_domain_warnings`,
  `deck_scope`) is an empty list for this engine, never fabricated.
- **`check` is always `"external"` and `layer` echoes the rule id.** An RDB
  report's `<category>` has no structured `width`/`space`/... check-kind
  identity or separate layer name the way this repo's own `DrcRule` does —
  only whatever the deck's own `report(...)`/`.output(...)` calls named.
- **No per-instance attribution.** `source_cell`/`source_path` are always
  `null` for this engine — mapping a violation back to its originating
  placed instance (see "Per-instance attribution" below) needs this
  module's own instance-tree walk against the *input* layout, which an RDB
  report does not carry.
- **`bbox`/`polygon` fidelity depends on the RDB value kind.** Every
  coordinate pair embedded in an item's reported geometry contributes to
  `bbox` (converted from the RDB's micrometre user units back to the input
  layout's own database units); `polygon` is populated only for a value
  reported as a plain closed polygon (`polygon: (x,y;x,y;...)`) — every
  other kind (`edge-pair:`, `box:`, `edge:`, ...) still bounds `bbox` but
  leaves `polygon` `null`, matching the curated engine's own "`null` if the
  check produced a degenerate edge pair" convention for the analogous case.
- **sky130A.lydrc's own `FEOL`/`BEOL` rule-group toggles are hard-coded Ruby
  locals, not `-rd`-settable script variables** — verified for issue #747 by
  running the golden-pair manifest's sky130 fixtures against a real,
  `volare`-fetched `sky130A.lydrc`: the script literally sets `FEOL = false`
  / `BEOL = true` (its own "do not change" comment) a few lines into the
  file, and there is no `$`-prefixed global this engine's `-rd` mechanism
  can override to flip it. **This means `--engine klayout` against sky130's
  default-resolved deck never checks any front-end-of-line rule** (poly,
  diff/difftap, tunm, vpp, capm, ...) **at all, regardless of what the
  input layout actually draws** — only back-end-of-line rules (li, mcon,
  m1, and everything after `if BEOL` in the script) are ever evaluated by
  this invocation shape. A caller relying on this engine as a ground-truth
  oracle for a FEOL-layer rule gets a silent `"clean"` for that layer's
  violations, not a warning — the same trap issue #747's own golden-pair
  cross-check hit and had to record as `expected_disagreement` rather than
  "fix" (there is nothing in the curated deck to fix; the native deck
  simply never ran that check). See `tests/golden_deck/README.md` for the
  concrete rule ids affected.
- **`met2`/`via1` (met1<->met2) rules live in a *different* script file,
  `sky130A_mr.drc`, not `sky130A.lydrc`.** `sky130.py`'s own `met2.width.1`/
  `met2.space.1`/`via.width.1`/`via.space.1` were transcribed from
  `sky130A_mr.drc` (a separate, standalone rule-deck script — see that
  module's own "met2/via rule coverage" provenance note), which
  `pdk.drc_deck_file()`/this engine's resolver never reaches (it only
  resolves `<variant>.lydrc`, i.e. `sky130A.lydrc`, per its own naming
  convention above). This is a *provenance* difference, not a coverage gap:
  the resolved `sky130A.lydrc` does carry its own met2/via1 rules in its
  `BEOL` block (`m2.1`, `m2.2`, `m2.5`, `via.1a`, ...), so cross-checking
  against it is meaningful — it is simply checking that file's rules, not
  the `sky130A_mr.drc` text these four were transcribed from. Verified for
  issue #747 against a real install: `met2.width.1`/`met2.space.1` agree
  with the native deck outright (tripping `m2.1`/`m2.2` respectively),
  while `via.width.1`/`via.space.1` disagree on their *clean* fixture
  because `sky130A.lydrc`'s `via.1a` demands an **exact** 0.15um square
  (`edges.without_length(0.15)`, i.e. min *and* max) where this engine's
  `width_check` enforces only a minimum — recorded as an
  `expected_disagreement` in `tests/golden_deck/sky130/manifest.json`. A
  caller who specifically needs the `sky130A_mr.drc` formulation of these
  rules would have to point `--deck-file` at that file explicitly.

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
`derived_layer` field (a `DerivedLayer(base, sized_by_um, intersect_with,
mode)`, issue #345) expresses exactly this: the checked region becomes a
computed combination of two drawn layers instead of `layer`'s own raw drawn
shapes. `layer` stays required and continues to name the rule's reporting
identity (`violations[].layer`, `coverage`), independent of
`derived_layer`'s two input layers.

`mode` selects which combination:

| `mode` | Checked region | Used by |
| --- | --- | --- |
| `"sized_intersection"` (default) | `intersect_with.interacting(base) & base.sized(sized_by_um)` — only shapes of `intersect_with` that already touch the *unsized* `base` region somewhere, clipped to `base`'s outline oversized by `sized_by_um` | gf180mcu `mim.enclosing.via4.1`, `mim.space.1` |
| `"overlapping"` | `base.overlapping(intersect_with.sized(sized_by_um))` — whole `base` polygons that share **area** with the (optionally guard-banded) second layer | gf180mcu `comp.width.mv.1`, `comp.space.mv.1` |
| `"not_interacting"` | `base.not_interacting(intersect_with.sized(sized_by_um))` — whole `base` polygons that do not touch it at all | gf180mcu `comp.width.1`, `comp.space.1` |

An unknown `mode` is a `DrcError` naming the rule id, not a silent fallback
to the default.

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

A `"separation"` rule (two-layer, unlike `"enclosing"`'s single derived
region above) can combine `derived_layer` with an `other_layer` equal to
`derived_layer.intersect_with` — gf180mcu's `mim.space.1` (issue #1033) does
exactly this: `region` is the derived virtual bottom plate, `other_layer` is
`Metal4` itself. `run_drc()` scopes that `other_layer` side too: rather than
a plain set-subtraction of the (partial, sizing-clipped) derived region,
it excludes the *whole* `Metal4` polygon for any polygon that already
overlaps the raw, unsized `base` region anywhere — the same
`.interacting(base)` pre-filter `region`'s own construction already applies.
This matters because a `Metal4` shape that only partly falls inside the
oversized `base` window would otherwise be split by a plain subtraction into
a "derived" fragment and a "leftover" fragment that exactly touch at the
sizing cutoff (zero gap) — a real `separation_check` reports that touching
seam as a violation, an artefact of one continuous physical shape being cut
in two by the derivation, not a real spacing gap. Excluding matching
`other_layer` polygons wholesale avoids this: a `Metal4` shape either
contributes to the plate (and is fully excluded from "other") or is
genuinely separate geometry (and is fully included), never both.

That exclusion is total, so the excluded polygons are never measured
against *each other* by the `separation_check` — which for `mim.space.1`
would drop half of the official rule (`MIMTM.1` is spacing to the bottom
plate metal "whether adjacent MiM **or** routing metal", and a neighbouring
capacitor's plate metal is excluded along with this one's). `run_drc()`
measures that half separately, as a peer-to-peer `isolated_check` among the
excluded plate-bearing polygons, and reports its edge pairs under the same
rule id — the same additive "supplementary result under one rule id"
pattern `_run_check`'s `outside_region` escape term already uses (#318).
`isolated_check` measures spacing between *different* polygons only, unlike
`space_check`, which also measures intra-polygon notches: one capacitor's
own slotted wide plate metal is a single merged polygon, and its slot is
not "spacing to another bottom plate". The peer check runs on the whole
plate-bearing polygons rather than the sizing-clipped plates, so a shared
bottom plate carrying two top plates further apart than `2 * sized_by_um`
is still one polygon with no gap to measure; the residual conservatism that
trades for is documented in `gf180mcu.py`'s `mim.space.1` note.

### Voltage-domain rule pairs (`_LV`/`_MV`, issue #1110)

The `"overlapping"`/`"not_interacting"` modes exist for a different problem:
a DRM rule whose threshold *depends on whether the geometry is marked*,
published as two columns rather than one number. gf180mcu's `Dualgate`
(55/0) marks its 5 V/6 V thick-oxide domain, and the DRM's `Comp`
width/space rules are 30 % larger inside it:

| Official rule | `klt drc` rule id | Threshold | Checked region |
| --- | --- | --- | --- |
| `DF.1a_LV` | `comp.width.1` | 0.22 um | `Comp` polygons touching no `Dualgate` |
| `DF.1a_MV` | `comp.width.mv.1` | 0.30 um | `Comp` polygons overlapping `Dualgate` |
| `DF.3a_LV` | `comp.space.1` | 0.28 um | `Comp` polygons touching no `Dualgate` |
| `DF.3a_MV` | `comp.space.mv.1` | 0.36 um | `Comp` polygons overlapping `Dualgate` |

Before #1110 the two `_LV` rules checked the *whole* `Comp` layer, so a
0.25 um stripe drawn entirely inside `Dualgate` — illegal at 5 V — reported
`status: "clean"` against the 3.3 V column (issue #552's reproducer). It now
reports a `comp.width.mv.1` violation, while the identical stripe drawn
*outside* `Dualgate` stays clean, unchanged.

Two properties of the selection are load-bearing, and both are transcribed
verbatim from the PDK's own executable deck
(`libs.tech/klayout/drc/rule_decks/comp.drc`: `comp_56v =
comp.overlapping(dualgate)`, `comp_3p3v =
comp.not_interacting(v5_xtor).not_interacting(dualgate)`):

- **Whole polygons, never clipped.** A `Comp` shape only partly inside the
  marker is assigned to one column in its entirety. A boolean
  `and`/`not` clip would instead cut it at the marker's edge and a
  `width`/`space` check would then measure that cut as a narrow sliver that
  exists nowhere in the drawn layout.
- **An absent marker layer never disables the unmarked rule.** A stream that
  draws no `Dualgate` at all — the ordinary thin-oxide-only case — still
  runs `comp.width.1`/`comp.space.1` against the full `Comp` layer:
  `"not_interacting"` treats a missing second layer as an empty region
  rather than skipping the rule (the `"overlapping"` halves do skip, and
  appear in `coverage.rules_skipped`).

Only these two rule pairs are voltage-scoped today. Every other gf180mcu
rule still encodes the 3.3 V column, which is what
[`coverage.voltage_domain_warnings`](#coveragevoltage_domain_warnings) now
warns about — see that section for how the warning's gate distinguishes
them.

### `"area"` / `"density"` / `"antenna"` check kinds (issue #812)

Three more check kinds exist alongside `"width"`/`"space"`/`"notch"`
(single-layer) and `"separation"`/`"enclosing"`/`"enclosed"`/`"overlap"`
(two-layer) above. None of the shipped `sky130`/`gf180mcu` decks author a
rule of any of these three kinds yet — that's a separate follow-on issue;
this section documents the primitives themselves.

**`"area"`** — single-layer, minimum and/or maximum polygon area. Driven by
`klayout.db.Region.with_area(min_area, max_area, inverse=True)`, which
returns the *violating* polygons directly (a `Region`, not the `EdgePairs`
collection every check above returns) — each is reported as its own
`violations[]` entry, the same way an `"enclosing"`/`"enclosed"` rule's
zero-overlap-escape term already is. Uses two new `DrcRule` fields instead
of `threshold_dbu`: `area_min_dbu2`/`area_max_dbu2`, in **square** database
units of the deck's own nominal dbu — rescaled by `dbu_scale ** 2` (the
*squared* nominal-to-actual dbu ratio), not the plain `dbu_scale` a linear
distance threshold uses, since an area scales with the square of a linear
rescaling. At least one of the two must be set. `other_layer`/
`derived_layer` are unused.

**`"density"`** — single-layer, windowed area-fill fraction. No native
`Region` primitive computes this, so `run_drc` tiles the checked layer's own
drawn extent (`region.bbox()` — there is no chip-boundary layer concept in
this engine, unlike a real density-check flow that scopes to a floorplan
boundary) into non-overlapping `density_window_um` x `density_window_um`
squares and flags any window whose covered-area fraction falls outside
`[density_min, density_max]` (either bound may be omitted for "no
floor"/"no ceiling"; at least one must be set). Each violating window is
reported as one `violations[]` entry, `bbox`/`polygon` set to the window's
own rectangle. `density_window_um` is a real physical size in micrometres,
rescaled against the *input layout's own* `dbu` directly — like
`DerivedLayer.sized_by_um`, not like `threshold_dbu`/the area fields above,
since a window size has no natural "nominal dbu" the way a distance or area
threshold does. Only whole windows entirely inside the checked extent are
tiled; a remainder narrower than one window at the right/top edge is left
unchecked — a documented approximation of this first cut, not a defect.

**`"antenna"`** — two-layer, and the one deliberate approximation of the
three. **This is not a net-aware antenna/process-antenna-area-ratio (PAAR)
check.** A real antenna rule accumulates conductor area *per net*, reset at
each via level — this purely-geometric engine has no connectivity/net
information available to `drc.py` (net extraction is a separate code path,
`extract.py`, not wired in here), so it cannot compute that. Instead,
`run_drc` sums `layer`'s and `other_layer`'s total merged area across the
*whole checked cell* (no per-net split) and reports a single flat violation
when their ratio exceeds the new `antenna_ratio_max` field — or when `layer`
has nonzero area but `other_layer` has none at all anywhere in the cell (an
undefined/infinite ratio, always worse than any finite maximum). The
reported `bbox` is `layer`'s own merged bounding box and `polygon` is always
`null`, since no single real polygon "is" this flat, whole-cell violation
the way there is for an `"area"` check. This mirrors how `"enclosing"`/
`"enclosed"` above already document their own approximation of the official
rule they check — a future golden-pair author authoring a real `"antenna"`
rule against this primitive must not assume more precision (in particular,
no per-net isolation) than it actually has.

## Coverage

The `sky130` deck is a **curated starter subset**, not the full sky130
design rule manual (which spans hundreds of rules). It currently covers 17
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

Two of the seventeen rules approximate an official rule defined on a
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
always the real, unmodified source values, with exactly one documented
exception described next. The official deck's `m2.6`
(minimum met2 area, 0.0676 um²) is **not** transcribed: authoring it (and
any other `"area"`/`"density"`/`"antenna"` rule) is out of scope for the
check-primitive work that added those three kinds (issue #812, see
"`"area"`/`"density"`/`"antenna"` check kinds" above) — tracked as a
candidate follow-on rather than silently dropped.

`li1.enclosing.licon1.1` (issue #551) is that one exception, and the only
rule in either deck whose threshold is deliberately *not* its source value.
It closes the same asymmetry as gf180mcu's `metal1.enclosing.contact.1`
below — `diff` and `poly` (the layers *below* `licon1`) were checked by
`licon.5`/`licon.8`, but `li1`, the conductor immediately *above* it, was
not — but its source rule, `li.5`, requires its 0.08 um margin only on **two
adjacent edges** of each cut, a per-edge-pair conditional `DrcRule`'s
vocabulary cannot express. Real layout takes advantage of that: a
minimum-width `li1` strap sits exactly flush with the cut on the other two
edges, so an unconditional 0.08 um enclosure check flags correct-by-
construction geometry (measured against this repo's own corpus: 6-56 sites
per standard cell, 6566 across the OpenROAD-produced GCD macro). The rule is
therefore transcribed at li.5's *unconditional floor* — a 0.0 um threshold,
i.e. "`li1` must actually cover the `licon1` cut it lands on", carried
entirely by the zero-overlap escape term described above. That catches the
defect class the issue reports (a conductor missing part of its cut) with no
false positives on correct geometry; the 0.08 um two-adjacent-edges half
stays uncovered, like the end-of-line variants noted for gf180mcu below.

The `gf180mcu` deck is likewise a **curated starter subset**: 44 rules —
width, spacing, and enclosure checks across the `Poly2`, `Comp`
(diffusion/active), `Contact`, `Via1`-`Via4`, `Metal1`-`Metal5`, and
`MetalTop`
layers (two of them, `Comp` width and spacing, as `_LV`/`_MV` pairs scoped
to the `Dualgate` voltage-domain marker — see "Voltage-domain rule pairs"
above), plus a first increment of well/substrate-tap coverage (`Nwell`
spacing and Nwell-tap enclosure), one bipolar (BJT)-specific device rule
(`DRC_BJT` mark-layer separation), the MiM capacitor stack
(`Metal4`/`FuseTop` bottom-/top-plate spacing and overlap, plus the virtual
bottom plate's `Via4` overlap, see "Sized/derived-layer rules" above), and
the bond-pad chapter's one hard, coded rule (`Metal5`'s overlap of the
`Pad` passivation opening, 5LM variant only) — transcribed from the
published GlobalFoundries 180nm MCU **Design Rule Manual**
([`google/gf180mcu-pdk`](https://github.com/google/gf180mcu-pdk),
`docs/physical_verification/design_manual/`; Apache License 2.0),
specifically the "7.4 Nwell" (`NW.*`), "7.5 Comp" (`DF.*`), "7.7 Poly2"
(`PL.*`), "7.12 Contact" (`CO.*`), "7.13 Metaln" (`Mn.*`, extended to
`n = 2..5`), "7.15 MetalTop" (`MT.*`), "9.1 Bond Pad" (`PAD.*`), "10.4.2
MIM Capacitor, Option B" (`MIMTM.*`), and "10.7 DRC_BJT Mark Layer"
(`BJT.*`) sections. Unlike
sky130 (transcribed from a live, KLayout-runnable `.lydrc` script), most of
this deck cites the DRM's own published rule ids (e.g. `"DF.1a"`, `"PL.1"`,
`"CO.1"`, `"Mn.1"`, `"MT.1"`, `"MIMTM.1"`, `"NW.2a"`, `"DF.4d"`, `"BJT.3"`)
and numeric values directly, because the snapshot of the companion KLayout
DRC-deck repo
([`google/globalfoundries-pdk-libs-gf180mcu_fd_pv`](https://github.com/google/globalfoundries-pdk-libs-gf180mcu_fd_pv))
originally consulted did not open-source the core FEOL/BEOL width/space/
enclosure checks as executable rule-deck code. That is no longer true of a
current PDK build: the conductor-over-cut enclosure rules added in issue
#551 (`CO.6`, `V1.3a`, `Vn.3b`/`Vn.4a` — see the next paragraph) are
re-derived from a real fetched install's own
`libs.tech/klayout/drc/rule_decks/{contact,via1,via2,via3,via4}.drc`
(`volare enable gf180mcu c6d73a35f524070e85faff4a6a9eef49553ebc2b`, the same
open_pdks build this repo already cites for gf180mcu's LVS-derived
extraction deck), and each cites both its DRM rule id and the executable
statement it was re-derived from.

The conductor *above* a cut is checked as well as the layers below it
(issue #551). `Contact` previously had `CO.3` (Poly2) and `CO.4` (Comp) —
both *below* the cut — and nothing above it, so a contact whose Metal1 strap
missed one of its edges reported `status: clean`. `metal1.enclosing.contact.1`
(`CO.6`, 0.005 um) closes that, and eight more rules do the same for every
via level in both directions: `metal1`/`metal2` around `Via1` (`V1.3a` at a
literal 0.0 um, `V1.4a` at 0.01 um), `metal2`/`metal3` around `Via2`
(`V2.3b`/`V2.4a`), `metal3`/`metal4` around `Via3` (`V3.3b`/`V3.4a`), and
`metal4`/`metal5` around `Via4` (`V4.3b`/`V4.4a`), all 0.01 um. Each maps
exactly onto the PDK deck's own `cut.enclosed(metal, d) OR cut.not(metal)`
form — the same pair of conditions `klt drc` reports for an `"enclosing"`
rule (see "`"enclosing"` / `"enclosed"` also catch zero-overlap escapes"
above) — so none of the nine is an approximation. Their **end-of-line**
companions (`CO.6a`/`CO.6b`, `Vn.3c`/`Vn.3d`, `Vn.4b`/`Vn.4c`) are *not*
transcribed: each conditions a 0.06 um margin on a narrow-metal
end-of-line predicate that `DrcRule`'s vocabulary cannot express, the same
class of gap as sky130's `m2.6` above.

Nineteen of the forty-four gf180mcu rules approximate an official DRM rule in
some way — either a compound-layer context our single/two-layer check
primitives can't isolate (`comp.space.1`/`comp.space.mv.1`, `poly2.space.1`,
`poly2.width.1`, `nwell.enclosing.comp.1`), a marker layer this deck's
curated layer set doesn't draw (`comp.width.1`/`comp.space.1` model the
`Dualgate` half of the PDK's own thin-oxide region but not its `v5_xtor`
half; `comp.width.mv.1` doesn't exclude the `mvsd`/`mvpsd` LDMOS drain
markers the official `DF.1a_MV` does), a bound our primitives don't support
(`contact.width.1`'s and `via1.width.1`-`via4.width.1`'s fixed-size squares,
approximated as a minimum only), an array-density context our primitives
have no notion of (`via1.space.1`-`via4.space.1`, which use the ordinary
two-via `Vn.2a` threshold rather than the tighter `Vn.2b` one that applies
inside a >=4x4 via array), a plate-outline-vs-whole-polygon choice in one
half of one rule (`mim.space.1`'s "adjacent MiM" half, see below), or
context our engine has no data for at all — net-potential
(`nwell.space.1`) or device connectivity (`bjt.separation.comp.1`), both of
which require netlist/connectivity information the geometry-only check
primitives don't have. Each is called out explicitly in its rule's
docstring in `gf180mcu.py`; the threshold values used are always the real,
unmodified DRM values.

The DRM's two MiM capacitor rules scoped to the "virtual bottom plate" —
`MIMTM.1` (minimum bottom-plate spacing to other bottom-plate-or-routing
metal, transcribed as `mim.space.1`) and `MIMTM.2` (minimum bottom-plate
overlap of `Via4`, transcribed as `mim.enclosing.via4.1`, issue #345) — both
use the `derived_layer` primitive described above rather than being
approximated against raw `Metal4`, so a design with **zero** recognised MiM
structures (no `FuseTop` shapes at all) produces zero violations for either
rule, not the false positives an unscoped whole-`Metal4` check would
produce against ordinary routing or PDN power-stripe geometry. `mim.space.1`
was fixed this way in issue #1033 — before that, it approximated `MIMTM.1`
as a general `Metal4`-to-`Metal4` `"space"` check across the whole drawn
layer, over-flagging any two ordinary `Metal4` shapes spaced tighter than
1.2 um regardless of whether the layout drew a MiM capacitor anywhere (this
was confirmed in practice against a real OpenROAD-routed digital block with
a `Metal4` PDN grid and zero MiM devices: 188 false-positive `mim.space.1`
violations, all attributable to top-level PDN/routing geometry, none to a
qualified library cell). Because `"space"` is a single-region check
primitive, `mim.space.1` is instead expressed as a `"separation"` check
between the derived virtual bottom plate and the rest of `Metal4` — see
"Sized/derived-layer rules (`DerivedLayer`)" above, and `drc.py`'s
`run_drc()` for how the `"other"` side of that separation check further
excludes any `Metal4` shape that is itself part of the same virtual-plate
construction (not just a plain set-subtraction, which would produce a
spurious violation at the sizing cutoff of a plate that straddles the
oversized `FuseTop` window).

`MIMTM.1` covers spacing to the bottom plate metal "whether adjacent MiM
**or** routing metal". The separation check above measures the routing-metal
half; the adjacent-MiM half is measured alongside it as a peer-to-peer
`isolated_check` among the plate-bearing `Metal4` polygons that the "other"
side excludes, reported under the same `mim.space.1` rule id (see
"Sized/derived-layer rules (`DerivedLayer`)" above). That peer check runs on
whole plate-bearing polygons rather than the sizing-clipped plate outlines,
which is the one approximation left in this rule: two neighbouring MiM caps
whose plate metals face each other across routing tails closer than 1.2 um
are flagged even when the plates proper clear the rule (the conservative
direction, and confined to `Metal4` that already touches a `FuseTop`).
Measuring clipped outlines instead would split a *shared* bottom plate
carrying two top plates more than 2 x 1.06 um apart into two "plates" and
report a violation across continuous metal — a false positive of exactly
the kind #1033 removed.

Coverage does **not** yet include: `Pplus`/`Nplus` implant-specific rules
(width/space/enclosure of the implant layers themselves), `LVPWELL` or
`DNWELL`, the remaining `BJT.*` rules (`BJT.1`/`BJT.2`, which key off
`DNWELL`), the MIM Option-A (`MIM.*`) rule set (a different, 3-metal-layer
process variant this deck doesn't model — see `gf180mcu.py`'s docstring), or
5V/6V high-voltage variants beyond the `DF.1a`/`DF.3a` `_LV`/`_MV` `Comp`
pairs (issue #1110): `DF.6`, `PL.5a`/`PL.5b` and the rest of the DRM's
`_MV` column are not transcribed at all yet — left for follow-on issues.

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

The run did surface four `diff.enclosing.licon.1` violations, originally
documented here as a genuine property of the **input** (row gaps between
adjacent standard-cell instances that a filler-cell insertion stage, which
`klt place-and-route` does not yet run, would close). Issue #995 showed
that reading was wrong: **all four were false positives from `klt drc`
itself**, and the fixture now reports `"status": "clean"`.

The mechanism (fixed in `_run_check`): `run_drc` built each checked
`Region` straight from the raw shape iterator, and `Region.enclosing_check`
measures the *primary* region's raw polygon edges rather than its merged
outline. `sky130_fd_sc_hd__and3_1` draws its own `diff` as two abutting,
unmerged rectangles; each flagged `licon1` cut sits 25 dbu from that
internal seam — every one of the four reported edge pairs was exactly 25
dbu wide, entirely inside one cell instance — while the *merged* `diff`
region encloses it by ~925 dbu on that side, far beyond the rule's 0.04 um
margin. Both regions are now merged before any check runs, which only ever
removes false positives: a merged region covers the same area with weakly
fewer edges, so a genuine enclosure shortfall still reports (pinned by
`test_run_drc_sky130_diff_enclosing_licon_touching_shapes_real_shortfall`).

Splitting one layer's geometry into several touching shapes is an ordinary
GDS authoring/tiling choice, not a drawn defect, so this class of report
was never a real violation to fix in the layout.

### gf180mcu standard-cell row abutment (#1028)

Issue #1028 reported that checking a row of abutted
`gf180mcu_fd_sc_mcu9t5v0` standard-cell instances (adjacent library-cell
placements with zero overlap at the library's own declared cell-outline
pitch — an ordinary, correct digital place-and-route pattern) against the
`gf180mcu` deck produced false-positive `comp.enclosing.contact.1`,
`contact.space.1`, `metal1.space.1`, and `poly2.space.1` violations. Two
distinct findings, both verified against real `gf180mcu_fd_sc_mcu9t5v0`
corpus GDS (`tests/corpus/gf180mcu/`), not synthetic geometry:

- **A row of real standard-cell instances abutted at the library's own true
  pitch is DRC-clean today.** Tiling the real `and2_1` corpus cell at its
  own cell-outline shape's width (4.48um, read from the GDS's own `(0,0)`
  layer — the real "`SIZE`" the reproducer needed) — as a single-cell row,
  a heterogeneous multi-cell row, and a 3x3 row-flipped grid — reports
  `status: "clean"` for every rule, including all four the issue named. The
  `enclosing`/`enclosed` half of that (`comp.enclosing.contact.1`) is
  covered by the same `_run_check` `.merged()` fix #995/#998 already made
  (above); the three `*.space.1` rules dispatch through a different code
  path (`_SINGLE_LAYER_CHECKS`, `Region.space_check`) that #995/#998 never
  touched, but `space_check`'s own `merged_semantics` already tolerates an
  exact zero-gap abutment seam with no engine change needed — pinned by
  `test_run_drc_gf180mcu_row_abutment_real_pitch_clean` in
  `tests/test_drc.py`.
- **The issue's own reproduction script's pitch was itself the bug, not
  `klt drc`.** It assumed a flat `2.8` um pitch for `and2_1` rather than
  reading the cell's real 4.48um outline width — a pitch narrower than the
  real cell, so consecutive instances *genuinely overlap* by 1.68um rather
  than merely abut. Reproducing that literal (incorrect) pitch against
  current `origin/main` reproduces the issue's exact reported rule counts
  (`comp.enclosing.contact.1: 2`, `contact.space.1: 4`,
  `metal1.space.1: 4`, `poly2.space.1: 2`) — real, correctly-detected
  violations from a corrupted (overlapping) placement, not a false
  positive — pinned by
  `test_run_drc_gf180mcu_row_abutment_issue_reported_pitch_is_real_overlap`.

**By design, not a coverage gap: a genuine sub-dbu residual gap between
otherwise correctly-pitched instances still reports a real
`metal1.space.1` violation, and this is intentionally not loosened.**
`and2_1`'s power/ground rail (`Metal1`) is drawn flush to the cell's own
outline specifically so that row abutment forms one continuous rail; any
nonzero gap — even a single database unit (0.001um at this deck's nominal
scale) — between two instances leaves two real, facing rail edges closer
than `metal1.space.1`'s 0.23um threshold, which `space_check` correctly
flags. A real place-and-route flow that leaves any such residual gap (a
DEF->GDS-merge or router-output snapping artifact) has a genuine rail
discontinuity — precisely the defect a filler-cell/rail-continuity
insertion stage exists to close — so `klt drc` treats it as a real
violation rather than tolerating an arbitrary gap magnitude. Pinned by
`test_run_drc_gf180mcu_row_abutment_subdbu_gap_real_shortfall`, the same
"merging only removes false positives, a genuine shortfall still flags"
monotonic guarantee #995/#998's own `..._real_shortfall` test pins for the
`enclosing`/`enclosed` check family, extended here to `*.space.1`.

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

1. **A pre-existing violation** (`diff.enclosing.licon.1`, the same class
   documented above) was found in the standard-cell region of the
   unmodified layout — direct evidence the deck was already evaluating
   that domain. (#995 later showed this rule's reports on unmodified
   `sky130_fd_sc_hd` geometry were the merge false positive described
   above, so treat this as evidence the *rule ran* there, not that the
   layout had a real defect; point 2 below is the load-bearing half of the
   finding either way.)
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
(via `Cell.begin_shapes_rec`). By default every top cell is checked
independently and the `cell` field reports the top cell a violation was
found under; pass `--top <cell>` (issue #554) to scope the run to a single
named top cell instead — see "Usage" above.

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
    "rules_skipped": ["metal1.width.1", "metal1.space.1"],
    "voltage_domain_warnings": [],
    "deck_scope": ["7.13 Metaln"]
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
    "rules_skipped": [],
    "voltage_domain_warnings": [],
    "deck_scope": ["licon", "m1", "poly"]
  }
}
```

### Top-level fields

| Field             | Type                     | Description                                                             |
| ----------------- | ------------------------ | ------------------------------------------------------------------------ |
| `schema_version`  | integer                  | Version of this command's JSON shape (starts at `1`; per-command).       |
| `file`            | string                   | The input path exactly as provided on the command line.                  |
| `deck`            | string                   | `--engine curated`: the deck name used (`"sky130"` or `"gf180mcu"`). `--engine klayout`: the resolved/given deck script's own path (no separate short name exists for an arbitrary PDK-native script). |
| `engine`          | string                   | Present only for `--engine klayout` (always `"klayout"`) — purely additive; the curated engine's own output carries no `engine` key at all, unchanged since it was the sole engine until issue #565. |
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
| `rules_skipped`                    | array\<string\> | Rule ids silently skipped because a layer they read was absent from this stream — `layer`/`other_layer`, or a `derived_layer` input (with one documented exception: a `"not_interacting"` derived rule still runs when its marker layer is absent, see "Voltage-domain rule pairs" above). Sorted alphabetically. |
| `voltage_domain_warnings`          | array\<object\> | `{"marker": "<layer>/<datatype>", "description": string}` — see below. Sorted by marker `(layer, datatype)`. |
| `deck_scope`                       | array\<string\> | Every distinct DRM section / official rule-id prefix the selected deck's rules claim to implement — a static property of the deck, independent of the input stream (issue #566). Sorted alphabetically. |

`layers_checked` and `layers_in_stream_without_rules` are computed from the
input stream's own layer table (reusing the same per-layer enumeration
`klt layers` uses — see [`klt layers`](layers.md)), not from shape counts: a
layer present in the stream's layer table with zero shapes still counts as
"in the stream" for this purpose, matching `Layout.find_layer(...)`'s own
semantics.

#### `coverage.voltage_domain_warnings`

A second, narrower trust gap `layers_in_stream_without_rules` alone does not
surface: some PDKs draw two gate-oxide/voltage domains on the same wafer,
selected by a marker layer — e.g. gf180mcu's `Dualgate` (55/0) selects its
5V/6V thick-oxide domain, whose DRM publishes a second, materially different
(30–60% larger) column of DRC thresholds. Where a curated deck encodes only
the default (3.3V) column and never reads the marker, geometry drawn
*inside* it is checked against the wrong thresholds — and, because that
geometry sits on an ordinary checked layer (e.g. `Comp`), it shows up in
`coverage.layers_checked`, not `layers_in_stream_without_rules`: a `"clean"`
status with no other signal that anything is off.

Whenever a deck-registered marker (today, only gf180mcu's `Dualgate`) is
present in the input stream *and* its geometry geometrically interacts with
geometry an **unscoped rule actually checked on this run**, one entry is
added, naming the marker and the concrete consequence:

```json
{
  "marker": "55/0",
  "description": "Dualgate (55/0) marks gf180mcu's 5V/6V thick-oxide voltage domain. This curated deck models it for the DF.1a/DF.3a COMP width/space rules only, which ship as _LV/_MV pairs (0.22/0.30 and 0.28/0.36 um) scoped to the marker; every other DRC rule still applies the 3.3V/_LV thresholds regardless of Dualgate's presence (e.g. DF.6 COMP extend beyond gate 0.24 vs. 0.40, PL.5a/PL.5b field poly to COMP 0.10 vs. 0.30 -- all in um, and neither transcribed in this deck yet), and MOS extraction always binds the 3.3V models (nfet_03v3/pfet_03v3) even to a transistor drawn entirely inside Dualgate."
}
```

"Unscoped" is evaluated **per rule**, not per deck (issue #1110), because a
marker can be partially modelled — as gf180mcu's `Dualgate` now is. Two
classes of rule are excluded from the gate:

- a rule whose `derived_layer` reads the marker itself (the
  `comp.width.1`/`comp.width.mv.1` and `comp.space.1`/`comp.space.mv.1`
  pairs — see "Voltage-domain rule pairs" above): it applied the *right*
  column, so the geometry it checked is not evidence of a gap;
- a rule skipped on this run (`coverage.rules_skipped`): it applied no
  threshold at all, right or wrong.

So the `Dualgate`-inside `Comp` stripe from issue #552 now produces a
`comp.width.mv.1` violation and **no** warning, while the same stripe with a
contact on it still warns — `comp.enclosing.contact.1` (`CO.4`) reads no
marker. A marker shape that never overlaps any unscoped checked geometry
(e.g. one drawn only over a device this deck already scopes to it correctly,
such as a `Dualgate`-narrowed ESD diode) likewise produces no entry —
avoiding a warning with nothing behind it. Always a list, empty for a deck
that registers no such marker (sky130's curated deck registers none today —
it does not yet name an `hvi`-equivalent layer at all) or a layout that
draws none of it overlapping unscoped checked geometry.

**What this field does and does not guarantee**: it flags that the checked
thresholds may be the wrong column for geometry the *remaining, unscoped*
rules checked. It does **not** correct `status`/`violations` against the real
5V/6V (`_MV`) thresholds for those rules — only the two `Comp` width/space
pairs are voltage-scoped today (issue #1110); the rest of the DRM's `_MV`
column is not transcribed. Treat a non-empty `voltage_domain_warnings` as
"re-check this geometry against the PDK's own tooling for the marked
domain," not as a corrected verdict. The converse also holds and is the
point of the per-rule gate: an *empty* `voltage_domain_warnings` on a layout
that draws the marker means every rule that touched marked geometry read the
marker, not that the marker was ignored quietly.

#### `coverage.deck_scope`

`layers_in_stream_without_rules` answers a **per-layer** question — "what
geometry did I draw that the deck ignored entirely" — but cannot answer a
**per-rule** one: a layer can report as `"checked"` because one unrelated
rule references it, even though the specific DRM rule/section a caller
actually cares about was never evaluated. `deck_scope` closes that gap with a
coarser, section-level statement of intent: every distinct DRM section (e.g.
gf180mcu's `"7.5 Comp"`, `"10.4.2 MIM Option B"`) or official rule-id prefix
(e.g. sky130's `"li"`, `"m1"`, whose source `sky130.lydrc`/`sky130A_mr.drc`
has no numbered-section structure to cite) the selected deck's rules claim to
implement, populated per rule via
[`DrcRule.scope`](../../src/klayout_tools/decks/__init__.py) and aggregated
here deduplicated and sorted. A caller building a signoff gate on top of
`klt drc` can diff `coverage.deck_scope` against the DRM's own table of
contents to see which chapters this curated deck does not attempt at all —
something no per-layer field can express, since one layer (e.g. gf180mcu's
`Metal4`) can be referenced by rules spanning several different DRM sections.

`deck_scope` is purely additive (see
[`docs/json-contract.md`](../json-contract.md)): the existing
`layers_in_stream_without_rules` field is unchanged, byte-identical output
for any input that predates this field. `deck_scope` is a static property of
the deck (like `deck_layers`), not filtered by what a given run's input
stream actually contains — running the same deck against two different
layouts reports the same `deck_scope`. A rule with no `scope` set (the
`DrcRule` default, `""`) contributes nothing to this list; a deck where every
rule leaves `scope` unset (none of the two shipped decks today) reports an
empty `deck_scope` rather than raising.

Worked example: on the `gf180mcu` deck, `coverage.deck_scope` includes
`"7.4 Nwell"` and `"7.5 Comp"`, but the MIM Option-A rule set and the
`LVPWELL`/`DNWELL`-keyed `BJT.1`/`BJT.2` rules — both called out as *not*
covered in "Coverage" above — contribute no entry at all, since no curated
rule references them. A caller asserting "this deck claims the Nwell
chapter" checks `"7.4 Nwell" in coverage.deck_scope`, distinct from (and a
strictly coarser question than) "did this run actually check any Nwell
geometry" (`coverage.layers_checked`).

## Rule provenance and the golden-pair manifest

Issue #747 (piloting `docs/design/deck-compiler-proposal.md` §5/§6) adds two
things, originally scoped to the 37 width/space `DrcRule` entries across
`sky130.py` (11) and `gf180mcu.py` (26) as a pilot slice. **Issue #904 (Epic
#711 Phase 3a) widens gf180mcu's own coverage to all of its `DrcRule`
entries** (adding `enclosing`/`separation`, its only two remaining check
kinds) — sky130's own scope stays at its original 11-rule width/space pilot,
a deliberate, unscoped-by-#904 decision (see `tests/golden_deck/README.md`'s
"Why width/space only for sky130, but full coverage for gf180mcu"):

### `DrcRule.provenance`

A machine-readable, **per-rule** citation of the exact upstream source a
rule's threshold/geometry was transcribed from —
[`RuleProvenance`](../../src/klayout_tools/decks/__init__.py): `source_repo`
(`"owner/repo"`), `source_path` (the exact file within that repo — a live
`.lydrc`/`.drc` script for sky130, a DRM CSV table for gf180mcu),
`rule_id` (the *official* upstream rule id, e.g. `"poly.1a"`, `"DF.1a"` —
distinct from this deck's own dotted `DrcRule.id`), and `commit` (the
upstream commit/tag the value was verified against, when pinned).

**Distinct from the existing [`DrcRule.scope`](../../src/klayout_tools/decks/__init__.py)
field** (issue #566, see "`coverage.deck_scope`" above): `scope` is
coarser and deck-level-aggregated — a DRM section number or rule-id-family
prefix *shared by every rule* transcribed from it, rolled up into
`coverage.deck_scope` so a caller can diff "what sections does this deck
claim" against the DRM's own table of contents. `provenance` is **per-rule
and exact** — the one specific file and official rule id *this individual
rule* came from, with no deck-wide aggregation. Many rules sharing one
`scope` value each carry a different `provenance.rule_id` (e.g. gf180mcu's
`metal1.width.1`/`metal2.width.1`/`metal3.width.1`/`metal5.width.1` all
share `scope="7.13 Metaln"` but each cites the DRM's own `"Mn.1"` row for
their own metal level). `provenance` is not (yet) surfaced in `klt drc`'s
JSON output — it lives on `DrcRule` itself, queryable by a caller that
imports `klayout_tools.decks` directly (e.g. a coverage-audit script), the
same way `scope` was before `coverage.deck_scope` aggregated it.

As of issue #747, `provenance` was populated only for the 37 piloted
width/space rules. As of issue #904, it is populated for **all** of
gf180mcu's `DrcRule` entries (42 then, 44 as of issue #1110) — sky130's own remaining (non-width/space)
rules still leave it `None` (the default), an unpopulated field, not a
claim that no provenance exists (the prose citation in each rule's own
inline comment remains the record for those rules, exactly as before this
field existed).

### The golden-pair manifest (`tests/golden_deck/`)

A declarative, rule-id-keyed fixture manifest — one `"violate"` and one
`"clean"` tiny-layout spec per piloted rule,
`tests/golden_deck/<deck>/manifest.json` (`sky130`/`gf180mcu`) — formalising
the informal `_violation`/`_clean` test-function pairs `tests/test_drc.py`
already carried for some of these rules into a single, coverage-checked
artifact (`tests/test_golden_deck.py` asserts every piloted rule has both).
Regenerated deterministically from each rule's own `layer`/`other_layer`/
`threshold_dbu` via `tests/golden_deck/generate_golden_deck.py` — see
`tests/golden_deck/README.md` for the manifest schema, the regeneration
workflow, and the sky130 native-deck (`--engine klayout`) cross-check
results this issue ran against a real `sky130A.lydrc`. gf180mcu's own
`--engine klayout` cross-check remains deferred as of issue #904 (its
native *DRC* deck still has no single runnable file — matching this
section's existing "no single runnable native deck" limitation above), even
though its golden-pair manifest now covers all 44 of its `DrcRule` entries
(issue #904, extended by #1110) — the manifest and coverage/curated-engine tiers are complete
for gf180mcu, only the native-DRC-deck cross-check tier is sky130-only.
gf180mcu's *LVS* device-extraction rules are cross-checked against a real,
directly-runnable native deck instead — a DRC/LVS split, not a contradiction
— see [`docs/cli/extract.md`](extract.md)'s "gf180mcu native-deck LVS
device-extraction cross-check" section.

## Exit codes

| Code | Meaning                                                     |
| ---- | ------------------------------------------------------------ |
| `0`  | Ran clean — no violations.                                   |
| `1`  | Failed to run — bad file, unknown `--deck`, `--top` names a cell absent from the stream, or engine error. |
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
