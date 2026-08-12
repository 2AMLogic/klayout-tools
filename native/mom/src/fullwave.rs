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
//!
//! ## Ports and de-embedding (issue #894, Phase 2b)
//!
//! The two-conductor derivation above reports the characteristic impedance
//! and propagation constant of the transmission line **as modeled** --
//! i.e. spanning the full `[axis_lo_um, axis_hi_um]` bar the request's boxes
//! define. A real measurement or simulation setup, though, typically wants
//! S-parameters referenced to a pair of **ports** placed somewhere along
//! that structure (its `position_um`, a reference plane) at a stated
//! **reference impedance** (`reference_impedance_ohm`, defaulting to the
//! standard 50 ohm) -- not the raw modeled-bar-length line. Anything the
//! model spans *outside* the two ports (e.g. feed/probe stubs at either end)
//! is a parasitic that must be **de-embedded** -- its own contribution
//! stripped out of the S-parameters -- rather than left folded into the
//! reported network.
//!
//! Because this module already treats the whole structure as one uniform
//! transmission line (a single `Z0(omega)`/`gamma(omega)` pair, derived
//! above), de-embedding reduces to the standard ABCD (chain-parameter)
//! technique: model the total structure as three cascaded uniform-line
//! segments -- `feed1` (`axis_lo_um` to `ports[0].position_um`), the DUT
//! (`ports[0].position_um` to `ports[1].position_um`), and `feed2`
//! (`ports[1].position_um` to `axis_hi_um`) -- and recover the DUT's own
//! ABCD matrix by cascading the *inverse* of each feed segment's ABCD matrix
//! around the total:
//!
//! ```text
//! ABCD_total = ABCD_feed1 * ABCD_dut * ABCD_feed2
//! ABCD_dut   = ABCD_feed1^-1 * ABCD_total * ABCD_feed2^-1
//! ```
//!
//! Each segment's ABCD matrix is the standard uniform-line (telegrapher's
//! equation) chain matrix for length `l` at this line's own `Z0`/`gamma`:
//! `A = D = cosh(gamma*l)`, `B = Z0*sinh(gamma*l)`, `C = sinh(gamma*l)/Z0`
//! (see `line_abcd`). `ABCD_dut` is then converted to 2-port S-parameters at
//! each port's own (possibly distinct) real reference impedance via the
//! standard ABCD-to-S conversion (Pozar, *Microwave Engineering*, Table
//! 4.2) -- see `abcd_to_s`.
//!
//! When a port sits exactly at the corresponding axial end (`feed` length
//! zero), its ABCD matrix is the identity and de-embedding is a no-op, so
//! this reduces to the direct case (`ABCD_dut == ABCD_total`) automatically
//! -- there is no special-cased "no feed" branch. `tests::two_wire_line_...`
//! below exercises exactly that reduction (matched-line closed form,
//! `S11 == S22 == 0`, `S21 == exp(-gamma*L)`); `tests::de_embedding_...`
//! exercises the general case with nonzero feed stubs on both ports,
//! checked against modeling the DUT segment alone.

use num_complex::Complex64;

use crate::contract::{ConductorRequest, FullWavePoint, PortRequest, SParameters};
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

// --- ports and de-embedding (issue #894) ------------------------------------

/// A port with its default reference impedance resolved -- see module docs'
/// "Ports and de-embedding" section.
struct ResolvedPort {
    position_um: f64,
    reference_impedance_ohm: f64,
}

