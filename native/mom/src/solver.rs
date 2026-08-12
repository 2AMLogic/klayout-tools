//! Potential-coefficient matrix fill + linear solve: turns a flat panel list
//! into a Maxwell (short-circuit) capacitance matrix.
//!
//! Method: constant-panel, point-collocation Method of Moments for the
//! quasi-static (Laplace) free-space Green's function, following the
//! classic direct BEM capacitance-extraction procedure (e.g. Nabors & White,
//! "FastCap: A Multipole Accelerated 3-D Capacitance Extraction Program",
//! IEEE TCAD 1991): fill the potential-coefficient matrix `P` such that
//! `V = P q` relates panel potentials `V` to panel charges `q`, then solve
//! `n_conductors` right-hand sides -- one per conductor held at 1V while
//! every other conductor is grounded -- and sum the resulting per-panel
//! charge onto each conductor to get one column of the capacitance matrix.
//!
//! This is a *simplified* point-collocation fill (off-diagonal entries use
//! the bare point-charge kernel between panel centroids, not a proper
//! panel-to-panel double integral) -- adequate for the MVP's "produce a
//! numeric result" acceptance bar (accuracy-vs-refinement is validated by
//! the sibling issue #719), and matches how many introductory BEM codes
//! implement the same method.

use nalgebra::DMatrix;

use crate::geometry::Panel;

/// Vacuum permittivity, F/m (CODATA 2018 value).
const EPS0_SI: f64 = 8.854_187_812_8e-12;

/// Self-potential-coefficient constant for a square panel of side `s`:
/// the potential at the center of a uniformly charged square lamina of side
/// `s` and unit surface charge density, divided by `4 pi eps` -- a standard
/// closed-form BEM self-term (see e.g. Nabors & White 1991, or any
/// constant-panel BEM reference): `ln(1 + sqrt(2)) / (pi * eps * s)`.
/// Non-square (rectangular) panels use the equivalent-area square
/// approximation (`s = sqrt(area)`) -- standard practice in simple
/// point-collocation BEM codes.
fn self_term_ln1p_sqrt2() -> f64 {
    (1.0 + std::f64::consts::SQRT_2).ln()
}

/// Fill the potential-coefficient matrix and solve for the Maxwell
/// capacitance matrix, in femtofarads.
///
/// Unit handling: panel coordinates/areas are in micrometers (um); this
/// function works internally in an "as-if-meters" system (plugging raw um
/// values directly into the SI Green's-function formula) and applies the
/// resulting `1e9` scale factor at the end to land directly in femtofarads
/// -- see the module-level derivation note in `docs/cli/mom.md`'s
/// "Units" section. This avoids scaling every coordinate by `1e-6` (meters)
/// and then the resulting charges by `1e15` (farads -> femtofarads)
/// separately; the two conversions collapse to one `1e9` factor because the
/// Green's function is linear in `1/length`.
pub fn solve_capacitance_matrix_ff(
    panels: &[Panel],
    conductor_count: usize,
    background_permittivity: f64,
) -> Result<Vec<Vec<f64>>, String> {
    let n = panels.len();
    let eps = EPS0_SI * background_permittivity;
    let ln1p_sqrt2 = self_term_ln1p_sqrt2();

    let mut p = DMatrix::<f64>::zeros(n, n);
    for i in 0..n {
        for j in 0..n {
            p[(i, j)] = if i == j {
                let side = panels[i].area_um2.sqrt();
                ln1p_sqrt2 / (std::f64::consts::PI * eps * side)
            } else {
                let r = distance(&panels[i].center, &panels[j].center);
                1.0 / (4.0 * std::f64::consts::PI * eps * r)
            };
        }
    }

    // One right-hand side per conductor: column k is 1.0 on every panel of
    // conductor k, 0.0 elsewhere (that conductor at 1V, every other
    // conductor grounded).
    let mut rhs = DMatrix::<f64>::zeros(n, conductor_count);
    for (i, panel) in panels.iter().enumerate() {
        rhs[(i, panel.conductor_index)] = 1.0;
    }

    let lu = p.lu();
    let charge = lu.solve(&rhs).ok_or_else(|| {
        "potential-coefficient matrix is singular (or numerically \
         indistinguishable from singular) -- check for overlapping/coincident \
         conductor surfaces or an extreme scale mismatch between conductors"
            .to_string()
    })?;

    let mut c = vec![vec![0.0_f64; conductor_count]; conductor_count];
    for (i, panel) in panels.iter().enumerate() {
        for k in 0..conductor_count {
            c[panel.conductor_index][k] += charge[(i, k)];
        }
    }
    // as-if-meters SI charge -> femtofarads: 1e-6 (um -> m applied once to
    // the Green's function's length) * 1e15 (F -> fF) = 1e9.
    for row in c.iter_mut() {
        for v in row.iter_mut() {
            *v *= 1e9;
        }
    }

    if c.iter().flatten().any(|v| !v.is_finite()) {
        return Err(
            "capacitance solve produced a non-finite value -- the system is \
             too ill-conditioned for this geometry/panel-size combination"
                .to_string(),
        );
    }

    Ok(c)
}

