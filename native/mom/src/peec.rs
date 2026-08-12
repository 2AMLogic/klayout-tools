//! PEEC partial elements: the partial-inductance matrix for a set of
//! rectangular current-carrying filaments (issue #797, Phase 1 of the
//! Method-of-Moments epic #701).
//!
//! # What a "partial" inductance is
//!
//! Following Ruehli's Partial Element Equivalent Circuit formulation
//! (A. E. Ruehli, "Inductance Calculations in a Complex Integrated Circuit
//! Environment", IBM J. Res. Dev. 16(5):470-481, 1972), the partial
//! inductance between two parallel conductor segments `a` and `b` carrying
//! uniform current density along a common axis is
//!
//! ```text
//! Lp_ab = (mu0 / 4 pi) * 1/(A_a A_b) * int_{V_a} int_{V_b} dV_a dV_b / |r_a - r_b|
//! ```
//!
//! where `A_a`, `A_b` are the cross-sectional areas *perpendicular to the
//! current-flow axis*. Two crucial consequences the code below leans on:
//!
//! 1. The volume double integral itself is isotropic -- it knows nothing
//!    about which axis carries the current. The current direction enters
//!    only through the `1/(A_a A_b)` normalisation. So one closed form
//!    serves every axis.
//! 2. For *perpendicular* segments the Neumann integrand carries a
//!    `dl_a . dl_b` factor that vanishes identically, so the partial mutual
//!    inductance between two perpendicular filaments is exactly zero. An
//!    L-shaped path therefore needs no special handling.
//!
//! # How the integral is evaluated
//!
//! For two axis-aligned rectangular bars the sixfold integral has a closed
//! form: because the integrand depends only on coordinate *differences*,
//! integrating twice in each of x, y, z collapses to an alternating sum of
//! a single scalar function `f` evaluated at the four difference-limits per
//! axis -- 4^3 = 64 terms. That is the Hoer & Love result (C. Hoer and
//! C. Love, "Exact Inductance Equations for Rectangular Conductors with
//! Applications to More Complicated Geometries", J. Res. NBS 69C(2):127-137,
//! 1965), and `hoer_love_f` below is their `f`.
//!
//! **The formula is not taken on trust.** `f` is defined by the property
//! `d^6 f / dx^2 dy^2 dz^2 = 1 / sqrt(x^2 + y^2 + z^2)`, and the test module
//! checks exactly that by sixth-order finite differencing, independently of
//! any transcription. The assembled partial inductance is then cross-checked
//! three further ways: against numerical quadrature, against Grover's exact
//! thin-filament formula in the far field, and against the classical
//! mean-distance asymptote for a long straight bar (whose two constants --
//! the cross-section's geometric and arithmetic self mean distances -- are
//! themselves computed from their definitions by quadrature, not
//! transcribed). See `docs/design/mom-validation.md`.
//!
//! # The far-field branch
//!
//! The 64-term alternating sum is catastrophically ill-conditioned when the
//! two bars are far apart relative to their own size: individual terms grow
//! like `D^5` while their sum falls off like `1/D`, so the result is the
//! difference of numbers ~`D^6/V` times larger than itself. Past a measured
//! cancellation floor the code switches to a small Gauss-Legendre quadrature
//! over the two cross-sections of the *exact* thin-filament axial integral,
//! which is numerically stable and (being a far-field regime) accurate to
//! well under the crossover error. Both branches are checked against each
//! other at the crossover in the tests.

use crate::geometry::{transverse_axes, Filament};

/// `mu0 / (4 pi)`, in H/m. CODATA 2018 gives
/// `mu0 = 1.256 637 062 12e-6 H/m`, so this is `1.000 000 000 55e-7` rather
/// than the pre-2019 exact `1e-7`.
const MU0_OVER_4PI_H_PER_M: f64 = 1.000_000_000_55e-7;

/// Converts a geometric factor expressed in micrometres into nanohenries:
/// `mu0/4pi [H/m] * 1e-6 [m/um] * 1e9 [nH/H]`.
const GEOM_UM_TO_NH: f64 = MU0_OVER_4PI_H_PER_M * 1e3;

/// Relative-cancellation floor for the Hoer & Love sum. When the 64-term
/// alternating sum comes back smaller than this fraction of its largest
/// single term, more than ~8 significant digits have been lost to
/// cancellation and the far-field quadrature branch is used instead. In
/// practice this trips at a bar-separation-to-size ratio of roughly 20:1,
/// where the quadrature branch is itself good to ~1e-8 relative (measured in
/// `far_field_and_closed_form_agree_at_the_crossover`).
///
/// **The floor alone is not a sufficient test** -- see
/// `partial_inductance_nh`, which additionally requires the two bars to be
/// geometrically disjoint before it will believe it.
const CANCELLATION_FLOOR: f64 = 1e-8;

/// Three-point Gauss-Legendre nodes on `[-1, 1]`.
const GAUSS3_NODES: [f64; 3] = [-0.774_596_669_241_483_4, 0.0, 0.774_596_669_241_483_4];

/// Three-point Gauss-Legendre weights, pre-divided by the interval width 2
/// so they sum to 1 and the rule computes an *average* over the interval.
const GAUSS3_WEIGHTS: [f64; 3] = [5.0 / 18.0, 8.0 / 18.0, 5.0 / 18.0];

