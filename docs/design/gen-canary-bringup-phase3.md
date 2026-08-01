# `klt gen` phase 3 canary bring-up (Epic #152, issue #160)

Phase 3 of Epic #152 tasked itself with driving one of the two public
canary repos' floorplan/matching-plan issues —
[`sky130-bandgap#15`](https://github.com/2AMLogic/sky130-bandgap/issues/15)
or [`gf180-bandgap#16`](https://github.com/2AMLogic/gf180-bandgap/issues/16)
— with the phase 1/2 generators (#158/#159), iterating against `klt drc`
until clean, and filing any friction back here rather than working around
it silently. This is the record of that bring-up.

This phase's own "Affected Files" section anticipated no `klayout-tools`
source change: it is an integration/bring-up exercise against
already-shipped generators, not new library code. That held — this
document, the friction issue it produced, and the cross-repo comments it
left are the whole diff.

## What was evaluated

Both candidate targets were inspected directly (issue bodies, labels,
dependency chains, and — for `gf180-bandgap` — the merged PRs behind its
resolved dependencies) before choosing which to drive:

- **`sky130-bandgap#15`** was still `loom:blocked` on five open upstream
  dependencies (#8 core schematic, #9 error amplifier, #12 Monte Carlo,
  #13 trim network, #14 layout/DRC-LVS bring-up) at the time of this
  bring-up. The design itself isn't far enough along to drive real layout
  from — not a `klt gen` problem, a sequencing one.
- **`gf180-bandgap#16`** had all of its own dependencies resolved and was
  the more advanced target, so it was the one actually driven. It closed
  during this same bring-up window via a merged PR (#52 in that repo) —
  correctly, as the **document-only** floorplan/matching-plan deliverable
  it was scoped to be. That PR's own body states plainly: "this deliverable
  is a document, not GDS, so no `klt` invocation was needed for this
  issue's scope." The next step — actually drawing the block's physical
  layout from that floorplan document — is not yet a filed issue in that
  repo; the closest existing issue (`gf180-bandgap#17`) is scoped to a
  *post-layout* extraction re-run and assumes a layout already exists.

So neither canary currently has an issue whose own scope is "produce a
DRC-clean GDS via `klt gen`." That is itself a finding, reported back on
both canary issues (see "Where this was reported" below) rather than
worked around by inventing layout work outside what either repo's own
issue currently calls for.

## What was verified anyway

Independent of that sequencing gap, the phase 1/2 generators were run for
real against the exact PDK variant the more-advanced canary target
actually uses (`gf180mcuD`, `open_pdks c6d73a35f524070e85faff4a6a9eef49553ebc2b`
— matching that repo's own simulation harness), to check the generators
themselves are usable for a bandgap-class block's floorplan, not just on
their own documented-default proof cases:

```
$ klt gen res_array   --pdk gf180mcuD -o res_array.gds
$ klt drc res_array.gds   --deck gf180mcu --format json   # status: clean, violation_count: 0

$ klt gen diff_pair   --pdk gf180mcuD -o diff_pair.gds
$ klt drc diff_pair.gds   --deck gf180mcu --format json   # status: clean, violation_count: 0

$ klt gen guard_ring  --pdk gf180mcuD -o guard_ring.gds
$ klt drc guard_ring.gds  --deck gf180mcu --format json   # status: clean, violation_count: 0

$ klt gen mos_array   --pdk gf180mcuD -o mos_array.gds
$ klt drc mos_array.gds   --deck gf180mcu --format json   # status: clean, violation_count: 0
```

All four generators' documented defaults are DRC-clean against the real
PDK variant, confirming `docs/cli/gen.md`'s own claim holds outside the
generators' own test suite. `res_array`, `diff_pair`, and `guard_ring` map
directly onto a bandgap-class floorplan's resistor/trim-ladder, amp
input-pair/mirror, and substrate/guard-ring sections respectively.

## The blocker

The floorplan's remaining — and, per both canary repos' own independent
Monte Carlo/sensitivity analyses, most consequential — element is the
common-centroid matched bipolar (PNP) array. No generator produces this
today. `mos_array` only draws generic MOS-shaped geometry (active + poly
gate + contact + local metal, the same four roles for every unit device
regardless of parameters); there is no bipolar-specific layer role, no
device-type parameter, and no path in `klt gen` to instantiate a PDK's own
fixed vendor BJT library cell (how vertical PNP devices are actually
realized in both sky130 and gf180mcu). Phase 2's own issue (#159) framed
bipolar arrays as already covered by `mos_array`'s "array/matching logic,
generalized to a different device primitive" — that generalization was
never implemented. Filed generically as
[#176](https://github.com/2AMLogic/klayout-tools/issues/176), with a
recommendation that it become its own follow-up epic-phase issue (the
right generation mechanism — drawn-from-base-layers vs. vendor-library-cell
instantiation — is itself an open design question, not a parameter tweak).

## Outcome against this phase's acceptance criteria

- A DRC-clean layout artifact for a canary block was **not** produced —
  honestly, not forced. Three of the four needed device families are
  verified DRC-clean against the real target PDK; the fourth (the matched
  bipolar array) has no generator at all, and is the load-bearing element
  for this device class regardless of which canary reaches a real
  layout-drawing issue first.
- Both candidate canary issues were updated with this outcome and
  cross-linked to #176 (see below).
- The generator gap was filed as new friction (#176), not silently worked
  around.
- Epic #152's own third success-criterion checkbox ("at least one canary
  repo's floorplan/layout issue unblocks and consumes a generator from this
  epic") is **not** checked off — it is genuinely not met, for the two
  independent reasons above (sequencing gap; generator gap).

## Where this was reported

- Friction (generator gap): `2AMLogic/klayout-tools#176`.
- Canary issue updates:
  [`gf180-bandgap#16`](https://github.com/2AMLogic/gf180-bandgap/issues/16#issuecomment-5150656362),
  [`sky130-bandgap#15`](https://github.com/2AMLogic/sky130-bandgap/issues/15#issuecomment-5150656738).
- Epic status: [`klayout-tools#152`](https://github.com/2AMLogic/klayout-tools/issues/152#issuecomment-5150657406).
