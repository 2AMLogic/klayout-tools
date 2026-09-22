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
//! Off-diagonal entries use a **near-field/far-field split** (#2061): pairs
//! whose centroids are closer than a few panel widths integrate the charge
//! over the source panel properly (Gauss-Legendre quadrature of the
//! potential at the target centroid, symmetrised across the pair), while
//! well-separated pairs keep the bare point-charge kernel between
//! centroids. A point charge at the centroid is a good stand-in for a
//! uniformly charged panel only while the separation is large compared to
//! the panel size; in the near field it costs percent-level accuracy
//! against FastCap's analytic panel integrals -- measured 3.29% worst-case
//! on a close parallel-plate fixture by the FastCap cross-validation oracle
//! (`tests/test_mom_capacitance_oracle.py`,
//! `docs/design/fastcap-oracle.md`), which is what motivated the split.
//! The correction is deliberately the *collocation* integral -- the
//! potential of the source panel evaluated at the target centroid, which is
//! what FastCap itself computes for near pairs (analytically, with the same
//! constant-basis discretisation) -- rather than a Galerkin double integral
//! over both panels: on this oracle the Galerkin variant moved the
//! close-plate fixtures *away* from FastCap (they are the same discretised
//! problem only in the refinement limit), while the collocation integral
//! reproduces FastCap's own near-field kernel. The diagonal (self) entry
//! stays on the closed form (`self_term_ln1p_sqrt2`): the self integral is
//! genuinely singular, and a quadrature cannot integrate its own
//! collocation point.
//!
//! ## The solve step: preconditioned Conjugate Gradient, not a direct LU
//! (#799)
//!
//! `P` is symmetric (the centroid kernel `1/(4 pi eps r)` is symmetric in
//! `i`/`j` by construction, and the near-field quadrature applies the same
//! panel-to-panel rule from either side, so it inherits the double
//! integral's Fubini symmetry) and positive definite: `q^T P q` is (twice) the
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

/// How near is "near field": an off-diagonal pair uses the quadrature
/// panel-to-panel integral when its centroid separation is below this
/// multiple of the sum of the two panels' circumradii (half-diagonals). At
/// 3x, the centroid point charge is still accurate to a fraction of a
/// percent (the centroid approximation's leading error falls off as the
/// square of panel-size-over-separation), while every pair shape that made
/// the FastCap oracle's close-plate fixture disagree at the percent level
/// -- facing pairs across a one-panel gap *and* the edge-adjacent
/// neighbours within one plate, barely half a panel apart centre to centre
/// -- sits far below the threshold. Raising it would only spend fill time
/// on accuracy the solve does not need; the sensitivity is pinned by
/// `far_field_pairs_agree_with_the_centroid_kernel` below.
///
/// The hard cutoff's own artifact -- the two kernels disagreeing by
/// ~0.1-0.5% for a pair sitting exactly at the boundary -- is measured,
/// bounded, and accepted rather than blended away:
/// `kernel_disagreement_at_the_threshold_stays_under_the_documented_
/// artifact_band` pins the entry-level mismatch, and
/// `threshold_perturbation_moves_the_solution_far_less_than_the_agreement_
/// band` pins the solved matrix's insensitivity to walking pairs across the
/// boundary (#2323, with the numbers in `docs/design/mom-validation.md`).
const NEAR_FIELD_CIRCUMRADII_MULTIPLE: f64 = 3.0;

/// Gauss-Legendre 4-point rule on `[-1, 1]` (nodes ascending; exact for
/// polynomials through degree 7; weights sum to 2, the interval length).
/// Four points per panel axis put the double-integral kernel well inside
/// the FastCap oracle's agreement band on every near-field pair shape the
/// discretiser emits, including edge-sharing pairs whose integrand turns
/// steep across the shared edge -- pinned against an order-8 reference by
/// `panel_pair_quadrature_matches_high_order_reference`. Hand-rolled
/// constants: a quadrature crate dependency would cost more than twelve
/// numbers buy.
const GL4_NODES: [f64; 4] = [
    -0.861_136_311_594_052_6,
    -0.339_981_043_584_856_3,
    0.339_981_043_584_856_3,
    0.861_136_311_594_052_6,
];
const GL4_WEIGHTS: [f64; 4] = [
    0.347_854_845_137_453_8,
    0.652_145_154_862_546_1,
    0.652_145_154_862_546_1,
    0.347_854_845_137_453_8,
];

/// Half a panel's diagonal (um): the radius of the smallest sphere centred
/// on the panel centroid that contains it -- the length scale the
/// near-field threshold (`NEAR_FIELD_CIRCUMRADII_MULTIPLE`) is measured in.
fn panel_circumradius_um(panel: &Panel) -> f64 {
    let u = norm3(&panel.side_vectors[0]);
    let v = norm3(&panel.side_vectors[1]);
    0.5 * (u * u + v * v).sqrt()
}

fn norm3(v: &[f64; 3]) -> f64 {
    (v[0] * v[0] + v[1] * v[1] + v[2] * v[2]).sqrt()
}

/// The near-field predicate: true when the pair's circumspheres come
/// within `NEAR_FIELD_CIRCUMRADII_MULTIPLE` of each other, i.e. when the
/// centroid separation is no longer large compared to the panel sizes.
fn panels_are_near_field(a: &Panel, b: &Panel) -> bool {
    panels_are_near_field_within(a, b, NEAR_FIELD_CIRCUMRADII_MULTIPLE)
}

/// [`panels_are_near_field`] at an arbitrary threshold multiple. The
/// production predicate is this function at the constant; keeping the
/// parameterised form here (not behind `#[cfg(test)]`) is what guarantees
/// the boundary/sensitivity tests below exercise the *same* comparison the
/// production fill makes, not a test-side copy of it (#2323).
fn panels_are_near_field_within(a: &Panel, b: &Panel, multiple: f64) -> bool {
    distance(&a.center, &b.center)
        < multiple * (panel_circumradius_um(a) + panel_circumradius_um(b))
}

