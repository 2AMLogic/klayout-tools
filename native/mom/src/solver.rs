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
//!
//! ## The solve step: preconditioned Conjugate Gradient, not a direct LU
//! (#799)
//!
//! `P` is symmetric (the off-diagonal kernel `1/(4 pi eps r)` is symmetric in
//! `i`/`j` by construction) and positive definite: `q^T P q` is (twice) the
//! electrostatic energy of the charge distribution `q`, which is strictly
//! positive for any nonzero `q` as long as no two panels coincide (the same
//! non-degeneracy the geometry layer already guarantees). That makes
//! Conjugate Gradient -- not the more general GMRES -- "the iterative solver
//! appropriate to the system's structure": CG is the standard Krylov method
//! for SPD systems, converges monotonically in the energy norm, and (unlike
//! GMRES) needs only one matrix-vector product and a handful of length-`n`
//! vectors per iteration, no growing Krylov basis to store.
//!
//! The direct path this replaces was an in-place LU factorisation
//! (`O(n^3)`, dominating for large panel counts) followed by an `O(n^2)`
//! triangular solve per right-hand side. Jacobi (diagonal)-preconditioned CG
//! instead does `O(n^2)` work (one dense mat-vec) per iteration, so the
//! total solve time is `O(k * n^2)` for `k` iterations to convergence --
//! cheaper than the direct path whenever `k << n`, which the measurements in
//! `docs/design/mom-iterative-solver.md` show holds well before the panel
//! count reaches the `MAX_PANELS` guard in `geometry.rs`. See that document
//! for the measured convergence rate and the solve-time comparison against
//! the direct solve on a larger-than-MVP conductor count.
//!
//! The diagonal (Jacobi) preconditioner is the simplest choice appropriate
//! to this system: because `P`'s diagonal is the panel self-term (always the
//! single largest entry in its row -- every off-diagonal coupling `1/(4 pi
//! eps r)` is a *fraction* of a panel's own self-potential once `r` exceeds
//! roughly a panel width), scaling by `1/P_ii` materially improves the
//! matrix's effective conditioning without the `O(n^2)`-or-worse setup cost
//! a fill-in-based preconditioner (e.g. incomplete Cholesky on this fully
//! dense matrix) would add.

use nalgebra::{DMatrix, DVector};

use crate::geometry::Panel;

/// Relative-residual stopping tolerance for the preconditioned CG solve:
/// `||r_k|| <= tol * ||b||` (or `||r_k|| <= tol` for a zero right-hand side).
/// `1e-12` is tight enough that the solved capacitance matrix reproduces the
/// old direct-LU path's accuracy to solver round-off -- see
/// `solver::tests::iterative_solve_matches_direct_lu_solve` -- including the
/// `docs/design/mom-validation.md` exact-linearity-in-permittivity check,
/// which requires agreement to `1e-12` relative.
const ITERATIVE_REL_TOL: f64 = 1e-12;

/// Absolute cap on CG iterations per right-hand side, independent of `n`. In
/// exact arithmetic CG converges in at most `n` iterations; this cap exists
/// only to fail fast with a clear error (see `pcg_solve`) rather than loop
/// indefinitely if a future geometry produces a matrix CG cannot resolve to
/// `ITERATIVE_REL_TOL`, since `MAX_PANELS` (`geometry.rs`) already bounds `n`
/// at 8000.
const ITERATIVE_MAX_ITER: usize = 8000;

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
    let (c, _stats) =
        solve_capacitance_matrix_ff_with_stats(panels, conductor_count, background_permittivity)?;
    Ok(c)
}

/// Same as `solve_capacitance_matrix_ff`, additionally returning the
/// per-right-hand-side [`IterativeSolveStats`] from the CG solve -- used by
/// the convergence-rate measurements in `docs/design/mom-iterative-solver.md`
/// and this module's own tests. The public JSON contract
/// (`contract::MomResponse`) deliberately does not expose these yet (no
/// caller need has been identified for per-request convergence stats over
/// the CLI/JSON boundary); this function is the seam a future `--verbose`
/// flag or diagnostic field would hang off.
pub fn solve_capacitance_matrix_ff_with_stats(
    panels: &[Panel],
    conductor_count: usize,
    background_permittivity: f64,
) -> Result<(Vec<Vec<f64>>, IterativeSolveStats), String> {
    let eps = EPS0_SI * background_permittivity;
    let p = build_potential_matrix(panels, eps);
    let rhs = build_rhs(panels, conductor_count);

    let (charge, stats) = pcg_solve(&p, &rhs, ITERATIVE_REL_TOL, ITERATIVE_MAX_ITER)?;
    let c = assemble_capacitance_matrix_ff(panels, conductor_count, &charge)?;
    Ok((c, stats))
}