/// Check the returned matrix against the two properties a Maxwell
/// capacitance matrix of any physical multiconductor system must have --
/// positive diagonal, non-positive off-diagonal (bringing conductor `k` to
/// 1V can only induce charge of the *opposite* sign on a grounded neighbour)
/// -- and return one human-readable warning per violation.
///
/// A violation is never a solver bug in the linear algebra; it means the
/// point-collocation fill itself has broken down, which happens when
/// `panel_size_um` is comparable to or larger than the smallest
/// conductor-to-conductor separation: two panels on facing conductors are
/// then far closer to each other than the panels are wide, and the
/// centroid-to-centroid `1/r` kernel badly overestimates their coupling.
/// The result is still returned (this command's bar is "produces a numeric
/// result"), but the caller is told plainly not to trust it -- silently
/// handing back a sign-flipped mutual capacitance would be worse than an
/// inaccurate one. Refine `panel_size_um` and re-run.
pub fn physicality_warnings(c: &[Vec<f64>], names: &[String]) -> Vec<String> {
    let name_of = |i: usize| -> &str {
        names
            .get(i)
            .map(String::as_str)
            .unwrap_or("<unnamed conductor>")
    };
    let mut warnings = Vec::new();
    for (j, row) in c.iter().enumerate() {
        for (k, value) in row.iter().enumerate() {
            if j == k {
                if *value <= 0.0 {
                    warnings.push(format!(
                        "self-capacitance of conductor {:?} is {value:.6} fF, but a \
                         physical capacitance matrix has a positive diagonal -- the \
                         discretisation is too coarse to trust; reduce panel_size_um",
                        name_of(j)
                    ));
                }
            } else if k > j && *value > 0.0 {
                // `k > j` only: the matrix is symmetric, so reporting both
                // `(j,k)` and `(k,j)` would just say the same thing twice.
                warnings.push(format!(
                    "mutual capacitance between conductors {:?} and {:?} is \
                     {value:.6} fF, but a physical Maxwell capacitance matrix has \
                     non-positive off-diagonal entries -- the point-collocation fill \
                     has broken down, most likely because panel_size_um is coarse \
                     relative to the spacing between these conductors; reduce \
                     panel_size_um and re-run",
                    name_of(j),
                    name_of(k)
                ));
            }
        }
    }
    warnings
}

