//! Surface discretisation: turn each conductor's axis-aligned box(es) into a
//! flat set of constant-charge-density panels for the potential-coefficient
//! matrix fill in `solver.rs`.

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
/// is `O(n^2)` in both memory and per-iteration solve time (a full matrix at
/// this cap is `8000^2 * 8 bytes` ~= 512 MiB), so an unreasonably fine
/// `panel_size_um` relative to a large conductor -- most likely a caller
/// mismatching scale between a tiny and a huge conductor in the same request
/// -- fails fast with a clear message instead of exhausting memory or
/// hanging on an oversized solve. `solver.rs`'s iterative (preconditioned CG)
/// solve (#799) is materially cheaper at this scale than the direct `O(n^3)`
/// LU factorisation it replaced -- see
/// `docs/design/mom-iterative-solver.md` -- but the dense `O(n^2)` fill this
/// guard bounds is unchanged by that; a matrix-free reformulation would be
/// needed to lift this cap meaningfully, not just a faster solve step. MVP
/// conductor counts (a handful of plates/coax cross-sections) stay far under
/// this cap at the default `panel_size_um`; raise it, or increase
/// `panel_size_um`, if a legitimate geometry needs more panels than this.
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
        dedupe_coincident_panels(&mut panels, before);
    }
    Ok(panels)
}

/// Remove exact-duplicate panels (identical center, within `EPS_UM`) that
/// were added for *this* conductor since index `start`, keeping the first
/// occurrence of each.
///
/// A multi-box conductor -- e.g. a coax-style shield built from several
/// abutting wall boxes (see `docs/cli/mom.md`'s "Worked example: coax") --
/// can have two different boxes meet at a right-angle corner such that one
/// box's outward face and its neighbour's outward face occupy the *exact
/// same* 3D rectangle (one wall's `y0` face and the perpendicular wall's
/// `y1` face, both spanning the same `x`/`z` range at their shared `y`).
/// Both panels describe the identical physical patch of the conductor's own
/// surface: keeping both double-counts that patch in the potential-
/// coefficient fill, and gives `solver.rs` two panels at `r = 0` of each
/// other -- a divide-by-zero in the off-diagonal `1/(4 pi eps r)` kernel.
///
/// This generalises the flat-plate case just above (`z0_um == z1_um` emits
/// one face, not six coincident ones, "avoiding... a singular matrix") from
/// one box's own opposite faces to two *different* boxes of the same
/// conductor. Only ever compares panels within one conductor -- a genuine
/// coincidence *between* different conductors (overlapping/short-circuited
/// surfaces) is a malformed request, not a discretisation artefact to paper
/// over, and is left for the solver to report as singular/ill-conditioned.
fn dedupe_coincident_panels(panels: &mut Vec<Panel>, start: usize) {
    let mut seen: std::collections::HashSet<[i64; 3]> = std::collections::HashSet::new();
    let mut write = start;
    for read in start..panels.len() {
        if seen.insert(quantize(&panels[read].center)) {
            panels.swap(write, read);
            write += 1;
        }
    }
    panels.truncate(write);
}

