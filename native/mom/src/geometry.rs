//! Geometry discretisation, in two independent flavours:
//!
//! - [`discretize`] -- *surface* discretisation: each conductor's
//!   axis-aligned box(es) become a flat set of constant-charge-density
//!   panels for the potential-coefficient matrix fill in `solver.rs`.
//! - [`discretize_filaments`] -- *volumetric* discretisation: each box
//!   becomes one or more current-carrying rectangular bars (filaments) with
//!   a defined current-flow axis, for the PEEC partial-inductance fill in
//!   `peec.rs`.
//!
//! The two are deliberately separate rather than one shared mesh. A surface
//! panel is a zero-thickness lamina with an area and no cross-section; a
//! PEEC filament is a solid bar whose cross-sectional area is exactly what
//! current is pushed through. A flat plate is a perfectly good capacitance
//! conductor and a nonsensical current path, so the capacitance path keeps
//! accepting `z0_um == z1_um` while the PEEC path rejects it (issue #797).

use crate::contract::{BoxRequest, ConductorRequest};

/// One flat rectangular panel: a constant-charge-density collocation point
/// (`center`) with an `area` (um^2), tagged with the index of the conductor
/// it belongs to (an index into the caller's conductor-name list).
#[derive(Debug, Clone, Copy)]
pub struct Panel {
    pub center: [f64; 3],
    pub area_um2: f64,
    pub conductor_index: usize,
}

/// Minimum extent (um) below which a box dimension is treated as exactly
/// zero (a flat plate / a degenerate wall to skip), guarding against
/// floating-point noise in caller-supplied coordinates rather than requiring
/// exact equality.
const EPS_UM: f64 = 1e-9;

/// Upper bound on total panel count. The dense potential-coefficient matrix
/// is `O(n^2)` in both memory and fill/solve time (a full matrix at this cap
/// is `8000^2 * 8 bytes` ~= 512 MiB), so an unreasonably fine
/// `panel_size_um` relative to a large conductor -- most likely a caller
/// mismatching scale between a tiny and a huge conductor in the same request
/// -- fails fast with a clear message instead of exhausting memory or
/// hanging on the `O(n^3)` LU solve. MVP conductor counts (a handful of
/// plates/coax cross-sections) stay far under this cap at the default
/// `panel_size_um`; raise it, or increase `panel_size_um`, if a legitimate
/// geometry needs more panels than this.
const MAX_PANELS: usize = 8000;

/// Discretise every conductor's boxes into panels, target panel edge length
/// `panel_size_um`. Returns an error string (surfaced to the caller as a
/// `ValueError`) if a conductor has no boxes, a box is a degenerate point/line
/// (zero area on every face), `panel_size_um` is not positive, or the
/// resulting panel count would exceed `MAX_PANELS`.
pub fn discretize(
    conductors: &[ConductorRequest],
    panel_size_um: f64,
) -> Result<Vec<Panel>, String> {
    if panel_size_um <= 0.0 {
        return Err(format!(
            "panel_size_um must be positive, got {panel_size_um}"
        ));
    }
    if conductors.is_empty() {
        return Err("at least one conductor is required".to_string());
    }

    // Count the panels each box *would* produce before generating any of
    // them -- a caller mismatching a very fine panel_size_um against a very
    // large conductor can imply an astronomical panel count (e.g. 1e12),
    // and materialising that many `Panel`s (even transiently, before a
    // post-hoc length check) is exactly the OOM/hang this guard exists to
    // avoid. `estimated_panel_count` uses saturating arithmetic, so a
    // request that would overflow lands well above `MAX_PANELS` and is
    // rejected without ever allocating.
    let mut estimated_total: u64 = 0;
    for conductor in conductors {
        if conductor.boxes.is_empty() {
            return Err(format!(
                "conductor {:?} has no boxes -- every conductor needs at least one",
                conductor.name
            ));
        }
        for b in &conductor.boxes {
            estimated_total =
                estimated_total.saturating_add(estimated_panel_count(b, panel_size_um));
        }
        if estimated_total > MAX_PANELS as u64 {
            return Err(format!(
                "geometry would discretise into more than {MAX_PANELS} panels \
                 (a dense O(n^2) solve at that size risks exhausting memory) \
                 -- increase panel_size_um, or check for a large scale mismatch \
                 between conductors in this request"
            ));
        }
    }

    let mut panels = Vec::new();
    for (conductor_index, conductor) in conductors.iter().enumerate() {
        let before = panels.len();
        for b in &conductor.boxes {
            discretize_box(b, panel_size_um, conductor_index, &mut panels);
        }
        if panels.len() == before {
            return Err(format!(
                "conductor {:?} produced zero panels -- every box is a degenerate point or line (check x0/x1, y0/y1)",
                conductor.name
            ));
        }
    }
    Ok(panels)
}

