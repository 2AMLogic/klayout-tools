//! Frequency-domain, full-wave partial-impedance solve, with a retarded
//! free-space Green's function -- 2AMLogic/klayout-tools issue #893, Phase 2a
//! of the Method-of-Moments epic #701. Extends `klt mom` from the
//! quasi-static core (capacitance via `solver.rs`, PEEC partial
//! inductance/resistance via `peec.rs`) to a genuine frequency sweep: given
//! `frequencies_hz`, this module reports each conductor pair's complex
//! partial impedance at every requested frequency, and -- for the
//! canonical two-conductor "transmission line" case -- the derived
//! characteristic impedance and propagation constant.
//!
//! ## Method: retarded thin-wire partial impedance, reusing PEEC's bar scope
//!
//! This reuses exactly the same "bar-shaped-conductor" geometric MVP
//! restriction PEEC's `compute_inductance` already established
//! (`geometry::classify_shared_axis_bars`: every conductor is a single box,
//! all conductors share one current-flow axis and axial extent) -- see
//! docs/cli/mom.md's "PEEC inductance/resistance" section for why. Where
//! PEEC's static solve (`peec.rs`) discretises each conductor's
//! *cross-section* into a filament bundle (refining the bundle grid to
//! converge the static self/mutual inductance), this module keeps each
//! conductor as a **single equivalent thin wire** (radius `a_eff =
//! sqrt(area / pi)`, the same "equal-area circle" convention `peec.rs`'s own
//! test oracles use) and instead refines the wire **axially**: each
//! conductor's shared axial extent is subdivided into `n` segments (target
//! length `segment_size_um`), and the conductor-pair partial impedance is the
//! Riemann-sum (point-collocation) approximation of the classical partial
//! mutual/self impedance double integral, generalised from PEEC's static
//! Neumann-formula kernel `1/R` to the frequency-domain **retarded** kernel
//! `exp(-jkR)/R`:
//!
//! ```text
//! Z_pq(omega) = j*omega*mu0/(4*pi) * integral_0^l integral_0^l
//!               exp(-j*k*R(z,z')) / R(z,z') dz dz'
//! ```
//!
//! for conductors `p`/`q` sharing axial length `l`, wavenumber `k = omega *
//! sqrt(background_permittivity) / c0` (a single homogeneous background
//! dielectric -- the same MVP restriction the capacitance solve already
//! documents). `R(z,z')` is the straight-line distance between the two
//! points: `sqrt(d^2 + (z-z')^2)` for `p != q` (`d` the fixed transverse
//! distance between the two conductors' centroids), or the standard
//! "reduced kernel" thin-wire regularisation `sqrt(a_eff^2 + (z-z')^2)` for
//! `p == q` (avoiding the `R=0` self-singularity -- the same regularisation
//! classical thin-wire antenna/transmission-line MoM codes use, see e.g.
//! Harrington, *Field Computation by Moment Methods*).
//!
//! Unlike the static Neumann formula (`peec.rs`'s `mutual_inductance_h`),
//! the retarded double integral has no simple closed form, so it is
//! evaluated by the same **point-collocation** (Riemann-sum, segment
//! midpoint-to-midpoint) approximation `solver.rs`'s own capacitance fill
//! already uses for its off-diagonal terms -- "adequate for the MVP's
//! 'produce a numeric result' acceptance bar... accuracy-vs-refinement is
//! validated" (`solver.rs`'s own module docs), the same standard this module
//! holds itself to (`tests::self_impedance_converges_under_segment_refinement`
//! below and its Python-level counterpart in
//! `tests/test_mom_fullwave_validation.py`).
//!
//! As `omega -> 0` (so `k -> 0`), `exp(-jkR) -> 1` and `Z_pq(omega) / (j
//! omega)` reduces to exactly the same geometric double integral PEEC's
//! static partial inductance uses -- so a low-but-nonzero-frequency point is
//! expected to reproduce `peec.rs`'s static answer up to the (small, at this
//! frequency) retardation correction; see
//! `tests::low_frequency_limit_matches_the_static_neumann_formula`.
//!
//! ## The canonical structure: a two-conductor transmission-line segment
//!
//! When exactly two conductors are present, this module additionally derives
//! the per-unit-length telegrapher's-equation quantities and, from them, the
//! **characteristic impedance** and **propagation constant** -- the
//! observable this issue's acceptance criteria ask for:
//!
//! ```text
//! Z'(omega) = (Z_11 + Z_22 - 2*Z_12) / length         -- per-unit-length series impedance
//! C'        = (C_11 - C_12) / 2 / length              -- per-unit-length line capacitance
//! Y'(omega) = j * omega * C'
//! gamma(omega) = sqrt(Z'(omega) * Y'(omega)) = alpha + j*beta
//! Z0(omega)    = sqrt(Z'(omega) / Y'(omega))
//! ```
//!
//! `Z_11 + Z_22 - 2*Z_12` is the same "loop" combination `peec.rs`'s own
//! validated two-wire-line test already uses for the static case (see its
//! `loop_inductance_matches_two_wire_transmission_line_formula`); this module
//! reuses it at nonzero frequency. `C'` is the *differential-mode* capacitance
//! dual of that same loop combination -- **not** simply `C_11`, the
//! already-computed static Maxwell self-capacitance (`solver.rs`) for
//! "conductor 1 at 1V, conductor 2 grounded". For an isolated two-conductor
//! system (no third, enclosing reference node -- exactly this MVP's
//! canonical structure, two floating bars in a homogeneous background), a
//! meaningful fraction of `C_11`'s field lines terminate "at infinity" rather
//! than on the other conductor, so `C_11` alone over-counts the capacitance
//! the telegrapher-equation model needs. Driving the pair differentially
//! (`+Q` on conductor 1, `-Q` on conductor 2, by symmetry `V_1 = -V_2 = V/2`)
//! and solving `Q = C_11 V_1 + C_12 V_2` gives the correct line capacitance
//! `C' = (C_11 - C_12) / 2` -- the standard reciprocal-network reduction of a
//! 2-port Maxwell capacitance matrix to a single differential-mode
//! capacitance, and the capacitance-side counterpart of the loop-inductance
//! combination above (`tests::two_wire_line_characteristic_impedance_matches_closed_form`
//! is what caught the original `C_11`-only version being wrong -- see that
//! test's module history in this issue's PR description). The transverse
//! charge distribution is treated as quasi-electrostatic throughout (valid
//! once the conductors' cross-section is small relative to the wavelength,
//! which this MVP does not check explicitly, see docs/cli/mom.md's "Scope
//! and limitations"), while the *axial* current distribution is the piece
//! this module makes genuinely full-wave via the retarded kernel above. This
//! combination -- retarded series impedance, quasi-static shunt capacitance
//! -- is the standard "full-wave PEEC" approximation (Ruehli et al.'s later
//! extensions of the original static PEEC method to include retardation).
//!
//! `docs/cli/mom.md`'s "Full-wave frequency sweep" section validates this
//! against the classical two-wire-line closed form
//! (`Z0 = eta0 / (pi * sqrt(er)) * acosh(D / (2*a))`) and the TEM identity
//! `L' * C' = er / c0^2` any lossless homogeneous-medium transmission line
//! satisfies.