/// Fill the dense, symmetric potential-coefficient matrix `P` (`V = P q`) in
/// SI units (`eps` already includes `EPS0_SI * background_permittivity`).
///
/// Assumes `panels` contains no two panels at the exact same location --
/// `geometry::discretize` deduplicates the numerically-coincident panels a
/// box-based discretisation can otherwise produce at abutting-box corners
/// (see its doc comment); genuine coincidence between conductors that
/// remains (overlapping/short-circuited surfaces) is a malformed request,
/// and `pcg_solve` reports it as a singular/ill-conditioned matrix rather
/// than dividing by the resulting `r = 0`.
fn build_potential_matrix(panels: &[Panel], eps: f64) -> DMatrix<f64> {
    let n = panels.len();
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
    p
}

/// One right-hand side per conductor: column `k` is `1.0` on every panel of
/// conductor `k`, `0.0` elsewhere (that conductor at 1V, every other
/// conductor grounded).
fn build_rhs(panels: &[Panel], conductor_count: usize) -> DMatrix<f64> {
    let mut rhs = DMatrix::<f64>::zeros(panels.len(), conductor_count);
    for (i, panel) in panels.iter().enumerate() {
        rhs[(i, panel.conductor_index)] = 1.0;
    }
    rhs
}

/// Sum the per-panel solved charge onto each conductor and convert to
/// femtofarads, checking for a non-finite result.
fn assemble_capacitance_matrix_ff(
    panels: &[Panel],
    conductor_count: usize,
    charge: &DMatrix<f64>,
) -> Result<Vec<Vec<f64>>, String> {
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

/// Per-right-hand-side convergence diagnostics from [`pcg_solve`]: how many
/// iterations each of the `conductor_count` solves took, and the relative
/// residual (`||r|| / ||b||`, or `||r||` for a zero right-hand side) each
/// landed on when it stopped.
///
/// `solver` is a private (`mod solver;`, not `pub mod`) module, so this
/// struct is not reachable from outside the crate today -- it is read by
/// this module's own tests (the convergence-rate measurements in
/// `docs/design/mom-iterative-solver.md`) and reserved as the seam a future
/// `--verbose`/diagnostic surface would read from, per
/// `solve_capacitance_matrix_ff_with_stats`'s doc comment. `#[allow(dead_code)]`
/// because a normal (non-test) build never reads these fields today.
#[derive(Debug, Clone)]
#[allow(dead_code)]
pub struct IterativeSolveStats {
    pub iterations: Vec<usize>,
    pub relative_residuals: Vec<f64>,
}

/// Solve the symmetric positive-definite system `p x = rhs` (one column of
/// `rhs` per right-hand side) with Jacobi (diagonal)-preconditioned Conjugate
/// Gradient -- see this module's doc comment for why CG-with-a-diagonal-
/// preconditioner is the solver appropriate to `p`'s structure.
///
/// Each right-hand side is solved independently (columns of `rhs` do not
/// share a Krylov subspace); `conductor_count` is small relative to `n` in
/// every geometry this command targets, so the separate solves are cheap
/// next to the `O(n^2)`-per-iteration cost each one pays.
///
/// Returns an error, mirroring the direct solve's previous "singular matrix"
/// error, if any right-hand side fails to reach `rel_tol` within `max_iter`
/// iterations -- CG making no progress (or a NaN residual) is the iterative
/// analogue of a direct solve hitting a singular pivot: an ill-conditioned
/// or degenerate geometry, not a solver bug.
fn pcg_solve(
    p: &DMatrix<f64>,
    rhs: &DMatrix<f64>,
    rel_tol: f64,
    max_iter: usize,
) -> Result<(DMatrix<f64>, IterativeSolveStats), String> {
    let n = p.nrows();
    let k = rhs.ncols();

    // Jacobi preconditioner: M^-1 = diag(1 / P_ii). Every diagonal entry is
    // strictly positive (it is the panel self-term, see
    // `self_term_ln1p_sqrt2`), so no zero-guard is needed here.
    let inv_diag: DVector<f64> = DVector::from_iterator(n, (0..n).map(|i| 1.0 / p[(i, i)]));

    let mut x = DMatrix::<f64>::zeros(n, k);
    let mut iterations = Vec::with_capacity(k);
    let mut relative_residuals = Vec::with_capacity(k);

    for col in 0..k {
        let b = rhs.column(col).clone_owned();
        let b_norm = b.norm();
        // A zero right-hand side (a conductor with no panels never occurs --
        // `geometry::discretize` rejects that -- but a defensive zero-norm
        // check keeps the relative-residual math well-defined regardless).
        let stop_norm = if b_norm > 0.0 {
            rel_tol * b_norm
        } else {
            rel_tol
        };

        let mut xk = DVector::<f64>::zeros(n);
        let mut r = b.clone(); // b - p*xk, xk = 0
        let mut z = r.component_mul(&inv_diag);
        let mut d = z.clone();
        let mut rz_old = r.dot(&z);

        let mut iters = 0usize;
        let mut r_norm = r.norm();
        while r_norm > stop_norm && iters < max_iter {
            let ap = p * &d;
            let denom = d.dot(&ap);
            if !denom.is_finite() || denom == 0.0 {
                return Err(format!(
                    "iterative solve stalled (zero/non-finite curvature) after \
                     {iters} iteration(s) on right-hand side {col} -- the \
                     potential-coefficient matrix is singular (or numerically \
                     indistinguishable from singular); check for overlapping/coincident \
                     conductor surfaces or an extreme scale mismatch between conductors"
                ));
            }
            let alpha = rz_old / denom;
            xk += &d * alpha;
            r -= &ap * alpha;
            z = r.component_mul(&inv_diag);
            let rz_new = r.dot(&z);
            let beta = rz_new / rz_old;
            d = &z + &d * beta;
            rz_old = rz_new;
            iters += 1;
            r_norm = r.norm();
        }

        if !r_norm.is_finite() || r_norm > stop_norm {
            return Err(format!(
                "iterative solve did not converge within {max_iter} iterations on \
                 right-hand side {col} (relative residual {:.3e}, target {rel_tol:.0e}) \
                 -- the potential-coefficient matrix is too ill-conditioned for this \
                 geometry/panel-size combination",
                if b_norm > 0.0 {
                    r_norm / b_norm
                } else {
                    r_norm
                }
            ));
        }

        iterations.push(iters);
        relative_residuals.push(if b_norm > 0.0 {
            r_norm / b_norm
        } else {
            r_norm
        });
        x.set_column(col, &xk);
    }

    Ok((
        x,
        IterativeSolveStats {
            iterations,
            relative_residuals,
        },
    ))
}

/// The original direct solve (in-place LU factorisation + triangular solve)
/// this issue's iterative path replaces, kept for the accuracy
/// cross-check (`iterative_solve_matches_direct_lu_solve`) and the solve-time
/// comparison recorded in `docs/design/mom-iterative-solver.md`. Not used by
/// `solve_capacitance_matrix_ff` any more.
#[cfg(test)]
fn solve_dense_lu(p: &DMatrix<f64>, rhs: &DMatrix<f64>) -> Option<DMatrix<f64>> {
    p.clone().lu().solve(rhs)
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

    // --- iterative (PCG) solver: accuracy, convergence, and scale (#799) ---

    /// A grid of `count` small "finger" plates over one shared ground plate
    /// -- an interdigitated-capacitor-shaped geometry with a
    /// larger-than-MVP conductor count (MVP fixtures throughout this module
    /// use 1-2 conductors), used both to check the iterative solve against
    /// the direct one at scale and to measure the convergence rate recorded
    /// in `docs/design/mom-iterative-solver.md`.
    fn finger_grid(
        count: usize,
        side_um: f64,
        gap_um: f64,
        pitch_um: f64,
    ) -> Vec<ConductorRequest> {
        let mut conductors: Vec<ConductorRequest> = (0..count)
            .map(|i| {
                let x0 = i as f64 * pitch_um;
                ConductorRequest {
                    name: format!("finger{i}"),
                    boxes: vec![BoxRequest {
                        x0_um: x0,
                        y0_um: 0.0,
                        x1_um: x0 + side_um,
                        y1_um: side_um,
                        z0_um: gap_um,
                        z1_um: gap_um,
                    }],
                    conductivity_s_per_m: None,
                }
            })
            .collect();
        let total_x = count as f64 * pitch_um;
        conductors.push(ConductorRequest {
            name: "ground".to_string(),
            boxes: vec![BoxRequest {
                x0_um: -pitch_um,
                y0_um: -pitch_um,
                x1_um: total_x + pitch_um,
                y1_um: side_um + pitch_um,
                z0_um: 0.0,
                z1_um: 0.0,
            }],
            conductivity_s_per_m: None,
        });
        conductors
    }

    #[test]
    fn iterative_solve_matches_direct_lu_solve() {
        // A geometry with more than the two-plate MVP fixtures' conductor
        // count -- 6 fingers + 1 ground -- so the cross-check exercises a
        // real (non-2x2) right-hand-side count, not just the smallest
        // possible case. Kept small (n in the low hundreds) so the direct
        // LU side of this comparison stays fast in an unoptimised debug
        // test build; `iterative_solve_scaling_report` below is the
        // dedicated larger-n measurement.
        let conductors = finger_grid(6, 1.0, 0.5, 2.0);
        let panels = discretize(&conductors, 0.5).unwrap();
        let eps = EPS0_SI;
        let p = build_potential_matrix(&panels, eps);
        let rhs = build_rhs(&panels, conductors.len());

        let direct = solve_dense_lu(&p, &rhs).expect("direct LU solve");
        let (iterative, stats) =
            pcg_solve(&p, &rhs, ITERATIVE_REL_TOL, ITERATIVE_MAX_ITER).expect("PCG solve");

        assert_eq!(stats.iterations.len(), conductors.len());
        let mean_iters =
            stats.iterations.iter().sum::<usize>() as f64 / stats.iterations.len() as f64;
        let max_iters = *stats.iterations.iter().max().unwrap();
        let max_residual = stats
            .relative_residuals
            .iter()
            .cloned()
            .fold(0.0_f64, f64::max);
        // Printed (not just asserted) so the convergence rate this issue's
        // acceptance criteria asks for is visible in a normal `cargo test`
        // run's log -- `docs/design/mom-iterative-solver.md`'s larger-scale
        // `--ignored` report is the deliberately-slow companion measurement,
        // this is the one every CI run actually sees.
        println!(
            "\nmom PCG convergence (representative geometry): n={} panels, \
             k={} conductors -- iterations: mean={mean_iters:.1}, max={max_iters} \
             (n={}); max relative residual={max_residual:.3e}",
            panels.len(),
            conductors.len(),
            panels.len(),
        );
        for (col, iters) in stats.iterations.iter().enumerate() {
            assert!(
                *iters > 0,
                "right-hand side {col} converged in 0 iterations"
            );
            // Structural convergence-rate check (not timing-based, so not
            // flaky under CI load): CG must resolve each right-hand side in
            // well under n iterations, or the O(k * iterations * n^2)
            // iterative path could not possibly beat the O(n^3) direct
            // factorisation it replaces at any scale.
            assert!(
                *iters < panels.len() / 2,
                "right-hand side {col} used {iters} CG iterations against n={} panels",
                panels.len()
            );
        }

        for i in 0..panels.len() {
            for j in 0..conductors.len() {
                assert_relative_eq!(
                    iterative[(i, j)],
                    direct[(i, j)],
                    max_relative = 1e-8,
                    epsilon = 1e-20
                );
            }
        }
    }

    #[test]
    fn iterative_solve_matches_direct_lu_solve_through_the_public_api() {
        // Same cross-check one level up, through the function `klt mom`
        // actually calls -- confirms the assembled/scaled capacitance
        // matrix (not just the raw per-panel charge solve) agrees too.
        let conductors = vec![plate("top", 1.0), plate("bottom", 0.0)];
        let panels = discretize(&conductors, 0.5).unwrap();
        let p = build_potential_matrix(&panels, EPS0_SI);
        let rhs = build_rhs(&panels, 2);
        let direct_charge = solve_dense_lu(&p, &rhs).unwrap();
        let direct_c = assemble_capacitance_matrix_ff(&panels, 2, &direct_charge).unwrap();

        let iterative_c = solve_capacitance_matrix_ff(&panels, 2, 1.0).unwrap();

        for j in 0..2 {
            for k in 0..2 {
                assert_relative_eq!(iterative_c[j][k], direct_c[j][k], max_relative = 1e-8);
            }
        }
    }

    #[test]
    fn iterative_solve_preserves_exact_linearity_in_permittivity() {
        // The tightest existing accuracy bar on this solver
        // (`tests/test_mom_validation.py::test_parallel_plate_is_exactly_linear_in_permittivity`,
        // rel=1e-12): the Laplace problem is linear in eps, so C(eps_r) =
        // eps_r * C(1) to solver round-off. Re-checked here at the Rust
        // level so a regression is caught by `cargo test`, not only by the
        // slower end-to-end Python suite.
        let conductors = vec![plate("top", 1.0), plate("bottom", 0.0)];
        let panels = discretize(&conductors, 0.5).unwrap();
        let vacuum = solve_capacitance_matrix_ff(&panels, 2, 1.0).unwrap();
        let oxide = solve_capacitance_matrix_ff(&panels, 2, 3.9).unwrap();
        assert_relative_eq!(oxide[0][0], 3.9 * vacuum[0][0], max_relative = 1e-12);
        assert_relative_eq!(oxide[0][1], 3.9 * vacuum[0][1], max_relative = 1e-12);
    }

    #[test]
    fn pcg_converges_in_one_iteration_for_a_single_panel_system() {
        // n=1: the Jacobi preconditioner is exact (M = P), so the first CG
        // step lands exactly on the solution -- one iteration, not many.
        let conductors = vec![plate("only", 0.0)];
        let panels = discretize(&conductors, 20.0).unwrap(); // one panel
        assert_eq!(panels.len(), 1);
        let (_c, stats) = solve_capacitance_matrix_ff_with_stats(&panels, 1, 1.0).unwrap();
        assert_eq!(stats.iterations, vec![1]);
        assert!(stats.relative_residuals[0] <= ITERATIVE_REL_TOL);
    }

    /// Measures the iterative solve's convergence rate and wall-clock cost
    /// against the direct solve on a larger-than-MVP conductor count, per
    /// issue #799's acceptance criteria: `docs/design/mom-iterative-solver.md`
    /// transcribes the numbers this prints.
    ///
    /// `#[ignore]`d: at this panel count the direct `O(n^3)` factorisation
    /// alone takes minutes in an *unoptimised* debug test build (measured:
    /// well past a minute at a third of this n -- see this module's git
    /// history), which would make every ordinary `cargo test` run pay for a
    /// one-off scale measurement. Run it deliberately, in release mode,
    /// where the same comparison finishes in seconds:
    ///
    /// ```text
    /// cargo test --release iterative_solve_scaling_report -- --ignored --nocapture
    /// ```
    ///
    /// The correctness assertion (iterative must still match direct) and the
    /// convergence-rate assertion (CG must resolve in well under `n`
    /// iterations) still run and still gate the result -- only the "print a
    /// human-readable report" part is advisory.
    #[test]
    #[ignore = "O(n^3) direct-solve baseline is minutes-slow in debug builds -- \
                run explicitly with --release (see doc comment)"]
    fn iterative_solve_scaling_report() {
        // 8 fingers + 1 shared ground, finely discretised: a conductor count
        // well past the MVP's 1-2, at a panel count (~6900, under the
        // 8000-panel `MAX_PANELS` guard) chosen so `n` dwarfs `k` -- the
        // regime where the iterative path's O(k * iterations * n^2) beats
        // the direct path's O(n^3), per this module's doc comment.
        let conductors = finger_grid(8, 2.0, 1.0, 4.0);
        let panels = discretize(&conductors, 0.25).unwrap();
        let n = panels.len();
        let k = conductors.len();
        let p = build_potential_matrix(&panels, EPS0_SI);
        let rhs = build_rhs(&panels, k);

        let t_direct = std::time::Instant::now();
        let direct = solve_dense_lu(&p, &rhs).expect("direct LU solve");
        let direct_elapsed = t_direct.elapsed();

        let t_iter = std::time::Instant::now();
        let (iterative, stats) =
            pcg_solve(&p, &rhs, ITERATIVE_REL_TOL, ITERATIVE_MAX_ITER).expect("PCG solve");
        let iter_elapsed = t_iter.elapsed();

        for i in 0..n {
            for j in 0..k {
                assert_relative_eq!(
                    iterative[(i, j)],
                    direct[(i, j)],
                    max_relative = 1e-7,
                    epsilon = 1e-20
                );
            }
        }

        let max_iters = *stats.iterations.iter().max().unwrap();
        let mean_iters = stats.iterations.iter().sum::<usize>() as f64 / k as f64;
        println!(
            "\nmom iterative-solver scaling report: n={n} panels, k={k} conductors\n\
             \x20   direct LU solve   : {direct_elapsed:?}\n\
             \x20   iterative (PCG)   : {iter_elapsed:?}\n\
             \x20   speedup           : {:.2}x\n\
             \x20   CG iterations     : mean={mean_iters:.1}, max={max_iters} (n={n})\n\
             \x20   max relative resid: {:.3e}",
            direct_elapsed.as_secs_f64() / iter_elapsed.as_secs_f64().max(1e-12),
            stats
                .relative_residuals
                .iter()
                .cloned()
                .fold(0.0_f64, f64::max)
        );

        // The scaling argument only holds if convergence happens in well
        // under n iterations; assert that structurally rather than trusting
        // eyeballed printed output.
        assert!(
            max_iters < n / 2,
            "CG used {max_iters} iterations against n={n} panels -- the \
             O(k * iterations * n^2) iterative path is not actually cheaper \
             than the O(n^3) direct factorisation at this operating point"
        );
    }
}