/// Fill the conductor-level partial-inductance matrix, in nanohenries.
///
/// `L[p][q] = sum_{i in p} sum_{j in q} w_i w_j Lp_ij`, with `w` the
/// per-filament current weight `Filament::weight` (see its doc comment for
/// why one uniform weight per box is exactly right). The result is
/// symmetric by construction -- only the upper triangle of filament pairs is
/// evaluated.
pub fn partial_inductance_matrix_nh(
    filaments: &[Filament],
    conductor_count: usize,
) -> Result<Vec<Vec<f64>>, String> {
    let mut l = vec![vec![0.0_f64; conductor_count]; conductor_count];
    for (i, a) in filaments.iter().enumerate() {
        for (j, b) in filaments.iter().enumerate().skip(i) {
            if a.axis != b.axis {
                // Perpendicular segments: dl_a . dl_b == 0.
                continue;
            }
            let value = a.weight * b.weight * partial_inductance_nh(a, b);
            l[a.conductor_index][b.conductor_index] += value;
            if i != j {
                // The pair (j, i) contributes the same amount; adding it
                // here is what makes skipping the lower triangle exact.
                l[b.conductor_index][a.conductor_index] += value;
            }
        }
    }

    if l.iter().flatten().any(|v| !v.is_finite()) {
        return Err(
            "partial-inductance fill produced a non-finite value -- check for \
             degenerate (zero-extent) or coincident filament geometry"
                .to_string(),
        );
    }
    Ok(l)
}

/// Partial inductance between two parallel rectangular bars, in nanohenries.
/// `a.axis` must equal `b.axis`; the caller is responsible for skipping
/// perpendicular pairs (whose partial inductance is identically zero).
pub fn partial_inductance_nh(a: &Filament, b: &Filament) -> f64 {
    debug_assert_eq!(a.axis, b.axis);
    let areas = a.cross_area_um2() * b.cross_area_um2();
    let (sum, largest_term) = hoer_love_sum(a, b);
    // A *slender* bar's own self term cancels just as hard as a distant
    // pair's -- the 64-term sum's largest term grows like `l^5` for a bar of
    // length `l`, so a 1000:1 aspect ratio loses as many digits as a 1000:1
    // separation does. The far-field branch is nonsense for a bar paired with
    // itself (the thin-filament kernel it integrates is singular exactly
    // there), so the cancellation floor is only trusted for bars that are
    // genuinely apart. Without this guard a 200 x 0.25 x 0.25 um bar's self
    // inductance came back 6.3% low.
    let geom_um = if min_gap_um(a, b) > 0.0
        && largest_term > 0.0
        && sum.abs() < CANCELLATION_FLOOR * largest_term
    {
        far_field_geom_um(a, b)
    } else {
        sum / areas
    };
    GEOM_UM_TO_NH * geom_um
}

/// Shortest distance between the two bars' surfaces, in micrometers; exactly
/// `0.0` when they touch, overlap, or are the same bar.
fn min_gap_um(a: &Filament, b: &Filament) -> f64 {
    let mut squared = 0.0;
    for k in 0..3 {
        let gap = (b.lo[k] - a.hi[k]).max(a.lo[k] - b.hi[k]).max(0.0);
        squared += gap * gap;
    }
    squared.sqrt()
}

/// The raw 64-term Hoer & Love alternating sum (units: um^5), together with
/// the magnitude of its largest single term -- the caller compares the two
/// to detect catastrophic cancellation.
fn hoer_love_sum(a: &Filament, b: &Filament) -> (f64, f64) {
    let mut sum = 0.0;
    let mut largest = 0.0_f64;
    for (dx, sx) in difference_limits(a, b, 0) {
        for (dy, sy) in difference_limits(a, b, 1) {
            for (dz, sz) in difference_limits(a, b, 2) {
                let term = sx * sy * sz * hoer_love_f(dx, dy, dz);
                sum += term;
                largest = largest.max(term.abs());
            }
        }
    }
    (sum, largest)
}

/// The four `(difference, sign)` pairs one axis contributes to the
/// alternating sum.
///
/// For an integrand depending only on `v - u`,
/// `int_{a0}^{a1} int_{b0}^{b1} g(v - u) du dv
///  = G(b1-a0) - G(b1-a1) - G(b0-a0) + G(b0-a1)` where `G'' = g`. Applying
/// that in each of x, y, z is what turns the sixfold integral into 4^3
/// evaluations of `hoer_love_f`.
fn difference_limits(a: &Filament, b: &Filament, axis: usize) -> [(f64, f64); 4] {
    [
        (b.hi[axis] - a.lo[axis], 1.0),
        (b.hi[axis] - a.hi[axis], -1.0),
        (b.lo[axis] - a.lo[axis], -1.0),
        (b.lo[axis] - a.hi[axis], 1.0),
    ]
}

/// Hoer & Love's `f`: the sixfold antiderivative of the free-space static
/// kernel, i.e. the function satisfying
/// `d^6 f / dx^2 dy^2 dz^2 = 1 / sqrt(x^2 + y^2 + z^2)`.
///
/// It is symmetric under any permutation of its arguments (the kernel is
/// isotropic) and even in each argument separately, so the sign convention
/// chosen in `difference_limits` does not matter.
fn hoer_love_f(x: f64, y: f64, z: f64) -> f64 {
    let (x2, y2, z2) = (x * x, y * y, z * z);
    let r2 = x2 + y2 + z2;
    if r2 <= 0.0 {
        return 0.0;
    }
    let r = r2.sqrt();

    // Three logarithmic terms, one per axis:
    //   (b^2 c^2 / 4 - b^4 / 24 - c^4 / 24) * a * ln((a + R) / sqrt(b^2 + c^2))
    let mut acc = log_term(x, y, z, r) + log_term(y, z, x, r) + log_term(z, x, y, r);

    // Algebraic term.
    acc += (x2 * x2 + y2 * y2 + z2 * z2 - 3.0 * x2 * y2 - 3.0 * y2 * z2 - 3.0 * z2 * x2) * r / 60.0;

    // Three arctangent terms; the cubed variable is the one in the
    // denominator of the arctangent's argument.
    acc -= atan_term(z, x, y, r) + atan_term(y, x, z, r) + atan_term(x, y, z, r);
    acc
}

