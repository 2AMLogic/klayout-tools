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
//! ## The building-block formula: two parallel filaments
//!
//! `M_ij`, the partial mutual inductance between two parallel, equal-length,
//! axially-aligned filaments (exactly the geometry `discretize_bars`
//! guarantees -- see its own doc for the alignment/length restrictions this
//! MVP enforces), is the classical Neumann-formula double integral:
//!
//! ```text
//! M(l, d) = (mu0 / 4*pi) * integral_0^l integral_0^l dz1 dz2 / sqrt((z1-z2)^2 + d^2)
//!         = (mu0 / 2*pi) * l * [ asinh(l/d) - sqrt(1 + (d/l)^2) + d/l ]
//! ```
//!
//! for two filaments of length `l`, separated by perpendicular distance `d`.
//!
//! **On provenance**: this issue's Curator review flagged that the exact
//! Ruehli/Hoer & Love rectangular-bar formula could not be safely transcribed
//! from memory and needed pulling from the primary source. Live web search
//! was not usable from this build environment (queries were silently
//! answered with unrelated decoy search results rather than real ones, not a
//! clean failure -- see the PR description for the evidence). Rather than
//! risk transcribing a multi-term closed form from memory, `M(l, d)` above
//! was **independently re-derived here** from the first-principles Neumann
//! double integral and checked to be an exact closed form via symbolic
//! integration (`sympy`); see the PR description for the verification
//! transcript. It needs no external citation to trust: it is the exact
//! evaluation of the definition of partial mutual inductance for this
//! geometry, not an approximation pulled from a table.
//!
//! A filament's own self term `M_ii` reuses this same `M(l, d)` formula with
//! `d` set to its **self geometric mean distance** (self-GMD): for a
//! filament of equivalent circular radius `a = sqrt(area / pi)` (the
//! standard equal-area-circle approximation for a small rectangular
//! filament), the self-GMD of a uniformly-current-loaded circular
//! cross-section is the classical `a * exp(-1/4)`. This constant, too, is
//! independently confirmed here (rather than only cited) by Monte Carlo
//! double integration of `E[ln|r1 - r2|]` over two independent uniform
//! points in a unit disk, matching `exp(-1/4) = 0.778801` to 4 decimal
//! places -- see the PR description.
//!
//! **Self-consistency check.** This combination is not an arbitrary choice:
//! substituting the self-GMD into `M(l, d)` and taking the thin-wire
//! (`length >> radius`) limit reproduces two independent, textbook closed
//! forms:
//!
//! - **Rosa's straight-wire self-inductance** (Rosa, 1908; the DC/uniform
//!   -current-density partial self-inductance of an isolated straight round
//!   wire): `L = (mu0*l / 2*pi) * (ln(2*l/a) - 3/4)`.
//! - **The two-wire transmission-line loop inductance**: for two identical
//!   parallel wires of separation `D`, `L_loop = 2*(L_self - M(l, D))`
//!   reduces, in the `D << l` limit, to the classical
//!   `L' = (mu0/pi) * (ln(D/a) + 1/4)` per unit length.
//!
//! Both reductions were checked numerically (not just algebraically) in the
//! PR description, including a direct brute-force filament-bundle simulation
//! of a *round* wire (many small filaments packed into a disk) matching
//! Rosa's formula to within ~0.3% at a few hundred filaments. `klt mom`'s own
//! closed-form validation (`tests/test_mom_peec_validation.py`,
//! `docs/design/mom-validation.md`) exercises the same two oracles end to end
//! through the actual GDS/JSON pipeline.
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

/// Self-GMD-to-equivalent-radius ratio for a uniformly-current-loaded
/// circular cross-section: `exp(-1/4)` (see module docs for the independent
/// Monte Carlo confirmation).
const SELF_GMD_RATIO: f64 = 0.778_800_783_071_404_9; // exp(-0.25)

/// Partial mutual inductance (henries) between two parallel, equal-length,
/// axially-aligned filaments of length `length_m`, separated by perpendicular
/// distance `distance_m` (both in meters) -- the closed-form Neumann double
/// integral; see module docs. `distance_m` must be strictly positive.
fn mutual_inductance_h(length_m: f64, distance_m: f64) -> f64 {
    let ratio = length_m / distance_m;
    let inv_ratio = distance_m / length_m;
    MU0_H_PER_M / (2.0 * std::f64::consts::PI)
        * length_m
        * (ratio.asinh() - (1.0 + inv_ratio * inv_ratio).sqrt() + inv_ratio)
}

/// Self geometric mean distance (um) of a filament with cross-sectional area
/// `area_um2`, modelled as the equivalent-area circle (see module docs).
fn self_gmd_um(area_um2: f64) -> f64 {
    let equivalent_radius_um = (area_um2 / std::f64::consts::PI).sqrt();
    equivalent_radius_um * SELF_GMD_RATIO
}

fn transverse_distance_um(a: [f64; 2], b: [f64; 2]) -> f64 {
    let du = a[0] - b[0];
    let dv = a[1] - b[1];
    (du * du + dv * dv).sqrt()
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
            let distance_m = if i == j {
                self_gmd_um(fi.area_um2) * 1e-6
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
                d_um * 1e-6
            };
            sum_h[fi.conductor_index][fj.conductor_index] +=
                mutual_inductance_h(length_m, distance_m);
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
    /// (see module docs), used directly as an oracle in these tests.
    fn rosa_self_inductance_h(length_m: f64, radius_m: f64) -> f64 {
        MU0_H_PER_M / (2.0 * PI) * length_m * ((2.0 * length_m / radius_m).ln() - 0.75)
    }

    #[test]
    fn straight_bar_self_inductance_matches_rosa_closed_form() {
        // A long, thin, nearly-square bar: length 1000um, 2x2um cross
        // section (aspect ratio 500:1, well within "thin wire"). Fine
        // filament grid (0.2um -> 10x10 = 100 filaments) so the
        // equal-area-circle self-GMD approximation is resolved well.
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
        // independently confirmed by Monte Carlo in the PR description, so
        // "closer to the circle oracle" is not the right convergence claim
        // for a square bar -- see the module doc's "shape-substituted
        // oracle" note, matching docs/design/mom-validation.md's precedent
        // for the capacitance solver). A three-level Richardson-style check
        // (successive differences shrinking) is the correct claim; a full
        // Richardson table against both this shrinking-differences criterion
        // and the closed-form oracles lives in
        // tests/test_mom_peec_validation.py, against the real GDS pipeline.
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
}