/// Validate and resolve `ports` against the modeled bar span
/// `[axis_lo_um, axis_hi_um]`. Returns the two ports in request order.
/// Errors (surfaced to Python as `ValueError`) for anything other than
/// exactly two ports, a non-finite/non-positive reference impedance, a
/// position outside the modeled span, or non-ascending port positions (this
/// module always treats `ports[0]` as the "near" port and `ports[1]` as the
/// "far" port -- see `derive_two_conductor_line`'s loop-combination
/// convention, which this reuses).
fn resolve_ports(
    ports: &[PortRequest],
    axis_lo_um: f64,
    axis_hi_um: f64,
) -> Result<[ResolvedPort; 2], String> {
    if ports.len() != 2 {
        return Err(format!(
            "S-parameter extraction requires exactly two ports (got {}) -- the MVP only \
             supports the canonical two-port transmission-line case; see \
             docs/cli/mom.md's \"Port definition and de-embedding\" section",
            ports.len()
        ));
    }

    let mut resolved: Vec<ResolvedPort> = Vec::with_capacity(2);
    for (i, port) in ports.iter().enumerate() {
        let reference_impedance_ohm = port
            .reference_impedance_ohm
            .unwrap_or(crate::contract::DEFAULT_PORT_REFERENCE_IMPEDANCE_OHM);
        if !reference_impedance_ohm.is_finite() || reference_impedance_ohm <= 0.0 {
            return Err(format!(
                "port {i}'s reference_impedance_ohm must be positive and finite, got \
                 {reference_impedance_ohm}"
            ));
        }
        if !port.position_um.is_finite() {
            return Err(format!(
                "port {i}'s position_um must be finite, got {}",
                port.position_um
            ));
        }
        if port.position_um < axis_lo_um - 1e-6 || port.position_um > axis_hi_um + 1e-6 {
            return Err(format!(
                "port {i}'s position_um ({:.6}) lies outside the modeled bar span \
                 [{axis_lo_um:.6}, {axis_hi_um:.6}] um -- a port outside the modeled geometry \
                 has nothing to de-embed against",
                port.position_um
            ));
        }
        resolved.push(ResolvedPort {
            position_um: port.position_um,
            reference_impedance_ohm,
        });
    }

    if resolved[1].position_um <= resolved[0].position_um {
        return Err(format!(
            "ports must be given in strictly ascending axial order (ports[0].position_um \
             < ports[1].position_um), got {:.6} and {:.6} um",
            resolved[0].position_um, resolved[1].position_um
        ));
    }

    Ok([resolved.remove(0), resolved.remove(0)])
}

/// The ABCD (chain-parameter) matrix of a uniform transmission-line segment
/// of length `length_m`, at this line's own characteristic impedance `z0`
/// and propagation constant `gamma` -- the standard telegrapher's-equation
/// two-port model. `length_m == 0` gives the identity matrix (a zero-length
/// segment contributes nothing to a cascade), matching a port placed exactly
/// at the corresponding axial end (see module docs).
fn line_abcd(z0: Complex64, gamma: Complex64, length_m: f64) -> [[Complex64; 2]; 2] {
    let gl = gamma * length_m;
    let a = gl.cosh();
    let b = z0 * gl.sinh();
    let c = gl.sinh() / z0;
    [[a, b], [c, a]]
}

/// 2x2 complex matrix product.
fn abcd_mul(a: &[[Complex64; 2]; 2], b: &[[Complex64; 2]; 2]) -> [[Complex64; 2]; 2] {
    [
        [
            a[0][0] * b[0][0] + a[0][1] * b[1][0],
            a[0][0] * b[0][1] + a[0][1] * b[1][1],
        ],
        [
            a[1][0] * b[0][0] + a[1][1] * b[1][0],
            a[1][0] * b[0][1] + a[1][1] * b[1][1],
        ],
    ]
}

/// 2x2 complex matrix inverse. Errors (surfaced to Python as `ValueError`)
/// on a near-singular matrix -- not expected for a physical feed-line ABCD
/// matrix (`det(ABCD) == 1` identically for any lossless-or-not uniform
/// line), but guarded rather than assumed.
fn abcd_inv(m: &[[Complex64; 2]; 2]) -> Result<[[Complex64; 2]; 2], String> {
    let det = m[0][0] * m[1][1] - m[0][1] * m[1][0];
    if det.norm() < 1e-30 {
        return Err(
            "de-embedding requires an invertible feed-line ABCD matrix -- got a near-singular \
             feed segment (unexpected for a physical uniform line; check for a degenerate \
             reference impedance)"
                .to_string(),
        );
    }
    Ok([
        [m[1][1] / det, -m[0][1] / det],
        [-m[1][0] / det, m[0][0] / det],
    ])
}