/// `(b^2 c^2 / 4 - b^4 / 24 - c^4 / 24) * a * ln((a + R) / sqrt(b^2 + c^2))`.
///
/// Evaluated as `-ln((R - a) / s)` when `a < 0`: the identity
/// `(R + a)(R - a) = b^2 + c^2 = s^2` makes the two forms exactly equal,
/// and taking whichever of `R + a` / `R - a` is the *sum* of like-signed
/// quantities avoids cancellation for a strongly negative `a`.
fn log_term(a: f64, b: f64, c: f64, r: f64) -> f64 {
    let (b2, c2) = (b * b, c * c);
    let coefficient = b2 * c2 / 4.0 - b2 * b2 / 24.0 - c2 * c2 / 24.0;
    if coefficient == 0.0 {
        // Also the only case in which `s` below can be zero (b == c == 0),
        // so this guard doubles as the singularity guard.
        return 0.0;
    }
    let s = (b2 + c2).sqrt();
    let log = if a >= 0.0 {
        ((a + r) / s).ln()
    } else {
        -((r - a) / s).ln()
    };
    coefficient * a * log
}

/// `(u * v * w^3 / 6) * atan(u * v / (w * R))`.
fn atan_term(w: f64, u: f64, v: f64, r: f64) -> f64 {
    if w == 0.0 || r == 0.0 {
        // The `w^3` coefficient vanishes; `atan` of the resulting infinity
        // is bounded, so the product is zero.
        return 0.0;
    }
    u * v * w * w * w / 6.0 * (u * v / (w * r)).atan()
}

// --- far-field branch --------------------------------------------------------

/// Partial inductance geometric factor (um) for two well-separated parallel
/// bars, by 3x3-point Gauss-Legendre quadrature over each cross-section of
/// the *exact* thin-filament axial double integral.
///
/// This is the same integral `hoer_love_sum` evaluates, only with the two
/// axial integrations done analytically (`axial_filament_um`) and the four
/// transverse ones done numerically. The transverse integrand is smooth and
/// slowly varying once the bars are far apart, so three points per dimension
/// is plenty; it is numerically stable where the closed form is not.
fn far_field_geom_um(a: &Filament, b: &Filament) -> f64 {
    let axis = a.axis;
    let (p, q) = transverse_axes(axis);
    let mut acc = 0.0;
    for (ia, &na) in GAUSS3_NODES.iter().enumerate() {
        let ap = midpoint(a, p, na);
        for (ja, &ma) in GAUSS3_NODES.iter().enumerate() {
            let aq = midpoint(a, q, ma);
            let wa = GAUSS3_WEIGHTS[ia] * GAUSS3_WEIGHTS[ja];
            for (ib, &nb) in GAUSS3_NODES.iter().enumerate() {
                let bp = midpoint(b, p, nb);
                for (jb, &mb) in GAUSS3_NODES.iter().enumerate() {
                    let bq = midpoint(b, q, mb);
                    let (du, dv) = (ap - bp, aq - bq);
                    let rho = (du * du + dv * dv).sqrt();
                    acc += wa
                        * GAUSS3_WEIGHTS[ib]
                        * GAUSS3_WEIGHTS[jb]
                        * axial_filament_um(a.lo[axis], a.hi[axis], b.lo[axis], b.hi[axis], rho);
                }
            }
        }
    }
    acc
}

/// Map a Gauss-Legendre node on `[-1, 1]` onto `filament`'s extent along
/// `axis`.
fn midpoint(filament: &Filament, axis: usize, node: f64) -> f64 {
    let (lo, hi) = (filament.lo[axis], filament.hi[axis]);
    0.5 * (lo + hi) + 0.5 * (hi - lo) * node
}

/// `int_{a0}^{a1} int_{b0}^{b1} dv du / sqrt(rho^2 + (v - u)^2)` -- the
/// exact partial inductance (bar the `mu0/4pi`) of two parallel thin
/// filaments spanning `[a0, a1]` and `[b0, b1]` along their common axis and
/// separated transversely by `rho`.
///
/// The second antiderivative of `1/sqrt(rho^2 + t^2)` is
/// `H(t) = t asinh(t/rho) - sqrt(t^2 + rho^2)`; the alternating sum over the
/// four difference-limits gives the double integral. For equal, aligned
/// filaments this reduces to Grover's classical
/// `2 [l asinh(l/rho) - sqrt(l^2 + rho^2) + rho]`, which the tests check.
fn axial_filament_um(a0: f64, a1: f64, b0: f64, b1: f64, rho: f64) -> f64 {
    let limits = [
        (b1 - a0, 1.0),
        (b1 - a1, -1.0),
        (b0 - a0, -1.0),
        (b0 - a1, 1.0),
    ];
    limits
        .iter()
        .map(|(t, sign)| sign * axial_antiderivative(*t, rho))
        .sum()
}

/// `H(t) = t asinh(t / rho) - sqrt(t^2 + rho^2)`, with the `rho -> 0` limit
/// handled explicitly.
///
/// As `rho -> 0`, `H(t) -> |t| ln(2|t|) - |t| ln(rho) - |t|`. The
/// `-|t| ln(rho)` piece is dropped: the four difference-limits of two
/// *disjoint* collinear filaments all share a sign, so their signed sum is
/// `(b1-a0) - (b1-a1) - (b0-a0) + (b0-a1) = 0` and that term cancels
/// exactly. (Two *overlapping* collinear thin filaments genuinely diverge --
/// but overlapping bars are never far-field, so they take the closed-form
/// branch, where the real cross-section keeps the integral finite.)
fn axial_antiderivative(t: f64, rho: f64) -> f64 {
    if t == 0.0 {
        return -rho;
    }
    if rho <= 0.0 {
        let at = t.abs();
        return at * (2.0 * at).ln() - at;
    }
    t * (t / rho).asinh() - (t * t + rho * rho).sqrt()
}

