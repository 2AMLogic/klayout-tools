//! PEEC (Partial Element Equivalent Circuit) partial-inductance and DC
//! resistance solve: turns the bar/filament geometry from
//! `geometry::discretize_bars` into a partial-inductance matrix and a
//! per-conductor DC resistance vector (2AMLogic/klayout-tools issue #797,
//! Phase 1a of the Method-of-Moments epic #701).
//!
//! ## Method: bundle-of-filaments PEEC
//!
//! Ruehli's PEEC formulation ("Inductance Calculations in a Complex
//! Integrated Circuit Environment", IBM J. Res. Dev. 16(5), 1972) represents
//! a conductor's partial self/mutual inductance via a bundle of parallel,
//! equal-current filaments. For `N` filaments carrying `I/N` each, the
//! magnetic energy `W = (1/2) sum_i sum_j I_i I_j M_ij` with `I_i = I_j =
//! I/N` gives the bundle's *effective* partial inductance as the plain
//! average of every pairwise filament partial mutual inductance (`i == j`
//! included, via each filament's own self term):
//!
//! ```text
//! L = (1 / N^2) * sum_i sum_j M_ij
//! ```
//!
//! and, for the partial mutual inductance *between* two different
//! conductors' filament bundles (`N1`, `N2` filaments respectively), the
//! analogous cross-bundle average:
//!
//! ```text
//! M_12 = (1 / (N1 * N2)) * sum_{i in bundle 1} sum_{j in bundle 2} M_ij
//! ```
//!
//! This is exactly the technique `geometry::discretize_bars`'s cross-section
//! filament grid feeds `solve_inductance_matrix_nh` below, and it is also
//! the standard technique used by filament-based PEEC/partial-inductance
//! extractors (e.g. FastHenry) for conductors too wide/thick to treat as a
//! single ideal thin wire.
//!
//! ## Mutual terms: the thin-filament Neumann formula
//!
//! `M_ij` (`i != j`), the partial mutual inductance between two parallel,
//! equal-length, axially-aligned *thin* filaments (exactly the geometry
//! `discretize_bars` guarantees -- see its own doc for the alignment/length
//! restrictions this MVP enforces), is the classical Neumann-formula double
//! integral:
//!
//! ```text
//! M(l, d) = (mu0 / 4*pi) * integral_0^l integral_0^l dz1 dz2 / sqrt((z1-z2)^2 + d^2)
//!         = (mu0 / 2*pi) * l * [ asinh(l/d) - sqrt(1 + (d/l)^2) + d/l ]
//! ```
//!
//! for two filaments of length `l`, separated by perpendicular distance `d`.
//! This was independently re-derived here from the first-principles Neumann
//! double integral and checked to be an exact closed form via symbolic
//! integration (`sympy`); see #797's PR description for the verification
//! transcript. It needs no external citation to trust: it is the exact
//! evaluation of the definition of partial mutual inductance in the
//! thin-filament (zero-cross-section) limit, and `discretize_bars`'s
//! filament grid keeps every *pair* of distinct filaments well separated
//! relative to their own cross-section, where that limit is accurate.
//!
//! ## Self terms: the exact rectangular-bar closed form
//!
//! A filament's own self term `M_ii` is **not** amenable to the thin-filament
//! limit above -- a filament's distance from itself is zero, so `M(l, d)`
//! needs a `d` standing in for the filament's own cross-sectional extent.
//! The first cut at this (#797) substituted the *equal-area circle*'s self
//! geometric mean distance (self-GMD), `a * exp(-1/4)` with
//! `a = sqrt(area / pi)`, into `M(l, d)` -- but a rectangular filament's true
//! self-GMD differs systematically from the equal-area circle's (by
//! `0.44705 / 0.43939 = 1.01743` for a square, Rosa's classical constant vs.
//! the circular one), which inflates `ln(2l/GMD)` and over-predicts self
//! inductance by a measured ~0.3% (issue #836).
//!
//! `self_partial_inductance_nh` below fixes this by evaluating the filament's
//! **exact** partial self-inductance directly, via Hoer & Love's closed form
//! for the partial inductance of two parallel rectangular bars (C. Hoer and
//! C. Love, "Exact Inductance Equations for Rectangular Conductors with
//! Applications to More Complicated Geometries", J. Res. NBS 69C(2):127-137,
//! 1965), applied to the filament against itself. Because the integrand
//! depends only on coordinate *differences*, the sixfold volume integral
//! `(mu0/4pi) * (1/A^2) int_{V} int_{V} dV dV' / |r - r'|` collapses to an
//! alternating sum of a single scalar function `f` (`hoer_love_f` below)
//! evaluated at 4^3 = 64 difference-limit combinations -- no asymptotic
//! substitution, no equivalent-shape approximation.
//!
//! **The formula is not taken on trust.** `f` is defined by the property
//! `d^6 f / dx^2 dy^2 dz^2 = 1 / sqrt(x^2 + y^2 + z^2)`, and
//! `f_is_the_sixfold_antiderivative_of_the_static_kernel` below checks
//! exactly that by sixth-order finite differencing -- independently of any
//! inductance formula, so a dropped term or sign error cannot survive it.
//! The assembled self-inductance is further cross-checked against the
//! classical mean-distance asymptote for a long straight bar
//! (`self_inductance_matches_the_mean_distance_asymptote`), whose two
//! constants -- the cross-section's self geometric and arithmetic mean
//! distances -- are themselves computed from their integral definitions by
//! quadrature (`self_gmd_of_a_square_matches_its_published_value`,
//! `self_mean_distance_of_a_square_matches_its_published_value`), not
//! transcribed. See `docs/design/mom-validation.md` for the measured
//! numbers.
//!
//! ### The far-field branch, and why the self term needs it too
//!
//! The 64-term alternating sum is catastrophically ill-conditioned for a
//! *slender* bar: individual terms grow like `l^5` (`l` the bar length)
//! while the self-inductance itself only grows like `l * ln(l)`, so a
//! filament with a 1000:1 length-to-cross-section aspect ratio loses as many
//! digits to cancellation in its own self term as two bars 1000 cross-section
//! -widths apart would in their mutual term. `far_field_geom_um` below
//! (a small Gauss-Legendre quadrature over the exact thin-filament axial
//! integral) is the far-*separation* fallback for exactly that mutual-term
//! case -- but it is nonsense for a bar paired with itself: the thin-filament
//! kernel it integrates is singular exactly at zero separation. A numeric
//! -cancellation-only trigger cannot tell the two cases apart (a slender
//! self term cancels just as hard as a distant pair), so
//! `partial_inductance_nh`'s branch guard additionally requires the two bars
//! to be **geometrically disjoint** (`min_gap_um(a, b) > 0.0`) before it will
//! trust the cancellation floor. Without that guard a naive port silently
//! returned a 200x0.25x0.25 um bar's self inductance 6.3% low; with it, a
//! self term (gap always exactly zero) never takes the far-field branch, and
//! the closed form's own degrading-but-still-usable precision at extreme
//! aspect ratios is what's exercised instead (see
//! `docs/design/mom-validation.md`).
//!
//! ## Resistance needs no approximation
//!
//! `R = length / (conductivity * cross_sectional_area)` is Ohm's law for a
//! uniform bar -- exact, not asymptotic, and independent of the inductance
//! formulation above.