use num_complex::Complex64;

use crate::contract::{ConductorRequest, FullWavePoint};
use crate::geometry::{self, FullWaveBarLayout};

/// Vacuum permeability, H/m -- see `peec.rs`'s identical constant for the
/// pre-/post-2019-SI note.
const MU0_H_PER_M: f64 = 4.0 * std::f64::consts::PI * 1e-7;

/// Speed of light in vacuum, m/s (exact by SI definition).
const C0_M_PER_S: f64 = 299_792_458.0;

/// Upper bound on the *total* axial segment count across every conductor.
/// The retarded-kernel fill is `O(n^2)` per frequency point (a dense sum
/// over every segment pair, mirroring `solver.rs`'s dense potential-
/// coefficient fill), recomputed independently for every entry in
/// `frequencies_hz` -- unlike the panel/filament counts this mirrors
/// (`geometry::MAX_PANELS`/`MAX_FILAMENTS`), there is no `O(n^2)` *memory*
/// allocation risk here (the accumulation is a running sum, not a stored
/// matrix), but an unbounded segment count times an unbounded frequency
/// count is still an unbounded amount of CPU work; this cap keeps a single
/// request's cost bounded and fails fast with a clear message (name the knob
/// to change) rather than hanging.
const MAX_TOTAL_SEGMENTS: usize = 4000;