#[cfg(test)]
mod tests {
    use super::*;
    use approx::assert_relative_eq;

    fn bar(lo: [f64; 3], hi: [f64; 3], axis: usize) -> Filament {
        Filament {
            lo,
            hi,
            axis,
            weight: 1.0,
            conductor_index: 0,
        }
    }

    /// A bar of length `l` (along z) and square-ish cross-section `w x t`,
    /// with its near corner at the origin plus the given offset.
    fn z_bar(l: f64, w: f64, t: f64, offset: [f64; 3]) -> Filament {
        bar(offset, [offset[0] + w, offset[1] + t, offset[2] + l], 2)
    }

    // --- oracle 1: f is the sixfold antiderivative of 1/R -------------------

    /// `d^6 f / dx^2 dy^2 dz^2` by central differences, which is the
    /// *defining* property of Hoer & Love's `f`. This checks the
    /// transcription of the closed form directly, without going through any
    /// inductance formula.
    fn sixth_mixed_difference(x: f64, y: f64, z: f64, h: f64) -> f64 {
        // Second-difference stencil (1, -2, 1) applied in each of x, y, z.
        let stencil = [(-1.0_f64, 1.0_f64), (0.0, -2.0), (1.0, 1.0)];
        let mut acc = 0.0;
        for (dx, cx) in stencil {
            for (dy, cy) in stencil {
                for (dz, cz) in stencil {
                    acc += cx * cy * cz * hoer_love_f(x + dx * h, y + dy * h, z + dz * h);
                }
            }
        }
        acc / h.powi(6)
    }

    #[test]
    fn f_is_the_sixfold_antiderivative_of_the_static_kernel() {
        // Points chosen to exercise the general case, a near-axis case, and
        // an asymmetric case. h = 0.05 balances truncation error (O(h^2))
        // against the round-off amplification of dividing by h^6.
        for (x, y, z) in [
            (1.0, 1.0, 1.0),
            (2.0, 0.7, 1.3),
            (3.0, 3.0, 0.4),
            (0.6, 2.5, 1.9),
        ] {
            let numeric = sixth_mixed_difference(x, y, z, 0.05);
            let exact = 1.0 / (x * x + y * y + z * z).sqrt();
            assert_relative_eq!(numeric, exact, max_relative = 1e-4);
        }
    }

    // --- oracle 2: numerical quadrature of the same integral ---------------

    /// Independent evaluation of the sixfold integral by 24-point
    /// Gauss-Legendre quadrature over the four transverse dimensions (the
    /// two axial integrations are done exactly by `axial_filament_um`).
    /// Valid only for bars whose cross-sections do not overlap, where the
    /// transverse integrand is bounded.
    fn quadrature_geom_um(a: &Filament, b: &Filament, order: usize) -> f64 {
        let (nodes, weights) = gauss_legendre(order);
        let axis = a.axis;
        let (p, q) = transverse_axes(axis);
        let mut acc = 0.0;
        for (ia, &na) in nodes.iter().enumerate() {
            for (ja, &ma) in nodes.iter().enumerate() {
                let (ap, aq) = (midpoint(a, p, na), midpoint(a, q, ma));
                for (ib, &nb) in nodes.iter().enumerate() {
                    for (jb, &mb) in nodes.iter().enumerate() {
                        let (bp, bq) = (midpoint(b, p, nb), midpoint(b, q, mb));
                        let (du, dv) = (ap - bp, aq - bq);
                        let rho = (du * du + dv * dv).sqrt();
                        acc += weights[ia]
                            * weights[ja]
                            * weights[ib]
                            * weights[jb]
                            * axial_filament_um(
                                a.lo[axis], a.hi[axis], b.lo[axis], b.hi[axis], rho,
                            );
                    }
                }
            }
        }
        acc
    }

    /// Gauss-Legendre nodes/weights on `[-1, 1]`, weights normalised to sum
    /// to 1. Nodes are found by Newton iteration on the Legendre polynomial
    /// (standard Numerical-Recipes `gauleg`), so the test oracle does not
    /// depend on a hard-coded table.
    fn gauss_legendre(n: usize) -> (Vec<f64>, Vec<f64>) {
        let mut nodes = vec![0.0; n];
        let mut weights = vec![0.0; n];
        for i in 0..n {
            let mut x = (std::f64::consts::PI * (i as f64 + 0.75) / (n as f64 + 0.5)).cos();
            for _ in 0..100 {
                let (mut p0, mut p1) = (1.0_f64, 0.0_f64);
                for j in 0..n {
                    let p2 = p1;
                    p1 = p0;
                    p0 = ((2.0 * j as f64 + 1.0) * x * p1 - j as f64 * p2) / (j as f64 + 1.0);
                }
                let dp = n as f64 * (x * p0 - p1) / (x * x - 1.0);
                let dx = p0 / dp;
                x -= dx;
                if dx.abs() < 1e-15 {
                    break;
                }
            }
            let (mut p0, mut p1) = (1.0_f64, 0.0_f64);
            for j in 0..n {
                let p2 = p1;
                p1 = p0;
                p0 = ((2.0 * j as f64 + 1.0) * x * p1 - j as f64 * p2) / (j as f64 + 1.0);
            }
            let dp = n as f64 * (x * p0 - p1) / (x * x - 1.0);
            nodes[i] = -x;
            // 2/((1-x^2) dp^2) is the standard weight; /2 normalises to an
            // average over the interval.
            weights[i] = 1.0 / ((1.0 - x * x) * dp * dp);
        }
        (nodes, weights)
    }

