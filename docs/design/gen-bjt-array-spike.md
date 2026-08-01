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
is surrounded by a collector guard ring; on gf180mcu the whole array is
covered by the `DRC_BJT` device-mark layer its curated deck's
`bjt.separation.comp.1` (`BJT.3`) rule keys off.

**Why it wins for this phase:**

- It reuses the one geometry backend (`pya`) and the one PDK-role mechanism
  (`_PDK_ROLE_LAYERS`) already proven headless in CI — adding only a
  `bjt_mark` role (gf180mcu `127/5`; sky130 `None`, mirroring the existing
  `well: None` handling exactly).
- It is verifiable against this repo's curated decks in CI: the generated
  output is DRC-clean on gf180mcu **including the bipolar-specific
  `bjt.separation.comp.1` mark-layer rule**, and on sky130 (whose curated deck
  has no bipolar rule at all).
- It exercises the novel DRC surface honestly. The `DRC_BJT`-to-COMP
  separation rule fires only for a *positive* mark-to-COMP gap below 0.1 µm
  (verified empirically: an overlapping/enclosing/coincident mark reports zero
  separation). `bjt_array` therefore draws one array-level mark box coincident
  with the shared well (zero separation to every enclosed emitter/base COMP)
  and holds the collector ring `BJT_COLLECTOR_GAP_UM` (0.4 µm) outside the
  well, well beyond the 0.1 µm limit.

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

This limitation is surfaced *in-band*, not just here: on any PDK family
without a curated bipolar mark rule (sky130 today), `bjt_array`'s response
emits a `drc_hints.notes` entry saying so and pointing back to this note. That
is the same honesty the existing generators already carry (e.g. `mos_array` is
documented as "not a bipolar substitute"; none claim model equivalence).

## Second PDK family

Both families the phase-2 generators support are supported here and are
DRC-clean out of the box on their documented default `params`:

| PDK family | variant tested | device mark | shared well | DRC deck | result |
| ---------- | -------------- | ----------- | ----------- | -------- | ------ |
| gf180mcu   | `gf180mcuD`    | `DRC_BJT` (127/5) drawn, `bjt.separation.comp.1` satisfied | Nwell (21/0) | `gf180mcu` | 0 violations |
| sky130     | `sky130A`      | none (curated deck has no bipolar mark rule — noted in `drc_hints`) | none (curated deck has no well rule) | `sky130`   | 0 violations |

gf180mcu is the primary family (it carries the meaningful bipolar-specific DRC
surface). sky130 is supported too, drawing the same diffusion/contact/metal
device without a device-mark or well layer its curated deck does not check.

## Follow-on friction (not blocking this PR)

- sky130's curated deck has **no** bipolar device rule (no analogue to
  gf180mcu's `BJT.3`). `bjt_array` is DRC-clean there partly *because* the
  deck under-constrains the device. Adding a sky130 bipolar mark/separation
  rule is out of scope for this phase (the issue scopes out "new DRC rule
  coverage beyond what's needed to validate this generator's own output") and
  is filed as follow-on friction.
- Vendor-library-cell instantiation (Option A) remains the right long-term
  mechanism for a *model-exact* device and for fixed vendor primitives
  generally (MiM/MoM caps). It needs its own epic — a library-locate/instantiate
  path plus a CI story for a real (or minimally bundled, open) PDK cell — and
  is filed as follow-on, not attempted here.