/// Quantise a panel center to `EPS_UM` resolution for exact-duplicate
/// detection (a `HashSet` needs `Eq`/`Hash`, which `f64` does not
/// implement).
fn quantize(center: &[f64; 3]) -> [i64; 3] {
    [
        (center[0] / EPS_UM).round() as i64,
        (center[1] / EPS_UM).round() as i64,
        (center[2] / EPS_UM).round() as i64,
    ]
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

// --- PEEC bar/filament discretisation ---------------------------------------
//
// Turns each conductor's single box into a "bar": a current-flow axis (the
// box's longest dimension -- see `MIN_BAR_ASPECT_RATIO`) plus a grid of
// cross-section filaments spanning the bar's full length. This is a
// *volumetric* discretisation (unlike `discretize`'s surface panels), feeding
// `peec::solve_inductance_matrix_nh`/`peec::resistance_ohm` rather than
// `solver::solve_capacitance_matrix_ff`. See docs/cli/mom.md's "PEEC
// inductance/resistance" section for the full scope and the reasoning behind
// the restrictions below.

/// Minimum current-flow-axis aspect ratio (`length / max(other two
/// extents)`) a box must have to be treated as a PEEC "bar" -- i.e. a
/// conductor whose current flow is well-approximated as uniform along one
/// axis. A conductor whose box is not at least this elongated (e.g. a square
/// pad or via) is rejected with a clear error rather than silently given a
/// made-up current-flow direction -- see docs/cli/mom.md's "PEEC
/// inductance/resistance" section.
pub const MIN_BAR_ASPECT_RATIO: f64 = 3.0;

/// Upper bound on total filament count across every conductor. PEEC's
/// pairwise mutual-inductance fill is `O(f^2)` in the total filament count
/// `f` (see `peec::solve_inductance_matrix_nh`); this cap keeps that
/// quadratic accumulation bounded, the same role `MAX_PANELS` plays for the
/// capacitance solve's dense matrix (looser here since PEEC has no `O(n^3)`
/// factorisation to worry about).
const MAX_FILAMENTS: usize = 6000;

pub(crate) const AXIS_NAMES: [&str; 3] = ["x", "y", "z"];

/// One PEEC current-flow filament: a thin bar-shaped sub-element spanning the
/// full (shared) bar length along the current-flow axis, tagged with its
/// position in the plane transverse to that axis and its cross-sectional
/// area. See `BarLayout`.
#[derive(Debug, Clone, Copy)]
pub struct Filament {
    pub conductor_index: usize,
    /// Position (um) in the plane perpendicular to the shared current-flow
    /// axis: the box's two non-axis coordinates, in ascending axis-index
    /// order (e.g. `axis == 1` (y) -> `[x, z]`).
    pub transverse_um: [f64; 2],
    pub area_um2: f64,
}

/// The shared bar geometry PEEC discretises every conductor into: one
/// current-flow axis (`0` = x, `1` = y, `2` = z) and one axial length (um)
/// common to *every* conductor in the request (see `discretize_bars`'s doc
/// for why), plus every conductor's cross-section filaments.
#[derive(Debug)]
pub struct BarLayout {
    pub axis: usize,
    pub length_um: f64,
    pub filaments: Vec<Filament>,
}

/// One conductor's box reduced to "bar" terms.
struct Bar {
    axis: usize,
    axis_lo_um: f64,
    axis_hi_um: f64,
    /// The two non-axis extents, in ascending axis-index order.
    transverse_um: [(f64, f64); 2],
}

fn classify_bar(name: &str, b: &BoxRequest) -> Result<Bar, String> {
    let (x0, x1) = minmax(b.x0_um, b.x1_um);
    let (y0, y1) = minmax(b.y0_um, b.y1_um);
    let (z0, z1) = minmax(b.z0_um, b.z1_um);
    let lo = [x0, y0, z0];
    let hi = [x1, y1, z1];
    let extent = [x1 - x0, y1 - y0, z1 - z0];

    for (i, e) in extent.iter().enumerate() {
        if *e < EPS_UM {
            return Err(format!(
                "conductor {name:?}: PEEC requires a true 3-D bar (non-zero extent on \
                 every axis) -- its {} extent is ~0 (a flat/zero-thickness box, fine for \
                 capacitance, has no cross-sectional area to carry current)",
                AXIS_NAMES[i]
            ));
        }
    }

    let axis = (0..3)
        .max_by(|&a, &c| extent[a].partial_cmp(&extent[c]).unwrap())
        .expect("0..3 is non-empty");
    let other: Vec<usize> = (0..3).filter(|&i| i != axis).collect();
    let max_other = extent[other[0]].max(extent[other[1]]);
    if extent[axis] < MIN_BAR_ASPECT_RATIO * max_other {
        return Err(format!(
            "conductor {name:?}: PEEC requires a bar-shaped conductor (its longest axis at \
             least {MIN_BAR_ASPECT_RATIO}x its other two extents, so 'current flows along \
             the long axis' is a defensible approximation) -- got extents x={:.4}um \
             y={:.4}um z={:.4}um, not elongated enough; a conductor this close to \
             square/cubic (e.g. a pad or a via) has no well-defined single current-flow \
             direction under this MVP's model",
            extent[0], extent[1], extent[2]
        ));
    }

    Ok(Bar {
        axis,
        axis_lo_um: lo[axis],
        axis_hi_um: hi[axis],
        transverse_um: [(lo[other[0]], hi[other[0]]), (lo[other[1]], hi[other[1]])],
    })
}

/// Discretise every conductor's single box into a shared-axis bar layout,
/// target filament edge length `filament_size_um`. Every conductor must
/// reduce to exactly one bar-shaped box (see `MIN_BAR_ASPECT_RATIO`), and
/// every conductor's bar must share the same current-flow axis and the same
/// `[lo, hi]` extent along it -- see docs/cli/mom.md's "PEEC
/// inductance/resistance" section for why these MVP restrictions exist and
/// what a follow-up would need to relax them.
pub fn discretize_bars(
    conductors: &[ConductorRequest],
    filament_size_um: f64,
) -> Result<BarLayout, String> {
    if filament_size_um <= 0.0 {
        return Err(format!(
            "filament_size_um must be positive, got {filament_size_um}"
        ));
    }
    if conductors.is_empty() {
        return Err("at least one conductor is required".to_string());
    }

    let mut bars: Vec<(&str, Bar)> = Vec::with_capacity(conductors.len());
    for c in conductors {
        if c.boxes.len() != 1 {
            return Err(format!(
                "conductor {:?}: PEEC requires every conductor to be exactly one box (got \
                 {}) -- a conductor built from several boxes (e.g. a coax shield's wall \
                 segments) has no single well-defined bar cross-section under this MVP's \
                 model; this is a follow-up",
                c.name,
                c.boxes.len()
            ));
        }
        bars.push((c.name.as_str(), classify_bar(&c.name, &c.boxes[0])?));
    }

    let axis = bars[0].1.axis;
    for (name, bar) in &bars[1..] {
        if bar.axis != axis {
            return Err(format!(
                "PEEC requires every conductor to share the same current-flow axis -- \
                 conductor {:?} is along {}, but conductor {:?} is along {}; a request \
                 mixing axes (e.g. an L-shaped loop) needs the general Ruehli mesh, a \
                 follow-up beyond this MVP's single-axis bar model",
                bars[0].0, AXIS_NAMES[axis], name, AXIS_NAMES[bar.axis]
            ));
        }
    }

    let (axis_lo, axis_hi) = (bars[0].1.axis_lo_um, bars[0].1.axis_hi_um);
    for (name, bar) in &bars[1..] {
        if (bar.axis_lo_um - axis_lo).abs() > EPS_UM || (bar.axis_hi_um - axis_hi).abs() > EPS_UM {
            return Err(format!(
                "PEEC requires every conductor's bar to share the same length and axial \
                 alignment -- conductor {:?} spans [{axis_lo:.4}, {axis_hi:.4}] um along {}, \
                 but conductor {name:?} spans [{:.4}, {:.4}] um; differing extents (e.g. an \
                 offset loop) need the general unequal-length Neumann formula, a follow-up \
                 beyond this MVP",
                bars[0].0, AXIS_NAMES[axis], bar.axis_lo_um, bar.axis_hi_um
            ));
        }
    }
    let length_um = axis_hi - axis_lo;

    // Pre-count filaments before generating any of them, mirroring
    // `discretize`'s panel guard -- fail fast on a scale-mismatched request
    // rather than materialise an astronomical filament count.
    let mut estimated_total: u64 = 0;
    for (_, bar) in &bars {
        let n0 = face_segment_count(
            bar.transverse_um[0].1 - bar.transverse_um[0].0,
            filament_size_um,
        );
        let n1 = face_segment_count(
            bar.transverse_um[1].1 - bar.transverse_um[1].0,
            filament_size_um,
        );
        estimated_total = estimated_total.saturating_add(n0.saturating_mul(n1));
    }
    if estimated_total > MAX_FILAMENTS as u64 {
        return Err(format!(
            "PEEC cross-section discretisation would produce more than {MAX_FILAMENTS} \
             filaments -- increase filament_size_um, or check for a scale mismatch between \
             conductors in this request"
        ));
    }

    let mut filaments = Vec::new();
    for (conductor_index, (name, bar)) in bars.iter().enumerate() {
        let before = filaments.len();
        let (u0, u1) = bar.transverse_um[0];
        let (v0, v1) = bar.transverse_um[1];
        for (uc, ulen) in subdivide_1d(u0, u1, filament_size_um) {
            for (vc, vlen) in subdivide_1d(v0, v1, filament_size_um) {
                filaments.push(Filament {
                    conductor_index,
                    transverse_um: [uc, vc],
                    area_um2: ulen * vlen,
                });
            }
        }
        if filaments.len() == before {
            return Err(format!(
                "conductor {name:?} produced zero PEEC filaments (degenerate cross-section)"
            ));
        }
    }

    Ok(BarLayout {
        axis,
        length_um,
        filaments,
    })
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

    #[test]
    fn abutting_boxes_at_a_right_angle_corner_do_not_emit_coincident_panels() {
        // Two boxes of the same conductor meeting at a right-angle corner --
        // the exact shape of the coax fixture's shield walls
        // (docs/design/mom-iterative-solver.md's "Fixing a fill bug the
        // direct solve had been hiding", #799). Box "north" spans the full
        // x range at y=[8,9]; box "east" spans x=[8,9] at y=[1,8]. North's
        // y0 (bottom) face and east's y1 (top) face both lie in the y=8
        // plane and overlap over x=[8,9] -- without deduplication this
        // would emit two panels at the exact same 3-D location, each a
        // divide-by-zero (`r = 0`) away from the other in the
        // potential-coefficient fill.
        let c = ConductorRequest {
            name: "shield".to_string(),
            boxes: vec![
                BoxRequest {
                    x0_um: 0.0,
                    y0_um: 8.0,
                    x1_um: 9.0,
                    y1_um: 9.0,
                    z0_um: 0.0,
                    z1_um: 0.5,
                },
                BoxRequest {
                    x0_um: 8.0,
                    y0_um: 1.0,
                    x1_um: 9.0,
                    y1_um: 8.0,
                    z0_um: 0.0,
                    z1_um: 0.5,
                },
            ],
            conductivity_s_per_m: None,
        };
        let panels = discretize(&[c], 0.5).unwrap();

        // No two panels share a (quantised) center -- the coincident pair
        // this fixture is built to trigger was actually removed, not just
        // absent by chance.
        let mut seen = std::collections::HashSet::new();
        for panel in &panels {
            let key = quantize(&panel.center);
            assert!(
                seen.insert(key),
                "duplicate panel center {:?} survived deduplication",
                panel.center
            );
        }

        // Sanity: deduplication actually removed panels relative to the
        // naive per-box sum (2 panels at the shared corner, at panel_size
        // 0.5um over a 1x0.5um overlap strip) -- this assertion would catch
        // a regression where `dedupe_coincident_panels` silently became a
        // no-op.
        let naive_total: usize = [
            BoxRequest {
                x0_um: 0.0,
                y0_um: 8.0,
                x1_um: 9.0,
                y1_um: 9.0,
                z0_um: 0.0,
                z1_um: 0.5,
            },
            BoxRequest {
                x0_um: 8.0,
                y0_um: 1.0,
                x1_um: 9.0,
                y1_um: 8.0,
                z0_um: 0.0,
                z1_um: 0.5,
            },
        ]
        .iter()
        .map(|b| {
            let mut out = Vec::new();
            discretize_box(b, 0.5, 0, &mut out);
            out.len()
        })
        .sum();
        assert!(
            panels.len() < naive_total,
            "expected deduplication to remove at least one coincident panel: \
             deduped={}, naive sum={naive_total}",
            panels.len()
        );
    }
    // --- bar/filament (PEEC) discretisation ---------------------------------

    fn bar_box(x1: f64, y1: f64, z1: f64) -> BoxRequest {
        BoxRequest {
            x0_um: 0.0,
            y0_um: 0.0,
            x1_um: x1,
            y1_um: y1,
            z0_um: 0.0,
            z1_um: z1,
        }
    }

    #[test]
    fn straight_bar_discretises_into_a_filament_grid() {
        // Length along x (100um), 2x2um cross section in y/z.
        let c = one_box_conductor("wire", bar_box(100.0, 2.0, 2.0));
        let layout = discretize_bars(&[c], 1.0).unwrap();
        assert_eq!(layout.axis, 0); // x is the longest extent
        assert_eq!(layout.length_um, 100.0);
        // 2x2um cross section at 1um filament size -> 2x2 = 4 filaments.
        assert_eq!(layout.filaments.len(), 4);
        let total_area: f64 = layout.filaments.iter().map(|f| f.area_um2).sum();
        assert!((total_area - 4.0).abs() < 1e-9);
    }

    #[test]
    fn near_cubic_box_is_rejected_as_not_bar_shaped() {
        // Aspect ratio ~1:1:1 -- no well-defined current-flow axis.
        let c = one_box_conductor("pad", bar_box(5.0, 5.0, 5.0));
        let err = discretize_bars(&[c], 1.0).unwrap_err();
        assert!(err.contains("bar-shaped"), "unexpected error: {err}");
    }

    #[test]
    fn flat_zero_thickness_box_is_rejected_for_peec() {
        // Fine for capacitance (a flat plate), but has no cross-sectional
        // area to carry current.
        let c = one_box_conductor("plate", bar_box(10.0, 10.0, 0.0));
        let err = discretize_bars(&[c], 1.0).unwrap_err();
        assert!(err.contains("3-D bar"), "unexpected error: {err}");
    }

    #[test]
    fn multi_box_conductor_is_rejected_for_peec() {
        let c = ConductorRequest {
            name: "shield".to_string(),
            boxes: vec![bar_box(100.0, 2.0, 2.0), bar_box(100.0, 2.0, 2.0)],
            conductivity_s_per_m: None,
        };
        let err = discretize_bars(&[c], 1.0).unwrap_err();
        assert!(err.contains("exactly one box"), "unexpected error: {err}");
    }

    #[test]
    fn mismatched_axis_is_rejected() {
        let along_x = one_box_conductor("a", bar_box(100.0, 2.0, 2.0));
        let along_y = ConductorRequest {
            name: "b".to_string(),
            boxes: vec![BoxRequest {
                x0_um: 0.0,
                y0_um: 0.0,
                x1_um: 2.0,
                y1_um: 100.0,
                z0_um: 0.0,
                z1_um: 2.0,
            }],
            conductivity_s_per_m: None,
        };
        let err = discretize_bars(&[along_x, along_y], 1.0).unwrap_err();
        assert!(
            err.contains("same current-flow axis"),
            "unexpected error: {err}"
        );
    }

    #[test]
    fn mismatched_length_or_alignment_is_rejected() {
        let short = one_box_conductor("a", bar_box(100.0, 2.0, 2.0));
        let long = one_box_conductor("b", bar_box(200.0, 2.0, 2.0));
        let err = discretize_bars(&[short, long], 1.0).unwrap_err();
        assert!(
            err.contains("same length and axial alignment"),
            "unexpected error: {err}"
        );
    }

    #[test]
    fn excessive_filament_count_is_a_clean_error() {
        // Long enough to satisfy the bar-shape aspect-ratio guard (length
        // 100000um vs a 1000x1000um cross section), but that same
        // 1000x1000um cross section at a 0.01um filament size implies ~1e10
        // filaments -- must fail fast rather than allocate.
        let c = one_box_conductor("wire", bar_box(100_000.0, 1000.0, 1000.0));
        let err = discretize_bars(&[c], 0.01).unwrap_err();
        assert!(err.contains("filaments"), "unexpected error: {err}");
    }
}