    #[test]
    fn closed_form_matches_numerical_quadrature_for_nearby_bars() {
        // Two 1 x 1 x 10 um bars, 3 um apart transversely and offset
        // axially -- close enough that the closed form is the branch taken,
        // far enough that the quadrature oracle's integrand is smooth.
        let a = z_bar(10.0, 1.0, 1.0, [0.0, 0.0, 0.0]);
        let b = z_bar(8.0, 1.0, 1.0, [3.0, 0.5, 1.5]);
        let (sum, largest) = hoer_love_sum(&a, &b);
        assert!(
            sum.abs() > CANCELLATION_FLOOR * largest,
            "expected the closed-form branch for this separation"
        );
        let closed = sum / (a.cross_area_um2() * b.cross_area_um2());
        let quadrature = quadrature_geom_um(&a, &b, 24);
        assert_relative_eq!(closed, quadrature, max_relative = 1e-6);
    }

    #[test]
    fn closed_form_matches_quadrature_for_side_by_side_touching_bars() {
        // Abutting bars (zero gap) are the hardest case for any far-field
        // approximation and the one PEEC most needs to get right; the
        // closed form handles it, and quadrature still converges here
        // because the cross-sections only touch along an edge.
        let a = z_bar(20.0, 1.0, 0.5, [0.0, 0.0, 0.0]);
        let b = z_bar(20.0, 1.0, 0.5, [1.0, 0.0, 0.0]);
        let closed = hoer_love_sum(&a, &b).0 / (a.cross_area_um2() * b.cross_area_um2());
        let quadrature = quadrature_geom_um(&a, &b, 40);
        assert_relative_eq!(closed, quadrature, max_relative = 5e-4);
    }

    // --- oracle 3: Grover's exact thin-filament mutual inductance ----------

    /// Mutual inductance (nH) of two parallel *thin* filaments of equal
    /// length `l` and transverse separation `d`, both in um:
    /// `M = (mu0 l / 2 pi) [asinh(l/d) - sqrt(1 + d^2/l^2) + d/l]`
    /// (F. W. Grover, "Inductance Calculations", 1946).
    fn grover_filament_nh(l: f64, d: f64) -> f64 {
        let geom = 2.0 * (l * (l / d).asinh() - (l * l + d * d).sqrt() + d);
        GEOM_UM_TO_NH * geom
    }

    #[test]
    fn far_field_mutual_matches_grovers_thin_filament_formula() {
        // Cross-sections small relative to the separation: the finite-bar
        // answer must collapse onto the thin-filament closed form.
        let l = 100.0;
        let d = 50.0;
        let a = z_bar(l, 0.05, 0.05, [0.0, 0.0, 0.0]);
        let b = z_bar(l, 0.05, 0.05, [d, 0.0, 0.0]);
        let computed = partial_inductance_nh(&a, &b);
        assert_relative_eq!(computed, grover_filament_nh(l, d), max_relative = 1e-6);
    }

    #[test]
    fn far_field_branch_is_the_one_taken_for_distant_bars() {
        let a = z_bar(100.0, 1.0, 1.0, [0.0, 0.0, 0.0]);
        let b = z_bar(100.0, 1.0, 1.0, [500.0, 0.0, 0.0]);
        let (sum, largest) = hoer_love_sum(&a, &b);
        assert!(
            sum.abs() < CANCELLATION_FLOOR * largest,
            "expected catastrophic cancellation at 500:1 separation"
        );
        // ... and the answer is still right, to the thin-filament oracle.
        assert_relative_eq!(
            partial_inductance_nh(&a, &b),
            grover_filament_nh(100.0, 500.0),
            max_relative = 1e-6
        );
    }

    #[test]
    fn far_field_and_closed_form_agree_at_the_crossover() {
        // Walk the separation out until the cancellation guard trips, then
        // compare the two branches at that exact geometry. This is what
        // makes the switch safe: it is not a discontinuity.
        let a = z_bar(20.0, 1.0, 1.0, [0.0, 0.0, 0.0]);
        let mut crossover = None;
        for step in 1..400 {
            let d = step as f64;
            let b = z_bar(20.0, 1.0, 1.0, [d, 0.0, 0.0]);
            let (sum, largest) = hoer_love_sum(&a, &b);
            if sum.abs() < CANCELLATION_FLOOR * largest {
                crossover = Some((d, sum / (a.cross_area_um2() * b.cross_area_um2())));
                break;
            }
        }
        let (d, _) = crossover.expect("the guard must trip within 400 um");
        // One step *before* the crossover the closed form is still trusted;
        // the far-field branch must already agree with it there.
        let b = z_bar(20.0, 1.0, 1.0, [d - 1.0, 0.0, 0.0]);
        let closed = hoer_love_sum(&a, &b).0 / (a.cross_area_um2() * b.cross_area_um2());
        let far = far_field_geom_um(&a, &b);
        assert_relative_eq!(closed, far, max_relative = 1e-6);
    }

    // --- oracle 4: the GMD asymptote for a straight bar --------------------

