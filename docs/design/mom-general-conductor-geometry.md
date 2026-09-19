# Decision: generalize `klt mom`'s native PEEC engine for non-bar conductors

**Status:** decision record, no implementation. This document resolves the
open design question in [#1519](https://github.com/2AMLogic/klayout-tools/issues/1519)
("generalize `klt mom`'s native PEEC vs. route to `klt em`/geode-fem") that
was originally raised as part of [#1517](https://github.com/2AMLogic/klayout-tools/issues/1517).
The operator ruled for **path 1 — generalize the native PEEC engine** on
2026-09-15; this document records that decision, why, and the two-increment
plan that follows from it, with a named validation oracle per increment. It
follows the same decision-record pattern as the accepted spikes in this
directory (e.g.
[docs/design/digital-fleet-unit-abstraction-decision.md](digital-fleet-unit-abstraction-decision.md)):
no code changes ship in the PR that adds this file.

## The problem

`klt mom`'s PEEC inductance/resistance solve and its full-wave frequency
sweep are restricted to a "bar-shaped-conductor" MVP geometry class,
documented in [`docs/cli/mom.md`](../cli/mom.md)'s "The bar-shaped-conductor
MVP restriction" section and enforced in `native/mom/src/geometry.rs`:

- `classify_bar()` (`geometry.rs:344-389`) rejects a conductor whose box is
  not elongated at least `MIN_BAR_ASPECT_RATIO = 3.0` (`geometry.rs:286`)
  along its longest axis relative to the other two extents.
- `classify_shared_axis_bars()` (`geometry.rs:407-457`) additionally rejects
  any conductor built from more than one box (`geometry.rs:416-425`),
  conductors that don't all share one current-flow axis
  (`geometry.rs:430-439`), and conductors with mismatched axial span
  (`geometry.rs:443-454`).
- The full-wave path (`fullwave.rs:522`, `classify_full_wave_bars`) reuses
  the identical restriction (`fullwave.rs:11-16`).

A coiled/spiral on-chip inductor — one continuous conductor winding through
multiple turns — fails this classification for at least one of two
independent reasons: its bounding box is rarely elongated 3:1 (fails the
aspect check), and/or it is represented in the layout stackup as multiple
discrete box segments per turn (fails the "exactly one box per conductor"
check). `compute_inductance: true` and any `frequencies_hz` sweep are
rejected outright rather than approximated, with no escape hatch
(`docs/cli/mom.md`, "Scope and limitations").

This is not a bug — it is the MVP's documented, deliberate scope boundary
from Method-of-Moments epic [#701](https://github.com/2AMLogic/klayout-tools/issues/701)
(fully closed, phases 0-2c). The code comments already name the two
relaxations this would need: (1) multi-box conductors (relax "exactly one
box per conductor"), and (2) the general Ruehli mesh / general
unequal-length Neumann formula for conductors that don't share one axis and
one axial span — i.e. arbitrary bar orientation and offset, which a real
spiral (a faceted winding, not an axis-aligned bar) needs.

## The two paths considered

### Path 1 — generalize `klt mom`'s native PEEC engine (chosen)

Relax the single-box-per-conductor and shared-axis restrictions in
`native/mom/src/geometry.rs`, and generalize the Neumann mutual-inductance
formula (`docs/cli/mom.md`'s "Method: filament bundle + Neumann's formula")
from parallel/aligned/equal-length filament pairs to arbitrary orientation
and offset — then extend the same generalization to the full-wave retarded
kernel in `fullwave.rs`. This is the boundary-integral, incremental-extension
path, architecturally closest to how tools like FastHenry handle spiral
inductors (general multi-segment PEEC, not a full 3-D field solve).

### Path 2 — route to `klt em`/geode-fem (not chosen)

`docs/design/em-field-sim-spike.md` (a design spike, not yet scheduled)
ranks "Spiral inductor L/Q/SRF extraction" as its #1 use case by
tractability-times-demand, citing the gap documented in
`kb/entries/sky130-spiral-inductor.json` (sky130 ships no characterized
inductor primitive). Epic [#708](https://github.com/2AMLogic/klayout-tools/issues/708)
(finite-element full-field solver, `loom:operator-only`/`loom:operator-decision`,
currently stalled) is the closest existing tracking issue for this
direction, though its stated Phase 0-3 plan is 2-D/3-D electrostatics and
electrothermal, not RF/S-parameters specifically. Epic
[#840](https://github.com/2AMLogic/klayout-tools/issues/840) (closed)
already shipped a spiral-inductor L/Q benchmark export
([#877](https://github.com/2AMLogic/klayout-tools/issues/877), closed) via
`geode-fem`/WebGPU for the klayout-tools.org site gallery — evidence the
geode-fem path can already produce spiral L/Q numbers for visualization, not
a solution to `klt mom`'s CLI/JSON-contract gap.

### Why path 1

- **Not irreversible, and not gated on Epic #708.** Relaxing
  `classify_shared_axis_bars()` and generalizing the Neumann kernel is an
  incremental extension of a shipped engine (#701, phases 0-2c), not a new
  engine. A full-field FEM path, if it ever lands, is complementary
  (boundary-integral vs. volumetric) rather than a replacement — the MoM
  epic itself frames it that way.
- **The code already names the follow-up.** `geometry.rs:416-425` (multi-box)
  and `geometry.rs:430-454` (shared axis / equal length) both say, in their
  own error messages, "this is a follow-up" / "needs the general Ruehli
  mesh" / "needs the general unequal-length Neumann formula" — the
  generalization this document scopes is not a new idea, it is the MVP's own
  documented deferred work.
- **`klt em`/geode-fem is not CLI/JSON-contracted today.** Epic #708 is
  `loom:operator-only`, stalled, and scoped to electrostatics/electrothermal
  — not S-parameters. Routing this issue's ask (a `klt mom` JSON-contract
  gap) onto a stalled, differently-scoped epic would block indefinitely on a
  decision this document does not need to make.

The FEM path remains cross-referenced via epic #708 for any future
full-field work; it is out of scope for this decision and for
[#1519](https://github.com/2AMLogic/klayout-tools/issues/1519).

## The plan: two increments

### Increment (i) — multi-box conductors sharing one axis

**Scope.** Drop `classify_shared_axis_bars()`'s "exactly one box per
conductor" check (`geometry.rs:416-425`). A conductor may be built from
several boxes — the coax-shield-wall-segments case the existing error
message names — provided every box still individually classifies as a bar
(`classify_bar`, unchanged aspect-ratio/3-D-extent checks) and every box,
across every conductor in the request, still shares the *same* current-flow
axis and the *same* axial `[lo, hi]` span (`geometry.rs:430-454`, unchanged).
Concretely: `BarLayout`'s discretization (`discretize_bars`) needs to walk a
conductor's ordered box list and emit one filament grid per box, tagged back
to the owning conductor, rather than assuming one box per conductor — the
mutual-inductance formula itself (Neumann's thin-filament closed form,
`peec.rs:165-172`) is unchanged, since every filament pair remains parallel,
aligned, and equal-length under this increment's restrictions. This is the
narrower of the two relaxations: it changes *how many boxes* build a
conductor, not the *geometric relationship* between filaments.

**Validation oracle.** The existing analytic two-parallel-bars case already
in the test suite: `far_field_branch_is_the_one_taken_for_distant_bars` and
`far_field_and_closed_form_agree_at_the_crossover`
(`native/mom/src/peec.rs`), which check `partial_inductance_nh` against
Grover's exact thin-filament formula,

```
M(l, d) = (mu0 / 2*pi) * l * [ asinh(l/d) - sqrt(1 + (d/l)^2) + d/l ]
```

(the same formula documented in `docs/cli/mom.md`'s "Method: filament bundle
+ Neumann's formula"). A multi-box conductor discretizes to more filaments,
but every pairwise filament-filament mutual term is still exactly this
parallel/aligned/equal-length case, so the existing oracle is sufficient —
no new closed form is needed for increment (i). The new coverage this
increment adds is at the *classification* boundary, not the *arithmetic*:
a multi-box, single-axis fixture (e.g. a coax shield's four wall segments)
must no longer raise `MomError("bar-shaped ...")`, while the genuinely
out-of-scope shapes (mixed-axis, non-bar single box) must continue to.

### Increment (ii) — arbitrary orientation/offset filament pairs

**Scope.** Generalize the Neumann mutual-inductance formula from
parallel/aligned/equal-length filament pairs to filaments of arbitrary
relative orientation and offset — Grover's general filament-pair formulas
("Inductance Calculations: Working Formulas and Tables", Grover 1946), or a
numerical filament-pair integration where a closed form is impractical to
safely re-derive (following the same "don't transcribe a multi-term formula
from memory without independent verification" discipline #797/#836 already
established for this codebase — see `docs/design/mom-validation.md`'s "Why
re-derived, not cited"). This is what removes `classify_shared_axis_bars()`'s
shared-axis and shared-axial-span checks (`geometry.rs:430-454`) and is the
relaxation a genuine faceted spiral or offset loop needs — increment (i)
alone (same axis, same span, just more boxes) cannot represent a winding
that turns corners. The full-wave retarded kernel in `fullwave.rs` needs the
equivalent generalization for the frequency-domain sweep to follow.

**Validation oracle.** Two, per the "cite your oracle" convention:

1. **FastHenry** on a spiral fixture — a simple 2-3 turn square or
   octagonal spiral with known FastHenry-computed inductance, since FastHenry
   is the standard reference PEEC/filament-based extractor for exactly this
   geometry class (already cited as the architectural analogue in "Path 1"
   above and in `peec.rs`'s own module docs, "the standard technique used by
   filament-based PEEC/partial-inductance extractors (e.g. FastHenry)").

   **Superseded as written (2026-09-18).** FastHenry itself is out
   permanently — unpackaged everywhere this repo installs from, and licensed
   under MIT RLE's noncommercial/no-redistribution research notice rather
   than an OSI license (the operator ruling on
   [#1886](https://github.com/2AMLogic/klayout-tools/issues/1886); full
   citation in [`em-field-sim-spike.md`](em-field-sim-spike.md)'s Section 3
   license row). The oracle role is filled instead by **PyPEEC** (Dartmouth
   College, MPL-2.0), the same method class, on the same 2-turn square spiral
   fixture this section asks for: see
   [`mom-cross-validation.md`](mom-cross-validation.md)'s "The spiral
   fixture's oracle" and [`mom-validation.md`](mom-validation.md)'s "The
   external PyPEEC comparison".
2. **The existing analytic two-parallel-bars case** (same oracle as
   increment (i), `peec.rs`'s Grover-formula tests) — as a **regression**
   check that the generalized formula reduces exactly to the existing
   closed form in the parallel/aligned/equal-length special case (relative
   orientation → 0, offset → the existing `d`). This is the standard way to
   validate a generalization: a general formula's degenerate case must
   reproduce the specific formula it replaces, to at least the tolerance the
   specific formula is already validated to (`peec.rs`'s existing
   `max_relative = 1e-6`).

## Out of scope

- Any change to the full-field `klt em`/geode-fem direction (epic #708) —
  cross-referenced only.
- A schema change to `native/mom/src/contract.rs`'s conductor request shape
  is anticipated for increment (i) (a conductor's `boxes` field already
  accepts a list — `classify_shared_axis_bars` is what currently collapses
  that to length-1 — so this may not need a *wire-format* change, only a
  validation-logic change; confirm at increment (i) implementation time)
  but is not decided here.
- `docs/cli/mom.md` and `docs/design/mom-validation.md` updates reflecting
  the relaxed scope — deferred to the PR that lands each increment, so the
  documentation change lands alongside the behavior it describes.

## Next steps

Per this issue's "Revision 2026-09-15" plan: implement increment (i) with
`tests/test_mom.py` and Rust unit coverage in a follow-up PR (the existing
`pytest.raises(MomError, match="bar-shaped")` tests at
`tests/test_mom.py:628,802` move to cover only the shapes that remain
genuinely out of scope after increment (i) lands — mixed-axis, non-bar
single box), then increment (ii) in a subsequent PR. Tracking issues filed
for each increment reference this document and
[#1519](https://github.com/2AMLogic/klayout-tools/issues/1519).