use crate::contract::ConductorRequest;
use crate::geometry::{BarLayout, AXIS_NAMES};

/// Vacuum permeability, H/m. Exact by definition under the pre-2019 SI
/// (`4*pi*1e-7` exactly); post-2019 it is a measured quantity that differs
/// from this by less than 1e-9 relative -- far below every tolerance this
/// module cares about.
const MU0_H_PER_M: f64 = 4.0 * std::f64::consts::PI * 1e-7;

/// Converts a Hoer & Love geometric factor (um for the near-field closed
/// form and the far-field quadrature alike -- see their doc comments) into
/// nanohenries: `mu0/(4*pi) [H/m] * 1e-6 [m/um] * 1e9 [nH/H]`. With the exact
/// pre-2019 `MU0_H_PER_M` above this evaluates to exactly `1e-4`.
const GEOM_UM_TO_NH: f64 = MU0_H_PER_M / (4.0 * std::f64::consts::PI) * 1e3;

/// Relative-cancellation floor for the Hoer & Love sum (see module docs'
/// "far-field branch" section). When the 64-term alternating sum comes back
/// smaller than this fraction of its largest single term, more than ~8
/// significant digits have been lost to cancellation and the far-field
/// quadrature branch is used instead -- but only for genuinely separated
/// bars (`partial_inductance_nh` additionally gates on a non-zero geometric
/// gap; see module docs).
const CANCELLATION_FLOOR: f64 = 1e-8;