    /// `ln` of the self *geometric mean distance* of a `w x t` rectangle, in
    /// um -- i.e. `(1/A^2) int int ln|r1 - r2| dA1 dA2`, straight from the
    /// definition, with no formula transcribed from anywhere.
    ///
    /// Because the integrand depends only on the coordinate difference, the
    /// fourfold integral collapses onto the rectangle's own autocorrelation
    /// `(w - |u|)(t - |v|)`, and symmetry folds it into the first quadrant:
    ///
    /// ```text
    /// ln GMD = 4/(w^2 t^2) int_0^w int_0^t (w-u)(t-v) ln sqrt(u^2+v^2) du dv
    /// ```
    ///
    /// The `ln` singularity at the origin is removed by integrating in polar
    /// coordinates (the Jacobian's `r` beats it) with a quadratic grading in
    /// `r` towards the corner, so plain Gauss-Legendre converges cleanly.
    fn ln_self_gmd_um(w: f64, t: f64) -> f64 {
        let (nodes, weights) = gauss_legendre(96);
        let corner = (t / w).atan();
        let mut total = 0.0;
        for (theta_lo, theta_hi, bounded_by_w) in [
            (0.0, corner, true),
            (corner, std::f64::consts::FRAC_PI_2, false),
        ] {
            for (i, &node) in nodes.iter().enumerate() {
                let theta = 0.5 * (theta_lo + theta_hi) + 0.5 * (theta_hi - theta_lo) * node;
                let (cos, sin) = (theta.cos(), theta.sin());
                let r_max = if bounded_by_w { w / cos } else { t / sin };
                let mut radial = 0.0;
                for (j, &m) in nodes.iter().enumerate() {
                    // x in [0, 1], graded as r = r_max x^2 so the r ln r
                    // integrand's corner sits on a cluster of nodes.
                    let x = 0.5 * (1.0 + m);
                    let r = r_max * x * x;
                    if r <= 0.0 {
                        continue;
                    }
                    let dr_dx = 2.0 * r_max * x;
                    radial += weights[j] * (w - r * cos) * (t - r * sin) * r.ln() * r * dr_dx;
                }
                total += (theta_hi - theta_lo) * weights[i] * radial;
            }
        }
        4.0 * total / (w * w * t * t)
    }

    /// Self *arithmetic* mean distance of a `w x t` rectangle, in um --
    /// `(1/A^2) int int |r1 - r2| dA1 dA2`, again straight from the
    /// definition and folded onto the same autocorrelation kernel as
    /// `ln_self_gmd_um`. No singularity here, so plain Gauss-Legendre on the
    /// rectangle suffices.
    fn self_mean_distance_um(w: f64, t: f64) -> f64 {
        let (nodes, weights) = gauss_legendre(96);
        let mut total = 0.0;
        for (i, &nu) in nodes.iter().enumerate() {
            let u = 0.5 * w * (1.0 + nu);
            for (j, &nv) in nodes.iter().enumerate() {
                let v = 0.5 * t * (1.0 + nv);
                total += weights[i] * weights[j] * (w - u) * (t - v) * (u * u + v * v).sqrt();
            }
        }
        // Weights average over each interval, so multiply back by w * t.
        4.0 * total * w * t / (w * w * t * t)
    }

    /// The classical mean-distance asymptote for the partial self inductance
    /// of a straight bar of length `l` (um):
    ///
    /// ```text
    /// Lp = (mu0 l / 2 pi) [ ln(2 l / GMD) - 1 + AMD/l + O((a/l)^2) ]
    /// ```
    ///
    /// with `GMD` the cross-section's self geometric mean distance and `AMD`
    /// its self arithmetic mean distance -- both computed above from their
    /// definitions, so the only thing transcribed is the shape of the
    /// expansion itself.
    ///
    /// Ruehli's (1972) `ln(2l/(w+t)) + 0.5 + 0.2235 (w+t)/l` is this formula
    /// with two roundings: `0.2235 (w+t)` for `GMD` (`ln((w+t)/GMD) = 1.5`
    /// is what turns `-1` into `+0.5`), and the *same* `0.2235 (w+t)` reused
    /// in the correction slot where the AMD belongs. For a square
    /// cross-section `AMD/GMD = 0.5214/0.4470 = 1.166`, so that second
    /// rounding leaves a first-order `0.074 a / l` residue -- which is
    /// exactly the floor measured against Ruehli's form and the reason this
    /// oracle uses the AMD instead.
    fn asymptotic_self_nh(l: f64, w: f64, t: f64) -> f64 {
        let gmd = ln_self_gmd_um(w, t).exp();
        let amd = self_mean_distance_um(w, t);
        GEOM_UM_TO_NH * 2.0 * l * ((2.0 * l / gmd).ln() - 1.0 + amd / l)
    }

    #[test]
    fn self_mean_distance_of_a_square_matches_its_published_value() {
        // The mean distance between two uniformly random points in a unit
        // square is the classical 0.521405433... constant; reproducing it is
        // what licenses using this quadrature in the oracle above.
        for a in [0.25_f64, 1.0, 7.5] {
            let ratio = self_mean_distance_um(a, a) / a;
            println!("square side {a}: AMD/a = {ratio:.9}");
            assert_relative_eq!(ratio, 0.521_405_433, max_relative = 1e-8);
        }
    }

    #[test]
    fn self_gmd_of_a_square_matches_its_published_value() {
        // Rosa's classical result for a square of side a is
        // GMD = 0.44705 a; the quadrature above must reproduce it, which is
        // what licenses using it as an oracle below. (It also pins down the
        // 0.2235 in Ruehli's formula: 0.2235 (w + t) = 0.4470 a.)
        for a in [0.25_f64, 1.0, 7.5] {
            let ratio = ln_self_gmd_um(a, a).exp() / a;
            println!("square side {a}: GMD/a = {ratio:.8}");
            assert_relative_eq!(ratio, 0.44705, max_relative = 1e-4);
        }
    }

