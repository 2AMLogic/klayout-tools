# `klt mom` numeric validation: closed-form oracles and convergence

Phase 1 (and Phase 2a) of the Method-of-Moments epic
([#701](https://github.com/2AMLogic/klayout-tools/issues/701)), delivered by
[#719](https://github.com/2AMLogic/klayout-tools/issues/719) (capacitance),
[#797](https://github.com/2AMLogic/klayout-tools/issues/797) (PEEC
inductance/resistance, Phase 1a), and
[#893](https://github.com/2AMLogic/klayout-tools/issues/893) (full-wave
frequency sweep, Phase 2a). Where
[#718](https://github.com/2AMLogic/klayout-tools/issues/718) set `klt mom`'s
bar at "produces a numeric capacitance-matrix result", this document records
what that result is actually *worth*: the analytic oracles it is checked
against, the tolerances chosen, and the measured numbers behind them.

The executable form of the capacitance sections below is
`tests/test_mom_validation.py`; the inductance/resistance section ("§
Inductance/resistance") is `tests/test_mom_peec_validation.py`; the
full-wave frequency sweep section ("§ Full-wave frequency sweep") is
`tests/test_mom_fullwave_validation.py`. This file is the rationale, those
files are the gate. Values below were measured on `native/mom` at the
commit that introduced each harness; re-run `pytest tests/test_mom_validation.py
tests/test_mom_peec_validation.py tests/test_mom_fullwave_validation.py -v
--capture=tee-sys` to reprint them.

## Why analytic oracles at all

`klt mom` is a fresh, purpose-built BEM/MoM core (see
[`docs/cli/mom.md`](../cli/mom.md#why-rust)), not a wrapper around a
pre-validated engine. Nothing else in the repo can tell you whether its
numbers are right. Electrostatics is unusually well served here: several
geometries have exact closed forms, so correctness is *checkable* rather than
asserted — which is the whole point of epic #701's Phase 1, "the
reality-grounding discipline".

## The oracles

### 1. Ideal parallel plate — `C = εr ε0 A / d`

The textbook infinite-plate result, with no fringing. For *finite* plates it
is a **strict lower bound**: the fringing field beyond the plate edges can
only add capacitance. Both halves of that statement are asserted — the solver
must never fall below the bound, and the excess must shrink as `d/L → 0`,
which is the closed form's own regime of validity.

Fixture: two coincident 10 × 10 µm zero-thickness plates, `panel_size_um =
d/2`, `εr = 1`.

| gap `d` | `L/d` | measured `−C01` | `ε0 A/d` | excess |
| ------- | ----- | --------------- | -------- | ------ |
| 2.00 µm | 5     | 0.535612 fF     | 0.442709 fF | +21.0% |
| 1.00 µm | 10    | 1.013580 fF     | 0.885419 fF | +14.5% |
| 0.50 µm | 20    | 1.935653 fF     | 1.770838 fF | +9.3%  |

Measured 2026-09-22 with the near-field quadrature kernel (#2061). The excess
is fringing, and it decays as expected. **Stated tolerance** at the
`L/d = 20` operating point: the measured value must lie in
`[1.00, 1.25] × ε0 A/d`. That band is deliberately one-sided-by-physics (the
lower edge is a real bound, not a fudge) with headroom over the measured
+9.3%.

Reaching a genuinely *tight* two-sided band against this oracle would need
`L/d ≳ 60`, i.e. `panel_size_um = d/2` over a 10 µm plate — well past the
solver's 8000-panel guard and its dense `O(n³)` solve. Hence the second
oracle below.

### 2. Parallel plate with fringing — Kirchhoff (1877)

For a circular-disk capacitor of radius `a` and separation `d`:

```
C = ε π a² / d · [ 1 + (d / (π a)) · ( ln(16 π a / d) − 1 ) ]
```

evaluated for the disk with the *same area* as the square fixture. This is a
**shape-substituted oracle**: a square has ~13% more perimeter than an
equal-area disk, so the true square value sits slightly *above* it. Its role
is to bound the fringing magnitude, not to be exact.

| gap `d` | `L/d` | measured / Kirchhoff |
| ------- | ----- | -------------------- |
| 2.00 µm | 5     | 0.8366               |
| 1.00 µm | 10    | 0.9069               |
| 0.50 µm | 20    | 0.9500               |

Measured 2026-09-22 with the near-field quadrature kernel (#2061) —
`panel_size_um = d/2` throughout. **Stated tolerance**: 6% at `L/d = 20`
(measured: 5.0% low).

Why the band widened from 5%, and why that is the *right* answer rather than
a relaxation: pre-#2061 the centroid point-charge kernel overstated
near-field coupling by a few percent, which happened to offset the
constant-density basis's own under-resolution of the charge piling up on the
gap-facing surfaces — the old 0.982 ratio was two errors cancelling. With
the kernel corrected, what remains is the discretisation's, and it is shared
by the whole method class: FastCap, run on this very fixture as a co-witness,
measures 0.952 of Kirchhoff at `panel_size_um = 0.25` and 0.957 at `0.125`
— converging toward the closed form from the same side, ~0.2% from this
solver at the same mesh. The closed form bounds the *continuum* answer;
both solvers are honest about the *discretised* one.

### 3. Square coaxial line — `C' = 2πε / ln(R_outer / R_inner)`

The coaxial closed form, with the two radii replaced by the **exact
conformal-mapping equivalent radii** of a square cross-section:

- **Inner** — the exterior logarithmic capacity (transfinite diameter) of a
  square of side `a`: `Γ(¼)² / (4 π^{3/2}) · a ≈ 0.590170 a`. This is the
  radius of the circular cylinder whose *external* field is asymptotically
  identical.
- **Outer** — the interior conformal radius, at its centre, of a square of
  side `b`: `4 √π / Γ(¼)² · b ≈ 0.539326 b`, from the Schwarz–Christoffel map
  `f(z) = ∫ dw / √(1 − w⁴)` of the unit disk onto a square (its prevertices
  map to the square's *corners*, so `|f'(0)| = 1` gives half-diagonal
  `Γ(¼)² / (4 √(2π))`).

The shape factor collapses to a constant:

```
R_outer / R_inner = (16 π² / Γ(¼)⁴) · (b / a) ≈ 0.913852 · b/a
```

— a square coax is within ~9% *inside the logarithm* of a circular coax with
the same side-to-diameter ratio. The mapping is asymptotically exact as `b/a`
grows; at the `b/a = 3` used here it dominates the oracle's own error budget,
which is why the stated tolerance is 3% rather than 0.1%.

**Extracting a per-unit-length quantity from a 3-D solve.** `klt mom` solves
the full 3-D electrostatic problem, not a 2-D cross-section, so a finite coax
carries end effects the 2-D closed form knows nothing about (the fixture's
shield is an open-ended tube). Those effects are length-*independent*, so
solving two lengths of the same cross-section and differencing cancels them:

```
C' = ( C(L₂) − C(L₁) ) / ( L₂ − L₁ )
```

Fixture: 1 µm square inner conductor, shield inner face 3 µm across, 0.5 µm
walls, axis along x, `εr = 1`, lengths 4 and 8 µm.

| `panel_size_um` | panels (both lengths) | measured `dC/dL` | vs closed form |
| --------------- | --------------------- | ---------------- | -------------- |
| 2.00 µm         | 204                   | 0.04927575 fF/µm | −10.67%        |
| 1.00 µm         | 540                   | 0.05235736 fF/µm | −5.08%         |
| 0.50 µm         | 1760                  | 0.05390826 fF/µm | −2.27%         |

Measured 2026-09-22 with the near-field quadrature kernel (#2061).

Closed form: **0.05515975 fF/µm**. **Stated tolerance**: 3% at
`panel_size_um = 0.5` (measured: 2.27%). An independent finer run at
`panel_size_um = 0.25` lands at −0.68%, confirming the trend continues; it is
not in the test suite because that level costs tens of seconds (~7000 panels
across both lengths). The residual sits below the closed form for the same
reason the Kirchhoff band widened: the constant-density basis
under-resolves the corner-adjacent charge the inner conductor's edges
concentrate, and the pre-#2061 centroid kernel's over-coupling was
partly masking it.

### 4. Enclosure — `C_inner,inner = −C_inner,outer`

For a conductor fully surrounded by a shield, every field line from the inner
conductor terminates on the shield, so the Maxwell matrix's diagonal and
off-diagonal entries are equal and opposite. The fixture's tube is
open-ended, so the equality holds only up to end leakage — which must shrink
as the line lengthens:

| length | `1 + C01/C00` (leakage) |
| ------ | ----------------------- |
| 4 µm   | 4.90%                   |
| 8 µm   | 2.50%                   |

### 5. Permittivity linearity — `C(εr) = εr · C(1)`

The Laplace problem is linear in `ε`, so this is exact, and it is asserted at
`rel=1e-12` (round-off, not tolerance).

### 6. The near/far kernel-split boundary — an accepted discretisation artifact (#2323)

The near-field/far-field split (#2061) switches kernels at a hard distance
cutoff: pairs with centroid separation `d < 3·(r_i + r_j)` (panel
circumradii) get the Gauss-quadrature panel integral, everything farther out
gets the centroid point-charge kernel. The two kernels are different
approximations of the same potential coefficient, so a pair sitting exactly
at the cutoff gets a matrix entry that depends on which side of it the pair
lands: an **entry-level discontinuity** at the boundary, first probed during
PR #2322's review (issue #2323, reported as ~0.23%).

Measured (order-4 symmetrised collocation quadrature vs the centroid kernel,
for equal 1 µm square panels placed with centroid separation exactly at
`d = 3·(r_i + r_j)`; executable form:
`solver::tests::kernel_disagreement_at_the_threshold_stays_under_the_
documented_artifact_band`, re-run with
`cargo test -p klt-mom-native kernel_disagreement -- --nocapture`):

| pair shape at the boundary | kernel disagreement |
| -------------------------- | ------------------- |
| facing coaxial squares | 0.4585% |
| coplanar edge gap (the #2323 probe's pair) | 0.2303% |
| coplanar diagonal | 0.2360% |
| perpendicular (wall/floor) | 0.1141% |
| unequal sizes 2:1, facing | 0.5070% (worst) |

**Decision: document, don't blend.** The entry-level artifact is bounded by
**0.6%** (asserted band: measured worst 0.51%), but the entry level is not
what the oracles in this document gate — the solved capacitance matrix is.
Moving the threshold **±20%** (3.0 → 2.4 / 3.6) walks every panel pair
within that band of the boundary across it and re-solves, which is the
worst-case version of "a refinement sweep or geometry change moves entries
across the threshold" (executable form:
`threshold_perturbation_moves_the_solution_far_less_than_the_agreement_band`
in the default suite at 1.0 µm panels;
`threshold_perturbation_report -- --ignored --nocapture` in release mode at
the oracle's 0.5 µm operating point; measured with the #2061 kernel):

| fixture (0.5 µm panels) | panels | pairs switching (−20% / +20%) | worst solved-entry movement |
| ----------------------- | ------ | ----------------------------- | --------------------------- |
| parallel plates 10 × 10 µm, 1 µm gap | 800 | 15004 / 12852 | 0.1110% |
| coupled lines 20 × 2 × 0.6 µm, 1 µm gap | 816 | 9876 / 10052 | 0.0063% |
| shielded pair (the FastCap-oracle fixture) | 3068 | 69580 / 88352 | 0.0781% |

Even with up to ~88 000 pairs (≈1.9% of all pairs) forced across the
boundary at once, the solved matrix moves at most **0.111%** — a ~27×
margin under the 3% FastCap-oracle agreement band, and the flat-plate
band is the same 3%. The CG solve's charge redistribution damps the
entry-level kernel mismatch by another factor of several. A blended/ramped
transition band would add a third kernel regime (its own tuning knob, its
own boundary artifacts) to remove a mismatch that never reaches the
documented accuracy budget, so the hard cutoff stays and this section is
the record of why.

Two tests pin the artifact so it cannot grow silently: the boundary
predicate's strict-`<` semantics at exactly `d = 3·(r_i + r_j)`
(`near_field_predicate_is_strictly_less_at_the_threshold`), and the
kernel-disagreement and threshold-sensitivity bands above.

## Convergence under refinement

Two independent demonstrations, because they answer different questions.

### Against the solver's own extrapolated limit (parallel plate)

Standard three-mesh Richardson procedure: with `d₁ = v₀ − v₁`, `d₂ = v₁ − v₂`
and refinement ratio `r = 2`, the observed order is `p = ln|d₁/d₂| / ln r`
and the extrapolated limit is `v₂ + d₂ / (r^p − 1)`.

Fixture: 8 × 8 µm plates, `d = 1 µm`, `εr = 1`.

| `panel_size_um` | panels | `−C01` | rel. error vs limit |
| --------------- | ------ | ------- | ------------------- |
| 1.00 µm | 128  | 0.64184971 fF | 7.7547% |
| 0.50 µm | 512  | 0.66000137 fF | 5.1460% |
| 0.25 µm | 2048 | 0.67204672 fF | 3.4149% |

Measured 2026-09-22 with the near-field quadrature kernel (#2061).
**Observed order of convergence: p = 0.59**; Richardson limit
**0.69580754 fF**. With the kernel corrected, what the sequence converges at
is the constant-density basis's intrinsic rate against the plate-edge charge
concentration — no longer disguised by the old centroid kernel's
faster-shrinking over-coupling error. Two pieces of evidence that this is
the discretisation, not a kernel defect: FastCap, solving the same fixture
at `panel_size_um = 0.25`, measures 0.671246 fF — 0.12% from this solver's
finest level — and its own refinement (1.9400 → 1.9502 fF on the 10 µm
plates as `panel_size_um` halves 0.25 → 0.125) shows the same slow, ~half
order-of-convergence rate. The gate therefore asserts `p ≥ 0.4` for this
fixture — a floor set by co-witnessed measurement, not a fit — and still
rejects stagnant or diverging sequences outright.

### Against the analytic oracle (square coax)

The coax table in §3 *is* a convergence study with a known exact answer:
error 4.48% → 0.75% under one halving, i.e. an **observed order of 2.58**
measured against the closed form rather than against an extrapolation. This
is the stronger of the two statements — it rules out the failure mode where a
solver converges cleanly to the *wrong* number.

### A non-converging solver is not accepted

Issue #719 is explicit that the rate must be *gated*, not merely reported.
`ConvergenceReport.converged` requires both that successive refinements move
the answer strictly less each time and that the observed order reaches the
fixture's stated floor — first order by default, with the parallel-plate
fixture's FastCap-co-witnessed 0.4 floor as the one documented exception
(below). The gate is shown to fire three ways:

1. **On real solver output.** Run the same harness in the solver's documented
   breakdown regime — `panel_size_um` far larger than the plate gap, where a
   constant-density panel cannot resolve the charge facing a narrower gap
   (flagged by the discretisation warnings, #2061) — and refinement makes
   the answer move *more*, not less:

   | `panel_size_um` | `−C01` (10 × 10 µm plates, `d = 0.1 µm`) |
   | --------------- | ---------------------------------------- |
   | 4.0 µm | 2.706102 fF |
   | 2.0 µm | 4.318839 fF |
   | 1.0 µm | 7.022468 fF |

   Measured 2026-09-22 with the near-field quadrature kernel: observed order
   **−0.75**, and every solve carries a coarseness warning naming
   `panel_size_um`. `converged` is `False`. (The mutual term's *sign* — the
   old failure mode here, where the centroid kernel flipped it positive —
   survives the #2061 kernel fix, which is precisely why the coarseness
   diagnostic now carries this warning instead.)

2. **On synthetic sequences with known behaviour** — exactly first order,
   exactly second order (accepted); stagnant (`p = 0`), diverging (`p < 0`),
   and convergent-but-below-the-stated-floor (`p = 0.5` against the default
   first-order floor) (all rejected).

3. **By construction** — the estimator returns `p ≤ 0` for any sequence whose
   successive differences do not shrink, so there is no path by which a
   non-converging sequence reports "converged".

## Inductance/resistance

Phase 1a of the Method-of-Moments epic, delivered by
[#797](https://github.com/2AMLogic/klayout-tools/issues/797), which extends
`klt mom` to PEEC (Partial Element Equivalent Circuit) partial-inductance and
DC-resistance extraction alongside the capacitance solve above (see
[`docs/cli/mom.md`](../cli/mom.md#peec-inductanceresistance) for the method
and the bar-shaped-conductor MVP scope). The executable form of this section
is `tests/test_mom_peec_validation.py`; re-run
`pytest tests/test_mom_peec_validation.py -v --capture=tee-sys` to reprint
the numbers below.

### Why re-derived, not cited

Unlike the capacitance solver's oracles above (each a well-known closed
form, safely recalled and cross-checked against a second source), the
standard PEEC partial-inductance literature (Ruehli 1972; Hoer & Love 1965's
exact rectangular-bar formulas) has multi-term closed forms whose exact
coefficients #797's Curator review flagged as unsafe to transcribe from
memory. Live web search was not usable from the build environment either at
that time (queries came back with unrelated decoy results rather than a
clean failure). #797 shipped with the mutual-term Neumann double integral
**independently re-derived from first principles and numerically verified**
(symbolic integration via `sympy`), but paired it with an equal-area-circle
substitution for each filament's own self term (a circular cross-section's
self geometric mean distance, self-GMD, Monte-Carlo-verified rather than
transcribed) — safe to derive, but not exact for a rectangular filament: a
square's *true* self-GMD is ~1.7% larger than the equal-area circle's, which
systematically over-predicted self inductance by a measured ~0.3%
([#836](https://github.com/2AMLogic/klayout-tools/issues/836)).

#836 closed that gap by porting Hoer & Love's exact rectangular-bar closed
form for the self term specifically (`native/mom/src/peec.rs`'s
`self_partial_inductance_nh`), while keeping the mutual-term Neumann formula
above unchanged (filaments in `discretize_bars`'s grid never coincide, so the
thin-filament limit it assumes is accurate there). The same
transcription-trust problem #797 was worried about applies here too, so the
closed form is **not taken on trust**: it is defined by the property
`d^6 f / dx^2 dy^2 dz^2 = 1 / sqrt(x^2 + y^2 + z^2)`, and
`f_is_the_sixfold_antiderivative_of_the_static_kernel` checks exactly that by
sixth-order finite differencing, independently of any inductance formula — a
dropped term or sign error cannot survive it. The assembled self-inductance
is further cross-checked against the classical mean-distance asymptote for a
long straight bar, whose two constants (the cross-section's self geometric
and arithmetic mean distances) are themselves computed from their integral
definitions by quadrature, not transcribed. See `native/mom/src/peec.rs`'s
module docs for the full derivation of both the #797 mutual term and the
#836 self term.

[#1842](https://github.com/2AMLogic/klayout-tools/issues/1842) (increment
(ii) of `mom-general-conductor-geometry.md`'s two-increment plan) extends the
#797 mutual-term formula from parallel/aligned/equal-length filament pairs
to **arbitrary relative orientation and offset** — Grover's general
filament-pair result — which is what lets `classify_bars` (formerly
`classify_shared_axis_bars`) drop its shared-axis and shared-axial-span
checks, unlocking cross-axis and offset conductor geometry (an L-shaped
loop, a coiled/spiral winding). The same discipline applies again: the
general closed form is **re-derived, not transcribed**, and checked three
independent ways in `native/mom/src/peec.rs` —
`skew_antiderivative_is_the_double_antiderivative_of_the_static_kernel`
(the defining-property finite-difference check, the same technique
`f_is_the_sixfold_antiderivative_of_the_static_kernel` uses),
`skew_formula_matches_brute_force_quadrature` (a from-scratch 2-D
Gauss-Legendre quadrature over the raw double integral, sharing no code with
the closed form), and `skew_formula_reduces_to_the_parallel_closed_form`
(the required regression: the general formula reproduces the #797 closed
form exactly, in the parallel/aligned/equal-length degenerate case, to the
same `max_relative = 1e-6` that formula is already validated to).

### 0. Self-inductance closed form vs the mean-distance asymptote

The oracle #836 added, and the one that is *not* shape-substituted (unlike
Rosa's formula in §1 below): the classical mean-distance asymptote for the
partial self-inductance of a long straight bar,

```
Lp = (mu0 * l / 2*pi) * [ ln(2*l / GMD) - 1 + AMD/l + O((a/l)^2) ]
```

with `GMD`/`AMD` the cross-section's self geometric/arithmetic mean
distances — computed by 2-D quadrature straight from their `E[ln|r1-r2|]` /
`E[|r1-r2|]` definitions
(`self_gmd_of_a_square_matches_its_published_value`,
`self_mean_distance_of_a_square_matches_its_published_value`), reproducing
Rosa's published `GMD/a = 0.44705` (measured: `0.44704916`) and the classical
`AMD/a = 0.521405433` to `1e-8` relative. This is checked directly against
`self_partial_inductance_nh` (one filament, no bundle averaging), not
end-to-end through `klt mom`.

Fixture: 200 µm long, 0.25×0.25 µm square cross section (`l/(w+t) = 400`,
deep in the asymptote's regime — also the slender-bar geometry that a naive
port of the closed form without the far-field guard misfires 6.3% low on;
see `native/mom/src/peec.rs`'s "far-field branch" module docs).

| computed self-inductance | mean-distance asymptote | rel. error |
| ------------------------- | ------------------------ | ---------- |
| 0.287339648 nH             | 0.287339895 nH            | 8.598e-7   |

**Stated tolerance**: `5e-6` (measured: `8.6e-7`, ~6x headroom).

### 1. Straight-wire self-inductance — Rosa's formula

Rosa's classical DC (uniform current density) partial self-inductance of an
isolated straight round wire:

```
L = (mu0 * l / 2*pi) * (ln(2*l / a) - 3/4)
```

`klt mom`'s bar is rectangular, not round; the oracle's radius `a` is taken
as the equal-area circle's, `a = sqrt(width * height / pi)` — a
**shape-substituted oracle**, the same role the Kirchhoff-disk oracle plays
for the capacitance solver's square plates (§2 above). Because the oracle
itself is circular, the gap between it and `klt mom`'s (now-exact, per §0)
rectangular self term is the same ~1.7%-in-GMD, ~0.2%-in-inductance offset
regardless of #836's fix — this section is a shape-substituted end-to-end
pipeline sanity check, not the precision claim for the self term (§0 is).

Fixture: 2000 µm long, 4×4 µm square cross section (aspect ratio 500:1),
copper conductivity, `filament_size_um = 0.5`.

| measured `L₀₀` | Rosa oracle | rel. error | filaments |
| -------------- | ----------- | ---------- | --------- |
| 2.685629 nH    | 2.692048 nH | 0.2384%    | 64 (8×8)  |

**Stated tolerance**: 2% (measured: 0.24%, well inside — and in the expected
direction, slightly below the oracle, matching the theoretical circle-vs-
square GMD gap rather than solver imprecision).

### 2. Loop inductance — the two-wire transmission-line formula

For two identical parallel round wires of separation `D` (`a << D << l`):

```
L' = (mu0 / pi) * (ln(D/a) + 1/4)      [per unit length]
L_loop = 2 * (L_self - M(l, D))         [in the same asymptotic limit]
```

Fixture: 5000 µm long, 4×4 µm bars ("go"/"return"), 60 µm separation
(`l ≫ D ≫ a` throughout), `filament_size_um = 0.5`.

| measured `L_loop` | two-wire-line oracle | rel. error |
| ------------------ | --------------------- | ---------- |
| 7.007499 nH         | 7.060830 nH            | 0.7553%    |

**Stated tolerance**: 5% (measured: 0.76%) — looser than the straight-wire
check because the loop oracle compounds two asymptotic approximations
(`l ≫ a` *and* `l ≫ D`), each contributing its own residual (also a
shape-substituted, circular-radius oracle, same caveat as §1).

### 3. DC resistance — exact

`R = length / (conductivity * cross_sectional_area)` is Ohm's law for a
uniform bar — no approximation. Checked at `rel=1e-9` (round-off, not
tolerance), on a 200 µm × 2×2 µm copper bar.

### Convergence under filament refinement

Same fixture as §1 above (2000 µm, 4×4 µm bar), four filament-grid
refinement levels (1×1 → 2×2 → 4×4 → 8×8 filaments):

| `filament_size_um` | filaments | `L₀₀`         |
| ------------------- | --------- | ------------- |
| 4.00 (1×1)           | 1         | 2.68555391 nH |
| 2.00 (2×2)           | 4         | 2.68660626 nH |
| 1.00 (4×4)           | 16        | 2.68592860 nH |
| 0.50 (8×8)           | 64        | 2.68562896 nH |

Successive `|differences|`: `0.001052 → 0.000678 → 0.000300` nH — each step
strictly smaller than the last (the gate this test asserts, mirroring §"A
non-converging solver is not accepted" above). Post-#836 the self term no
longer contributes to this refinement signal at all (`self_partial_inductance_nh`
is exact for every filament regardless of grid), so what is left is purely
the bundle-of-filaments average's convergence as the mutual-term thin
-filament approximation is resolved on a finer cross-section grid — a
different (and, per the smaller absolute differences above, already tighter)
signal than pre-#836's coupled self-GMD-and-bundle convergence.

### 4. Systematic self-inductance error vs a square-bar geometry sweep

The five square-bar geometries #836 measured the pre-fix ~0.24–0.37%
over-prediction on, re-measured end-to-end through `klt mom` (`filament_size_um`
chosen for a 10×10 filament grid per bar) against the §0 mean-distance
asymptote:

| bar (`l × a × a`)      | pre-#836 (equal-area circle) | post-#836 (exact rectangle) | asymptote    |
| ------------------------ | ------------------------------ | ------------------------------ | ------------ |
| 100 × 1 × 1 µm            | 0.102501 nH (+0.32%)            | 0.102175 nH (+0.0028%)          | 0.102172 nH  |
| 20 × 1 × 1 µm              | 0.014132 nH (+0.37%)            | 0.014080 nH (+0.0041%)          | 0.014080 nH  |
| 10 × 1 × 1 µm              | 0.005723 nH (+0.30%)            | 0.005704 nH (+0.0306%)          | 0.005706 nH  |
| 200 × 0.25 × 0.25 µm      | 0.288027 nH (+0.24%)            | 0.287342 nH (+0.0006%)          | 0.287340 nH  |
| 50 × 2 × 2 µm              | 0.037519 nH (+0.37%)            | 0.037379 nH (+0.0013%)          | 0.037380 nH  |

The remaining residual (all `< 0.05%`, vs. the `~0.3%` before) is the
mutual-term bundle-averaging approximation and the asymptote's own
`O((a/l)^2)` remainder, not the self-GMD substitution #836 removed.

### 5. Generalized filament-pair formula (issue #1842): a spiral fixture

[#1842](https://github.com/2AMLogic/klayout-tools/issues/1842)'s stated
validation oracle was "FastHenry on a spiral fixture (2-3 turns) — a simple
2-3 turn square or octagonal spiral with known FastHenry-computed
inductance", since FastHenry is the classical reference filament-based PEEC
extractor for exactly this geometry class. **FastHenry is not that oracle,
and never will be** — the operator ruling on
[#1886](https://github.com/2AMLogic/klayout-tools/issues/1886) (2026-09-18)
resolved the licensing question
[`em-field-sim-spike.md`](em-field-sim-spike.md) had recorded as open, and it
resolved against FastHenry on two independent grounds:

1. **Unpackaged.** There is no `fasthenry` package in Debian/Ubuntu (`apt-get
   install fasthenry` → "No such package"), Homebrew, or PyPI. The earlier
   claim in this section that it "*is* packaged for Debian/Ubuntu" was
   wrong; CI would have had to build the sources itself.
2. **Not open source.** Those sources carry MIT RLE's 1990s research notice
   rather than an OSI license — present in FastHenry's own core
   (`src/fasthenry/induct.h`, `mulGlobal.h`) and in the FastCap-derived
   `zbuf/` code in every mirror (`ediloren/FastHenry2`,
   `ediloren/FastCap2`, and the `fasthenry-3.0wr` tarball inside
   `wrcad/xictools`, whose Apache-2.0 wrapper explicitly does not override
   inherited terms):

   > Permission to use, copy and modify for internal, noncommercial purposes
   > is hereby granted. Any distribution of this program or any part thereof
   > is strictly prohibited without prior written consent of M.I.T. […]
   > LICENSEE agrees not to make any copies except for LICENSEE'S internal
   > noncommercial use.

   The distribution clause forecloses not just vendoring but any future
   clean port of the code, so FastHenry is out permanently: **never a
   dependency, never an oracle, never ported.**

The criterion is instead closed out against
**[PyPEEC](https://github.com/otvam/pypeec)** (Dartmouth College, MPL-2.0,
JOSS [10.21105/joss.06644](https://doi.org/10.21105/joss.06644)) — the
modern, permissively-licensed solver in the same method class: 3-D
quasi-magnetostatic PEEC with an FFT-accelerated dense operator, extracting
terminal R/L from a voxelised geometry. Two independent checks now cover this
fixture:

- **In-repo, Rust**: `native/mom/src/peec.rs`'s
  `square_spiral_inductance_matches_independent_filament_oracle` validates
  the new capability against **the same method** — filament-based PEEC with
  Grover's general filament-pair formula — via a from-scratch second
  implementation that shares no code with `native/mom/src/peec.rs`'s
  production path: each spiral segment reduced to a single centreline
  filament (no cross-section bundle averaging), Rosa's closed-form self term
  (the same independent oracle §1 above already uses), and
  `brute_force_mutual_geom_um`'s 2-D Gauss-Legendre quadrature (not
  `mutual_geom_um`/`skew_antiderivative`) for every segment pair's mutual
  term. It is a genuine cross-check of the new physics (a real spiral corner
  turns axes; a real spiral's non-adjacent turns are parallel but offset and
  unequal in length — exactly what #1842 unlocks), but it is still this
  repo's own code, in the same language, by the same author.
- **External, Python**: `tests/test_mom_pypeec_cross_validation.py` +
  `scripts/mom_pypeec_reference.py` run PyPEEC on the same fixture in CI and
  compare — the genuinely-external check, with no correlated failure mode.
  See [`mom-cross-validation.md`](mom-cross-validation.md)'s "The spiral
  fixture's oracle" section for the methodology, and the measured numbers
  below.

Fixture: a 2-turn square spiral (8 segments), starting side 60 µm, 15 µm
pitch growth per turn, 2×2 µm cross-section, `filament_size_um = 1.0`
(2×2 filaments per segment). Each segment is modeled as its own PEEC
conductor (multi-box-per-conductor spirals are
[#1841](https://github.com/2AMLogic/klayout-tools/issues/1841)'s separate,
still-open scope); the total spiral inductance is the signed sum
`sum_i sum_j sign_i * sign_j * L[i][j]` over the full partial-inductance
matrix, `sign_i` correcting for the segments whose physical winding
direction runs opposite to `discretize_bars`' box-low-to-high filament
convention (the same external sign correction the two-wire "loop" fixtures
elsewhere in this document apply via `L[0][0] + L[1][1] - 2*L[0][1]`,
generalised to more than two segments).

| measured total (klt mom PEEC) | independent single-filament oracle | rel. error |
| ------------------------------ | ------------------------------------- | ---------- |
| 0.637718 nH                     | 0.638065 nH                            | 0.0542%    |

**Stated tolerance**: 2% (measured: 0.054%) — looser than the measured value
to leave headroom, the same convention every oracle in this document uses;
the residual is expected (the oracle's single-filament-per-segment
approximation skips the cross-section bundle averaging the production path
does).

That fixture's executable form is a **Rust** unit test — re-run `cargo test
-p klt-mom-native square_spiral_inductance -- --nocapture` from `native/mom`
to reprint the numbers above, not the `pytest
tests/test_mom_peec_validation.py` command this document's introduction gives
for the rest of this section.

#### The external PyPEEC comparison (issue #1886)

`tests/test_mom_pypeec_cross_validation.py` runs the same spiral through
`klt mom`'s Python entry point (`run_mom`, so the whole `klt mom` stack, not
just the Rust inductance kernel) and through PyPEEC as a subprocess, at
`voxel_pitch_um = 1.0` (2 voxels across the bar, 2 through it) and 100 MHz:

| quantity | klt mom PEEC | PyPEEC (external oracle) | rel. error |
| -------- | ------------ | ------------------------ | ---------- |
| total inductance | 0.637886 nH (32 filaments) | 0.635732 nH (2640 conductor voxels of 22684) | 0.339%     |
| DC resistance    | 2.887500 Ω                 | 2.865452 Ω                                   | 0.769%     |

**Stated tolerance**: 2% on the inductance (measured: 0.339%) — the same band
the Rust fixture states against its in-repo oracle, and for the same reason:
each solver carries its own discretisation error and the two budgets can add
rather than cancel. The resistance is a secondary metric with a deliberately
loose 10% band (measured: 0.769%): the two solvers model the **corners**
differently — `klt mom` reports the exact 1-D bar resistance summed over the
eight legs (`ρ · 660 µm / 4 µm²` = 2.8875 Ω, current strictly along each
leg's own axis) while PyPEEC solves the real 3-D distribution, in which the
current cuts each corner diagonally and so travels slightly less than the
full centreline length, which is why PyPEEC's value sits just below.

Two conditions were measured rather than assumed, since the comparison is
only meaningful if both hold:

- **Frequency independence** (the quasi-DC regime both solvers model — copper's
  skin depth at 100 MHz is ~6.6 µm, over 3× the bar's largest cross-sectional
  dimension): PyPEEC reports 0.635732 nH at 10 MHz, 0.635732 nH at 100 MHz,
  and 0.635730 nH at 500 MHz.
- **Convergence under voxel refinement**: halving the voxel pitch to 0.5 µm
  (21120 conductor voxels of 181472, ~9 s and ~230 MB vs. ~2 s at 1.0 µm)
  moves PyPEEC's answer to 0.636108 nH — +0.059%, i.e. *toward* `klt mom`'s
  value, and an order of magnitude inside the stated tolerance. CI uses the
  1.0 µm mesh.

The `klt mom` number here (0.637886 nH) differs slightly from the Rust
fixture's (0.637718 nH) because the two fixtures handle corner metal
differently, and deliberately so. The Rust fixture lets adjacent segment
boxes **overlap** by one 2×2 µm square at each corner (harmless there: it
calls the inductance path directly). The Python fixture cannot — it goes
through the full `run_mom`, whose capacitance solver rejects intersecting
conductors outright (coincident boundary panels make the
potential-coefficient matrix singular, and `klt mom` detects that and errors
rather than returning nonsense). Its eight boxes therefore **tile** the
trace: each end that meets another leg is pushed forward by half a width
along its own direction, so every corner square belongs to exactly one leg.
`scripts/mom_pypeec_reference.py` builds PyPEEC's voxel geometry from the
identical rule, independently implemented, so both solvers in *this*
comparison see exactly the same metal.

Re-run it with:

```bash
uv sync --extra dev --group mom --extra mom-pypeec-cross-validation
uv run --extra dev --group mom --extra mom-pypeec-cross-validation \
  pytest tests/test_mom_pypeec_cross_validation.py -v --capture=tee-sys
```

The test skips with an explicit reason when `pypeec` is not installed, and
`.github/workflows/ci.yml`'s mom leg carries a matching "no silent skip"
assertion step so a skip can never be the only thing CI sees — it greps the
tee'd pytest log for skip markers rather than re-asserting `import pypeec`
(issue #2307), which observes what pytest actually did instead of only one of
the module's two skip gates.

The module also carries a **falsifiability control**,
`test_the_comparison_can_actually_fail`, in the same role
`tests/test_mom_capacitance_oracle.py`'s control plays for the FastCap
pairing ([`fastcap-oracle.md`](fastcap-oracle.md)): it checks `klt mom`'s
clean-spiral answer against PyPEEC's answer for a *seeded-defect* spiral
(`start_side_um` 60 → 45 µm, the innermost side shrunk 25%) and requires the
result to land **outside** the 2% agreement band — measured **32.54%**,
versus 0.34% when the defect is removed. Without it, both agreement tests
above would pass just as happily if the oracle ignored the geometry it was
handed and re-solved a cached or default spiral, making "they agree" a
tautology rather than a falsifiable claim.

## Full-wave frequency sweep

Phase 2a of the Method-of-Moments epic, delivered by
[#893](https://github.com/2AMLogic/klayout-tools/issues/893), which extends
`klt mom` from the quasi-static solves above to a genuine frequency-domain,
full-wave sweep — a retarded free-space Green's function
(`exp(-jkR)/(4*pi*R)`) rather than the quasi-static `1/(4*pi*R)` kernel — for
the same bar-shaped-conductor geometry PEEC uses (see
[`docs/cli/mom.md`](../cli/mom.md#full-wave-frequency-sweep) for the method
and scope). The executable form of this section is
`tests/test_mom_fullwave_validation.py`; re-run
`pytest tests/test_mom_fullwave_validation.py -v --capture=tee-sys` to
reprint the numbers below.

### The oracles

- **Two-wire transmission-line characteristic impedance** — for two
  identical parallel round wires of radius `a` separated by `D` (`a << D`),
  `Z0 = (eta0 / pi) * acosh(D / (2*a))`, `eta0 = sqrt(mu0/eps0)` the
  free-space wave impedance. Same shape-substituted-oracle convention as the
  inductance/resistance section above (`a` is the equal-area-circle radius
  of the square bar cross-section).
- **TEM propagation** — in a homogeneous, lossless background medium, any
  uniform two-conductor transmission line's propagation constant is exactly
  `gamma = j*omega*sqrt(er)/c0` (`beta = omega*sqrt(er)/c0`, `alpha = 0`),
  independent of the line's cross-sectional geometry — the identity
  `L' * C' = er / c0^2` any TEM line satisfies. Unlike the `acosh` oracle
  above, this one is **not** shape-substituted: it holds exactly for this
  fixture's vacuum background regardless of the bar cross-section or
  separation, so it is the tighter of the two checks in principle (its
  measured error below is dominated by the solve's own MVP approximations —
  the equivalent-thin-wire shape substitution and the point-collocation
  axial fill — not by the oracle).

### 1. Characteristic impedance vs the two-wire-line closed form

Fixture: 500 µm long, 2×2 µm bars ("go"/"return"), 40 µm separation,
`panel_size_um = 2.0`, `segment_size_um = 5.0`, 1 MHz (`k*length ~= 1.6e-5`,
deeply quasi-TEM).

| measured `Z0` | two-wire-line oracle | rel. error |
| -------------- | --------------------- | ---------- |
| 452.3668 ohm    | 427.7799 ohm           | 5.75%      |

**Stated tolerance**: 10% (measured: 5.75%) — looser than the static
loop-inductance check's 5% (`docs/design/mom-validation.md`'s
"Inductance/resistance" section) because this measurement compounds three
independent MVP approximations rather than one: the capacitance solve's own
point-collocation discretisation error, the full-wave solve's
equivalent-thin-wire shape substitution, and the finite-length/end-effect
gap between a 500 µm finite bar and the oracle's infinite-line assumption.

### 2. Propagation constant vs the TEM identity

Same fixture, at 1 GHz (large enough to measure `beta` precisely).

| measured `beta`  | TEM oracle `omega/c0` | rel. error | measured `alpha` |
| ------------------ | ------------------------ | ---------- | ------------------- |
| 22.647 rad/m         | 20.958 rad/m               | 8.06%      | 1.7e-9 Np/m (~0)     |

**Stated tolerance**: 10% (measured: 8.06%), attenuation asserted `< 0.1%`
of the phase constant (effectively lossless, as expected for a vacuum
background with no loss mechanism modeled). The residual is the same MVP
approximation budget as §1 above, since `beta` is derived from the same
measured `Z'`/`C'` pair.

### Convergence under axial mesh refinement

Same fixture as §1, four `segment_size_um` refinement levels:

| `segment_size_um` | `Z0`          |
| -------------------- | --------------- |
| 40.00                  | 921.567293 ohm  |
| 20.00                  | 686.121716 ohm  |
| 10.00                  | 531.408270 ohm  |
| 5.00                   | 452.366829 ohm  |

Successive `|differences|`: `235.445577 -> 154.713446 -> 79.041441` — each
step strictly smaller than the last (the gate
`test_characteristic_impedance_converges_under_segment_refinement` asserts,
mirroring the "A non-converging solver is not accepted" discipline above).
The refinement has not yet reached the asymptotic regime at
`segment_size_um = 5.0` (the differences are still shrinking by roughly a
factor of 2 per halving, not yet the faster falloff a fully-resolved
point-collocation fill would show) — consistent with §1's oracle comparison
landing at 5.75% rather than closer to 0%. A materially finer
`segment_size_um` is not in the test suite (cheap to run manually, but not
worth the added CI time for a signal the four levels above already
establish); the remaining gap to the oracle is expected to keep shrinking
the same way `docs/design/mom-iterative-solver.md`'s own refinement
discussion frames the tradeoff for the capacitance solve.

## What is *not* validated here

- **Absolute accuracy of a general layout.** These are canonical fixtures
  with closed forms. Nothing here says what the solver does on a real
  extracted geometry with non-rectangular shapes — `klt mom` approximates
  every shape by its bounding box (see
  [`docs/cli/mom.md`](../cli/mom.md#scope-and-limitations)).
- **Multi-dielectric stacks.** The MVP solves one homogeneous medium; the
  permittivity check above only exercises the linear scaling of that single
  value.
- **Cross-validation against an external solver.** #719 lists this as
  optional. [`em-field-sim-spike.md`](em-field-sim-spike.md) recommends
  openEMS for full-wave work and geode-fem's quasi-static/DC-extrapolation
  mode as the cheaper in-house cross-check for exactly this regime; either
  would be a natural follow-up, and would test something these analytic
  oracles cannot (general geometry). This is **no longer a gap** for the two
  fixtures that have external oracles wired into CI — the full-wave sweep
  against NEC2++ ([#895](https://github.com/2AMLogic/klayout-tools/issues/895),
  [`mom-cross-validation.md`](mom-cross-validation.md)) and the spiral
  against PyPEEC ([#1886](https://github.com/2AMLogic/klayout-tools/issues/1886),
  "The external PyPEEC comparison" above) — but it remains one for every
  other fixture in this document, which is checked against closed forms and
  in-repo re-implementations only.
- **Multi-box-per-conductor PEEC/full-wave geometry.** Every conductor must
  still reduce to exactly one bar-shaped box —
  [#1841](https://github.com/2AMLogic/klayout-tools/issues/1841) (a separate,
  still-open increment) is what would let a single electrical conductor span
  several boxes (e.g. a coax shield's wall segments, or a real spiral
  modeled as one continuous net rather than one conductor per segment, as
  the spiral fixture above does). Ports, S-parameter de-embedding
  ([#894](https://github.com/2AMLogic/klayout-tools/issues/894)), and
  skin-effect (frequency-dependent resistance/capacitance) behavior remain
  later phases of #701.

## See also

- [`docs/cli/mom.md`](../cli/mom.md) — the command, its spec-file schema, and
  its JSON contract.
- [`em-field-sim-spike.md`](em-field-sim-spike.md) — the original E&M
  field-sim survey (#103) that ranked quasi-static capacitance extraction as
  a high-value use case and set the precedent for reporting a
  Richardson-extrapolated result against an analytic oracle.
- [`design-evidence-tiers.md`](../design-evidence-tiers.md) — where this kind
  of evidence sits on the repo's evidence ladder.
- `tests/test_mom_fullwave_validation.py` — the executable form of the
  "Full-wave frequency sweep" section above.
- [#893](https://github.com/2AMLogic/klayout-tools/issues/893) — delivered
  the full-wave frequency sweep (Phase 2a); [#894](https://github.com/2AMLogic/klayout-tools/issues/894)
  (port definition/de-embedding) and
  [#895](https://github.com/2AMLogic/klayout-tools/issues/895)
  (cross-validation against an external solver) are its planned follow-ons.