/// Upper bound on the number of panels `discretize_box` would emit for `b`,
/// computed without generating any of them (see `discretize`'s early guard).
/// Saturates at `u64::MAX` rather than overflowing/panicking on pathological
/// (huge extent, tiny panel size) inputs.
fn estimated_panel_count(b: &BoxRequest, panel_size_um: f64) -> u64 {
    let (x0, x1) = minmax(b.x0_um, b.x1_um);
    let (y0, y1) = minmax(b.y0_um, b.y1_um);
    let (z0, z1) = minmax(b.z0_um, b.z1_um);
    let flat = (z1 - z0).abs() < EPS_UM;

    let nx = face_segment_count(x1 - x0, panel_size_um);
    let ny = face_segment_count(y1 - y0, panel_size_um);
    let nz = face_segment_count(z1 - z0, panel_size_um);

    let xy_face = nx.saturating_mul(ny);
    if flat {
        return xy_face;
    }
    let yz_face = ny.saturating_mul(nz);
    let xz_face = nx.saturating_mul(nz);
    // top + bottom (xy_face each) + 2x side walls in each of the other two
    // orientations.
    xy_face
        .saturating_mul(2)
        .saturating_add(yz_face.saturating_mul(2))
        .saturating_add(xz_face.saturating_mul(2))
}

/// Segment count along one axis for `subdivide_1d`'s `n`, as a `u64` (a
/// zero/near-zero extent -- a degenerate wall `emit_face` skips entirely --
/// contributes `0`, matching that skip rather than the `.max(1)` floor
/// `subdivide_1d` itself applies to a *non-degenerate* extent).
fn face_segment_count(extent: f64, panel_size_um: f64) -> u64 {
    if extent.abs() < EPS_UM {
        return 0;
    }
    let n = (extent.abs() / panel_size_um).round();
    if !n.is_finite() || n < 1.0 {
        1
    } else {
        n as u64
    }
}

/// Subdivide a box's surface into panels appended to `out`.
///
/// A box with `z0_um == z1_um` is a flat plate: a single face in the XY
/// plane is emitted (not six coincident faces, which would make the
/// potential-coefficient matrix singular). A box with `z1_um > z0_um` emits
/// all six faces of the rectangular prism; any face with zero area (e.g. a
/// box that is itself a zero-width wall in X or Y) is silently skipped.
fn discretize_box(
    b: &BoxRequest,
    panel_size_um: f64,
    conductor_index: usize,
    out: &mut Vec<Panel>,
) {
    let (x0, x1) = minmax(b.x0_um, b.x1_um);
    let (y0, y1) = minmax(b.y0_um, b.y1_um);
    let (z0, z1) = minmax(b.z0_um, b.z1_um);

    let flat = (z1 - z0).abs() < EPS_UM;

    // Top/bottom (XY) faces.
    emit_face(
        (x0, x1),
        (y0, y1),
        panel_size_um,
        |u, v| [u, v, z1],
        conductor_index,
        out,
    );
    if !flat {
        emit_face(
            (x0, x1),
            (y0, y1),
            panel_size_um,
            |u, v| [u, v, z0],
            conductor_index,
            out,
        );
        // Side walls: X-normal (YZ faces) at x0 and x1.
        emit_face(
            (y0, y1),
            (z0, z1),
            panel_size_um,
            |u, v| [x0, u, v],
            conductor_index,
            out,
        );
        emit_face(
            (y0, y1),
            (z0, z1),
            panel_size_um,
            |u, v| [x1, u, v],
            conductor_index,
            out,
        );
        // Side walls: Y-normal (XZ faces) at y0 and y1.
        emit_face(
            (x0, x1),
            (z0, z1),
            panel_size_um,
            |u, v| [u, y0, v],
            conductor_index,
            out,
        );
        emit_face(
            (x0, x1),
            (z0, z1),
            panel_size_um,
            |u, v| [u, y1, v],
            conductor_index,
            out,
        );
    }
}