fn retarded_phase(k_r: f64) -> Complex64 {
    // exp(-j * k_r) = cos(k_r) - j*sin(k_r).
    Complex64::new(k_r.cos(), -k_r.sin())
}

fn transverse_distance_um(a: [f64; 2], b: [f64; 2]) -> f64 {
    let du = a[0] - b[0];
    let dv = a[1] - b[1];
    (du * du + dv * dv).sqrt()
}

/// One conductor's axial segment centers (um, along the shared axis) and
/// effective thin-wire radius (um).
struct ConductorMesh {
    centers_um: Vec<f64>,
    a_eff_um: f64,
    centroid_transverse_um: [f64; 2],
}

fn build_mesh(
    layout: &FullWaveBarLayout,
    segment_size_um: f64,
) -> Result<Vec<ConductorMesh>, String> {
    if segment_size_um <= 0.0 {
        return Err(format!(
            "segment_size_um must be positive, got {segment_size_um}"
        ));
    }
    let centers_um: Vec<f64> =
        geometry::subdivide_1d(layout.axis_lo_um, layout.axis_hi_um, segment_size_um)
            .into_iter()
            .map(|(center, _len)| center)
            .collect();

    let total_segments = centers_um.len().saturating_mul(layout.conductors.len());
    if total_segments > MAX_TOTAL_SEGMENTS {
        return Err(format!(
            "the full-wave solve's axial mesh would produce {total_segments} total segments \
             (across every conductor), exceeding the {MAX_TOTAL_SEGMENTS} cap -- increase \
             segment_size_um, or check for a scale mismatch between the request's axial \
             length and segment_size_um"
        ));
    }

    Ok(layout
        .conductors
        .iter()
        .map(|c| ConductorMesh {
            centers_um: centers_um.clone(),
            a_eff_um: (c.area_um2 / std::f64::consts::PI).sqrt(),
            centroid_transverse_um: c.centroid_transverse_um,
        })
        .collect())
}

/// Retarded partial-impedance matrix (complex, ohms) between every conductor
/// pair at one frequency, via the point-collocation Riemann sum described in
/// this module's docs.
fn impedance_matrix(
    meshes: &[ConductorMesh],
    k_per_um: f64,
    delta_um: f64,
) -> Result<Vec<Vec<Complex64>>, String> {
    let n = meshes.len();
    let mut z = vec![vec![Complex64::new(0.0, 0.0); n]; n];
    for (p, mesh_p) in meshes.iter().enumerate() {
        for (q, mesh_q) in meshes.iter().enumerate() {
            if q < p {
                z[p][q] = z[q][p];
                continue;
            }
            let mut sum = Complex64::new(0.0, 0.0);
            let same_conductor = p == q;
            let d_um = if same_conductor {
                0.0
            } else {
                let d = transverse_distance_um(
                    mesh_p.centroid_transverse_um,
                    mesh_q.centroid_transverse_um,
                );
                if d < 1e-9 {
                    return Err(format!(
                        "full-wave conductors {p} and {q} coincide (zero transverse \
                         separation) -- overlapping conductor geometry is not physical"
                    ));
                }
                d
            };
            for &zi in &mesh_p.centers_um {
                for &zj in &mesh_q.centers_um {
                    let dz = zi - zj;
                    let r_um = if same_conductor {
                        (mesh_p.a_eff_um * mesh_p.a_eff_um + dz * dz).sqrt()
                    } else {
                        (d_um * d_um + dz * dz).sqrt()
                    };
                    sum += retarded_phase(k_per_um * r_um) / r_um;
                }
            }
            z[p][q] = sum * (delta_um * delta_um);
        }
    }
    Ok(z)
}

/// Two-conductor derived line quantities at one frequency; see module docs.
struct TwoConductorLine {
    z0: Complex64,
    gamma: Complex64,
}