    #[test]
    fn self_inductance_matches_the_mean_distance_asymptote() {
        // l/(w+t) = 400 -- deep in the asymptote's regime. Measured
        // 8.6e-7 relative; the gate is 5e-6, ~6x headroom.
        let (l, w, t) = (200.0, 0.25, 0.25);
        let a = z_bar(l, w, t, [0.0, 0.0, 0.0]);
        let computed = partial_inductance_nh(&a, &a);
        let oracle = asymptotic_self_nh(l, w, t);
        let error = (computed - oracle).abs() / oracle;
        println!(
            "self-inductance l/(w+t)=400: closed form {computed:.9} nH, \
             mean-distance asymptote {oracle:.9} nH, rel.err {error:.3e}"
        );
        assert!(error < 5e-6, "relative error {error:.3e}");
    }

    #[test]
    fn self_inductance_approaches_the_asymptote_as_the_bar_slims() {
        // The oracle's remainder is O((a/l)^2), so its disagreement with the
        // exact closed form must shrink -- and shrink at *second* order -- as
        // the bar gets more slender. The rate is what rules out "both are
        // wrong in the same way": a constant offset in the bracket (which is
        // exactly what Ruehli's rounded `0.2235 (w+t)/l` correction leaves
        // behind, ~2e-4 relative and NOT shrinking) would show up as order 0.
        let (w, t) = (0.5_f64, 0.5_f64);
        let mut errors = Vec::new();
        for l in [10.0_f64, 40.0, 160.0, 640.0] {
            let a = z_bar(l, w, t, [0.0, 0.0, 0.0]);
            let computed = partial_inductance_nh(&a, &a);
            let oracle = asymptotic_self_nh(l, w, t);
            let (sum, largest) = hoer_love_sum(&a, &a);
            let error = (computed - oracle).abs() / oracle;
            println!(
                "l = {l:6.1} um (l/(w+t) = {:6.1}): rel.err vs asymptote \
                 {error:.3e}, closed-form cancellation {:.3e}",
                l / (w + t),
                sum.abs() / largest
            );
            errors.push(error);
        }
        // The l = 640 point is deliberately included but NOT gated: at
        // l/(w+t) = 1280 the Hoer & Love sum cancels to ~3e-10 of its largest
        // term, i.e. ~9.5 of 16 digits are gone, and the measured 1.2e-6 is
        // that round-off floor rather than the asymptote's remainder. It is
        // reported so the floor is on the record (docs/design/mom-validation.md).
        let gated = &errors[..3];
        for pair in gated.windows(2) {
            assert!(pair[1] < pair[0], "error must shrink: {gated:?}");
        }
        // Refinement ratio 4 in l, remainder O((a/l)^2) => ~16x per step.
        let order = (gated[0] / gated[1]).ln() / 4.0_f64.ln();
        println!("observed order in (a/l): p = {order:.2}");
        assert!(order > 1.8, "observed order {order:.2}");
    }

    // --- assembly ----------------------------------------------------------

    #[test]
    fn subdividing_a_bar_reproduces_its_partial_self_inductance() {
        // The partial-element integrals are closed-form, so refining the
        // filament mesh is a self-consistency axis: a bar cut into n^3
        // sub-filaments and reassembled with the documented current weights
        // must give back exactly the whole bar's value.
        let whole = z_bar(50.0, 1.0, 0.5, [0.0, 0.0, 0.0]);
        let reference = partial_inductance_nh(&whole, &whole);
        for n in [2_usize, 3, 4] {
            let mut pieces = Vec::new();
            let w = 1.0 / (n * n) as f64;
            for i in 0..n {
                for j in 0..n {
                    for k in 0..n {
                        let (dx, dy, dz) = (1.0 / n as f64, 0.5 / n as f64, 50.0 / n as f64);
                        pieces.push(Filament {
                            lo: [i as f64 * dx, j as f64 * dy, k as f64 * dz],
                            hi: [
                                (i + 1) as f64 * dx,
                                (j + 1) as f64 * dy,
                                (k + 1) as f64 * dz,
                            ],
                            axis: 2,
                            weight: w,
                            conductor_index: 0,
                        });
                    }
                }
            }
            let assembled = partial_inductance_matrix_nh(&pieces, 1).unwrap()[0][0];
            let drift = (assembled - reference).abs() / reference;
            println!("filament_subdivisions = {n}: {assembled:.9} nH (drift {drift:.3e})");
            assert!(drift < 1e-9, "drift {drift:.3e} at n = {n}");
        }
    }