fn minmax(a: f64, b: f64) -> (f64, f64) {
    if a <= b {
        (a, b)
    } else {
        (b, a)
    }
}

/// Subdivide a `(u0,u1) x (v0,v1)` rectangular face into a grid of panels,
/// mapping each `(u_center, v_center)` collocation point to 3D via `to_xyz`.
/// Skips the face entirely if either extent is ~zero (a degenerate wall).
fn emit_face(
    (u0, u1): (f64, f64),
    (v0, v1): (f64, f64),
    panel_size_um: f64,
    to_xyz: impl Fn(f64, f64) -> [f64; 3],
    conductor_index: usize,
    out: &mut Vec<Panel>,
) {
    let du = u1 - u0;
    let dv = v1 - v0;
    if du.abs() < EPS_UM || dv.abs() < EPS_UM {
        return;
    }
    for (uc, ulen) in subdivide_1d(u0, u1, panel_size_um) {
        for (vc, vlen) in subdivide_1d(v0, v1, panel_size_um) {
            out.push(Panel {
                center: to_xyz(uc, vc),
                area_um2: ulen * vlen,
                conductor_index,
            });
        }
    }
}

/// Split `[lo, hi]` into `n = max(1, round((hi-lo)/panel_size)))` equal
/// segments, returning each segment's `(center, length)`.
fn subdivide_1d(lo: f64, hi: f64, panel_size: f64) -> Vec<(f64, f64)> {
    let extent = hi - lo;
    let n = ((extent / panel_size).round() as usize).max(1);
    let len = extent / n as f64;
    (0..n).map(|i| (lo + (i as f64 + 0.5) * len, len)).collect()
}

// --- volumetric (PEEC) discretisation ---------------------------------------

/// One rectangular current-carrying bar: an axis-aligned volume plus the
/// axis its current flows along.
///
/// `weight` is the fraction of its parent box's total current this filament
/// carries. Sub-filaments split *across* the box's cross-section share the
/// current (uniform DC current density is assumed), while sub-filaments
/// split *along* the current-flow axis are in series and each carry the
/// whole of it -- so `weight` is `1 / n_cross` for every filament of a box
/// split into `n_cross` parallel cross-sectional cells, regardless of how
/// many segments it was cut into lengthwise. With that weighting,
/// `sum_ij w_i w_j Lp_ij` reproduces the parent box's partial inductance
/// exactly (the volume double integral splits additively over sub-volumes;
/// see `peec.rs`).
#[derive(Debug, Clone, Copy)]
pub struct Filament {
    pub lo: [f64; 3],
    pub hi: [f64; 3],
    /// Current-flow axis: `0` = x, `1` = y, `2` = z.
    pub axis: usize,
    pub weight: f64,
    pub conductor_index: usize,
}

impl Filament {
    /// Length along the current-flow axis, micrometers.
    pub fn length_um(&self) -> f64 {
        self.hi[self.axis] - self.lo[self.axis]
    }

    /// Cross-sectional area perpendicular to the current-flow axis,
    /// micrometers squared.
    pub fn cross_area_um2(&self) -> f64 {
        let (u, v) = transverse_axes(self.axis);
        (self.hi[u] - self.lo[u]) * (self.hi[v] - self.lo[v])
    }
}

/// The two axes perpendicular to `axis`, in ascending order.
pub fn transverse_axes(axis: usize) -> (usize, usize) {
    match axis {
        0 => (1, 2),
        1 => (0, 2),
        _ => (0, 1),
    }
}

/// Upper bound on total filament count. The partial-inductance matrix is
/// dense and `O(n^2)` to fill, and each entry costs a 64-term closed-form
/// evaluation, so this cap is a good deal tighter than `MAX_PANELS` -- it is
/// sized so a worst-case fill stays around a second, not so a matrix fits in
/// memory. `filament_subdivisions` multiplies the box count by `n^3`, which
/// is the usual way to hit it.
const MAX_FILAMENTS: usize = 512;