fn derive_two_conductor_line(
    z: &[Vec<Complex64>],
    capacitance_matrix_ff: &[Vec<f64>],
    axis_length_m: f64,
    omega: f64,
) -> Result<TwoConductorLine, String> {
    let z_loop = z[0][0] + z[1][1] - z[0][1] * 2.0;
    let z_prime = z_loop / axis_length_m;

    // Differential-mode ("line") capacitance -- see module docs for why this
    // is `(C_11 - C_12) / 2`, not the raw Maxwell self-capacitance `C_11`.
    let c_diff_f = (capacitance_matrix_ff[0][0] - capacitance_matrix_ff[0][1]) / 2.0 * 1e-15;
    if c_diff_f.is_nan() || c_diff_f <= 0.0 {
        return Err(format!(
            "full-wave two-conductor derivation requires a positive differential-mode line \
             capacitance (C_11 - C_12) / 2, got {c_diff_f} F (C_11 = {} fF, C_12 = {} fF) -- \
             refine panel_size_um so the capacitance solve is well resolved (see \
             docs/cli/mom.md's \"Warnings\")",
            capacitance_matrix_ff[0][0], capacitance_matrix_ff[0][1]
        ));
    }
    let c_prime = c_diff_f / axis_length_m;
    let y_prime = Complex64::new(0.0, omega * c_prime);

    let gamma = (z_prime * y_prime).sqrt();
    let z0 = (z_prime / y_prime).sqrt();
    Ok(TwoConductorLine { z0, gamma })
}

