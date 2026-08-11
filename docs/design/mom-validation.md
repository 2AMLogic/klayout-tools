# `klt mom` numeric validation: closed-form oracles and convergence

Phase 1 of the Method-of-Moments epic
([#701](https://github.com/2AMLogic/klayout-tools/issues/701)), delivered by
[#719](https://github.com/2AMLogic/klayout-tools/issues/719). Where
[#718](https://github.com/2AMLogic/klayout-tools/issues/718) set `klt mom`'s
bar at "produces a numeric capacitance-matrix result", this document records
what that result is actually *worth*: the analytic oracles it is checked
against, the tolerances chosen, and the measured numbers behind them.

The executable form of everything here is `tests/test_mom_validation.py` —
this file is the rationale, that file is the gate. Values below were measured
on `native/mom` at the commit that introduced the harness; re-run
`pytest tests/test_mom_validation.py -v --capture=tee-sys` to reprint them.

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
- **Inductance, resistance, ports, S-parameters.** Later phases of #701.

## See also

- [`docs/cli/mom.md`](../cli/mom.md) — the command, its spec-file schema, and
  its JSON contract.
- [`em-field-sim-spike.md`](em-field-sim-spike.md) — the original E&M
  field-sim survey (#103) that ranked quasi-static capacitance extraction as
  a high-value use case and set the precedent for reporting a
  Richardson-extrapolated result against an analytic oracle.
- [`design-evidence-tiers.md`](../design-evidence-tiers.md) — where this kind
  of evidence sits on the repo's evidence ladder.