/// Aspect ratio (longest box extent / next-longest) below which "the
/// longest axis is the current-flow axis" stops being a defensible reading
/// of the geometry. See `discretize_filaments`' doc comment.
const MIN_BAR_ASPECT_RATIO: f64 = 2.0;

/// Discretise every conductor's boxes into current-carrying filaments,
/// splitting each box into `subdivisions^3` sub-bars (`subdivisions` along
/// the current-flow axis and along each transverse axis).
///
/// **Current-flow direction is taken to be each box's longest axis.** The
/// MVP's boxes carry no port or direction information (`BoxRequest` is six
/// coordinates and nothing else), so the direction has to be inferred, and
/// "current runs the long way down a wire" is the standard PEEC MVP reading
/// for bar-shaped conductors. It is *not* defensible for a box that is not
/// bar-shaped -- a square pad has no long way -- so a box whose longest
/// extent is less than `MIN_BAR_ASPECT_RATIO` times its next-longest gets a
/// warning naming the conductor. It is a warning rather than a hard error to
/// match the precedent `solver::physicality_warnings` already sets for the
/// capacitance path: return the numbers, and say plainly that they should
/// not be trusted.
///
/// Returns `(filaments, warnings)`, or an error string when a conductor has
/// no boxes, a box has zero extent along any axis (a flat plate has no
/// cross-section to push current through), `subdivisions` is out of range,
/// or the filament count would exceed `MAX_FILAMENTS`.
pub fn discretize_filaments(
    conductors: &[ConductorRequest],
    subdivisions: usize,
) -> Result<(Vec<Filament>, Vec<String>), String> {
    if conductors.is_empty() {
        return Err("at least one conductor is required".to_string());
    }
    if subdivisions == 0 {
        return Err("filament_subdivisions must be at least 1".to_string());
    }
    if subdivisions > crate::contract::MAX_FILAMENT_SUBDIVISIONS {
        return Err(format!(
            "filament_subdivisions must be at most {} (each box becomes \
             subdivisions^3 filaments, and the partial-inductance fill is \
             O(filaments^2)), got {subdivisions}",
            crate::contract::MAX_FILAMENT_SUBDIVISIONS
        ));
    }

    let box_count: usize = conductors.iter().map(|c| c.boxes.len()).sum();
    let per_box = subdivisions.saturating_pow(3);
    if box_count.saturating_mul(per_box) > MAX_FILAMENTS {
        return Err(format!(
            "geometry would discretise into more than {MAX_FILAMENTS} PEEC \
             filaments ({box_count} boxes x {per_box} filaments each) -- the \
             partial-inductance fill is dense and O(n^2); reduce \
             filament_subdivisions or split the request"
        ));
    }

    let mut filaments = Vec::with_capacity(box_count * per_box);
    let mut warnings = Vec::new();
    for (conductor_index, conductor) in conductors.iter().enumerate() {
        if conductor.boxes.is_empty() {
            return Err(format!(
                "conductor {:?} has no boxes -- every conductor needs at least one",
                conductor.name
            ));
        }
        for b in &conductor.boxes {
            let bounds = sorted_bounds(b);
            let extents = [
                bounds[0].1 - bounds[0].0,
                bounds[1].1 - bounds[1].0,
                bounds[2].1 - bounds[2].0,
            ];
            if let Some(flat) = extents.iter().position(|e| *e < EPS_UM) {
                return Err(format!(
                    "conductor {:?} has a box with zero extent along {} -- a PEEC \
                     current path needs a real cross-sectional area, so a \
                     zero-thickness (z0_um == z1_um) plate cannot carry current; \
                     give it a thickness, or drop conductivity_S_per_m to run the \
                     capacitance-only solve",
                    conductor.name,
                    axis_name(flat)
                ));
            }

            let axis = longest_axis(&extents);
            let (u, v) = transverse_axes(axis);
            let next_longest = extents[u].max(extents[v]);
            if extents[axis] < MIN_BAR_ASPECT_RATIO * next_longest {
                warnings.push(format!(
                    "conductor {:?} has a box that is not bar-shaped ({:.4} x {:.4} \
                     x {:.4} um, aspect ratio {:.2}:1) -- PEEC takes the longest \
                     axis ({}) as the current-flow direction, which is only \
                     meaningful for a wire-like box; treat its inductance and \
                     resistance contribution as unreliable",
                    conductor.name,
                    extents[0],
                    extents[1],
                    extents[2],
                    extents[axis] / next_longest,
                    axis_name(axis)
                ));
            }

            let weight = 1.0 / (subdivisions * subdivisions) as f64;
            let cuts: Vec<Vec<(f64, f64)>> = (0..3)
                .map(|k| split_axis(bounds[k].0, bounds[k].1, subdivisions))
                .collect();
            for &(x0, x1) in &cuts[0] {
                for &(y0, y1) in &cuts[1] {
                    for &(z0, z1) in &cuts[2] {
                        filaments.push(Filament {
                            lo: [x0, y0, z0],
                            hi: [x1, y1, z1],
                            axis,
                            weight,
                            conductor_index,
                        });
                    }
                }
            }
        }
    }
    Ok((filaments, warnings))
}