/// Three-point Gauss-Legendre nodes on `[-1, 1]`.
const GAUSS3_NODES: [f64; 3] = [-0.774_596_669_241_483_4, 0.0, 0.774_596_669_241_483_4];

/// Three-point Gauss-Legendre weights, pre-divided by the interval width 2
/// so they sum to 1 and the rule computes an *average* over the interval.
const GAUSS3_WEIGHTS: [f64; 3] = [5.0 / 18.0, 8.0 / 18.0, 5.0 / 18.0];

/// Partial mutual inductance (henries) between two parallel, equal-length,
/// axially-aligned *thin* filaments of length `length_m`, separated by
/// perpendicular distance `distance_m` (both in meters) -- the closed-form
/// Neumann double integral; see module docs. `distance_m` must be strictly
/// positive. Not used for a filament's own self term -- see
/// `self_partial_inductance_nh`.
fn mutual_inductance_h(length_m: f64, distance_m: f64) -> f64 {
    let ratio = length_m / distance_m;
    let inv_ratio = distance_m / length_m;
    MU0_H_PER_M / (2.0 * std::f64::consts::PI)
        * length_m
        * (ratio.asinh() - (1.0 + inv_ratio * inv_ratio).sqrt() + inv_ratio)
}

fn transverse_distance_um(a: [f64; 2], b: [f64; 2]) -> f64 {
    let du = a[0] - b[0];
    let dv = a[1] - b[1];
    (du * du + dv * dv).sqrt()
}

// --- exact rectangular-bar self term (Hoer & Love) --------------------------

/// One rectangular bar in *local* coordinates for the Hoer & Love
/// closed-form calculations below: index 2 is always the axial
/// (current-flow) direction, indices 0 and 1 the transverse cross-section
/// directions. The sixfold integral this module evaluates is isotropic (see
/// module docs) -- it does not care which *real* axis that maps to, so
/// callers translate once at the call site rather than this type carrying a
/// general axis field.
#[derive(Debug, Clone, Copy)]
struct RectBar {
    lo: [f64; 3],
    hi: [f64; 3],
}

impl RectBar {
    fn cross_area_um2(&self) -> f64 {
        (self.hi[0] - self.lo[0]) * (self.hi[1] - self.lo[1])
    }
}

/// Exact partial self-inductance (nanohenries) of one PEEC filament of
/// length `length_um` and rectangular cross-section `w_um x t_um`, via the
/// Hoer & Love closed form applied to the filament against itself (see
/// module docs' "Self terms" section). Replaces the equal-area-circle
/// self-GMD substitution that over-predicted self inductance by ~0.3%
/// (issue #836).
fn self_partial_inductance_nh(length_um: f64, w_um: f64, t_um: f64) -> f64 {
    let bar = RectBar {
        lo: [0.0, 0.0, 0.0],
        hi: [w_um, t_um, length_um],
    };
    partial_inductance_nh(&bar, &bar)
}

