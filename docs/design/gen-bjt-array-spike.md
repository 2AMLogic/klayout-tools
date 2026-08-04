# Design note: `bjt_array` — mechanism choice (draw-from-scratch vs. vendor library cell)

**Status:** decision record for Epic #152 phase 4 (issue #176), written
alongside the implementation it justifies. It follows the "make the judgment
call explicitly and record it before/as the first step of implementation"
precedent the LVS epic set with its engine/contract spike
([docs/design/lvs-extraction-spike.md](lvs-extraction-spike.md), #161) — the
issue flags the mechanism choice here as a comparable `complex` judgment call
with no prior precedent in this codebase, because a wrong or oversimplified
choice could pass DRC while not being electrically equivalent to the real
device.

This note records **why `bjt_array` draws its vertical-bipolar (PNP/BJT)
geometry from base layers** rather than instantiating a PDK's pre-verified
vendor library cell, and — just as importantly — **what that choice does and
does not claim** about the generated device.

## Background

`klt gen`'s phase-2 generators (`mos_array`, `res_array`, `guard_ring`,
`diff_pair`) all draw geometry from scratch via `pya.PCellDeclarationHelper`,
resolving a small set of layer *roles* (`active`/`poly`/`contact`/`metal`,
plus an optional `well`) per PDK family from `_PDK_ROLE_LAYERS` in
`src/klayout_tools/gen.py`. None of them draw a bipolar device, and `klt gen`
has no path anywhere to instantiate an existing PDK library cell.

Epic #152's own phase list named a "PNP/BJT array" as a distinct required
primitive; phase 2 (#159) claimed it was covered by generalizing `mos_array`,
but that generalization was never implemented (verified against `gen.py` on
`main`). This note and the accompanying `bjt_array` generator are phase 4,
closing that gap.

Two structurally different mechanisms were on the table.

## Option A — instantiate the PDK's vendor library cell

Real PDKs realize vertical PNP/BJT devices as pre-built, pre-characterized
vendor GDS cells — gf180mcu's `pnp_05p00x05p00` / `pnp_10p00x10p00`, sky130's
`sky130_fd_pr__pnp_05v5` — not as geometry assembled from active/poly/contact
rectangles. Instantiating those cells N times in a common-centroid arrangement
is the closest match to how these devices are actually built, and the
mechanism could generalize beyond bipolar devices (precision MiM/MoM caps and
other fixed vendor primitives share the "pre-verified library cell" shape).

**Why it was rejected for this phase:**

1. **No library-GDS-instantiation infrastructure exists, and standing it up is
   materially larger than this issue's scope.** Every existing generator
   *draws* geometry; `klt gen` has no path to locate, load, and place a named
   cell out of a PDK's own GDS libraries. The PDK resolver every verb shares
   (`klayout_tools.pdk.find_pdk`) resolves an install *variant* by probing for
   a `libs.tech` directory — it does not enumerate or locate device-library
   GDS files. Adding that is a new generation *mechanism*, not the "new
   generator" this issue scopes.

2. **CI never has a real PDK installed — so a library-cell generator could not
   be verified DRC-clean where it matters.** The generator test suite
   (`tests/test_gen.py`) and the acceptance bar (`klt gen … && klt drc …`,
   0 violations) run against *fabricated* PDK installs under `tmp_path` (just
   an empty `libs.tech` probe) and this repo's *curated* DRC decks — never a
   downloaded PDK. A generator whose output is a vendor GDS cell only exists
   if that vendor GDS is present on disk; there is nothing for CI to
   instantiate or check. This collides directly with CLAUDE.md's "every
   command must be runnable in CI."

3. **CLAUDE.md forbids vendoring PDK cell data.** "Open PDKs only … never
   vendor proprietary PDK data." gf180mcu and sky130 are both open, so their
   PNP cells are *legally* redistributable — but this repo has deliberately
   never bundled any PDK's device-library GDS (it bundles only *curated DRC
   decks*, hand-authored in `src/klayout_tools/decks/`, not vendored layout).
   Bundling vendor PNP cells to make Option A testable would break that
   standing posture for one generator.