/// Map `(xi, eta)` in `[-1, 1]^2` onto the panel through its side vectors:
/// the affine sweep `center + (xi/2) u + (eta/2) v`, whose Jacobian is the
/// panel area over 4.
fn point_on_panel(panel: &Panel, xi: f64, eta: f64) -> [f64; 3] {
    let u = &panel.side_vectors[0];
    let v = &panel.side_vectors[1];
    [
        panel.center[0] + 0.5 * (xi * u[0] + eta * v[0]),
        panel.center[1] + 0.5 * (xi * u[1] + eta * v[1]),
        panel.center[2] + 0.5 * (xi * u[2] + eta * v[2]),
    ]
}

/// The area-averaged source-panel kernel `∬_S 1/|f - r'| dS' / A` -- the
/// potential at field point `f` of a uniform unit surface charge on `S`,
/// per unit area, evaluated by the `nodes`-point Gauss-Legendre rule per
/// panel axis (`weights` the matching weights, summing to 2). The `(A/4)`
/// Jacobian cancels against the `1/A` average, so a constant kernel
/// reproduces itself exactly (`2^2 / 4`). The collocation field point is
/// never inside or on the source panel (`geometry::discretize` keeps panel
/// interiors disjoint), so the integrand is bounded and analytic over the
/// panel and the rule converges geometrically -- see
/// `panel_pair_quadrature_matches_high_order_reference`.
fn panel_source_potential_average(
    field: &[f64; 3],
    source: &Panel,
    nodes: &[f64],
    weights: &[f64],
) -> f64 {
    let mut total = 0.0;
    for (w_xi, &xi) in weights.iter().zip(nodes) {
        for (w_eta, &eta) in weights.iter().zip(nodes) {
            let r_prime = point_on_panel(source, xi, eta);
            total += w_xi * w_eta / distance(field, &r_prime);
        }
    }
    total / 4.0
}

