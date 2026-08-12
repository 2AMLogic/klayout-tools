# `klt mom` numeric validation: closed-form oracles and convergence

Phase 1 of the Method-of-Moments epic
([#701](https://github.com/2AMLogic/klayout-tools/issues/701)), delivered by
[#719](https://github.com/2AMLogic/klayout-tools/issues/719) (capacitance)
and [#797](https://github.com/2AMLogic/klayout-tools/issues/797)
(inductance/resistance). Where
[#718](https://github.com/2AMLogic/klayout-tools/issues/718) set `klt mom`'s
bar at "produces a numeric result", this document records
what that result is actually *worth*: the analytic oracles it is checked
against, the tolerances chosen, and the measured numbers behind them.

The executable form is `tests/test_mom_validation.py` and
`tests/test_mom_inductance.py`, plus `native/mom`'s own `cargo test` suite —
this file is the rationale, those are the gate. Values below were measured on
`native/mom` at the commit that introduced each harness; re-run
`pytest tests/test_mom_validation.py tests/test_mom_inductance.py -v --capture=tee-sys`
and `cd native/mom && cargo test --lib -- --nocapture` to reprint them.

Sections 1–5 below cover **capacitance** (#719); "Inductance and resistance
(PEEC partial elements)" further down covers **L and R** (#797).

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

## Inductance and resistance (PEEC partial elements)

Added by [#797](https://github.com/2AMLogic/klayout-tools/issues/797). Where
the capacitance oracles above are *analytic solutions of a boundary-value
problem*, the PEEC partial elements are **defined** by a sixfold integral
(Ruehli 1972) that has an exact closed form for rectangular bars (Hoer & Love
1965). That changes what "validation" means here: the question is not "is the
physics right" but "is the closed form transcribed and evaluated correctly",
and that is checkable to round-off rather than to a percent.

The executable form is `native/mom/src/peec.rs`'s and
`native/mom/src/geometry.rs`'s test modules plus
`tests/test_mom_inductance.py`. Reprint the Rust numbers with
`cd native/mom && cargo test --lib -- --nocapture peec::tests`.

### 1. The closed form is the right function — `∂⁶f/∂x²∂y²∂z² = 1/R`

Hoer & Love's `f` is *defined* by being the sixfold antiderivative of the
free-space static kernel. `f_is_the_sixfold_antiderivative_of_the_static_kernel`
applies a `(1, −2, 1)` second-difference stencil in each of x, y, z to the
transcribed `f` and compares against `1/√(x²+y²+z²)` directly.

**Stated tolerance**: `1e-4` relative at `h = 0.05`, over four sample points
(general, near-axis, asymmetric). That is the truncation floor of an `O(h²)`
stencil divided by `h⁶`, not a slack allowance — the check either passes at
round-off-limited accuracy or the transcription is wrong.

This is the strongest single statement in this section: it validates the
formula **without going through any inductance calculation at all**, so a
sign error or a dropped term cannot hide behind a compensating error
elsewhere.

### 2. Independent numerical quadrature of the same integral

The assembled geometric factor is compared against a Gauss-Legendre
quadrature that does the two *axial* integrations analytically and the four
*transverse* ones numerically — a genuinely different evaluation path.

| fixture | order | measured agreement | stated tolerance |
| ------- | ----- | ------------------ | ---------------- |
| two 1 × 1 × 10 µm bars, 3 µm apart, axially offset | 24 | — | `1e-6` |
| two abutting 1 × 0.5 × 20 µm bars (zero gap) | 40 | — | `5e-4` |

The abutting case gets the looser tolerance because it is the quadrature
*oracle* that struggles there (the cross-sections touch along an edge), not
the closed form; it is included because zero-gap neighbours are the case PEEC
most needs to get right.

### 3. Grover's exact thin-filament formula — far field

For two parallel filaments of equal length `l` and transverse separation `d`:

```
M = (µ0/4π) · 2 [ l·asinh(l/d) − √(l² + d²) + d ]
```

exact for zero cross-section. Bars with cross-sections small relative to `d`
must collapse onto it.

| fixture | measured | stated tolerance |
| ------- | -------- | ---------------- |
| `l = 100 µm`, `0.05 × 0.05 µm`, `d = 50 µm` | — | `1e-6` |
| `l = 100 µm`, `1 × 1 µm`, `d = 500 µm` (far-field branch) | — | `1e-6` |

### 4. The mean-distance asymptote for a straight bar

The classical asymptotic partial self-inductance of a bar of length `l`:

```
Lp = (µ0 l / 2π) [ ln(2l / GMD) − 1 + AMD/l + O((a/l)²) ]
```

with `GMD` and `AMD` the cross-section's **geometric** and **arithmetic** self
mean distances. Both are computed in the test from their definitions by
quadrature — nothing is transcribed but the shape of the expansion:

| constant (square of side `a`) | computed | published | agreement |
| ----------------------------- | -------- | --------- | --------- |
| `GMD/a` | 0.44704916 | 0.44705 (Rosa) | 1.8e-6 |
| `AMD/a` | 0.521405433 | 0.521405433 | < 1e-9 |

> **Why not Ruehli's `ln(2l/(w+t)) + 0.5 + 0.2235 (w+t)/l` directly?** It is
> the same formula with two roundings: `0.2235 (w+t)` for the GMD (which is
> what turns `−1` into `+0.5`, since `ln((w+t)/GMD) = 1.5`), and *the same*
> `0.2235 (w+t)` reused in the correction slot where the AMD belongs. Since
> `AMD/GMD = 1.166` for a square, that second rounding leaves a **first-order
> `0.074 a/l` residue** in the bracket. Measured against Ruehli's form the
> disagreement therefore bottoms out at ~2e-4 and stops improving no matter
> how slender the bar gets — which is indistinguishable, from a test's point
> of view, from the solver being wrong. Using the AMD removes the floor and
> restores the second-order convergence the asymptote actually has. This was
> found by measurement, not assumed: the first cut of this suite gated on
> Ruehli's form and the non-monotone error is what exposed it.

**Point measurement** (`l = 200 µm`, `0.25 × 0.25 µm`, `l/(w+t) = 400`):
closed form **0.287339649 nH**, asymptote **0.287339896 nH**, relative error
**8.6e-7**. **Stated tolerance**: `5e-6` (≈6× headroom).

### 5. Convergence under refinement

Three independent refinement axes, because they answer different questions.

#### (a) Against the asymptote, refining the *geometry* (`w+t = 1 µm`)

| `l` | `l/(w+t)` | rel. error vs asymptote | closed-form cancellation |
| --- | --------- | ----------------------- | ------------------------ |
| 10 µm  | 10   | 5.917e-5 | 2.64e-3 |
| 40 µm  | 40   | 2.664e-6 | 1.43e-5 |
| 160 µm | 160  | 1.385e-7 | 7.17e-8 |
| 640 µm | 640  | 1.246e-6 | 3.42e-10 |

**Observed order in `a/l`: p = 2.24** over the first three rows, against the
`O((a/l)²)` the expansion's remainder predicts. The gate asserts monotone
decrease and `p > 1.8` over those rows.

The `l = 640 µm` row is **reported but deliberately not gated**, and it is
the honest limit of the method: at `l/(w+t) = 1280` the 64-term alternating
sum cancels down to `3.4e-10` of its largest term, i.e. ~9.5 of 16 digits are
gone, and the 1.2e-6 there is that round-off floor rather than the
asymptote's remainder. **Practical statement: the closed form is good to
better than 1e-6 relative for bar aspect ratios up to ~1000:1**, which covers
any on-chip interconnect segment.

#### (b) Refining the *filament mesh* — a self-consistency axis

Because the partial-element integrals are closed-form, subdividing a bar and
reassembling it with the documented current weights must return the *same*
number, not a better one. A 50 × 1 × 0.5 µm bar cut into `n³` sub-filaments:

| `filament_subdivisions` | assembled `Lp` | drift vs whole bar |
| ----------------------- | -------------- | ------------------ |
| 2 | 0.047056234 nH | 7.9e-11 |
| 3 | 0.047056234 nH | 3.0e-11 |
| 4 | 0.047056234 nH | 3.8e-11 |

**Stated tolerance**: `1e-9`. This is what makes `filament_subdivisions` a
documented no-op for accuracy (see
[`docs/cli/mom.md`](../cli/mom.md#spec-file)) rather than a knob a user is
expected to tune.

#### (c) The classical filament approximation *does* converge to it

The complementary statement: approximate every sub-filament by its centre
line (ignoring cross-sectional extent, self terms kept exact — the classical
PEEC split) and refine. A 50 × 2 × 2 µm bar, exact value 0.037378326 nH:

| `n` | filament-approximation `Lp` | rel. error |
| --- | --------------------------- | ---------- |
| 2 | 0.037419503 nH | 1.102e-3 |
| 4 | 0.037393053 nH | 3.940e-4 |
| 8 | 0.037382482 nH | 1.112e-4 |

**Observed order: p = 1.48**; the gate asserts monotone decrease and `p > 1`.
This rules out the failure mode where the closed form and the mesh disagree
about what they are computing.

### 6. Algebraic identities the assembly must satisfy

| property | fixture | measured / gate |
| -------- | ------- | --------------- |
| Symmetry `Lp_jk = Lp_kj` | two 50 µm bars, 4 µm apart | `1e-12` |
| Axis independence (the kernel is isotropic) | one bar oriented along x, y, z | 2.6e-12 measured, `1e-9` gate |
| Perpendicular pairs are **exactly** zero (`dl_a · dl_b = 0`) | x-bar vs z-bar | exact `== 0.0` |
| Series composition `L = L₁ + L₂ + 2M₁₂` | a 60 µm bar cut in half | `1e-9` |
| Mutual falls off with separation | 50 µm bars at 2 µm vs 20 µm | monotone, positive |

The axis-independence gate is `1e-9` rather than bit-exact because the 64
terms are summed in a different order per axis and the sum cancels ~5 digits
at that aspect ratio; the measured agreement is 2.6e-12.

### 7. DC resistance — exact, not approximate

`R = l / (σ · A)` for a straight bar involves no approximation, which makes
it the one quantity here that can be gated at round-off:

| check | fixture | gate |
| ----- | ------- | ---- |
| Matches the closed form | 20 × 1 × 0.5 µm copper (σ = 5.8e7 S/m) ⇒ 0.68965517 Ω | `1e-12` |
| Independent of `filament_subdivisions` | same bar, `n = 1…4` | `1e-12` |
| Boxes of one conductor add in series | 20 µm + 10 µm collinear boxes ⇒ 1.03448276 Ω | `1e-12` |

The series-composition row is a *documented modelling assumption* being
verified, not a physical law — see
[`docs/cli/mom.md`](../cli/mom.md#reading-the-resistances) for why parallel
boxes are over-counted.

### 9. End to end through `klt mom` — `tests/test_mom_inductance.py`

Everything above exercises the Rust core directly. This last set drives the
whole command — GDS in, JSON out — on one fixture: **two 100 × 1 × 1 µm
copper bars (σ = 5.8e7 S/m) on a 10 µm pitch, in vacuum**. It is the same
fixture the JSON example in [`docs/cli/mom.md`](../cli/mom.md#json-schema-the-contract)
is printed from.

| quantity | measured | oracle | rel. error | stated tolerance |
| -------- | -------- | ------ | ---------- | ---------------- |
| `resistance_ohm[sig]` | 1.724137931 Ω | `l/(σA)` = 1.724137931 Ω | 0 (bit-exact) | `1e-12` |
| `inductance_matrix_nh[sig][sig]` | 0.102172196 nH | mean-distance asymptote 0.102172325 nH | **1.26e-6** | `1e-5` |
| `inductance_matrix_nh[sig][gnd]` | 0.041866193 nH | Grover thin filament 0.041864708 nH | **3.55e-5** | `1e-4` |

The mutual's residual is the fixture's finite cross-section (1 µm across at
a 10 µm pitch), which Grover's oracle idealises to zero — the oracle's error,
not the solver's, and the reason that row is gated at `1e-4` rather than at
round-off.

Refinement through the CLI knob, on the same fixture:

| `filament_subdivisions` | filaments | self drift | mutual drift | `resistance_ohm` drift |
| ----------------------- | --------- | ---------- | ------------ | ---------------------- |
| 2 | 16  | 5.74e-12 | 7.03e-12 | 1.3e-16 |
| 3 | 54  | 1.97e-10 | 9.77e-12 | 5.2e-16 |
| 4 | 128 | 2.25e-10 | 1.90e-11 | 1.0e-15 |

**Stated tolerances**: `1e-9` on the inductances, `1e-12` on the resistance.
Both are self-consistency statements: the closed-form partial elements and
the exact `l/(σA)` must not move under mesh refinement, and they do not.

### 8. The far-field / closed-form branch switch

The 64-term sum is switched out for a Gauss-Legendre quadrature of the exact
thin-filament integral once it has lost too many digits. Two things are
gated:

- **The two branches agree at the crossover** (`1e-6` relative), so the
  switch is not a discontinuity.
- **The switch never fires for overlapping or touching bars.** The
  cancellation floor alone is *not* a sufficient criterion: a slender bar's
  own self term cancels exactly as hard as a distant pair's (both scale like
  `l⁵` over the answer), and the far-field kernel is singular for a bar
  paired with itself. Before this was caught, a 200 × 0.25 × 0.25 µm bar's
  self inductance came back **6.3% low**. The guard now additionally requires
  a non-zero geometric gap between the two bars.

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
  oracles cannot (general geometry).
- **Loop inductance, ports, S-parameters.** The inductance section above
  validates Ruehli *partial* elements — per-segment quantities with no return
  path. Nothing here says a loop inductance assembled from them matches a
  measured or full-wave result; that needs a mesh reduction and a port
  definition, which are later phases of #701.
- **Frequency dependence.** Skin and proximity effects are absent; the
  resistances are DC and the partial inductances are the uniform-current-density
  limit.
- **Non-bar-shaped conductors.** Every inductance fixture above is a bar, and
  the current-flow direction is inferred from the box's longest axis. A pad, a
  plane, or a bend expressed as a single box is warned about by the solver and
  is not validated by anything here.

## See also

- [`docs/cli/mom.md`](../cli/mom.md) — the command, its spec-file schema, and
  its JSON contract.
- [`em-field-sim-spike.md`](em-field-sim-spike.md) — the original E&M
  field-sim survey (#103) that ranked quasi-static capacitance extraction as
  a high-value use case and set the precedent for reporting a
  Richardson-extrapolated result against an analytic oracle.
- [`design-evidence-tiers.md`](../design-evidence-tiers.md) — where this kind
  of evidence sits on the repo's evidence ladder.
