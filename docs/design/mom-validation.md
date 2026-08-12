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
| 2.00 µm | 5     | 0.557075 fF     | 0.442709 fF | +25.83% |
| 1.00 µm | 10    | 1.050318 fF     | 0.885419 fF | +18.62% |
| 0.50 µm | 20    | 2.000852 fF     | 1.770838 fF | +12.99% |

The excess is fringing, and it decays as expected. **Stated tolerance** at
the `L/d = 20` operating point: the measured value must lie in
`[1.00, 1.25] × ε0 A/d`. That band is deliberately one-sided-by-physics (the
lower edge is a real bound, not a fudge) with headroom over the measured
+13.0%.

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
| 2.00 µm | 5     | 0.8701               |
| 1.00 µm | 10    | 0.9398               |
| 0.50 µm | 20    | 0.9820               |

**Stated tolerance**: 5% at `L/d = 20` (measured: 1.8% low). The residual
sign is the expected one — the solver slightly *under*-predicts fringing,
since the plate corners are the least well resolved region and the
point-charge off-diagonal kernel is weakest exactly where panels are close.

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
which is why the stated tolerance is 2% rather than 0.1%.

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
| 1.00 µm         | 540                   | 0.05268784 fF/µm | −4.48%         |
| 0.50 µm         | 1856                  | 0.05474662 fF/µm | −0.75%         |

Closed form: **0.05515975 fF/µm**. **Stated tolerance**: 2% at
`panel_size_um = 0.5` (measured: 0.75%). An independent finer run at
`panel_size_um = 0.25` lands at −0.23%, confirming the trend continues; it is
not in the test suite because that level costs ~35 s (≈4900 panels at the
longer length).

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

## Convergence under refinement

Two independent demonstrations, because they answer different questions.

### Against the solver's own extrapolated limit (parallel plate)

Standard three-mesh Richardson procedure: with `d₁ = v₀ − v₁`, `d₂ = v₁ − v₂`
and refinement ratio `r = 2`, the observed order is `p = ln|d₁/d₂| / ln r`
and the extrapolated limit is `v₂ + d₂ / (r^p − 1)`.

Fixture: 8 × 8 µm plates, `d = 1 µm`, `εr = 1`.

| `panel_size_um` | panels | `−C01` | rel. error vs limit |
| --------------- | ------ | ------- | ------------------- |
| 1.00 µm | 128  | 0.69110996 fF | 1.0398% |
| 0.50 µm | 512  | 0.68463811 fF | 0.0936% |
| 0.25 µm | 2048 | 0.68405546 fF | 0.0084% |

**Observed order of convergence: p = 3.47**; Richardson limit
**0.68399782 fF**. (The order is well above the first order a
point-collocation constant-panel fill guarantees asymptotically — at these
panel sizes the error is still dominated by a faster-decaying term. The gate
asserts `p ≥ 1`, a floor rather than a fit.)

### Against the analytic oracle (square coax)

The coax table in §3 *is* a convergence study with a known exact answer:
error 4.48% → 0.75% under one halving, i.e. an **observed order of 2.58**
measured against the closed form rather than against an extrapolation. This
is the stronger of the two statements — it rules out the failure mode where a
solver converges cleanly to the *wrong* number.

### A non-converging solver is not accepted

Issue #719 is explicit that the rate must be *gated*, not merely reported.
`ConvergenceReport.converged` requires both that successive refinements move
the answer strictly less each time and that the observed order is at least
first order; the tests assert it. The gate is shown to fire three ways:

1. **On real solver output.** Run the same harness in the solver's documented
   breakdown regime — `panel_size_um` far larger than the plate gap, where
   the centroid-to-centroid `1/r` kernel overestimates coupling — and
   refinement makes the answer move *more*, not less:

   | `panel_size_um` | `−C01` (10 × 10 µm plates, `d = 0.1 µm`) |
   | --------------- | ---------------------------------------- |
   | 4.0 µm | −0.090837 fF |
   | 2.0 µm | −0.229554 fF |
   | 1.0 µm | −0.951310 fF |

   Observed order **−2.38**, and every solve carries a physicality warning
   (the mutual term has the wrong sign). `converged` is `False`.

2. **On synthetic sequences with known behaviour** — exactly first order,
   exactly second order (accepted); stagnant (`p = 0`), diverging (`p < 0`),
   and convergent-but-slower-than-first-order (`p = 0.5`) (all rejected).

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
  oracles cannot (general geometry). [#895](https://github.com/2AMLogic/klayout-tools/issues/895)
  tracks this specifically for the full-wave sweep above.
- **Non-bar-shaped PEEC/full-wave geometry, ports, S-parameters.** The PEEC
  and full-wave solves' shared MVP scope (a single, elongated, axis-aligned
  bar per conductor — see `docs/cli/mom.md`'s "PEEC inductance/resistance")
  is a separate, documented restriction, not something this section's
  oracles exercise; general-mesh PEEC, port definition/de-embedding
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