fn distance(a: &[f64; 3], b: &[f64; 3]) -> f64 {
    let dx = a[0] - b[0];
    let dy = a[1] - b[1];
    let dz = a[2] - b[2];
    (dx * dx + dy * dy + dz * dz).sqrt()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::contract::{BoxRequest, ConductorRequest};
    use crate::geometry::discretize;
    use approx::assert_relative_eq;

    fn plate(name: &str, z: f64) -> ConductorRequest {
        ConductorRequest {
            name: name.to_string(),
            boxes: vec![BoxRequest {
                x0_um: 0.0,
                y0_um: 0.0,
                x1_um: 10.0,
                y1_um: 10.0,
                z0_um: z,
                z1_um: z,
            }],
            conductivity_s_per_m: None,
        }
    }

    #[test]
    fn two_plate_capacitance_is_positive_and_symmetric() {
        let conductors = vec![plate("top", 5.0), plate("bottom", 0.0)];
        let panels = discretize(&conductors, 1.0).unwrap();
        let c = solve_capacitance_matrix_ff(&panels, conductors.len(), 1.0).unwrap();

        assert_eq!(c.len(), 2);
        assert!(c[0][0] > 0.0);
        assert!(c[1][1] > 0.0);
        // Maxwell capacitance matrix is symmetric for a reciprocal (linear,
        // passive) system.
        assert_relative_eq!(c[0][1], c[1][0], max_relative = 1e-9);
        // Mutual term is negative (bringing one plate to 1V induces
        // opposite-sign charge on the other, grounded plate).
        assert!(c[0][1] < 0.0);
    }

    #[test]
    fn closer_plates_have_larger_magnitude_capacitance() {
        let near = vec![plate("top", 1.0), plate("bottom", 0.0)];
        let far = vec![plate("top", 5.0), plate("bottom", 0.0)];

        let near_panels = discretize(&near, 1.0).unwrap();
        let far_panels = discretize(&far, 1.0).unwrap();
        let c_near = solve_capacitance_matrix_ff(&near_panels, 2, 1.0).unwrap();
        let c_far = solve_capacitance_matrix_ff(&far_panels, 2, 1.0).unwrap();

        assert!(c_near[0][0] > c_far[0][0]);
    }

    #[test]
    fn single_conductor_degenerate_case_solves() {
        let conductors = vec![plate("only", 0.0)];
        let panels = discretize(&conductors, 1.0).unwrap();
        let c = solve_capacitance_matrix_ff(&panels, 1, 1.0).unwrap();
        assert_eq!(c.len(), 1);
        assert!(c[0][0] > 0.0);
    }

    #[test]
    fn well_resolved_solve_emits_no_physicality_warnings() {
        let conductors = vec![plate("top", 1.0), plate("bottom", 0.0)];
        let names: Vec<String> = conductors.iter().map(|c| c.name.clone()).collect();
        let panels = discretize(&conductors, 0.5).unwrap();
        let c = solve_capacitance_matrix_ff(&panels, 2, 1.0).unwrap();
        assert!(physicality_warnings(&c, &names).is_empty());
    }

    #[test]
    fn under_resolved_solve_warns_instead_of_silently_returning_a_bad_sign() {
        // Plates 0.05 um apart discretised with 2 um panels: each panel is
        // 40x wider than the gap it faces, so the centroid-to-centroid 1/r
        // kernel breaks down and the mutual term comes back with the wrong
        // (positive) sign. The solve still returns numbers, but must say so.
        let conductors = vec![plate("top", 0.05), plate("bottom", 0.0)];
        let names: Vec<String> = conductors.iter().map(|c| c.name.clone()).collect();
        let panels = discretize(&conductors, 2.0).unwrap();
        let c = solve_capacitance_matrix_ff(&panels, 2, 1.0).unwrap();
        assert!(c[0][1] > 0.0, "expected the unphysical sign flip: {c:?}");

        let warnings = physicality_warnings(&c, &names);
        assert!(!warnings.is_empty());
        assert!(
            warnings.iter().all(|w| w.contains("panel_size_um")),
            "every warning should name the knob to change: {warnings:?}"
        );
        let mutual: Vec<&String> = warnings
            .iter()
            .filter(|w| w.contains("mutual capacitance"))
            .collect();
        // Symmetric matrix -> the (top, bottom) violation is reported once,
        // not once per triangle.
        assert_eq!(mutual.len(), 1, "{warnings:?}");
        assert!(
            mutual[0].contains("top") && mutual[0].contains("bottom"),
            "warning should name both conductors: {mutual:?}"
        );
    }

    #[test]
    fn higher_permittivity_increases_capacitance() {
        let conductors = vec![plate("top", 1.0), plate("bottom", 0.0)];
        let panels = discretize(&conductors, 1.0).unwrap();
        let c_vac = solve_capacitance_matrix_ff(&panels, 2, 1.0).unwrap();
        let c_ox = solve_capacitance_matrix_ff(&panels, 2, 3.9).unwrap();
        assert!(c_ox[0][0] > c_vac[0][0]);
    }
}
