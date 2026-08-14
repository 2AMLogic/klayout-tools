# Phase 2c: grading `--critical-net`/`--distributed-rc` against `klt mom`

**Status:** measurement record, not a proposal. Epic
[#709](https://github.com/2AMLogic/klayout-tools/issues/709) Phase 2c, issue
[#978](https://github.com/2AMLogic/klayout-tools/issues/978). Phase 2
(coupling capacitance, issue
[#976](https://github.com/2AMLogic/klayout-tools/issues/976); distributed
RC, issue [#977](https://github.com/2AMLogic/klayout-tools/issues/977)) is
shipped. This document grades it against Epic
[#701](https://github.com/2AMLogic/klayout-tools/issues/701)'s Method-of-
Moments field solver (`klt mom`, shipped), per the parent epic's own
reality-grounding discipline: **"extraction accuracy is graded against MoM,
not asserted."** Every number below is reproducible by re-running
[`examples/critical-net-mom-fidelity/generate_and_measure.py`](../../examples/critical-net-mom-fidelity/generate_and_measure.py);
the resulting evidence record is committed at
[`evidence/sim/sky130-critical-net-fixture/mom-coupling-fidelity/`](../../evidence/sim/sky130-critical-net-fixture/mom-coupling-fidelity/),
following [`docs/design/sim-evidence-discipline-spike.md`](sim-evidence-discipline-spike.md)'s
append-only `evidence-record/1` convention.

## Why a purpose-built fixture

`--critical-net`'s lateral-coupling pass (issue #976) requires two named
nets with facing same-layer geometry inside the deck's own lookback distance
(`ParasiticsDeck.metal_sidewall_lookback_um`). Before building a new fixture,
this task first tried every existing candidate real layout in the repo:

- `examples/design-pipeline/06-layout.gds` (the `sky130-ota-5t` OTA canary
  already wired into the `sim/` evidence convention, #973) with every
  non-power net named `--critical-net`.
- Every `tests/corpus/sky130/*.gds` standard cell (`buf_4`, `dfxtp_2`,
  `inv_1`, `nand2_2`) with every non-power net named `--critical-net`.

**Every one reports `total_coupling_capacitance_ff == 0.0`.** `dfxtp_2`
alone shows non-zero *coupling* once every internal net is named critical,
but all of it is vertical-overlap coupling (issue #760, always on regardless
of `--critical-net`) — `lateral_levels: []` on every coupled pair, meaning
`--critical-net`'s own lateral pass contributes nothing on any of these four
cells either. This is itself a real finding, not a null result to hide: at
standard-cell scale, sky130's own met1/li1 pitch keeps named-net routing far
enough apart (or the cells are simply too small to have two *different*
named nets sharing a layer within lookback) that the lateral pass has
nothing to grade on the corpus this repo already commits. So this task
builds the smallest fixture that *does* exercise the pass with a known
answer — reusing, not reinventing, geometry: **the identical layout
`tests/test_extract.py`'s `_make_lateral_coupling_layout()` already uses**
for issue #976's own acceptance test
(`test_lateral_coupling_exact_value_and_lookback_cutoff`). Reusing that
exact geometry means this survey's own Phase 2a number (0.044 fF) is
checked against the same already-shipped, already-tested closed-form
coefficient math, not a fresh derivation.

Three same-layer (met1) 2×1 μm bars:

| Net | x-range (μm) | Gap to previous | Inside met1 lookback (0.28 μm)? |
| --- | --- | --- | --- |
| `AGR` | [0, 2] | — | — |
| `VIC` | [2.1, 4.1] | 0.1 μm | yes |
| `FAR` | [4.45, 6.45] | 0.35 μm (from `VIC`) | no |

## Method

For both the `AGR`/`VIC` pair (Phase 2a's in-lookback case) and the
`VIC`/`FAR` pair (the lookback-cutoff case), three `klt extract` runs plus
one direct `klt mom` solve:

1. **Phase 1 baseline** — `klt extract --parasitics` (no `--critical-net`):
   lateral coupling is not modelled at all, `0.0` fF for both pairs by
   construction.
2. **Phase 2a** — `klt extract --parasitics --critical-net VIC` (issue
   #976): sky130's curated `defaultsidewall` coefficient for met1
   (0.044 fF/μm × 1 μm facing edge) applies to `AGR`/`VIC` (inside
   lookback); `VIC`/`FAR` stays `0.0` (outside it).
3. **Phase 2b** — adds `--distributed-rc` (issue #977). This fixture has
   zero device terminals on any net (pure geometry + labels), so the ladder
   itself falls back to the star model here (documented "fewer than two
   terminals" tolerance) — see "Phase 2b has nothing for MoM to grade"
   below for why this doesn't leave a gap in the comparison.
4. **`klt mom` ground truth** — a direct, in-memory two-conductor solve
   (`klayout_tools.mom.solve_capacitance_matrix`, the same API `klt extract
   --mom-net`'s own crosscheck, issue #798, is built on): both bars as
   zero-thickness plates in the same z=0 plane, `background_permittivity =
   3.9` (SiO2, matching `--mom-net`'s own convention), **no synthesized
   ground plate** — see "Scope: no ground plate, and why" below.

## Results

### `AGR`/`VIC` (0.1 μm gap, inside lookback)

| Model | Coupling capacitance | Δ vs. `klt mom` |
| --- | --- | --- |
| Phase 1 (lumped RC, no coupling model) | `0.0` fF | **−100%** (misses the physical effect entirely) |
| Phase 2a (`--critical-net`, curated coefficient) | `0.044` fF | **−55.1%** |
| `klt mom` (converged, 7832 panels) | `0.09795` fF | — (oracle) |

**Measured fidelity gain: Phase 2a moves this net pair from "the model
reports zero, off by the whole physical quantity" to "the model reports
about 45% of an ab-initio isolated-plate estimate."** That is a genuine,
quantified improvement over Phase 1 — and an equally genuine, quantified gap
against the MoM oracle, stated as a number rather than a pass/fail badge.

**Convergence** (`panel_size_um` halved each step, capped by `klt mom`'s
8000-panel guard):

| `panel_size_um` | `panel_count` | `coupling_ff` |
| --- | --- | --- |
| 0.100 | 400 | 0.091238 |
| 0.050 | 1600 | 0.095536 |
| 0.025 | 6400 | 0.097734 |
| 0.0225 | 7832 | 0.097950 |

The answer is still moving (+0.9% between the last two points) but has
clearly slowed relative to the +4.7%/+2.3% earlier steps — consistent with
[`docs/design/mom-validation.md`](mom-validation.md)'s own documented
convergence behaviour, and the panel-count guard forecloses pushing further
on this exact geometry. `0.09795` fF is reported as the converged estimate
with that caveat stated, not silently rounded past.

### `VIC`/`FAR` (0.35 μm gap, outside lookback)

| Model | Coupling capacitance | Δ vs. `klt mom` |
| --- | --- | --- |
| Phase 1 | `0.0` fF | **−100%** |
| Phase 2a | `0.0` fF (outside declared lookback) | **−100%** |
| `klt mom` (converged, 6400 panels) | `0.070502` fF | — (oracle) |

Neither model improves on the other here — both report zero. The isolated
two-plate MoM solve, in contrast, still shows a meaningful residual
(`0.0705` fF is 72% as large as the in-lookback pair's own `0.098` fF, not a
small tail). Read plainly, that would say the lookback cutoff throws away a
lot of real coupling. Read against the idealisation this measurement makes
(next section), it says something narrower and more honest.

## Scope: no ground plate, and why

`klt extract --mom-net`'s own crosscheck (issue #798) places a synthesized
ground plate beneath a net at a z-gap inverted from the deck's own
`cap_area_ff_um2` coefficient — for sky130 met1, `_mom_crosscheck_gap_um`
gives roughly 1.34 μm. Sizing panels finely enough to resolve this fixture's
0.1 μm lateral gap (`panel_size_um ≤ gap / 4 = 0.025`) while also covering a
ground plate padded to that ~1.34 μm vertical gap's own 3× padding
convention (`_MOM_CROSSCHECK_GROUND_PAD_FACTOR`) was tried directly during
this task and blows past the 8000-panel guard by roughly 20× (≈175,000
panels for the combined AGR+VIC+padded-ground system) — the two length
scales (0.1 μm lateral gap vs. ~1.34 μm vertical gap, padded 3×) are too far
apart to resolve both with one global panel size inside the guard.

So the numbers above come from an **isolated two-conductor** solve: `AGR`
and `VIC` (or `VIC` and `FAR`) alone, no substrate, no other metal, no
shielding. This is a real, stated idealisation, not a hidden one:

- **It plausibly *overstates* both models' gap against reality.** A real
  sky130 stack has a grounded substrate and neighbouring structures nearby
  that terminate some of the field lines this isolated solve instead routes
  entirely between `AGR` and `VIC` (or `VIC` and `FAR`). Sky130's own
  `defaultsidewall` coefficient was itself derived from a real, shielded 3D
  field solve (a Magic/FastCap-class extraction over the real stack), not
  from an isolated two-plate abstraction — so part of the −55% delta above
  is measuring "curated coefficient vs. an idealisation that omits shielding
  the real coefficient's own derivation included," not purely "curated
  coefficient vs. reality."
- **This is exactly the same idealisation `--mom-net`'s own shipped
  crosscheck already makes and documents**, one level up: that crosscheck's
  synthesized ground plate is stated explicitly as "a modelling choice… not
  a measurement of this layout's real substrate/well geometry"
  (`docs/cli/extract.md`'s `--mom-net` section). This document's own
  idealisation is one level further from a full 3D solve (no ground plate
  at all, where `--mom-net` at least synthesizes one), stated with the same
  discipline.
- **The `VIC`/`FAR` result should be read through this lens specifically.**
  The lookback cutoff is a hard distance threshold; an isolated two-plate
  electrostatic solve has no such threshold — coupling always falls off
  smoothly, never to exactly zero. That the isolated solve still reports
  meaningful coupling at 0.35 μm does not, by itself, mean the lookback
  cutoff is wrong for the real (shielded) 3D case; it means an unshielded
  two-plate abstraction is the wrong oracle to grade a hard-cutoff heuristic
  against at longer range. A future increment that resolves both length
  scales in one solve (a ground plate at coarser panel granularity than the
  lateral gap — not supported by `klt mom`'s current single-`panel_size_um`
  request shape) would be a better oracle for exactly this question; it is
  out of this issue's scope (see "Follow-ups" below).

## Phase 2b has nothing for `klt mom` to grade

`--distributed-rc` (issue #977) reorders a net's own terminals along their
approximate physical spread and breaks its total R/C into a chain of
per-segment resistors and per-terminal ground capacitors — but the *total*
stays identical to the lumped star model's. Checked directly (not asserted)
on `tests/corpus/sky130/sky130_fd_sc_hd__dfxtp_2.gds` (which, unlike this
document's own fixture, has real device terminals to chain):

```
$ uv run python examples/critical-net-mom-fidelity/generate_and_measure.py
```

| | `total_capacitance_ff` | `total_coupling_capacitance_ff` |
| --- | --- | --- |
| Lumped (Phase 2a) | 10.860575 | 1.054535 |
| Distributed (Phase 2b) | 10.860575 | 1.054535 |

Byte-identical. `--distributed-rc` changes **where** a net's capacitance
sits (which internal node it's attached to) and **how many** resistors
separate two given terminals — it never changes **how much** capacitance
the net has, to ground or to a coupled neighbour. `klt mom`'s shipped solve
is a capacitance oracle; there is no separate capacitance number Phase 2b
produces for it to grade. Phase 2b's own fidelity claim is topological
(closer per-terminal delay modelling along a routed net, Elmore-style), not
a capacitance claim — a different axis than this issue's oracle can measure,
stated here rather than silently skipped.

## Bottom line

| Question | Answer |
| --- | --- |
| Does Phase 2a (coupling capacitance) measurably improve on Phase 1? | **Yes** — from "0% of the physical quantity" (not modelled) to "~45% of an idealised ab-initio estimate," on the one pair this repo's committed geometry has to test it on. |
| Is Phase 2a's remaining gap against `klt mom` small? | **No** — a measured 55% delta remains on the in-lookback pair, and the lookback cutoff itself isn't graded conclusively here (the oracle used is too idealised to arbitrate the cutoff distance specifically — see "Scope" above). |
| Does Phase 2b (distributed RC) have a measurable capacitance fidelity gain against `klt mom`? | **Not applicable** — verified directly that it changes zero capacitance values; its fidelity claim is topological, outside this oracle's scope. |

This is the epic's own bar met honestly: a **measured**, reproducible,
explainable delta — not an assertion that Phase 2 "improved fidelity,"
and not a bare pass/fail stamp either.

## Follow-ups (not implemented here)

- **A `--critical-net`-paired `klt mom` crosscheck as a shipped CLI
  feature** (mirroring `--mom-net`, issue #798) would let a caller run this
  exact comparison on their own design without hand-authoring a fixture —
  a natural next increment, filed as a separate issue rather than folded
  into this measurement task.
- **A two-length-scale `klt mom` request** (independent panel sizing for a
  coarse ground plate vs. a fine lateral gap) would let a future crosscheck
  include substrate shielding without the panel-count blowup this document
  hit — the concrete blocker preventing a more realistic (not isolated)
  oracle for the `VIC`/`FAR` lookback-cutoff question above.