    #[test]
    fn thin_filament_approximation_converges_to_the_closed_form() {
        // The genuine mesh-refinement statement: approximate every
        // sub-filament by its centre line (the classical filament
        // approximation, which ignores cross-sectional extent) and refine.
        // The error must fall monotonically towards the exact closed form,
        // at roughly second order.
        let whole = z_bar(50.0, 2.0, 2.0, [0.0, 0.0, 0.0]);
        let exact = partial_inductance_nh(&whole, &whole);
        let mut errors = Vec::new();
        for n in [2_usize, 4, 8] {
            let step = 2.0 / n as f64;
            let mut total = 0.0;
            for i in 0..n {
                for j in 0..n {
                    for k in 0..n {
                        for i2 in 0..n {
                            for j2 in 0..n {
                                for k2 in 0..n {
                                    let du = (i as f64 - i2 as f64) * step;
                                    let dv = (j as f64 - j2 as f64) * step;
                                    let rho = (du * du + dv * dv).sqrt();
                                    let (a0, a1) = (
                                        k as f64 * 50.0 / n as f64,
                                        (k + 1) as f64 * 50.0 / n as f64,
                                    );
                                    let (b0, b1) = (
                                        k2 as f64 * 50.0 / n as f64,
                                        (k2 + 1) as f64 * 50.0 / n as f64,
                                    );
                                    // Self terms of a zero-cross-section
                                    // filament diverge, so the diagonal
                                    // sub-filament pair uses the exact bar
                                    // value -- the classical PEEC "self
                                    // terms exact, mutuals by filament"
                                    // split.
                                    if rho == 0.0 && k == k2 {
                                        let sub = Filament {
                                            lo: [i as f64 * step, j as f64 * step, a0],
                                            hi: [(i + 1) as f64 * step, (j + 1) as f64 * step, a1],
                                            axis: 2,
                                            weight: 1.0,
                                            conductor_index: 0,
                                        };
                                        total += partial_inductance_nh(&sub, &sub)
                                            / (n * n) as f64
                                            / (n * n) as f64;
                                    } else {
                                        total += GEOM_UM_TO_NH
                                            * axial_filament_um(a0, a1, b0, b1, rho)
                                            / (n * n) as f64
                                            / (n * n) as f64;
                                    }
                                }
                            }
                        }
                    }
                }
            }
            let error = (total - exact).abs() / exact;
            println!("filament-approximation n = {n}: {total:.9} nH vs exact {exact:.9} nH (rel.err {error:.3e})");
            errors.push(error);
        }
        assert!(
            errors[1] < errors[0] && errors[2] < errors[1],
            "filament approximation must converge: {errors:?}"
        );
        let order = (errors[0] / errors[1]).ln() / 2.0_f64.ln();
        println!("observed order of convergence: p = {order:.2}");
        assert!(
            order > 1.0,
            "observed order {order:.2} is below first order"
        );
    }

    #[test]
    fn perpendicular_filaments_have_zero_partial_mutual_inductance() {
        let along_z = bar([0.0, 0.0, 0.0], [1.0, 1.0, 20.0], 2);
        let along_x = bar([0.0, 0.0, 0.0], [20.0, 1.0, 1.0], 0);
        let mut a = along_z;
        a.conductor_index = 0;
        let mut b = along_x;
        b.conductor_index = 1;
        let l = partial_inductance_matrix_nh(&[a, b], 2).unwrap();
        assert_eq!(l[0][1], 0.0);
        assert_eq!(l[1][0], 0.0);
        assert!(l[0][0] > 0.0 && l[1][1] > 0.0);
    }

    #[test]
    fn partial_inductance_matrix_is_symmetric_and_positive_definite_ish() {
        let mut a = z_bar(50.0, 1.0, 1.0, [0.0, 0.0, 0.0]);
        a.conductor_index = 0;
        let mut b = z_bar(50.0, 1.0, 1.0, [4.0, 0.0, 0.0]);
        b.conductor_index = 1;
        let l = partial_inductance_matrix_nh(&[a, b], 2).unwrap();
        assert_relative_eq!(l[0][1], l[1][0], max_relative = 1e-12);
        // Parallel bars carrying current the same way couple positively,
        // and the self terms dominate the mutual.
        assert!(l[0][1] > 0.0);
        assert!(l[0][0] > l[0][1]);
        assert_relative_eq!(l[0][0], l[1][1], max_relative = 1e-12);
    }

    #[test]
    fn mutual_inductance_falls_off_with_separation() {
        let a = z_bar(50.0, 1.0, 1.0, [0.0, 0.0, 0.0]);
        let near = z_bar(50.0, 1.0, 1.0, [2.0, 0.0, 0.0]);
        let far = z_bar(50.0, 1.0, 1.0, [20.0, 0.0, 0.0]);
        assert!(partial_inductance_nh(&a, &near) > partial_inductance_nh(&a, &far));
        assert!(partial_inductance_nh(&a, &far) > 0.0);
    }

    #[test]
    fn axis_orientation_does_not_change_the_answer() {
        // The volume integral is isotropic; only the cross-section
        // normalisation knows about the current axis. A bar rotated onto
        // another axis must return the same self-inductance. Gated at 1e-9
        // rather than bit-exactly: the 64 terms are summed in a different
        // order per axis and the sum cancels ~5 digits at this aspect ratio.
        // Measured agreement: 2.6e-12.
        let along_z = bar([0.0, 0.0, 0.0], [1.0, 0.5, 40.0], 2);
        let along_x = bar([0.0, 0.0, 0.0], [40.0, 1.0, 0.5], 0);
        let along_y = bar([0.0, 0.0, 0.0], [0.5, 40.0, 1.0], 1);
        let lz = partial_inductance_nh(&along_z, &along_z);
        assert_relative_eq!(
            partial_inductance_nh(&along_x, &along_x),
            lz,
            max_relative = 1e-9
        );
        assert_relative_eq!(
            partial_inductance_nh(&along_y, &along_y),
            lz,
            max_relative = 1e-9
        );
    }

    #[test]
    fn collinear_series_segments_sum_to_the_whole_bar() {
        // A bar cut in two along its length, both halves carrying the full
        // current in series: L_total = L1 + L2 + 2 M12 must equal the whole
        // bar's partial self-inductance.
        let whole = z_bar(60.0, 1.0, 1.0, [0.0, 0.0, 0.0]);
        let first = z_bar(30.0, 1.0, 1.0, [0.0, 0.0, 0.0]);
        let second = z_bar(30.0, 1.0, 1.0, [0.0, 0.0, 30.0]);
        let series = partial_inductance_nh(&first, &first)
            + partial_inductance_nh(&second, &second)
            + 2.0 * partial_inductance_nh(&first, &second);
        assert_relative_eq!(
            series,
            partial_inductance_nh(&whole, &whole),
            max_relative = 1e-9
        );
    }
}
