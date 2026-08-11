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

#[cfg(test)]
mod tests {
    use super::*;

    fn one_box_conductor(name: &str, b: BoxRequest) -> ConductorRequest {
        ConductorRequest {
            name: name.to_string(),
            boxes: vec![b],
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
}