/// Partial inductance between two parallel rectangular bars, in nanohenries.
/// Exact (via `hoer_love_sum`) for bars close enough that the 64-term sum has
/// not lost its precision to cancellation; falls back to `far_field_geom_um`
/// past that point, but only for bars with a non-zero geometric gap -- see
/// module docs' "far-field branch" section for why a self term (`a` and `b`
/// the same bar) must never take that branch.
fn partial_inductance_nh(a: &RectBar, b: &RectBar) -> f64 {
    let areas = a.cross_area_um2() * b.cross_area_um2();
    let (sum, largest_term) = hoer_love_sum(a, b);
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
/// `0.0` when they touch, overlap, or are the same bar (a self term).
fn min_gap_um(a: &RectBar, b: &RectBar) -> f64 {
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
fn hoer_love_sum(a: &RectBar, b: &RectBar) -> (f64, f64) {
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
fn difference_limits(a: &RectBar, b: &RectBar, axis: usize) -> [(f64, f64); 4] {
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
/// is plenty; it is numerically stable where the closed form is not. Never
/// called for a zero-gap (including self) pair -- see `partial_inductance_nh`
/// and module docs.
fn far_field_geom_um(a: &RectBar, b: &RectBar) -> f64 {
    let mut acc = 0.0;
    for (ia, &na) in GAUSS3_NODES.iter().enumerate() {
        let ap = midpoint(a, 0, na);
        for (ja, &ma) in GAUSS3_NODES.iter().enumerate() {
            let aq = midpoint(a, 1, ma);
            let wa = GAUSS3_WEIGHTS[ia] * GAUSS3_WEIGHTS[ja];
            for (ib, &nb) in GAUSS3_NODES.iter().enumerate() {
                let bp = midpoint(b, 0, nb);
                for (jb, &mb) in GAUSS3_NODES.iter().enumerate() {
                    let bq = midpoint(b, 1, mb);
                    let (du, dv) = (ap - bp, aq - bq);
                    let rho = (du * du + dv * dv).sqrt();
                    acc += wa
                        * GAUSS3_WEIGHTS[ib]
                        * GAUSS3_WEIGHTS[jb]
                        * axial_filament_um(a.lo[2], a.hi[2], b.lo[2], b.hi[2], rho);
                }
            }
        }
    }
    acc
}

/// Map a Gauss-Legendre node on `[-1, 1]` onto `bar`'s extent along `axis`.
fn midpoint(bar: &RectBar, axis: usize, node: f64) -> f64 {
    let (lo, hi) = (bar.lo[axis], bar.hi[axis]);
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
/// `2 [l asinh(l/rho) - sqrt(l^2 + rho^2) + rho]`.
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

/// Fill the partial-inductance matrix, in nanohenries, from `layout`'s
/// filaments (see module docs for the bundle-of-filaments method).
/// `conductor_count` is the number of electrical conductors (not filaments);
/// `layout.filaments[..].conductor_index` indexes into it.
pub fn solve_inductance_matrix_nh(
    layout: &BarLayout,
    conductor_count: usize,
) -> Result<Vec<Vec<f64>>, String> {
    let length_m = layout.length_um * 1e-6;
    let mut sum_h = vec![vec![0.0_f64; conductor_count]; conductor_count];
    let mut pair_count = vec![vec![0.0_f64; conductor_count]; conductor_count];

    for (i, fi) in layout.filaments.iter().enumerate() {
        for (j, fj) in layout.filaments.iter().enumerate() {
            let term_h = if i == j {
                self_partial_inductance_nh(layout.length_um, fi.extent_um[0], fi.extent_um[1])
                    * 1e-9
            } else {
                let d_um = transverse_distance_um(fi.transverse_um, fj.transverse_um);
                if d_um < 1e-9 {
                    return Err(format!(
                        "PEEC filaments coincide (zero separation transverse to the shared \
                         {} current-flow axis) -- overlapping conductor geometry is not \
                         physical",
                        AXIS_NAMES[layout.axis]
                    ));
                }
                mutual_inductance_h(length_m, d_um * 1e-6)
            };
            sum_h[fi.conductor_index][fj.conductor_index] += term_h;
            pair_count[fi.conductor_index][fj.conductor_index] += 1.0;
        }
    }

    let mut inductance_nh = vec![vec![0.0_f64; conductor_count]; conductor_count];
    for (row_sum, (row_count, row_out)) in sum_h
        .iter()
        .zip(pair_count.iter().zip(inductance_nh.iter_mut()))
    {
        for ((&s, &n), out) in row_sum.iter().zip(row_count.iter()).zip(row_out.iter_mut()) {
            if n > 0.0 {
                *out = s / n * 1e9;
            }
        }
    }

    if inductance_nh.iter().flatten().any(|v| !v.is_finite()) {
        return Err(
            "PEEC inductance solve produced a non-finite value -- check for degenerate \
             (near-zero-separation or near-zero-length) geometry"
                .to_string(),
        );
    }

    Ok(inductance_nh)
}

/// Per-conductor DC resistance, in ohms: `R = length / (conductivity *
/// cross_sectional_area)` (Ohm's law -- exact, not asymptotic). The
/// cross-sectional area is summed directly from `layout`'s filament areas
/// (rather than re-derived from the original box), so resistance is always
/// consistent with exactly the geometry the inductance solve used.
pub fn resistance_ohm(
    layout: &BarLayout,
    conductors: &[ConductorRequest],
    conductor_count: usize,
) -> Result<Vec<f64>, String> {
    let mut area_um2 = vec![0.0_f64; conductor_count];
    for f in &layout.filaments {
        area_um2[f.conductor_index] += f.area_um2;
    }

    let length_m = layout.length_um * 1e-6;
    let mut resistance = vec![0.0_f64; conductor_count];
    for (index, conductor) in conductors.iter().enumerate() {
        let sigma = conductor.conductivity_s_per_m.ok_or_else(|| {
            format!(
                "conductor {:?}: compute_inductance requires every conductor to set \
                 conductivity_s_per_m (used for DC resistance) -- see docs/cli/mom.md's \
                 PEEC inductance/resistance section",
                conductor.name
            )
        })?;
        if !sigma.is_finite() || sigma <= 0.0 {
            return Err(format!(
                "conductor {:?}: conductivity_s_per_m must be positive, got {sigma}",
                conductor.name
            ));
        }
        let area_m2 = area_um2[index] * 1e-12;
        resistance[index] = length_m / (sigma * area_m2);
    }
    Ok(resistance)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::contract::BoxRequest;
    use crate::geometry::discretize_bars;
    use approx::assert_relative_eq;
    use std::f64::consts::PI;

    fn bar_conductor(name: &str, sigma: Option<f64>, b: BoxRequest) -> ConductorRequest {
        ConductorRequest {
            name: name.to_string(),
            boxes: vec![b],
            conductivity_s_per_m: sigma,
        }
    }

    /// Rosa's classical DC straight-wire partial self-inductance closed form
    /// (see module docs), used directly as an oracle in these tests. A
    /// *shape-substituted* oracle for a rectangular bar (its radius is the
    /// equal-area circle's) -- the exact rectangular self term is now
    /// expected to sit consistently ~0.2-0.4% below it (the true square
    /// self-GMD is ~1.7% larger than the equal-area circle's), not to
    /// converge onto it exactly.
    fn rosa_self_inductance_h(length_m: f64, radius_m: f64) -> f64 {
        MU0_H_PER_M / (2.0 * PI) * length_m * ((2.0 * length_m / radius_m).ln() - 0.75)
    }

    #[test]
    fn straight_bar_self_inductance_matches_rosa_closed_form() {
        // A long, thin, nearly-square bar: length 1000um, 2x2um cross
        // section (aspect ratio 500:1, well within "thin wire"). Fine
        // filament grid (0.2um -> 10x10 = 100 filaments) so the bundle
        // average is well resolved.
        let c = bar_conductor(
            "wire",
            Some(5.96e7), // copper, S/m
            BoxRequest {
                x0_um: 0.0,
                y0_um: 0.0,
                x1_um: 1000.0,
                y1_um: 2.0,
                z0_um: 0.0,
                z1_um: 2.0,
            },
        );
        let layout = discretize_bars(&[c], 0.2).unwrap();
        let l_nh = solve_inductance_matrix_nh(&layout, 1).unwrap();

        let length_m = 1000.0e-6;
        let area_m2 = 2.0e-6 * 2.0e-6;
        let a_eq_m = (area_m2 / PI).sqrt();
        let oracle_nh = rosa_self_inductance_h(length_m, a_eq_m) * 1e9;

        let rel_err = (l_nh[0][0] - oracle_nh).abs() / oracle_nh;
        assert!(
            rel_err < 0.02,
            "self-inductance {} nH vs Rosa oracle {} nH, rel.err {:.4}%",
            l_nh[0][0],
            oracle_nh,
            rel_err * 100.0
        );
    }

    #[test]
    fn self_inductance_converges_under_filament_refinement() {
        // Refining the cross-section filament grid must make successive
        // answers move *less*, not compared against the Rosa oracle (which
        // is itself only exact for a *circular* cross-section -- a square
        // bar's true self-GMD differs from the equal-area circle's by ~1.7%,
        // so "closer to the circle oracle" is not the right convergence
        // claim for a square bar -- see the module doc's "self terms"
        // section). A three-level Richardson-style check (successive
        // differences shrinking) is the correct claim; a full Richardson
        // table against both this shrinking-differences criterion and the
        // closed-form oracles lives in tests/test_mom_peec_validation.py,
        // against the real GDS/JSON pipeline.
        let make = |filament_um: f64| {
            let c = bar_conductor(
                "wire",
                Some(5.96e7),
                BoxRequest {
                    x0_um: 0.0,
                    y0_um: 0.0,
                    x1_um: 2000.0,
                    y1_um: 4.0,
                    z0_um: 0.0,
                    z1_um: 4.0,
                },
            );
            let layout = discretize_bars(&[c], filament_um).unwrap();
            solve_inductance_matrix_nh(&layout, 1).unwrap()[0][0]
        };

        let v1 = make(4.0); // 1x1 filament
        let v2 = make(2.0); // 2x2 filaments
        let v3 = make(1.0); // 4x4 filaments
        let v4 = make(0.5); // 8x8 filaments

        let d1 = (v2 - v1).abs();
        let d2 = (v3 - v2).abs();
        let d3 = (v4 - v3).abs();
        assert!(
            d2 < d1 && d3 < d2,
            "successive refinements should move the answer strictly less each time: \
             v1={v1} v2={v2} v3={v3} v4={v4} (d1={d1} d2={d2} d3={d3})"
        );

        // Sanity: the whole sequence still sits within a few percent of the
        // equal-area-circle Rosa oracle (bounding the shape-substitution
        // error, not claiming exactness).
        let length_m = 2000.0e-6;
        let area_m2 = 4.0e-6 * 4.0e-6;
        let a_eq_m = (area_m2 / PI).sqrt();
        let oracle_nh = rosa_self_inductance_h(length_m, a_eq_m) * 1e9;
        let rel_err = (v4 - oracle_nh).abs() / oracle_nh;
        assert!(
            rel_err < 0.02,
            "finest level {v4} nH vs Rosa oracle {oracle_nh} nH, rel.err {:.4}%",
            rel_err * 100.0
        );
    }

    #[test]
    fn loop_inductance_matches_two_wire_transmission_line_formula() {
        // Two long, parallel, identical bars (a "loop", going out on one
        // and back on the other) separated by 50um, length 5000um -- deep
        // in the l >> D >> a regime the two-wire-line closed form assumes.
        let make_bar = |y0: f64, y1: f64, name: &str| {
            bar_conductor(
                name,
                Some(5.96e7),
                BoxRequest {
                    x0_um: 0.0,
                    y0_um: y0,
                    x1_um: 5000.0,
                    y1_um: y1,
                    z0_um: 0.0,
                    z1_um: 2.0,
                },
            )
        };
        let conductors = vec![make_bar(0.0, 2.0, "go"), make_bar(50.0, 52.0, "return")];
        let layout = discretize_bars(&conductors, 0.5).unwrap();
        let l_nh = solve_inductance_matrix_nh(&layout, 2).unwrap();

        assert_relative_eq!(l_nh[0][1], l_nh[1][0], max_relative = 1e-9);

        let length_m = 5000.0e-6;
        let area_m2 = 2.0e-6 * 2.0e-6;
        let a_eq_m = (area_m2 / PI).sqrt();
        let sep_m = 50.0e-6;
        let loop_nh = (l_nh[0][0] + l_nh[1][1] - 2.0 * l_nh[0][1]).max(0.0);
        let oracle_nh = length_m * (MU0_H_PER_M / PI) * ((sep_m / a_eq_m).ln() + 0.25) * 1e9;

        let rel_err = (loop_nh - oracle_nh).abs() / oracle_nh;
        assert!(
            rel_err < 0.05,
            "loop inductance {loop_nh} nH vs two-wire-line oracle {oracle_nh} nH, rel.err \
             {:.4}%",
            rel_err * 100.0
        );
    }

    fn wire_box() -> BoxRequest {
        BoxRequest {
            x0_um: 0.0,
            y0_um: 0.0,
            x1_um: 100.0,
            y1_um: 1.0,
            z0_um: 0.0,
            z1_um: 1.0,
        }
    }

    #[test]
    fn dc_resistance_matches_ohms_law_exactly() {
        let sigma = 5.96e7_f64; // copper, S/m
        let conductors = [bar_conductor("wire", Some(sigma), wire_box())];
        let layout = discretize_bars(&conductors, 0.5).unwrap();
        let r = resistance_ohm(&layout, &conductors, 1).unwrap();

        let length_m = 100.0e-6;
        let area_m2 = 1.0e-6 * 1.0e-6;
        let expected = length_m / (sigma * area_m2);
        assert_relative_eq!(r[0], expected, max_relative = 1e-9);
    }

    #[test]
    fn missing_conductivity_is_a_clear_error() {
        let conductors = [bar_conductor("wire", None, wire_box())];
        let layout = discretize_bars(&conductors, 0.5).unwrap();
        let err = resistance_ohm(&layout, &conductors, 1).unwrap_err();
        assert!(err.contains("conductivity_s_per_m"), "{err}");
    }

    #[test]
    fn inductance_matrix_is_symmetric() {
        let make_bar = |y0: f64, y1: f64, name: &str| {
            bar_conductor(
                name,
                Some(1.0),
                BoxRequest {
                    x0_um: 0.0,
                    y0_um: y0,
                    x1_um: 200.0,
                    y1_um: y1,
                    z0_um: 0.0,
                    z1_um: 1.0,
                },
            )
        };
        let conductors = vec![make_bar(0.0, 1.0, "a"), make_bar(10.0, 11.0, "b")];
        let layout = discretize_bars(&conductors, 0.5).unwrap();
        let l_nh = solve_inductance_matrix_nh(&layout, 2).unwrap();
        assert_relative_eq!(l_nh[0][1], l_nh[1][0], max_relative = 1e-9);
        assert!(l_nh[0][0] > 0.0 && l_nh[1][1] > 0.0);
        // Self-inductance exceeds mutual inductance for well-separated bars.
        assert!(l_nh[0][0] > l_nh[0][1]);
    }

    // --- Hoer & Love closed form: transcription and oracle checks ----------
    //
    // Ported from `origin/feature/issue-797` (commit `8058da6`, PR #828,
    // closed as a duplicate of #815) per issue #836's Curator guidance: this
    // is the subset of that branch's independent validation that exercises
    // `hoer_love_f`/`hoer_love_sum`/the far-field guard, adapted to this
    // module's `RectBar` (local, always-axis-2 convention) rather than the
    // branch's general `Filament`.

    fn z_bar(l: f64, w: f64, t: f64, offset: [f64; 3]) -> RectBar {
        RectBar {
            lo: offset,
            hi: [offset[0] + w, offset[1] + t, offset[2] + l],
        }
    }

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

    /// Gauss-Legendre nodes/weights on `[-1, 1]`, weights normalised to sum
    /// to 1. Nodes are found by Newton iteration on the Legendre polynomial
    /// (standard Numerical-Recipes `gauleg`), so the oracles below do not
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
        // what licenses using it as an oracle below.
        for a in [0.25_f64, 1.0, 7.5] {
            let ratio = ln_self_gmd_um(a, a).exp() / a;
            println!("square side {a}: GMD/a = {ratio:.8}");
            assert_relative_eq!(ratio, 0.44705, max_relative = 1e-4);
        }
    }

    #[test]
    fn self_inductance_matches_the_mean_distance_asymptote() {
        // l/(w+t) = 400 -- deep in the asymptote's regime.
        let (l, w, t) = (200.0, 0.25, 0.25);
        let computed = self_partial_inductance_nh(l, w, t);
        let oracle = asymptotic_self_nh(l, w, t);
        let error = (computed - oracle).abs() / oracle;
        println!(
            "self-inductance l/(w+t)=400: closed form {computed:.9} nH, \
             mean-distance asymptote {oracle:.9} nH, rel.err {error:.3e}"
        );
        assert!(error < 5e-6, "relative error {error:.3e}");
    }

    // --- far-field guard regression: a self term must never take it --------

    #[test]
    fn far_field_branch_is_the_one_taken_for_distant_bars() {
        let a = z_bar(100.0, 1.0, 1.0, [0.0, 0.0, 0.0]);
        let b = z_bar(100.0, 1.0, 1.0, [500.0, 0.0, 0.0]);
        let (sum, largest) = hoer_love_sum(&a, &b);
        assert!(
            sum.abs() < CANCELLATION_FLOOR * largest,
            "expected catastrophic cancellation at 500:1 separation"
        );
        // ... and the answer is still right, against Grover's exact
        // thin-filament formula.
        let l = 100.0_f64;
        let d = 500.0_f64;
        let grover_geom = 2.0 * (l * (l / d).asinh() - (l * l + d * d).sqrt() + d);
        let grover_nh = GEOM_UM_TO_NH * grover_geom;
        assert_relative_eq!(
            partial_inductance_nh(&a, &b),
            grover_nh,
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
                crossover = Some(d);
                break;
            }
        }
        let d = crossover.expect("the guard must trip within 400 um");
        // One step *before* the crossover the closed form is still trusted;
        // the far-field branch must already agree with it there.
        let b = z_bar(20.0, 1.0, 1.0, [d - 1.0, 0.0, 0.0]);
        let closed = hoer_love_sum(&a, &b).0 / (a.cross_area_um2() * b.cross_area_um2());
        let far = far_field_geom_um(&a, &b);
        assert_relative_eq!(closed, far, max_relative = 1e-6);
    }

    #[test]
    fn slender_self_term_does_not_reproduce_the_misfire() {
        // The specific regression from issue #836's Curator guidance: a
        // 200x0.25x0.25 um bar's self term must NOT take the far-field
        // branch (its own cancellation is as bad as a distant pair's), and
        // must NOT come back ~6.3% low. `min_gap_um` is always exactly zero
        // for a self pair, so the guard in `partial_inductance_nh` must keep
        // it on the closed-form branch regardless of how badly
        // `hoer_love_sum` cancels for this aspect ratio.
        let (l, w, t) = (200.0_f64, 0.25_f64, 0.25_f64);
        let bar = z_bar(l, w, t, [0.0, 0.0, 0.0]);
        assert_eq!(min_gap_um(&bar, &bar), 0.0);

        let computed = self_partial_inductance_nh(l, w, t);
        let oracle = asymptotic_self_nh(l, w, t);
        let error = (computed - oracle).abs() / oracle;
        assert!(
            error < 1e-4,
            "self term {computed} nH vs asymptote {oracle} nH, rel.err {error:.3e} -- a \
             far-field misfire would show up as ~6.3% low, not this"
        );
    }
}