fn sorted_bounds(b: &BoxRequest) -> [(f64, f64); 3] {
    [
        minmax(b.x0_um, b.x1_um),
        minmax(b.y0_um, b.y1_um),
        minmax(b.z0_um, b.z1_um),
    ]
}

fn axis_name(axis: usize) -> &'static str {
    match axis {
        0 => "x",
        1 => "y",
        _ => "z",
    }
}

/// Index of the largest of three extents; ties resolve to the lowest axis
/// index, so the choice is deterministic for a cube.
fn longest_axis(extents: &[f64; 3]) -> usize {
    let mut best = 0;
    for (i, e) in extents.iter().enumerate() {
        if *e > extents[best] {
            best = i;
        }
    }
    best
}

/// Split `[lo, hi]` into exactly `n` equal `(lo, hi)` sub-intervals.
fn split_axis(lo: f64, hi: f64, n: usize) -> Vec<(f64, f64)> {
    let step = (hi - lo) / n as f64;
    (0..n)
        .map(|i| (lo + i as f64 * step, lo + (i + 1) as f64 * step))
        .collect()
}

/// Per-conductor DC series resistance in ohms, assembled from the same
/// filaments the partial-inductance fill uses:
/// `R = sum_i w_i^2 * l_i / (sigma * A_i)`.
///
/// That weighting is the exact series/parallel reduction of a box cut into
/// `n^3` sub-bars, and it collapses to the textbook `l / (sigma * A)` for
/// the whole box regardless of `filament_subdivisions` -- `n_axial` segments
/// in series, each `n_cross` cells in parallel:
/// `n_axial * (l/n_axial) / (sigma * (A/n_cross) * n_cross) = l / (sigma A)`.
/// It is expressed per filament rather than per box so inductance and
/// resistance are read off one shared discretisation (`peec.rs` assembles
/// `sum_ij w_i w_j Lp_ij` from the same weights), which is what makes
/// `resistance_is_independent_of_filament_subdivisions` a meaningful check
/// rather than a tautology about box arithmetic.
///
/// **The boxes of one conductor are assumed to be in series** along the
/// current path (the usual case for a routed net expressed as a chain of
/// segments). Boxes that are actually in *parallel* are not detected, so
/// their resistance is over-counted -- documented in docs/cli/mom.md's
/// "Scope and limitations" rather than guessed at, since the MVP has no
/// connectivity information to tell the two apart.
pub fn dc_resistance_ohm(
    conductors: &[ConductorRequest],
    filaments: &[Filament],
) -> Result<Vec<f64>, String> {
    let mut sigmas = Vec::with_capacity(conductors.len());
    for conductor in conductors {
        let sigma = conductor
            .conductivity_s_per_m
            .ok_or_else(|| format!("conductor {:?} has no conductivity_S_per_m", conductor.name))?;
        if sigma <= 0.0 || !sigma.is_finite() {
            return Err(format!(
                "conductor {:?} has a non-positive or non-finite \
                 conductivity_S_per_m ({sigma})",
                conductor.name
            ));
        }
        sigmas.push(sigma);
    }

    let mut totals = vec![0.0; conductors.len()];
    for filament in filaments {
        let index = filament.conductor_index;
        // length [um] / (sigma [S/m] * area [um^2]) with um -> m:
        // (l * 1e-6) / (sigma * A * 1e-12) = 1e6 * l / (sigma * A).
        totals[index] += filament.weight * filament.weight * 1e6 * filament.length_um()
            / (sigmas[index] * filament.cross_area_um2());
    }
    Ok(totals)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn one_box_conductor(name: &str, b: BoxRequest) -> ConductorRequest {
        ConductorRequest {
            name: name.to_string(),
            boxes: vec![b],
            conductivity_s_per_m: None,
        }
    }

    #[test]
    fn flat_plate_emits_one_face_only() {
        let c = one_box_conductor(
            "plate",
            BoxRequest {
                x0_um: 0.0,
                y0_um: 0.0,
                x1_um: 2.0,
                y1_um: 2.0,
                z0_um: 1.0,
                z1_um: 1.0,
            },
        );
        let panels = discretize(&[c], 1.0).unwrap();
        // 2x2 um face at panel_size 1.0um -> 2x2 = 4 panels, all at z=1.0.
        assert_eq!(panels.len(), 4);
        assert!(panels.iter().all(|p| (p.center[2] - 1.0).abs() < 1e-12));
        let total_area: f64 = panels.iter().map(|p| p.area_um2).sum();
        assert!((total_area - 4.0).abs() < 1e-9);
    }

    #[test]
    fn thick_box_emits_six_faces() {
        let c = one_box_conductor(
            "box",
            BoxRequest {
                x0_um: 0.0,
                y0_um: 0.0,
                x1_um: 1.0,
                y1_um: 1.0,
                z0_um: 0.0,
                z1_um: 1.0,
            },
        );
        let panels = discretize(&[c], 0.5).unwrap();
        // Each face is 1x1um subdivided into 2x2 = 4 panels; 6 faces -> 24.
        assert_eq!(panels.len(), 24);
        let total_area: f64 = panels.iter().map(|p| p.area_um2).sum();
        // Surface area of a 1x1x1 cube = 6.
        assert!((total_area - 6.0).abs() < 1e-9);
    }

    #[test]
    fn empty_conductors_is_an_error() {
        assert!(discretize(&[], 1.0).is_err());
    }

    #[test]
    fn zero_panel_size_is_an_error() {
        let c = one_box_conductor(
            "plate",
            BoxRequest {
                x0_um: 0.0,
                y0_um: 0.0,
                x1_um: 1.0,
                y1_um: 1.0,
                z0_um: 0.0,
                z1_um: 0.0,
            },
        );
        assert!(discretize(&[c], 0.0).is_err());
    }

    #[test]
    fn degenerate_point_box_is_an_error() {
        let c = one_box_conductor(
            "point",
            BoxRequest {
                x0_um: 1.0,
                y0_um: 1.0,
                x1_um: 1.0,
                y1_um: 1.0,
                z0_um: 0.0,
                z1_um: 0.0,
            },
        );
        assert!(discretize(&[c], 1.0).is_err());
    }

    #[test]
    fn excessive_panel_count_is_a_clean_error_not_a_panic() {
        // A huge plate discretised far too finely relative to its size --
        // e.g. a mismatched-scale request pairing a tiny conductor's
        // panel_size_um with a much larger one -- must fail fast with a
        // message, not attempt an O(n^2) allocation.
        let c = one_box_conductor(
            "huge",
            BoxRequest {
                x0_um: 0.0,
                y0_um: 0.0,
                x1_um: 1000.0,
                y1_um: 1000.0,
                z0_um: 0.0,
                z1_um: 0.0,
            },
        );
        let err = discretize(&[c], 0.5).unwrap_err();
        assert!(err.contains("panels"), "unexpected error message: {err}");
    }

    #[test]
    fn mismatched_scale_rejected_without_materialising_panels() {
        // A tiny (nm-scale) conductor paired with a very large plate at a
        // panel size sized for the tiny one implies an astronomical (e.g.
        // ~1e12) panel count -- must be rejected by the pre-count guard
        // before any allocation is attempted, or this test would hang/OOM
        // rather than complete quickly.
        let tiny = one_box_conductor(
            "tiny",
            BoxRequest {
                x0_um: 0.0,
                y0_um: 0.0,
                x1_um: 0.002,
                y1_um: 0.002,
                z0_um: 0.0,
                z1_um: 0.0,
            },
        );
        let big = one_box_conductor(
            "big",
            BoxRequest {
                x0_um: 0.0,
                y0_um: 0.0,
                x1_um: 1000.0,
                y1_um: 1000.0,
                z0_um: 500.0,
                z1_um: 500.0,
            },
        );
        let err = discretize(&[tiny, big], 0.001).unwrap_err();
        assert!(err.contains("panels"), "unexpected error message: {err}");
    }

    // --- volumetric (PEEC) discretisation ------------------------------------

    /// Copper, in S/m -- the value the docs quote as the worked example.
    const COPPER_S_PER_M: f64 = 5.8e7;

    fn wire(name: &str, length_um: f64, w_um: f64, t_um: f64) -> ConductorRequest {
        ConductorRequest {
            name: name.to_string(),
            boxes: vec![BoxRequest {
                x0_um: 0.0,
                y0_um: 0.0,
                x1_um: length_um,
                y1_um: w_um,
                z0_um: 0.0,
                z1_um: t_um,
            }],
            conductivity_s_per_m: Some(COPPER_S_PER_M),
        }
    }

    #[test]
    fn a_bar_becomes_one_filament_along_its_longest_axis() {
        let (filaments, warnings) = discretize_filaments(&[wire("m1", 20.0, 1.0, 0.5)], 1).unwrap();
        assert_eq!(filaments.len(), 1);
        assert!(warnings.is_empty(), "unexpected warnings: {warnings:?}");
        let f = &filaments[0];
        assert_eq!(f.axis, 0, "current must run along the 20 um x extent");
        assert_relative_eq(f.length_um(), 20.0);
        assert_relative_eq(f.cross_area_um2(), 0.5);
        assert_relative_eq(f.weight, 1.0);
    }

    #[test]
    fn subdivisions_split_a_box_into_n_cubed_filaments_with_cross_sectional_weights() {
        let (filaments, _) = discretize_filaments(&[wire("m1", 20.0, 1.0, 0.5)], 3).unwrap();
        assert_eq!(filaments.len(), 27);
        // 3 cross-sectional cuts per transverse axis => 9 parallel paths.
        assert!(filaments
            .iter()
            .all(|f| (f.weight - 1.0 / 9.0).abs() < 1e-12));
        // The pieces tile the parent exactly.
        let volume: f64 = filaments
            .iter()
            .map(|f| (f.hi[0] - f.lo[0]) * (f.hi[1] - f.lo[1]) * (f.hi[2] - f.lo[2]))
            .sum();
        assert_relative_eq(volume, 20.0 * 1.0 * 0.5);
    }

    #[test]
    fn a_zero_thickness_plate_cannot_carry_current() {
        let plate = ConductorRequest {
            name: "plate".to_string(),
            boxes: vec![BoxRequest {
                x0_um: 0.0,
                y0_um: 0.0,
                x1_um: 10.0,
                y1_um: 10.0,
                z0_um: 1.0,
                z1_um: 1.0,
            }],
            conductivity_s_per_m: Some(COPPER_S_PER_M),
        };
        let err = discretize_filaments(&[plate], 1).unwrap_err();
        assert!(err.contains("zero extent along z"), "got: {err}");
        assert!(err.contains("capacitance-only"), "got: {err}");
    }

    #[test]
    fn a_non_bar_shaped_box_warns_about_the_current_direction_assumption() {
        // A 10 x 8 x 0.5 um pad: the longest axis beats the next-longest by
        // only 1.25:1, so "current runs the long way" is not defensible.
        let pad = ConductorRequest {
            name: "pad".to_string(),
            boxes: vec![BoxRequest {
                x0_um: 0.0,
                y0_um: 0.0,
                x1_um: 10.0,
                y1_um: 8.0,
                z0_um: 0.0,
                z1_um: 0.5,
            }],
            conductivity_s_per_m: Some(COPPER_S_PER_M),
        };
        let (filaments, warnings) = discretize_filaments(&[pad], 1).unwrap();
        assert_eq!(filaments.len(), 1);
        assert_eq!(warnings.len(), 1);
        assert!(warnings[0].contains("\"pad\""), "got: {}", warnings[0]);
        assert!(
            warnings[0].contains("not bar-shaped"),
            "got: {}",
            warnings[0]
        );
    }

    #[test]
    fn filament_subdivisions_out_of_range_are_rejected() {
        let c = wire("m1", 20.0, 1.0, 0.5);
        assert!(discretize_filaments(std::slice::from_ref(&c), 0)
            .unwrap_err()
            .contains("at least 1"));
        assert!(discretize_filaments(
            std::slice::from_ref(&c),
            crate::contract::MAX_FILAMENT_SUBDIVISIONS + 1
        )
        .unwrap_err()
        .contains("at most"));
    }

    #[test]
    fn excessive_filament_count_is_a_clean_error_not_a_panic() {
        let many: Vec<ConductorRequest> = (0..20)
            .map(|i| wire(&format!("m{i}"), 20.0, 1.0, 0.5))
            .collect();
        // 20 boxes x 8^3 = 10240 filaments, well past the cap.
        let err = discretize_filaments(&many, 8).unwrap_err();
        assert!(err.contains("filaments"), "unexpected error: {err}");
    }

    #[test]
    fn dc_resistance_matches_the_exact_closed_form() {
        // R = l / (sigma A). l = 20 um, A = 1 x 0.5 um^2, sigma = 5.8e7 S/m
        //   = 20e-6 / (5.8e7 * 0.5e-12) = 0.689655... ohm.
        let c = wire("m1", 20.0, 1.0, 0.5);
        let (filaments, _) = discretize_filaments(std::slice::from_ref(&c), 1).unwrap();
        let r = dc_resistance_ohm(std::slice::from_ref(&c), &filaments).unwrap();
        let exact = 20e-6 / (COPPER_S_PER_M * 1.0e-6 * 0.5e-6);
        assert!(
            (r[0] - exact).abs() / exact < 1e-12,
            "got {} want {exact}",
            r[0]
        );
    }

    #[test]
    fn resistance_is_independent_of_filament_subdivisions() {
        let c = wire("m1", 20.0, 1.0, 0.5);
        let reference = {
            let (f, _) = discretize_filaments(std::slice::from_ref(&c), 1).unwrap();
            dc_resistance_ohm(std::slice::from_ref(&c), &f).unwrap()[0]
        };
        for n in [2, 3, 4] {
            let (f, _) = discretize_filaments(std::slice::from_ref(&c), n).unwrap();
            let r = dc_resistance_ohm(std::slice::from_ref(&c), &f).unwrap()[0];
            assert!(
                (r - reference).abs() / reference < 1e-12,
                "n = {n}: {r} vs {reference}"
            );
        }
    }

    #[test]
    fn boxes_of_one_conductor_add_in_series() {
        let mut c = wire("m1", 20.0, 1.0, 0.5);
        c.boxes.push(BoxRequest {
            x0_um: 20.0,
            y0_um: 0.0,
            x1_um: 30.0,
            y1_um: 1.0,
            z0_um: 0.0,
            z1_um: 0.5,
        });
        let (filaments, _) = discretize_filaments(std::slice::from_ref(&c), 1).unwrap();
        let r = dc_resistance_ohm(std::slice::from_ref(&c), &filaments).unwrap();
        let exact = 30e-6 / (COPPER_S_PER_M * 1.0e-6 * 0.5e-6);
        assert!((r[0] - exact).abs() / exact < 1e-12, "got {}", r[0]);
    }

    #[test]
    fn non_positive_conductivity_is_rejected() {
        let mut c = wire("m1", 20.0, 1.0, 0.5);
        c.conductivity_s_per_m = Some(-1.0);
        let (filaments, _) = discretize_filaments(std::slice::from_ref(&c), 1).unwrap();
        assert!(dc_resistance_ohm(std::slice::from_ref(&c), &filaments)
            .unwrap_err()
            .contains("non-positive"));
    }

    fn assert_relative_eq(got: f64, want: f64) {
        assert!(
            (got - want).abs() <= 1e-12 * want.abs().max(1.0),
            "got {got}, want {want}"
        );
    }
}