The issue anticipates exactly this: *"If library-cell instantiation turns out
to require infrastructure this repo doesn't have … that itself is a
legitimate, well-justified reason to choose draw-from-scratch."* All three
points above are that reason.

## Option B — draw the geometry from base layers (chosen)

`bjt_array` extends the same draw-from-scratch, role-based pattern every
existing generator uses. A unit device is an emitter diffusion pad beside a
base-tie diffusion pad (each with a contact and a covering local-metal pad),
placed in a `rows` × `cols` common-centroid grid that shares one base well and
is surrounded by a collector guard ring; each unit's emitter pad is covered by
its PDK family's bipolar device-mark layer (gf180mcu's `DRC_BJT`, sky130's
`pnp.drawing`) and each unit's base-tie pad by a `tap`-role well tie.

> **Updated by issue #432.** As first shipped, the mark was one array-level
> box coincident with the shared well, and sky130 drew no mark at all (its
> curated *DRC* deck checks none — see the negative finding below). Both
> choices broke extraction: `klt extract` derives `base = well & marker` and
> `emitter = active & base`, so sky130's output extracted as **zero** devices
> and an array-level box would have made the whole well one base with every
> pad inside it an emitter. The mark is now per unit, covering that unit's
> emitter pad only (overhanging it by 0.1 µm, which also keeps ≥ 0.1 µm from
> every COMP it does not cover, satisfying `BJT.3`), and sky130 draws
> `pnp.drawing` because its *extraction* deck keys off it — the same
> "extraction is the other consumer of a mark layer" resolution issue #369
> applied to `res_array`'s resistor marker. The base-tie `tap` shape was added
> at the same time so the base terminal resolves to a real net rather than a
> floating anonymous node.

**Why it wins for this phase:**