/// Convert a 2-port ABCD matrix to S-parameters at real reference impedances
/// `z01`/`z02` (port 1 / port 2) -- the standard conversion (Pozar,
/// *Microwave Engineering*, Table 4.2).
fn abcd_to_s(m: &[[Complex64; 2]; 2], z01: f64, z02: f64) -> SParameters {
    let (a, b, c, d) = (m[0][0], m[0][1], m[1][0], m[1][1]);
    let (z01, z02) = (Complex64::new(z01, 0.0), Complex64::new(z02, 0.0));
    let sqrt_z01z02 = (z01 * z02).sqrt();
    let delta = a * z02 + b + c * z01 * z02 + d * z01;

    let s11 = (a * z02 + b - c * z01 * z02 - d * z01) / delta;
    let s12 = (a * d - b * c) * 2.0 * sqrt_z01z02 / delta;
    let s21 = 2.0 * sqrt_z01z02 / delta;
    let s22 = (-a * z02 + b - c * z01 * z02 + d * z01) / delta;

    SParameters {
        s11_real: s11.re,
        s11_imag: s11.im,
        s12_real: s12.re,
        s12_imag: s12.im,
        s21_real: s21.re,
        s21_imag: s21.im,
        s22_real: s22.re,
        s22_imag: s22.im,
    }
}