/// The near-field kernel entry for one panel pair: the collocation source
/// integral evaluated in both directions and averaged. The raw collocation
/// fill is not symmetric (`P_ij` integrates panel `j` at panel `i`'s
/// centroid, `P_ji` the reverse -- unequal whenever the two panels
/// differ), but the solve and the physics want a symmetric matrix, so the
/// pair entry is the mean -- the same symmetrisation FastCap applies to
/// its own collocation matrix when it prints it (`mksCapDump`'s
/// `sym_mat`), only performed before the solve, where it is what lets
/// Conjugate Gradient stand in for FastCap's GMRES.
fn panel_pair_near_field_kernel(panel_i: &Panel, panel_j: &Panel) -> f64 {
    0.5 * (panel_source_potential_average(&panel_i.center, panel_j, &GL4_NODES, &GL4_WEIGHTS)
        + panel_source_potential_average(&panel_j.center, panel_i, &GL4_NODES, &GL4_WEIGHTS))
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
/// Diagonal: the closed-form self term. Off-diagonal: the near-field
/// quadrature panel-to-panel integral for pairs inside the near-field
/// threshold, the centroid point-charge kernel for everything else (see
/// the module docstring).
///
/// Assumes `panels` contains no two panels at the exact same location --
/// `geometry::discretize` deduplicates the numerically-coincident panels a
/// box-based discretisation can otherwise produce at abutting-box corners
/// (see its doc comment); genuine coincidence between conductors that
/// remains (overlapping/short-circuited surfaces) is a malformed request,
/// and `pcg_solve` reports it as a singular/ill-conditioned matrix rather
/// than dividing by the resulting `r = 0`.
fn build_potential_matrix(panels: &[Panel], eps: f64) -> DMatrix<f64> {
    build_potential_matrix_with_kernel(panels, eps, true)
}

/// `use_quadrature_near_field = false` reinstates the pre-#2061 bare
/// centroid point-charge kernel on every off-diagonal entry. No caller
/// outside the tests wants that (it is the regression this module's
/// near-field assertions exist to catch), so the flag lives behind this
/// test-visible variant rather than in the public API.
fn build_potential_matrix_with_kernel(
    panels: &[Panel],
    eps: f64,
    use_quadrature_near_field: bool,
) -> DMatrix<f64> {
    let n = panels.len();
    let ln1p_sqrt2 = self_term_ln1p_sqrt2();
    let greens = 1.0 / (4.0 * std::f64::consts::PI * eps);

    let mut p = DMatrix::<f64>::zeros(n, n);
    for i in 0..n {
        p[(i, i)] = ln1p_sqrt2 / (std::f64::consts::PI * eps * panels[i].area_um2.sqrt());
        for j in (i + 1)..n {
            let r = if use_quadrature_near_field && panels_are_near_field(&panels[i], &panels[j]) {
                panel_pair_near_field_kernel(&panels[i], &panels[j])
            } else {
                1.0 / distance(&panels[i].center, &panels[j].center)
            };
            let entry = greens * r;
            p[(i, j)] = entry;
            p[(j, i)] = entry;
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
/// fill itself has broken down. The near-field quadrature kernel (#2061)
/// removed the historical trigger -- panels far wider than the gap they
/// face no longer flip the mutual term's sign -- so a violation today
/// points at something more genuinely degenerate (overlapping conductor
/// surfaces, an extreme scale mismatch). The result is still returned
/// (this command's bar is "produces a numeric result"), but the caller is
/// told plainly not to trust it -- silently handing back a sign-flipped
/// mutual capacitance would be worse than an inaccurate one. See also
/// [`discretisation_warnings`], the coarseness diagnostic that replaces the
/// warning this check used to emit on coarse-over-narrow-gap geometry.
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

/// One warning per conductor pair whose facing panels are wider (centroid
/// distance-wise) than the conductors are separated: a constant-density
/// panel cannot represent the charge distribution that piles up on surfaces
/// facing a gap narrower than the panel itself, so the returned coupling is
/// structurally unreliable for that pair even though the near-field
/// quadrature kernel (#2061) keeps its sign and its symmetry. The result is
/// still returned; the warning names the knob -- reduce `panel_size_um`.
///
/// The trigger is the closest cross-conductor panel pair: its centroid
/// separation (an upper bound on the true surface-to-surface gap) against
/// the larger panel's equivalent side `sqrt(area)`. At the default 0.5 um
/// panels, conductor separations of 1 um and up stay a factor of two clear
/// of the threshold; the FastCap oracle's coupled-line and shielded
/// fixtures (1 um gaps) therefore emit none, while the pre-#2061 sign-flip
/// fixture (2 um panels across a 0.01 um gap) does.
pub fn discretisation_warnings(panels: &[Panel], names: &[String]) -> Vec<String> {
    let name_of = |i: usize| -> &str {
        names
            .get(i)
            .map(String::as_str)
            .unwrap_or("<unnamed conductor>")
    };
    let conductor_count = names.len();
    // Per conductor pair: the narrowest panel-centroid separation seen and
    // the widest panel involved. One O(n^2) pass, the same order as the
    // fill itself; a warning fires when the pair's panels are wider than
    // its separation.
    let slot = |j: usize, k: usize| j.min(k) * conductor_count + j.max(k);
    let mut min_separation = vec![f64::INFINITY; conductor_count * conductor_count];
    let mut widest_panel = vec![0.0_f64; conductor_count * conductor_count];
    for (i, panel_i) in panels.iter().enumerate() {
        for panel_j in panels[i + 1..].iter() {
            let (j, k) = (panel_i.conductor_index, panel_j.conductor_index);
            if j == k {
                continue;
            }
            let s = slot(j, k);
            min_separation[s] = min_separation[s].min(distance(&panel_i.center, &panel_j.center));
            widest_panel[s] = widest_panel[s]
                .max(panel_i.area_um2.sqrt())
                .max(panel_j.area_um2.sqrt());
        }
    }

    let mut warnings = Vec::new();
    for j in 0..conductor_count {
        for k in (j + 1)..conductor_count {
            let s = slot(j, k);
            if widest_panel[s] > min_separation[s] {
                warnings.push(format!(
                    "conductors {j_name:?} and {k_name:?} are separated by as \
                     little as {min_sep:.4} um (panel-centroid scale) while \
                     discretised with panels up to {widest:.4} um wide -- a \
                     constant-density panel cannot resolve the charge facing a \
                     gap narrower than the panel, so their coupling should not \
                     be trusted; reduce panel_size_um and re-run",
                    j_name = name_of(j),
                    k_name = name_of(k),
                    min_sep = min_separation[s],
                    widest = widest_panel[s],
                ));
            }
        }
    }
    warnings
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
        // Same bar for the coarseness diagnostic: 0.5 um panels across a
        // 1 um gap is a factor of two clear of the trigger.
        assert!(discretisation_warnings(&panels, &names).is_empty());
    }

    #[test]
    fn panels_wider_than_the_gap_flag_the_coarseness_diagnostic_not_a_sign_flip() {
        // Plates 0.05 um apart discretised with 2 um panels: each panel is
        // 40x wider than the gap it faces. Pre-#2061 this flipped the
        // mutual term's sign (the centroid 1/r kernel badly overestimated
        // the coupling); the near-field quadrature kernel keeps the sign
        // and the symmetry physical -- but a constant-density panel still
        // cannot represent the charge facing a narrower gap, so the
        // coupling's magnitude is structurally unreliable and the solve
        // must say so instead of staying silent.
        let conductors = vec![plate("top", 0.05), plate("bottom", 0.0)];
        let names: Vec<String> = conductors.iter().map(|c| c.name.clone()).collect();
        let panels = discretize(&conductors, 2.0).unwrap();
        let c = solve_capacitance_matrix_ff(&panels, 2, 1.0).unwrap();
        // The sign flip itself is gone: the corrected kernel is physical.
        assert!(c[0][1] < 0.0, "mutual should stay negative: {c:?}");

        let warnings = discretisation_warnings(&panels, &names);
        assert!(!warnings.is_empty(), "coarse-over-narrow-gap must warn");
        assert!(
            warnings.iter().all(|w| w.contains("panel_size_um")),
            "every warning should name the knob to change: {warnings:?}"
        );
        // One warning for the offending conductor pair, not one per
        // offending panel pair (a coarse mesh produces thousands).
        let pair: Vec<&String> = warnings
            .iter()
            .filter(|w| w.contains("top") && w.contains("bottom"))
            .collect();
        assert_eq!(pair.len(), 1, "{warnings:?}");
        let physicality = physicality_warnings(&c, &names);
        assert!(
            physicality.is_empty(),
            "the physicality backstop should stay quiet for this fixture: {physicality:?}"
        );
    }

    #[test]
    fn physicality_warnings_flag_a_hand_built_nonphysical_matrix() {
        // The sign-flip failure mode that used to be reachable from a
        // coarse mesh is now corrected at the kernel (#2061), so the
        // physicality backstop is exercised with a deliberately corrupted
        // matrix -- the coverage that keeps it diagnosable if some future
        // fill regression reintroduces a genuine physicality break.
        let names = vec!["a".to_string(), "b".to_string(), "c".to_string()];
        let mut c = vec![
            vec![2.0, -0.5, -0.3],
            vec![-0.5, 1.5, -0.2],
            vec![-0.3, -0.2, 1.0],
        ];
        c[0][1] = 0.4;
        c[1][0] = 0.4;
        let warnings = physicality_warnings(&c, &names);
        assert!(
            warnings.iter().all(|w| w.contains("panel_size_um")),
            "every warning should name the knob to change: {warnings:?}"
        );
        let mutual: Vec<&String> = warnings
            .iter()
            .filter(|w| w.contains("mutual capacitance"))
            .collect();
        // Symmetric matrix -> the (a, b) violation is reported once, not
        // once per triangle.
        assert_eq!(mutual.len(), 1, "{warnings:?}");
        assert!(
            mutual[0].contains("a") && mutual[0].contains("b"),
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

    // --- near-field quadrature kernel (#2061) ---

    /// One axis-aligned square panel of the given side, centred on `center`.
    fn square_panel(center: [f64; 3], side: f64, conductor: usize) -> Panel {
        Panel {
            center,
            area_um2: side * side,
            conductor_index: conductor,
            side_vectors: [[side, 0.0, 0.0], [0.0, side, 0.0]],
        }
    }

    /// Gauss-Legendre order 8 on [-1, 1]: the near-field reference rule the
    /// production order-4 rule is checked against. 16 significant digits,
    /// straight from Abramowitz & Stegun table 25.4.
    const GL8_NODES: [f64; 8] = [
        -0.960_289_856_497_536_3,
        -0.796_666_477_413_626_7,
        -0.525_463_096_128_398_2,
        -0.183_434_642_495_649_8,
        0.183_434_642_495_649_8,
        0.525_463_096_128_398_2,
        0.796_666_477_413_626_7,
        0.960_289_856_497_536_3,
    ];
    const GL8_WEIGHTS: [f64; 8] = [
        0.101_228_536_290_376_3,
        0.222_381_034_453_374_5,
        0.313_706_645_877_887_3,
        0.362_683_783_378_362,
        0.362_683_783_378_362,
        0.313_706_645_877_887_3,
        0.222_381_034_453_374_5,
        0.101_228_536_290_376_3,
    ];

    #[test]
    fn panel_pair_quadrature_matches_high_order_reference() {
        // The two near-field pair shapes the discretiser can emit: facing
        // panels across a one-panel gap (the parallel-plate case), and
        // edge-sharing neighbours within one plate -- the pair whose
        // integrand turns steepest across the shared edge. The production
        // order-4 rule must sit close enough to the order-8 reference that
        // the kernel is never the accuracy bottleneck of the solve (the
        // FastCap oracle's band is 3%; the kernel's own convergence error
        // here must be orders of magnitude under it).
        let facing = (
            square_panel([0.0, 0.0, 1.0], 1.0, 0),
            square_panel([0.0, 0.0, 0.0], 1.0, 1),
        );
        let edge_sharing = (
            square_panel([0.5, 0.0, 0.0], 1.0, 0),
            square_panel([-0.5, 0.0, 0.0], 1.0, 1),
        );
        for (label, (a, b)) in [
            ("facing across a gap", facing),
            ("edge-sharing", edge_sharing),
        ] {
            assert!(
                panels_are_near_field(&a, &b),
                "{label}: fixture is not in the near field -- the case it exists to cover"
            );
            let symmetrised = |nodes: &[f64], weights: &[f64]| {
                0.5 * (panel_source_potential_average(&a.center, &b, nodes, weights)
                    + panel_source_potential_average(&b.center, &a, nodes, weights))
            };
            let order4 = symmetrised(&GL4_NODES, &GL4_WEIGHTS);
            let order8 = symmetrised(&GL8_NODES, &GL8_WEIGHTS);
            let relative_gap = ((order4 - order8) / order8).abs();
            println!(
                "{label}: order-4 {order4:.9} vs order-8 {order8:.9} (gap {relative_gap:.2e})"
            );
            assert!(
                relative_gap < 1e-3,
                "{label}: order-4 quadrature is {relative_gap:.3e} off the \
                 order-8 reference -- too coarse for the near-field kernel"
            );
        }
    }

    #[test]
    fn near_field_quadrature_keeps_the_fill_symmetric() {
        // Two 1 um plates half a panel width apart, at 0.5 um panels: every
        // inter-plate pair is deep in the near field, so this is where a
        // one-sided quadrature rule (each side's fill evaluating only its
        // own panel) would show up as P[i][j] != P[j][i] and quietly break
        // the CG solve's SPD assumption. The rule is symmetric by
        // construction; the assertion pins it.
        let conductors = vec![plate("top", 0.5), plate("bottom", 0.0)];
        let panels = discretize(&conductors, 0.5).unwrap();
        let p = build_potential_matrix(&panels, EPS0_SI);
        for i in 0..panels.len() {
            for j in (i + 1)..panels.len() {
                assert_relative_eq!(p[(i, j)], p[(j, i)], max_relative = 1e-12, epsilon = 1e-25);
            }
        }
    }

    #[test]
    fn far_field_pairs_agree_with_the_centroid_kernel() {
        // The split must be continuous at the threshold: a pair well
        // outside the near field gets the same answer from both branches
        // (the centroid point charge *is* the converged panel-to-panel
        // integral at large separation), so switching a pair across the
        // threshold cannot move the matrix discontinuously. 0.1% is the
        // same slack `NEAR_FIELD_CIRCUMRADII_MULTIPLE`'s doc comment claims
        // for the far side of the split.
        let a = square_panel([0.0, 0.0, 0.0], 1.0, 0);
        let far = square_panel([10.0, 0.0, 0.0], 1.0, 1);
        assert!(!panels_are_near_field(&a, &far));
        let centroid = 1.0 / distance(&a.center, &far.center);
        let quadrature = 0.5
            * (panel_source_potential_average(&a.center, &far, &GL4_NODES, &GL4_WEIGHTS)
                + panel_source_potential_average(&far.center, &a, &GL4_NODES, &GL4_WEIGHTS));
        assert_relative_eq!(centroid, quadrature, max_relative = 1e-3);
    }

    // --- the near/far boundary itself (#2323) ---

    #[test]
    fn near_field_predicate_is_strictly_less_at_the_threshold() {
        // The boundary the split switches kernels on: a pair whose centroid
        // separation sits exactly at `3 * (r_i + r_j)` is *far* field (the
        // comparison is strict `<`), a hair inside is near, a hair outside
        // is far. Pinning the three sides keeps the cutoff's semantics (and
        // its strictness) deliberate rather than accidental -- and keeps a
        // future `<=`/`>` flip or threshold change from landing silently.
        let a = square_panel([0.0, 0.0, 0.0], 1.0, 0);
        let radius_sum = panel_circumradius_um(&a) * 2.0;
        let threshold = NEAR_FIELD_CIRCUMRADII_MULTIPLE * radius_sum;

        // On-axis placement makes the centroid distance `sqrt(t^2)`, which
        // lands on `t` to well under a relative ulp-scale nudge of t used
        // below; assert the placement really is at the boundary before
        // asserting what side of it the pair lands on.
        let at = square_panel([0.0, 0.0, threshold], 1.0, 1);
        assert_relative_eq!(
            distance(&a.center, &at.center),
            threshold,
            max_relative = 1e-12
        );

        // Relative 1e-9 nudges: far tighter than anything a mesh refinement
        // could land a pair on, far looser than float noise in `distance`.
        let nudge = 1.0 - 1e-9;
        let inside = square_panel([0.0, 0.0, threshold * nudge], 1.0, 1);
        assert!(
            panels_are_near_field(&a, &inside),
            "a pair a relative 1e-9 inside the threshold must be near field"
        );
        assert!(
            !panels_are_near_field(&a, &at),
            "a pair exactly at the threshold is far field: the comparison is \
             strict `<`"
        );
        let outside = square_panel([0.0, 0.0, threshold / nudge], 1.0, 1);
        assert!(
            !panels_are_near_field(&a, &outside),
            "a pair a relative 1e-9 outside the threshold must be far field"
        );
    }

    /// Pair shapes representative of what the discretiser actually emits
    /// (see `panel_pair_quadrature_matches_high_order_reference` for the
    /// two canonical ones), each placed with its centroid separation at
    /// exactly `d = 3 * (r_i + r_j)` -- the boundary where the fill
    /// switches kernels. Returns `(label, a, b)`.
    fn boundary_pair_fixtures() -> Vec<(&'static str, Panel, Panel)> {
        let s = 1.0_f64;
        let r_sq = 0.5 * (s * s + s * s).sqrt(); // circumradius of a unit square

        // Facing coaxial squares (the parallel-plate pair), gap = threshold.
        let facing_gap = NEAR_FIELD_CIRCUMRADII_MULTIPLE * 2.0 * r_sq;
        // Coplanar squares with an edge gap putting the centroids on the
        // threshold (the coupled-line/in-plate neighbour pair).
        let coplanar_gap = NEAR_FIELD_CIRCUMRADII_MULTIPLE * 2.0 * r_sq - s;

        vec![
            (
                "facing coaxial squares",
                square_panel([0.0, 0.0, 0.0], s, 0),
                square_panel([0.0, 0.0, facing_gap], s, 1),
            ),
            (
                "coplanar edge gap",
                square_panel([0.0, 0.0, 0.0], s, 0),
                square_panel([s + coplanar_gap, 0.0, 0.0], s, 1),
            ),
            (
                "coplanar diagonal",
                square_panel([0.0, 0.0, 0.0], s, 0),
                square_panel(
                    [
                        NEAR_FIELD_CIRCUMRADII_MULTIPLE * 2.0 * r_sq / std::f64::consts::SQRT_2,
                        NEAR_FIELD_CIRCUMRADII_MULTIPLE * 2.0 * r_sq / std::f64::consts::SQRT_2,
                        0.0,
                    ],
                    s,
                    1,
                ),
            ),
            (
                "perpendicular (wall/floor)",
                Panel {
                    center: [0.0, 0.0, 0.0],
                    area_um2: s * s,
                    conductor_index: 0,
                    side_vectors: [[s, 0.0, 0.0], [0.0, 0.0, s]],
                },
                square_panel(
                    [0.0, NEAR_FIELD_CIRCUMRADII_MULTIPLE * 2.0 * r_sq, 0.0],
                    s,
                    1,
                ),
            ),
            (
                "unequal sizes 2:1 facing",
                square_panel([0.0, 0.0, 0.0], s, 0),
                square_panel(
                    [
                        0.0,
                        0.0,
                        NEAR_FIELD_CIRCUMRADII_MULTIPLE * (r_sq + r_sq * 2.0),
                    ],
                    2.0 * s,
                    1,
                ),
            ),
        ]
    }

    #[test]
    fn kernel_disagreement_at_the_threshold_stays_under_the_documented_artifact_band() {
        // #2323's measurement, kept as a gate: the hard kernel switch leaves
        // an entry-level discontinuity at the boundary -- the quadrature
        // kernel and the centroid kernel are different approximations of the
        // same potential coefficient, and they do not agree exactly at any
        // finite mesh resolution. Measured over these boundary pair shapes
        // (equal and 2:1 sizes, facing / coplanar / diagonal / perpendicular
        // orientations): 0.11%-0.51%, worst for the unequal facing pair,
        // ~0.23% for the equal-size edge-gap pair the issue probed. The
        // worst sits ~6x inside the FastCap oracle's 3% agreement band (see
        // docs/design/mom-validation.md's #2323 section). This pins it so
        // it cannot grow silently: a future kernel or threshold change that
        // worsens the boundary mismatch fails here.
        let mut worst = 0.0_f64;
        let mut worst_label = "";
        for (label, a, b) in boundary_pair_fixtures() {
            // The fixture must sit *at* the boundary. Not asserted as
            // literally outside: for the diagonal placement the centroid
            // distance is `sqrt((t/sqrt(2))^2 * 2)`, which can round one
            // ulp below `t` -- a hair inside. A relative 1e-9 window is
            // ~4 ulps wide here and is what "at the boundary" means for a
            // discontinuity measurement. (The exact strict-`<` semantics
            // are pinned by
            // `near_field_predicate_is_strictly_less_at_the_threshold`.)
            let separation = distance(&a.center, &b.center);
            let boundary = NEAR_FIELD_CIRCUMRADII_MULTIPLE
                * (panel_circumradius_um(&a) + panel_circumradius_um(&b));
            assert!(
                (separation - boundary).abs() < 1e-9 * boundary,
                "{label}: fixture is not at the threshold ({separation} vs \
                 {boundary})"
            );
            let centroid = 1.0 / distance(&a.center, &b.center);
            let quadrature = panel_pair_near_field_kernel(&a, &b);
            let relative_gap = ((quadrature - centroid) / centroid).abs();
            println!(
                "{label}: centroid {centroid:.9} vs quadrature {quadrature:.9} \
                 (rel gap {:.4}%)",
                relative_gap * 100.0
            );
            if relative_gap > worst {
                worst = relative_gap;
                worst_label = label;
            }
        }
        println!(
            "worst boundary kernel disagreement: {:.4}% ({worst_label})",
            worst * 100.0
        );
        assert!(
            worst < 6e-3,
            "boundary kernel disagreement grew to {:.4}% (worst shape: \
             {worst_label}); the documented artifact band is 0.6% -- see \
             docs/design/mom-validation.md (#2323)",
            worst * 100.0
        );
    }

    /// Builds the potential matrix exactly like
    /// [`build_potential_matrix_with_kernel`] but with the near-field
    /// threshold moved to `multiple` -- the ±20%-threshold knob the
    /// sensitivity test below measures with.
    fn build_potential_matrix_with_near_multiple(
        panels: &[Panel],
        eps: f64,
        multiple: f64,
    ) -> DMatrix<f64> {
        let n = panels.len();
        let ln1p_sqrt2 = self_term_ln1p_sqrt2();
        let greens = 1.0 / (4.0 * std::f64::consts::PI * eps);
        let mut p = DMatrix::<f64>::zeros(n, n);
        for i in 0..n {
            p[(i, i)] = ln1p_sqrt2 / (std::f64::consts::PI * eps * panels[i].area_um2.sqrt());
            for j in (i + 1)..n {
                let r = if panels_are_near_field_within(&panels[i], &panels[j], multiple) {
                    panel_pair_near_field_kernel(&panels[i], &panels[j])
                } else {
                    1.0 / distance(&panels[i].center, &panels[j].center)
                };
                let entry = greens * r;
                p[(i, j)] = entry;
                p[(j, i)] = entry;
            }
        }
        p
    }

    /// Solves the capacitance matrix from a pre-built potential matrix --
    /// the same pipeline [`solve_capacitance_matrix_ff_with_stats`] runs,
    /// test-visible so the threshold-sensitivity test can swap the fill.
    fn solve_from_matrix(
        panels: &[Panel],
        conductor_count: usize,
        p: &DMatrix<f64>,
    ) -> Vec<Vec<f64>> {
        let rhs = build_rhs(panels, conductor_count);
        let (charge, _stats) = pcg_solve(p, &rhs, ITERATIVE_REL_TOL, ITERATIVE_MAX_ITER)
            .expect("threshold-sensitivity solve");
        assemble_capacitance_matrix_ff(panels, conductor_count, &charge).unwrap()
    }

    #[test]
    fn threshold_perturbation_moves_the_solution_far_less_than_the_agreement_band() {
        // #2323's materiality measurement, kept as a gate: move the
        // near-field threshold ±20% (3.0 -> 2.4 / 3.6), forcing every panel
        // pair within that band of the boundary to switch kernels, and
        // compare the *solved* capacitance matrices. If walking pairs across
        // the boundary moved any aggregate value materially, the
        // discontinuity would need smoothing rather than documenting. It
        // does not: measured on the flat-plate and coupled-line fixtures,
        // every entry moves orders of magnitude less than the FastCap
        // oracle's 3% band (docs/design/mom-validation.md's "#2323"
        // section). 0.3% asserted = 10% of the oracle band. This default
        // gate runs at 1.0 um panels (inside the oracle's refinement band);
        // `threshold_perturbation_report` is the 0.5 um release-mode
        // companion measurement at the oracle's own operating point.
        let eps_r = 3.9;
        let eps = EPS0_SI * eps_r;
        let fixtures: Vec<(&str, Vec<ConductorRequest>)> = vec![
            (
                "parallel plates 10x10 um, 1 um gap, 1.0 um panels",
                vec![plate("top", 1.0), plate("bottom", 0.0)],
            ),
            (
                "coupled lines 20x2x0.6 um, 1 um gap, 1.0 um panels",
                vec![
                    box_conductor("a", 0.0, 0.0, 20.0, 2.0, 0.0, 0.6),
                    box_conductor("b", 0.0, 3.0, 20.0, 5.0, 0.0, 0.6),
                ],
            ),
        ];
        for (label, conductors) in &fixtures {
            let worst =
                threshold_perturbation_worst_entry_movement(conductors, 1.0, eps, &mut (0, 0));
            println!("{label}: worst solved-entry movement {:.4}%", worst * 100.0);
            assert!(
                worst < 3e-3,
                "{label}: moving the near-field threshold ±20% moved a solved \
                 capacitance entry by {:.4}% -- the boundary \
                 discontinuity is material after all; it needs smoothing, \
                 not documenting (docs/design/mom-validation.md #2323)",
                worst * 100.0
            );
        }
    }

    /// The ±20%-threshold measurement core shared by the default gate above
    /// and the release-mode report below: solve one fixture at threshold
    /// multiples 2.4 / 3.0 / 3.6 and return the worst solved-entry movement
    /// the ±20% perturbation causes, filling `switches` with how many panel
    /// pairs each direction of the perturbation walks across the boundary.
    fn threshold_perturbation_worst_entry_movement(
        conductors: &[ConductorRequest],
        panel_size_um: f64,
        eps: f64,
        switches: &mut (usize, usize),
    ) -> f64 {
        let panels = discretize(conductors, panel_size_um).unwrap();
        let k = conductors.len();
        let nominal = solve_from_matrix(
            &panels,
            k,
            &build_potential_matrix_with_near_multiple(&panels, eps, 3.0),
        );
        *switches = (0, 0);
        for i in 0..panels.len() {
            for j in (i + 1)..panels.len() {
                let (a, b) = (&panels[i], &panels[j]);
                // Near at the nominal 3.0 but far once the threshold drops
                // to 2.4 (ratio in [2.4, 3.0)) ...
                if panels_are_near_field(a, b) && !panels_are_near_field_within(a, b, 2.4) {
                    switches.0 += 1;
                }
                // ... and far at the nominal but near once it rises to 3.6
                // (ratio in [3.0, 3.6)).
                if !panels_are_near_field_within(a, b, 3.0)
                    && panels_are_near_field_within(a, b, 3.6)
                {
                    switches.1 += 1;
                }
            }
        }
        let mut worst = 0.0_f64;
        for threshold in [2.4, 3.6] {
            let shifted = solve_from_matrix(
                &panels,
                k,
                &build_potential_matrix_with_near_multiple(&panels, eps, threshold),
            );
            for (row_nom, row_shift) in nominal.iter().zip(&shifted) {
                for (v_nom, v_shift) in row_nom.iter().zip(row_shift) {
                    worst = worst.max((v_shift - v_nom).abs() / v_nom.abs());
                }
            }
        }
        worst
    }

    /// The 0.5 um-panel (FastCap-oracle operating point) companion to
    /// `threshold_perturbation_moves_the_solution_far_less_than_the_
    /// agreement_band`, on the oracle's full shielded fixture too --
    /// the numbers `docs/design/mom-validation.md`'s #2323 section
    /// transcribes. Multi-second debug-build solves at this panel count,
    /// hence `#[ignore]` + release mode, the same convention as
    /// `near_field_runtime_report`:
    ///
    /// ```text
    /// cargo test --release threshold_perturbation_report -- --ignored --nocapture
    /// ```
    ///
    /// The movement bound is still asserted -- the report cannot silently
    /// rot into "whatever the numbers happen to be".
    #[test]
    #[ignore = "multi-second 0.5 um-panel solves are slow in debug builds -- \
                run with --release"]
    fn threshold_perturbation_report() {
        let eps_r = 3.9;
        let eps = EPS0_SI * eps_r;
        let fixtures: Vec<(&str, Vec<ConductorRequest>)> = vec![
            (
                "parallel plates 10x10 um, 1 um gap, 0.5 um panels",
                vec![plate("top", 1.0), plate("bottom", 0.0)],
            ),
            (
                "coupled lines 20x2x0.6 um, 1 um gap, 0.5 um panels",
                vec![
                    box_conductor("a", 0.0, 0.0, 20.0, 2.0, 0.0, 0.6),
                    box_conductor("b", 0.0, 3.0, 20.0, 5.0, 0.0, 0.6),
                ],
            ),
            (
                "shielded pair (oracle fixture), 0.5 um panels",
                vec![
                    box_conductor("a", 0.0, 0.0, 20.0, 2.0, 0.0, 0.6),
                    box_conductor("b", 0.0, 3.0, 20.0, 5.0, 0.0, 0.6),
                    box_conductor("shield", -2.0, -3.0, 22.0, 8.0, -1.6, -1.0),
                ],
            ),
        ];
        for (label, conductors) in &fixtures {
            let mut switches = (0, 0);
            let worst =
                threshold_perturbation_worst_entry_movement(conductors, 0.5, eps, &mut switches);
            println!(
                "{label}: {n} panels, {lo}+{hi} pairs switch at -/+20% \
                 threshold -- worst solved-entry movement {:.4}%",
                worst * 100.0,
                n = panels_of(conductors, 0.5),
                lo = switches.0,
                hi = switches.1,
            );
            assert!(
                worst < 3e-3,
                "{label}: ±20% threshold perturbation moved a solved entry \
                 by {:.4}% (see docs/design/mom-validation.md #2323)",
                worst * 100.0
            );
        }
    }

    /// Panel count for the report's prints (the measurement itself
    /// re-discretises internally).
    fn panels_of(conductors: &[ConductorRequest], panel_size_um: f64) -> usize {
        discretize(conductors, panel_size_um).unwrap().len()
    }

    #[test]
    fn the_near_field_correction_can_actually_fail() {
        // Negative control, in the house style of the FastCap oracle's
        // `test_the_comparison_can_actually_fail`: seed the regression this
        // module's near-field tests exist to catch (revert the fill to the
        // pre-#2061 bare centroid kernel) and require it to move the
        // close-plate fixture's kernel entries by far more than the bands
        // asserted above. Without this half, a future refactor that
        // silently disabled the quadrature branch could pass every other
        // test here -- the assertions would be unfalsifiable.
        let conductors = vec![plate("top", 1.0), plate("bottom", 0.0)];
        let panels = discretize(&conductors, 1.0).unwrap();
        let with_split = build_potential_matrix(&panels, EPS0_SI);
        let seeded = build_potential_matrix_with_kernel(&panels, EPS0_SI, false);

        let n = panels.len();
        let mut worst_off_diagonal = 0.0_f64;
        for i in 0..n {
            for j in 0..n {
                if i == j {
                    continue;
                }
                let relative = ((with_split[(i, j)] - seeded[(i, j)]) / with_split[(i, j)]).abs();
                worst_off_diagonal = worst_off_diagonal.max(relative);
            }
        }
        println!(
            "seeded centroid-kernel regression moves near-field entries by \
             up to {:.3}% (order-4 vs order-8 band: 0.1%)",
            worst_off_diagonal * 100.0
        );
        assert!(
            worst_off_diagonal > 10.0 * 1e-3,
            "reverting to the centroid kernel moved the near-field entries by \
             only {:.3}% -- the quadrature-vs-reference assertions above would \
             pass without a real near-field correction",
            worst_off_diagonal * 100.0
        );
    }

    /// One box-shaped conductor, for building the oracle fixtures below.
    fn box_conductor(
        name: &str,
        x0: f64,
        y0: f64,
        x1: f64,
        y1: f64,
        z0: f64,
        z1: f64,
    ) -> ConductorRequest {
        ConductorRequest {
            name: name.to_string(),
            boxes: vec![BoxRequest {
                x0_um: x0,
                y0_um: y0,
                x1_um: x1,
                y1_um: y1,
                z0_um: z0,
                z1_um: z1,
            }],
            conductivity_s_per_m: None,
        }
    }

    /// The FastCap oracle's largest fixture verbatim (coupled lines over a
    /// grounded plane), so the runtime report below measures the geometry
    /// the AC's "no material runtime regression" claim is about.
    fn shielded_pair_fixture() -> Vec<ConductorRequest> {
        vec![
            box_conductor("a", 0.0, 0.0, 20.0, 2.0, 0.0, 0.6),
            box_conductor("b", 0.0, 3.0, 20.0, 5.0, 0.0, 0.6),
            box_conductor("shield", -2.0, -3.0, 22.0, 8.0, -1.6, -1.0),
        ]
    }

    /// Measures where the near-field split's cost lands on the oracle's
    /// largest fixture (3068 panels): the quadrature fill versus the
    /// seeded centroid-kernel fill, and the CG solve over each -- pinning
    /// issue #2061's "no material runtime regression" acceptance bar with
    /// numbers `docs/design/fastcap-oracle.md` transcribes.
    ///
    /// `#[ignore]`d like `iterative_solve_scaling_report`: a 3068-panel
    /// solve is seconds-slow in an unoptimised debug build. Run it in
    /// release mode:
    ///
    /// ```text
    /// cargo test --release near_field_runtime_report -- --ignored --nocapture
    /// ```
    ///
    /// The structural gate (each right-hand side must still converge in
    /// well under `n` iterations) runs in both profiles.
    #[test]
    #[ignore = "multi-second solve is slow in debug builds -- run with --release"]
    fn near_field_runtime_report() {
        let conductors = shielded_pair_fixture();
        let panels = discretize(&conductors, 0.5).unwrap();
        let rhs = build_rhs(&panels, conductors.len());

        let t_split_fill = std::time::Instant::now();
        let p_split = build_potential_matrix(&panels, EPS0_SI);
        let split_fill = t_split_fill.elapsed();

        let t_old_fill = std::time::Instant::now();
        let p_centroid = build_potential_matrix_with_kernel(&panels, EPS0_SI, false);
        let centroid_fill = t_old_fill.elapsed();

        let t_split_solve = std::time::Instant::now();
        let (split_c, split_stats) =
            pcg_solve(&p_split, &rhs, ITERATIVE_REL_TOL, ITERATIVE_MAX_ITER).expect("split solve");
        let split_solve = t_split_solve.elapsed();

        let t_old_solve = std::time::Instant::now();
        let (centroid_c, centroid_stats) =
            pcg_solve(&p_centroid, &rhs, ITERATIVE_REL_TOL, ITERATIVE_MAX_ITER)
                .expect("centroid solve");
        let centroid_solve = t_old_solve.elapsed();

        let max_split_iters = *split_stats.iterations.iter().max().unwrap();
        let max_centroid_iters = *centroid_stats.iterations.iter().max().unwrap();
        let max_abs_entry = split_c
            .iter()
            .chain(centroid_c.iter())
            .fold(0.0_f64, |m, v| m.max(v.abs()));
        let relative_drift = split_c
            .iter()
            .zip(centroid_c.iter())
            .map(|(a, b)| (a - b).abs())
            .fold(0.0_f64, f64::max)
            / max_abs_entry;
        println!(
            "\nmom near-field runtime report (oracle shielded fixture, n={} panels):\n\
             \x20   fill, near-field split : {split_fill:?}\n\
             \x20   fill, centroid kernel  : {centroid_fill:?}\n\
             \x20   CG solve, split        : {split_solve:?} (max {max_split_iters} iterations)\n\
             \x20   CG solve, centroid     : {centroid_solve:?} (max {max_centroid_iters} iterations)\n\
             \x20   matrix-scale drift     : {:.3}%",
            panels.len(),
            relative_drift * 100.0,
        );
        // The corrected kernel must not degrade the solve's convergence:
        // both fills have to resolve every right-hand side in well under
        // n iterations, or the iterative path loses its edge over the
        // direct one it replaced (#799).
        for (label, max_iters) in [("split", max_split_iters), ("centroid", max_centroid_iters)] {
            assert!(
                max_iters < panels.len() / 2,
                "{label} kernel: CG used {max_iters} iterations against n={} panels",
                panels.len()
            );
        }
    }
}