- It reuses the one geometry backend (`pya`) and the one PDK-role mechanism
  (`_PDK_ROLE_LAYERS`) already proven headless in CI — adding only a
  `bjt_mark` role (gf180mcu `127/5`; sky130 `82/44` as of issue #432).
- It is verifiable against this repo's curated decks in CI: the generated
  output is DRC-clean on gf180mcu **including the bipolar-specific
  `bjt.separation.comp.1` mark-layer rule**, and on sky130 (whose curated deck
  has no bipolar rule at all) — and, since issue #432, extracts back into one
  recognised bipolar device per drawn unit on both families.
- It exercises the novel DRC surface honestly. The `DRC_BJT`-to-COMP
  separation rule fires only for a *positive* mark-to-COMP gap below 0.1 µm
  (verified empirically: an overlapping/enclosing/coincident mark reports zero
  separation). Each per-unit mark box therefore either overlaps COMP (its own
  emitter pad, zero separation) or stands 0.3 µm from the nearest COMP it does
  not cover (the base-tie pad and the neighbouring unit, one 0.4 µm pitch away
  minus the mark's own 0.1 µm overhang), while the collector ring is held
  `BJT_COLLECTOR_GAP_UM` (0.4 µm) outside the well — all well beyond the
  0.1 µm limit.

### What Option B explicitly does *not* claim

This is the crux of the `complex` flag. The drawn geometry is a **DRC-clean,
matching-faithful floorplan** of the device — it reproduces the layer stack
(diffusion / contact / metal / shared base well / device mark) and the
common-centroid/dummy arrangement a matched bipolar group needs — but it is
**not** a drop-in, SPICE-model-exact replacement for the PDK's characterized
vertical PNP:

- The curated decks check **no implant layer** (no `Pplus`/`Nplus` rules), so
  emitter, base-tie, and collector all draw on the one `active` (COMP/diff)
  role. A process-exact device would distinguish P+ emitter, N+ base tie, and
  P+ collector by implant.
- The device is a schematic/matching floorplan, not a process-exact vertical
  cross-section; its terminals (emitter/base/collector ports) and its
  common-centroid placement are the load-bearing, downstream-consumable part,
  not its suitability for direct model extraction.

This limitation is surfaced *in-band*, not just here: on any PDK family for
which `_PDK_ROLE_LAYERS` declares no bipolar mark layer (neither supported
family, since issue #432 populated sky130's), `bjt_array`'s response emits a
`drc_hints.notes` entry saying so and pointing back to this note. That is the
same honesty the existing generators already carry (e.g. `mos_array` is
documented as "not a bipolar substitute"; none claim model equivalence).

What the geometry *does* now claim, since issue #432, is device
**recognition**: `klt extract` reads the drawn output back as one bipolar
device per drawn unit (dummies included — they are structurally identical unit
devices), with the emitter on its own net and the base resolved through the
per-unit `tap` tie. Recognition is not characterization: the extracted device
still carries no vendor model binding beyond what `--pdk` model binding infers
from measured emitter area.

## Second PDK family

Both families the phase-2 generators support are supported here and are
DRC-clean out of the box on their documented default `params`:

| PDK family | variant tested | device mark (per unit) | base tie | shared well | DRC deck | result |
| ---------- | -------------- | ---------------------- | -------- | ----------- | -------- | ------ |
| gf180mcu   | `gf180mcuD`    | `DRC_BJT` (127/5) drawn, `bjt.separation.comp.1` satisfied | `Comp` (22/0 — the `tap` role *is* `active` here) | Nwell (21/0) | `gf180mcu` | 0 violations |
| sky130     | `sky130A`      | `pnp.drawing` (82/44) drawn — no DRC rule checks it; the extraction deck's `pnp` device does | `tap.drawing` (65/44) | nwell (64/20) | `sky130`   | 0 violations |

gf180mcu is the primary family (it carries the meaningful bipolar-specific DRC
surface). sky130 is supported too, drawing the same diffusion/contact/metal
device plus the mark/tap layers its *extraction* deck needs, even though its
curated DRC deck checks neither.

## Follow-on friction (not blocking this PR)

- sky130's curated deck has **no** bipolar device rule (no analogue to
  gf180mcu's `BJT.3`). `bjt_array` is DRC-clean there partly *because* the
  deck under-constrains the device. Adding a sky130 bipolar mark/separation
  rule is out of scope for this phase (the issue scopes out "new DRC rule
  coverage beyond what's needed to validate this generator's own output") and
  is filed as follow-on friction.

  **Resolved, negative finding (issue #183):** research into sky130's
  official DRC deck (`sky130.lydrc`) found no analogous rule. sky130 does
  define a `pnp.drawing` device-mark layer (82/44) for the vertical-PNP
  device, but the only DRC rule referencing it (`dnwell.4`, a
  `dnwell`-vs-`pnp` overlap exclusion) checks an unrelated process-layer
  incompatibility, not a separation/spacing rule like gf180mcu's `BJT.3` —
  and its "must never overlap at all" semantics aren't representable by any
  check kind this repo's DRC engine supports (see `DrcRule`'s docstring).
  Documented as a negative finding in `src/klayout_tools/decks/sky130.py`'s
  module docstring. That finding still stands as a *DRC* statement — but
  issue #432 set `_PDK_ROLE_LAYERS["sky130"]["bjt_mark"]` to `82/44` anyway,
  because DRC is not the only consumer of a mark layer: sky130's extraction
  deck recognises its `pnp` device off exactly that layer, so a generator that
  skipped it produced output its own toolchain extracted as zero devices. The
  role's presence now tracks "some curated deck keys off this layer", not
  "a curated DRC rule checks it".
- Vendor-library-cell instantiation (Option A) remains the right long-term
  mechanism for a *model-exact* device and for fixed vendor primitives
  generally (MiM/MoM caps). It needs its own epic — a library-locate/instantiate
  path plus a CI story for a real (or minimally bundled, open) PDK cell — and
  is filed as follow-on, not attempted here.