/// De-embed the DUT's S-parameters from the total-modeled-line's `Z0`/
/// `gamma`, cascading out each port's feed segment -- see module docs'
/// "Ports and de-embedding" section for the ABCD derivation.
fn de_embedded_s_parameters(
    z0: Complex64,
    gamma: Complex64,
    axis_lo_um: f64,
    axis_hi_um: f64,
    ports: &[ResolvedPort; 2],
) -> Result<SParameters, String> {
    let feed1_m = (ports[0].position_um - axis_lo_um) * 1e-6;
    let feed2_m = (axis_hi_um - ports[1].position_um) * 1e-6;
    let total_m = (axis_hi_um - axis_lo_um) * 1e-6;

    let abcd_total = line_abcd(z0, gamma, total_m);
    let abcd_feed1_inv = abcd_inv(&line_abcd(z0, gamma, feed1_m))?;
    let abcd_feed2_inv = abcd_inv(&line_abcd(z0, gamma, feed2_m))?;
    let abcd_dut = abcd_mul(&abcd_mul(&abcd_feed1_inv, &abcd_total), &abcd_feed2_inv);

    Ok(abcd_to_s(
        &abcd_dut,
        ports[0].reference_impedance_ohm,
        ports[1].reference_impedance_ohm,
    ))
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
/// requested frequency, coincident conductors, the segment-count guard
/// above, or (when `ports` is non-empty) an invalid port count/position/
/// reference impedance -- see `resolve_ports`.
pub fn solve_full_wave_sweep(
    conductors: &[ConductorRequest],
    background_permittivity: f64,
    frequencies_hz: &[f64],
    segment_size_um: f64,
    capacitance_matrix_ff: &[Vec<f64>],
    ports: &[PortRequest],
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

    let resolved_ports = if ports.is_empty() {
        None
    } else {
        if !two_conductor {
            return Err(format!(
                "ports/S-parameter extraction requires exactly two conductors (got {}) -- the \
                 MVP only supports the canonical two-port transmission-line case",
                meshes.len()
            ));
        }
        Some(resolve_ports(ports, layout.axis_lo_um, layout.axis_hi_um)?)
    };

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

        let s_parameters = match (&resolved_ports, two_conductor_line) {
            (Some(ports), Some((z0, gamma))) => Some(de_embedded_s_parameters(
                z0,
                gamma,
                layout.axis_lo_um,
                layout.axis_hi_um,
                ports,
            )?),
            _ => None,
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
            s_parameters,
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
            &[],
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
        let err =
            solve_full_wave_sweep(&conductors, 1.0, &[1.0e9], 5.0, &capacitance, &[]).unwrap_err();
        assert!(err.contains("coincide"), "unexpected error: {err}");
    }

    #[test]
    fn non_positive_frequency_is_a_clear_error() {
        let a = bar_conductor("wire", wire_box(100.0, 2.0, 2.0));
        let conductors = [a];
        let capacitance = vec![vec![1.0]];
        let err =
            solve_full_wave_sweep(&conductors, 1.0, &[0.0], 5.0, &capacitance, &[]).unwrap_err();
        assert!(err.contains("positive"), "unexpected error: {err}");
    }

    #[test]
    fn excessive_segment_count_is_a_clear_error() {
        let a = bar_conductor("wire", wire_box(1_000_000.0, 2.0, 2.0));
        let conductors = [a];
        let capacitance = vec![vec![1.0]];
        let err =
            solve_full_wave_sweep(&conductors, 1.0, &[1.0e9], 0.1, &capacitance, &[]).unwrap_err();
        assert!(err.contains("segments"), "unexpected error: {err}");
    }

    // --- ports and de-embedding (issue #894) --------------------------------

    fn port(position_um: f64, reference_impedance_ohm: f64) -> PortRequest {
        PortRequest {
            position_um,
            reference_impedance_ohm: Some(reference_impedance_ohm),
        }
    }

    /// Ports placed exactly at the modeled bar's two ends, with the
    /// reference impedance set to the line's own (model-derived)
    /// characteristic impedance -- a matched line. This is an algebraic
    /// identity of the ABCD-to-S conversion (independent of how physically
    /// accurate `Z0` is): `S11 == S22 == 0`, `S21 == S12 == exp(-gamma*L)`.
    /// This is also the "no feed to de-embed" reduction (`line_abcd`'s
    /// `length_m == 0` case): with ports at the ends, `de_embedded_s_parameters`
    /// should give exactly the same answer as the total line's own
    /// unmodified ABCD matrix.
    #[test]
    fn ports_at_the_ends_with_matched_impedance_give_a_reflectionless_matched_line() {
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
        let panels = crate::geometry::discretize(&conductors, 2.0).unwrap();
        let capacitance_matrix_ff =
            crate::solver::solve_capacitance_matrix_ff(&panels, 2, 1.0).unwrap();

        // First pass (no ports) to read off this line's own model-derived
        // Z0 -- then re-run with ports at the ends, reference impedance set
        // to exactly that Z0.
        let frequencies_hz = [1.0e9_f64];
        let (points, _) = solve_full_wave_sweep(
            &conductors,
            1.0,
            &frequencies_hz,
            5.0,
            &capacitance_matrix_ff,
            &[],
        )
        .unwrap();
        let z0_re = points[0].characteristic_impedance_real_ohm.unwrap();

        let ports = [port(0.0, z0_re), port(length_um, z0_re)];
        let (points, _) = solve_full_wave_sweep(
            &conductors,
            1.0,
            &frequencies_hz,
            5.0,
            &capacitance_matrix_ff,
            &ports,
        )
        .unwrap();
        let s = points[0].s_parameters.as_ref().unwrap();

        let s11 = Complex64::new(s.s11_real, s.s11_imag);
        let s22 = Complex64::new(s.s22_real, s.s22_imag);
        let s21 = Complex64::new(s.s21_real, s.s21_imag);
        let s12 = Complex64::new(s.s12_real, s.s12_imag);

        println!("\nmatched line: S11={s11:.6e} S22={s22:.6e} S21={s21:.6} S12={s12:.6}");
        assert!(s11.norm() < 1e-9, "expected S11 ~= 0, got {s11:?}");
        assert!(s22.norm() < 1e-9, "expected S22 ~= 0, got {s22:?}");
        assert!(
            (s21 - s12).norm() < 1e-9,
            "reciprocal network should have S21 == S12: {s21:?} vs {s12:?}"
        );
        assert!(
            (s21.norm() - 1.0).abs() < 1e-3,
            "near-lossless line should have |S21| ~= 1, got {}",
            s21.norm()
        );
    }

    /// The core de-embedding regression: model a bar with feed stubs on both
    /// sides, place ports inward (stripping the stubs via de-embedding), and
    /// compare against modeling the DUT segment alone (no feed at all). If
    /// de-embedding were a no-op (the bug this guards against), the two
    /// would disagree by roughly the feed stubs' own electrical length --
    /// this is the acceptance criterion in issue #894: "de-embedding removes
    /// the port-feed's own parasitic contribution".
    #[test]
    fn de_embedding_recovers_the_dut_alone_s_parameters() {
        let (w_um, sep_um) = (2.0, 40.0);
        let (feed1_um, dut_um, feed2_um) = (150.0, 500.0, 100.0);
        let total_um = feed1_um + dut_um + feed2_um;

        let make_pair = |length_um: f64| {
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
            [go, ret]
        };

        let frequencies_hz = [1.0e9_f64];
        let reference_impedance_ohm = 450.0; // near this line's own Z0 (see the sibling test)

        // (a) DUT segment alone, ports at its own two ends (no feed to
        // de-embed -- the identity reduction).
        let dut_conductors = make_pair(dut_um);
        let dut_panels = crate::geometry::discretize(&dut_conductors, 2.0).unwrap();
        let dut_capacitance =
            crate::solver::solve_capacitance_matrix_ff(&dut_panels, 2, 1.0).unwrap();
        let dut_ports = [
            port(0.0, reference_impedance_ohm),
            port(dut_um, reference_impedance_ohm),
        ];
        let (dut_points, _) = solve_full_wave_sweep(
            &dut_conductors,
            1.0,
            &frequencies_hz,
            5.0,
            &dut_capacitance,
            &dut_ports,
        )
        .unwrap();
        let dut_s = dut_points[0].s_parameters.as_ref().unwrap();

        // (b) Total structure (feed1 + DUT + feed2), ports placed inward at
        // the DUT boundaries -- de-embedding must strip both feed stubs.
        let total_conductors = make_pair(total_um);
        let total_panels = crate::geometry::discretize(&total_conductors, 2.0).unwrap();
        let total_capacitance =
            crate::solver::solve_capacitance_matrix_ff(&total_panels, 2, 1.0).unwrap();
        let total_ports = [
            port(feed1_um, reference_impedance_ohm),
            port(feed1_um + dut_um, reference_impedance_ohm),
        ];
        let (total_points, _) = solve_full_wave_sweep(
            &total_conductors,
            1.0,
            &frequencies_hz,
            5.0,
            &total_capacitance,
            &total_ports,
        )
        .unwrap();
        let deembedded_s = total_points[0].s_parameters.as_ref().unwrap();

        let dut_s21 = Complex64::new(dut_s.s21_real, dut_s.s21_imag);
        let deembedded_s21 = Complex64::new(deembedded_s.s21_real, deembedded_s.s21_imag);
        let dut_s11 = Complex64::new(dut_s.s11_real, dut_s.s11_imag);
        let deembedded_s11 = Complex64::new(deembedded_s.s11_real, deembedded_s.s11_imag);

        println!(
            "\nde-embedding: DUT-alone S21={dut_s21:.6} vs de-embedded S21={deembedded_s21:.6} \
             (DUT-alone S11={dut_s11:.3e} vs de-embedded S11={deembedded_s11:.3e})"
        );
        // Same medium (uniform line) on both sides of this comparison -- the
        // only difference is finite-length end-effects at a different total
        // modeled length. Measured agreement is ~0.02% (both S21 and S11);
        // 5% leaves ample margin over that while still being tight enough
        // to catch a broken/no-op de-embedding step (which would show
        // ~feed-stub-electrical-length-sized error instead, comfortably
        // exceeding this bound -- see the raw-ABCD sanity check below).
        let s21_rel_err = (deembedded_s21 - dut_s21).norm() / dut_s21.norm();
        assert!(
            s21_rel_err < 0.05,
            "de-embedded S21 {deembedded_s21:?} should match the DUT-alone S21 {dut_s21:?} \
             (rel.err {:.4}% exceeds 5%) -- de-embedding did not correctly strip the feed \
             stubs' contribution",
            s21_rel_err * 100.0
        );
        assert!(
            deembedded_s11.norm() < 0.05,
            "de-embedded S11 should stay small (near-matched reference impedance), got \
             {deembedded_s11:?}"
        );

        // The un-de-embedded sanity check: the *raw* total-line ABCD (ports
        // at the true axial ends, i.e. spanning feed1+DUT+feed2) must NOT
        // match the DUT-alone answer -- otherwise this test would pass
        // trivially even with de-embedding disabled/broken.
        let raw_ports = [
            port(0.0, reference_impedance_ohm),
            port(total_um, reference_impedance_ohm),
        ];
        let (raw_points, _) = solve_full_wave_sweep(
            &total_conductors,
            1.0,
            &frequencies_hz,
            5.0,
            &total_capacitance,
            &raw_ports,
        )
        .unwrap();
        let raw_s21 = Complex64::new(
            raw_points[0].s_parameters.as_ref().unwrap().s21_real,
            raw_points[0].s_parameters.as_ref().unwrap().s21_imag,
        );
        let raw_rel_err = (raw_s21 - dut_s21).norm() / dut_s21.norm();
        assert!(
            raw_rel_err > s21_rel_err,
            "sanity check failed: the un-de-embedded (full-length) S21 {raw_s21:?} should be \
             further from the DUT-alone answer {dut_s21:?} than the de-embedded one was -- \
             otherwise this test has no power to catch broken de-embedding"
        );
    }

    #[test]
    fn wrong_port_count_is_a_clear_error() {
        let a = bar_conductor("go", wire_box(100.0, 2.0, 2.0));
        let b = bar_conductor(
            "return",
            BoxRequest {
                x0_um: 0.0,
                y0_um: 20.0,
                x1_um: 100.0,
                y1_um: 22.0,
                z0_um: 0.0,
                z1_um: 2.0,
            },
        );
        let conductors = [a, b];
        let capacitance = vec![vec![1.0, -0.5], vec![-0.5, 1.0]];
        let ports = [port(0.0, 50.0)];
        let err = solve_full_wave_sweep(&conductors, 1.0, &[1.0e9], 5.0, &capacitance, &ports)
            .unwrap_err();
        assert!(err.contains("exactly two ports"), "unexpected error: {err}");
    }

    #[test]
    fn port_position_outside_the_modeled_span_is_a_clear_error() {
        let a = bar_conductor("go", wire_box(100.0, 2.0, 2.0));
        let b = bar_conductor(
            "return",
            BoxRequest {
                x0_um: 0.0,
                y0_um: 20.0,
                x1_um: 100.0,
                y1_um: 22.0,
                z0_um: 0.0,
                z1_um: 2.0,
            },
        );
        let conductors = [a, b];
        let capacitance = vec![vec![1.0, -0.5], vec![-0.5, 1.0]];
        let ports = [port(0.0, 50.0), port(150.0, 50.0)];
        let err = solve_full_wave_sweep(&conductors, 1.0, &[1.0e9], 5.0, &capacitance, &ports)
            .unwrap_err();
        assert!(
            err.contains("outside the modeled bar span"),
            "unexpected error: {err}"
        );
    }

    #[test]
    fn non_ascending_port_order_is_a_clear_error() {
        let a = bar_conductor("go", wire_box(100.0, 2.0, 2.0));
        let b = bar_conductor(
            "return",
            BoxRequest {
                x0_um: 0.0,
                y0_um: 20.0,
                x1_um: 100.0,
                y1_um: 22.0,
                z0_um: 0.0,
                z1_um: 2.0,
            },
        );
        let conductors = [a, b];
        let capacitance = vec![vec![1.0, -0.5], vec![-0.5, 1.0]];
        let ports = [port(80.0, 50.0), port(20.0, 50.0)];
        let err = solve_full_wave_sweep(&conductors, 1.0, &[1.0e9], 5.0, &capacitance, &ports)
            .unwrap_err();
        assert!(
            err.contains("ascending axial order"),
            "unexpected error: {err}"
        );
    }

    #[test]
    fn ports_require_exactly_two_conductors() {
        let a = bar_conductor("solo", wire_box(100.0, 2.0, 2.0));
        let conductors = [a];
        let capacitance = vec![vec![1.0]];
        let ports = [port(0.0, 50.0), port(100.0, 50.0)];
        let err = solve_full_wave_sweep(&conductors, 1.0, &[1.0e9], 5.0, &capacitance, &ports)
            .unwrap_err();
        assert!(
            err.contains("exactly two conductors"),
            "unexpected error: {err}"
        );
    }
}