/// Run the full-wave frequency sweep end to end: classify the bar geometry
/// (reusing PEEC's bar-shape validation), build the axial mesh, and solve
/// the retarded partial-impedance matrix at every requested frequency --
/// plus, for exactly two conductors, the derived characteristic impedance
/// and propagation constant. `capacitance_matrix_ff` is the already-computed
/// static capacitance matrix (`solver::solve_capacitance_matrix_ff`), reused
/// unchanged for the two-conductor derivation's per-unit-length capacitance
/// (see module docs).
///
/// Returns `(points, total_segment_count)`. Errors (surfaced to Python as
/// `ValueError`) for a non-bar-shaped conductor, a non-positive/non-finite
/// requested frequency, coincident conductors, or the segment-count guard
/// above.
pub fn solve_full_wave_sweep(
    conductors: &[ConductorRequest],
    background_permittivity: f64,
    frequencies_hz: &[f64],
    segment_size_um: f64,
    capacitance_matrix_ff: &[Vec<f64>],
) -> Result<(Vec<FullWavePoint>, usize), String> {
    let layout = geometry::classify_full_wave_bars(conductors)?;
    let meshes = build_mesh(&layout, segment_size_um)?;
    let axis_length_um = layout.axis_hi_um - layout.axis_lo_um;
    let axis_length_m = axis_length_um * 1e-6;
    let delta_um = axis_length_um / meshes[0].centers_um.len() as f64;
    let total_segments = meshes[0].centers_um.len() * meshes.len();

    let two_conductor = meshes.len() == 2;
    if two_conductor && capacitance_matrix_ff.len() != 2 {
        return Err(
            "full-wave two-conductor derivation requires a 2x2 capacitance matrix".to_string(),
        );
    }

    let mut points = Vec::with_capacity(frequencies_hz.len());
    for &frequency_hz in frequencies_hz {
        if !frequency_hz.is_finite() || frequency_hz <= 0.0 {
            return Err(format!(
                "frequencies_hz entries must be positive and finite, got {frequency_hz}"
            ));
        }
        let omega = 2.0 * std::f64::consts::PI * frequency_hz;
        let k_per_m = omega * background_permittivity.sqrt() / C0_M_PER_S;
        let k_per_um = k_per_m * 1e-6;

        let z = impedance_matrix(&meshes, k_per_um, delta_um)?;
        // um-valued geometric sum -> SI ohms: the double integral was
        // evaluated with z/R in micrometers, so it is 1e6x the equivalent
        // integral in meters (see module docs' derivation, mirroring
        // `peec.rs`'s `GEOM_UM_TO_NH` unit-handling note); multiplying by
        // `1e-6` here converts back before applying the `j*omega*mu0/(4*pi)`
        // prefactor.
        let prefactor = Complex64::new(
            0.0,
            omega * MU0_H_PER_M / (4.0 * std::f64::consts::PI) * 1e-6,
        );
        let z_ohm: Vec<Vec<num_complex::Complex64>> = z
            .iter()
            .map(|row| row.iter().map(|v| v * prefactor).collect())
            .collect();

        if z_ohm
            .iter()
            .flatten()
            .any(|v| !v.re.is_finite() || !v.im.is_finite())
        {
            return Err(
                "full-wave impedance solve produced a non-finite value -- check for \
                 degenerate (near-zero-separation or near-zero-length) geometry"
                    .to_string(),
            );
        }

        let two_conductor_line = if two_conductor {
            let line =
                derive_two_conductor_line(&z_ohm, capacitance_matrix_ff, axis_length_m, omega)?;
            Some((line.z0, line.gamma))
        } else {
            None
        };

        points.push(FullWavePoint {
            frequency_hz,
            impedance_matrix_real_ohm: z_ohm
                .iter()
                .map(|row| row.iter().map(|v| v.re).collect())
                .collect(),
            impedance_matrix_imag_ohm: z_ohm
                .iter()
                .map(|row| row.iter().map(|v| v.im).collect())
                .collect(),
            characteristic_impedance_real_ohm: two_conductor_line.map(|(z0, _)| z0.re),
            characteristic_impedance_imag_ohm: two_conductor_line.map(|(z0, _)| z0.im),
            attenuation_np_per_m: two_conductor_line.map(|(_, gamma)| gamma.re),
            phase_rad_per_m: two_conductor_line.map(|(_, gamma)| gamma.im),
        });
    }

    Ok((points, total_segments))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::contract::BoxRequest;

    const EPS0_F_PER_M: f64 = 8.854_187_812_8e-12;

    fn bar_conductor(name: &str, b: BoxRequest) -> ConductorRequest {
        ConductorRequest {
            name: name.to_string(),
            boxes: vec![b],
            conductivity_s_per_m: None,
        }
    }

    fn wire_box(x1: f64, y1: f64, z1: f64) -> BoxRequest {
        BoxRequest {
            x0_um: 0.0,
            y0_um: 0.0,
            x1_um: x1,
            y1_um: y1,
            z0_um: 0.0,
            z1_um: z1,
        }
    }

    /// Static Neumann-formula mutual partial inductance, in henries (the
    /// same closed form `peec.rs`'s `mutual_inductance_h` implements),
    /// reimplemented locally so this module's low-frequency limit test does
    /// not depend on `peec`'s private items.
    fn static_mutual_inductance_h(length_m: f64, distance_m: f64) -> f64 {
        let ratio = length_m / distance_m;
        let inv_ratio = distance_m / length_m;
        MU0_H_PER_M / (2.0 * std::f64::consts::PI)
            * length_m
            * (ratio.asinh() - (1.0 + inv_ratio * inv_ratio).sqrt() + inv_ratio)
    }

    // --- low-frequency limit: reduces to the static Neumann formula --------

    #[test]
    fn low_frequency_limit_matches_the_static_neumann_formula() {
        // Two long, parallel, well-separated bars -- deep in the thin-wire
        // regime the static Neumann formula assumes. At a frequency low
        // enough that k*length << 1 (but nonzero, so Z/j*omega is
        // well-defined), Z_12(omega)/(j*omega) should reproduce the static
        // mutual partial inductance to a tight tolerance.
        let go = bar_conductor("go", wire_box(2000.0, 2.0, 2.0));
        let ret = bar_conductor(
            "return",
            BoxRequest {
                x0_um: 0.0,
                y0_um: 50.0,
                x1_um: 2000.0,
                y1_um: 52.0,
                z0_um: 0.0,
                z1_um: 2.0,
            },
        );
        let conductors = [go, ret];
        let layout = geometry::classify_full_wave_bars(&conductors).unwrap();
        let meshes = build_mesh(&layout, 5.0).unwrap();
        let delta_um = 2000.0 / meshes[0].centers_um.len() as f64;

        // 1 kHz: k*length = 2*pi*1e3/3e8 * 2000e-6 ~= 4e-8, deeply
        // quasi-static.
        let frequency_hz = 1.0e3_f64;
        let omega = 2.0 * std::f64::consts::PI * frequency_hz;
        let k_per_um = omega / C0_M_PER_S * 1e-6;
        let z = impedance_matrix(&meshes, k_per_um, delta_um).unwrap();
        let prefactor = Complex64::new(
            0.0,
            omega * MU0_H_PER_M / (4.0 * std::f64::consts::PI) * 1e-6,
        );
        let z12 = z[0][1] * prefactor;

        let l12_h = z12.im / omega; // Z = j*omega*L -> L = Im(Z)/omega
        let oracle_h = static_mutual_inductance_h(2000.0e-6, 50.0e-6);
        let rel_err = (l12_h - oracle_h).abs() / oracle_h;
        assert!(
            rel_err < 1e-4,
            "low-frequency L12={l12_h:e} H vs static oracle={oracle_h:e} H, rel.err={rel_err:e}"
        );
        // The real part (radiation resistance) must be tiny at this
        // frequency/geometry -- essentially lossless.
        assert!(
            z12.re.abs() < 1e-6 * z12.im.abs().max(1.0),
            "unexpectedly large real part at low frequency: {z12:?}"
        );
    }

    // --- convergence under axial mesh refinement ----------------------------

    #[test]
    fn self_impedance_converges_under_segment_refinement() {
        let bar = bar_conductor("wire", wire_box(4000.0, 4.0, 4.0));
        let conductors = [bar];
        let layout = geometry::classify_full_wave_bars(&conductors).unwrap();

        let frequency_hz = 10.0e9_f64; // 10 GHz -- genuinely full-wave scale
        let omega = 2.0 * std::f64::consts::PI * frequency_hz;
        let k_per_um = omega * 1.0_f64.sqrt() / C0_M_PER_S * 1e-6;
        let prefactor = Complex64::new(
            0.0,
            omega * MU0_H_PER_M / (4.0 * std::f64::consts::PI) * 1e-6,
        );

        let make = |segment_size_um: f64| {
            let meshes = build_mesh(&layout, segment_size_um).unwrap();
            let delta_um =
                (layout.axis_hi_um - layout.axis_lo_um) / meshes[0].centers_um.len() as f64;
            let z = impedance_matrix(&meshes, k_per_um, delta_um).unwrap();
            z[0][0] * prefactor
        };

        let v1 = make(200.0);
        let v2 = make(100.0);
        let v3 = make(50.0);
        let v4 = make(25.0);

        let d1 = (v2 - v1).norm();
        let d2 = (v3 - v2).norm();
        let d3 = (v4 - v3).norm();
        assert!(
            d2 < d1 && d3 < d2,
            "successive refinements should move the answer strictly less each time: \
             v1={v1:?} v2={v2:?} v3={v3:?} v4={v4:?} (d1={d1:e} d2={d2:e} d3={d3:e})"
        );
    }

    // --- canonical structure: two-wire line characteristic impedance -------

    #[test]
    fn two_wire_line_characteristic_impedance_matches_closed_form() {
        // Two long, parallel, thin bars in vacuum -- the classical
        // twin-lead/two-wire transmission line. Zc = eta0/pi * acosh(D/(2a))
        // for round wires of radius a separated by D; a is taken as the
        // equal-area-circle radius of the (square) bar cross section, the
        // same shape-substitution convention `peec.rs`'s own oracles use.
        let length_um = 500.0;
        let (w_um, sep_um) = (2.0, 40.0);
        let go = bar_conductor("go", wire_box(length_um, w_um, w_um));
        let ret = bar_conductor(
            "return",
            BoxRequest {
                x0_um: 0.0,
                y0_um: sep_um,
                x1_um: length_um,
                y1_um: sep_um + w_um,
                z0_um: 0.0,
                z1_um: w_um,
            },
        );
        let conductors = [go, ret];

        // Static capacitance matrix for this same geometry, computed via
        // the existing quasi-static solver (unchanged) -- exactly what
        // `lib.rs`'s `solve_mom_json` already computes before calling into
        // this module. panel_size_um=2.0 (matching the bar's own width)
        // keeps the total panel count in the low thousands -- well under
        // geometry::MAX_PANELS, and fast even for the dense O(n^2) CG solve
        // in an unoptimised debug test build -- while still resolving the
        // 2um-wide cross-section (a panel much wider than the bar would
        // under-resolve the capacitance, as a too-coarse panel_size_um
        // always does -- see docs/cli/mom.md's "Warnings").
        let panels = crate::geometry::discretize(&conductors, 2.0).unwrap();
        let capacitance_matrix_ff =
            crate::solver::solve_capacitance_matrix_ff(&panels, 2, 1.0).unwrap();

        let frequencies_hz = [1.0e6_f64]; // 1 MHz: k*length ~= 1.6e-5, quasi-TEM
        let (points, _segments) = solve_full_wave_sweep(
            &conductors,
            1.0, // vacuum background
            &frequencies_hz,
            5.0,
            &capacitance_matrix_ff,
        )
        .unwrap();
        let point = &points[0];

        let z0_re = point.characteristic_impedance_real_ohm.unwrap();
        let z0_im = point.characteristic_impedance_imag_ohm.unwrap();

        let a_eq_m = (w_um * w_um * 1e-12 / std::f64::consts::PI).sqrt();
        let sep_m = sep_um * 1e-6;
        let eta0 = (MU0_H_PER_M / EPS0_F_PER_M).sqrt();
        let oracle_ohm = eta0 / std::f64::consts::PI * (sep_m / (2.0 * a_eq_m)).acosh();

        let rel_err = (z0_re - oracle_ohm).abs() / oracle_ohm;
        println!(
            "\ntwo-wire line: Z0 = {z0_re:.4} + j{z0_im:.4} ohm, closed-form oracle = \
             {oracle_ohm:.4} ohm, rel.err = {:.4}%",
            rel_err * 100.0
        );
        assert!(
            rel_err < 0.10,
            "Z0 rel.err {:.4}% exceeds the 10% tolerance (shape-substitution + \
             point-collocation MVP approximation, matching peec.rs's own 5% loop-inductance \
             tolerance plus the capacitance solve's own discretisation error)",
            rel_err * 100.0
        );
        // Effectively lossless (vacuum, subwavelength cross-section):
        // Im(Z0) small relative to Re(Z0).
        assert!(
            z0_im.abs() < 0.1 * z0_re.abs(),
            "unexpectedly lossy characteristic impedance: {z0_re} + j{z0_im}"
        );

        // Propagation constant: TEM in a homogeneous vacuum medium ->
        // beta ~= omega/c0, alpha ~= 0.
        let omega = 2.0 * std::f64::consts::PI * frequencies_hz[0];
        let beta_oracle = omega / C0_M_PER_S;
        let beta_rel_err = (point.phase_rad_per_m.unwrap() - beta_oracle).abs() / beta_oracle;
        println!(
            "beta = {:e} rad/m, TEM oracle omega/c0 = {beta_oracle:e} rad/m, rel.err = {:.4}%",
            point.phase_rad_per_m.unwrap(),
            beta_rel_err * 100.0
        );
        // Same 10% MVP tolerance as Z0 above -- beta = omega*sqrt(L'*C'), so
        // it inherits the same shape-substitution/discretisation error
        // budget as Z0 = sqrt(L'/C') (both derived from this module's L' and
        // the differential-mode line capacitance C', see module docs).
        assert!(
            beta_rel_err < 0.10,
            "beta rel.err {:.4}% exceeds the 10% tolerance vs the TEM oracle omega/c0",
            beta_rel_err * 100.0
        );
        assert!(
            point.attenuation_np_per_m.unwrap().abs() < 1e-3 * beta_oracle,
            "unexpectedly large attenuation: {:e} Np/m",
            point.attenuation_np_per_m.unwrap()
        );
    }

    #[test]
    fn coincident_conductors_are_a_clear_error() {
        let a = bar_conductor("a", wire_box(100.0, 2.0, 2.0));
        let b = bar_conductor("b", wire_box(100.0, 2.0, 2.0));
        let conductors = [a, b];
        let capacitance = vec![vec![1.0, -1.0], vec![-1.0, 1.0]];
        let err = solve_full_wave_sweep(&conductors, 1.0, &[1.0e9], 5.0, &capacitance).unwrap_err();
        assert!(err.contains("coincide"), "unexpected error: {err}");
    }

    #[test]
    fn non_positive_frequency_is_a_clear_error() {
        let a = bar_conductor("wire", wire_box(100.0, 2.0, 2.0));
        let conductors = [a];
        let capacitance = vec![vec![1.0]];
        let err = solve_full_wave_sweep(&conductors, 1.0, &[0.0], 5.0, &capacitance).unwrap_err();
        assert!(err.contains("positive"), "unexpected error: {err}");
    }

    #[test]
    fn excessive_segment_count_is_a_clear_error() {
        let a = bar_conductor("wire", wire_box(1_000_000.0, 2.0, 2.0));
        let conductors = [a];
        let capacitance = vec![vec![1.0]];
        let err = solve_full_wave_sweep(&conductors, 1.0, &[1.0e9], 0.1, &capacitance).unwrap_err();
        assert!(err.contains("segments"), "unexpected error: {err}");
    }
}
