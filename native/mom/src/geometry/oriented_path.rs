//! Oriented series paths for mixed-axis conductors -- increment (iii) of
//! `docs/design/mom-general-conductor-geometry.md` (issue #2728, part of
//! #1519).
//!
//! The parallel-bundle model in the parent module (`classify_bars`) treats a
//! multi-box conductor as several *parallel* co-axis, co-spanning bars: one
//! bar length per conductor, every box a cross-section sample of the same
//! current. A winding (an L/U-shaped trace, a square spiral) is the opposite
//! shape: its boxes are *series* legs of one current path, joined at turns.
//! This module gives that shape a deterministic, explicit representation --
//! an [`OrientedPath`] of [`PathSegment`]s -- without relaxing
//! `classify_bars`, whose single-length / single-equivalent-wire assumptions
//! the current PEEC and full-wave consumers still depend on.
//!
//! **Consumers.** The static PEEC solve (`geometry::discretize_bars` ->
//! `peec`, issue #2729) consumes an [`OrientedPath`] as a series L/R model.
//! The retarded full-wave sweep (#2730) does not yet: a single mixed-axis
//! winding is still rejected by `classify_bars` (via
//! `classify_full_wave_bars`) on every `frequencies_hz` request.
//!
//! The full contract -- contact rules, tolerance, corner-volume ownership,
//! canonical orientation, collinear subdivision, and every rejected case -- is
//! stated in the design doc's "Increment (iii)" section; the summary below is
//! what the code enforces.
//!
//! - **Contacts are between physical solids** (the request's axis-aligned
//!   boxes), never centreline endpoints: two boxes are connected when their
//!   closed intersection is a rectangle of positive area (a face contact),
//!   with coordinates compared to within [`CONTACT_TOL_UM`]. A positive-volume
//!   intersection (overlap) and an edge/point-only contact are both rejected.
//! - **Collinear subdivision** is canonicalised away first: boxes whose union
//!   is exactly one rectangular box (same extents on two axes, abutting on the
//!   third) merge into one straight run, transitively. Each merged run must be
//!   a rectangular solid that classifies as a bar (`classify_bar`); individual
//!   pieces need not meet the aspect threshold.
//! - **Turns** join two runs of different axes where one run's *end face*
//!   sits flush on the other run's side face at that run's end. The run whose
//!   side face carries the contact physically owns the corner square; the
//!   electrical centrelines of both runs meet at the corner's centre (the
//!   *vertex*), which conserves total metal volume exactly -- see
//!   [`OrientedPath`].
//! - **Orientation** is canonical: the path starts at the lexicographically
//!   smaller terminal (x, then y, then z), independent of box enumeration
//!   order. An explicit terminal reversal is a separate operation
//!   ([`OrientedPath::reversed`]) that is recorded in
//!   [`OrientedPath::orientation`], so it can never be confused with an input
//!   permutation.

// Some items (`reversed`, `vertices_um`, ...) are exercised only by this
// module's tests until #2730 (retarded coupling) consumes them -- see the
// module docs. Without this, `cargo clippy -- -D warnings` would fail on the
// not-yet-consumed API.
#![allow(dead_code)]

use super::{
    classify_bar, face_segment_count, minmax, subdivide_1d, to_xyz_on_axis, Filament, AXIS_NAMES,
    MAX_FILAMENTS,
};
use crate::contract::{BoxRequest, ConductorRequest};

/// Coordinate tolerance (um) for every contact / flushness / equal-extent
/// comparison in this module: 1 pm. `klt mom`'s GDS translation multiplies
/// integer database units by `dbu` (typically 0.001 um) in floating point, so
/// two boxes drawn edge-to-edge in the layout can differ by ~1e-14 um in the
/// request; 1e-6 um absorbs that noise while staying three orders of magnitude
/// below the smallest physically drawable feature (one 1 nm dbu step). Two
/// faces closer than this are *touching*; farther apart, they are *separate*.
pub const CONTACT_TOL_UM: f64 = 1e-6;

/// Which terminal a path starts from.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PathOrientation {
    /// Starts at the lexicographically smaller terminal (compare x, then y,
    /// then z, each to within [`CONTACT_TOL_UM`]). Every successful
    /// [`classify_oriented_path`] returns this, whatever the box order.
    Canonical,
    /// The canonical path traversed from its other terminal -- only ever
    /// produced by an explicit [`OrientedPath::reversed`] call.
    Reversed,
}

/// One straight series leg of an [`OrientedPath`].
#[derive(Debug, Clone, PartialEq)]
pub struct PathSegment {
    /// Owning conductor (its position in the request's `conductors`).
    pub conductor_index: usize,
    /// Position of this leg along the traversal, `0..segments.len()`.
    pub segment_index: usize,
    /// The request's box indices (within the owning conductor's `boxes`)
    /// that make up this leg, ascending. More than one when the leg was drawn
    /// as collinear pieces.
    pub box_indices: Vec<usize>,
    /// Current-flow axis (0 = x, 1 = y, 2 = z).
    pub axis: usize,
    /// `+1.0` when the traversal runs toward increasing `axis` coordinate,
    /// `-1.0` otherwise -- the per-leg sign a series sum `sum s_i s_j L_ij`
    /// over box-low-to-high partial inductances needs.
    pub axis_sign: f64,
    /// Electrical centreline start (um), in traversal order: a terminal (the
    /// centre of a free end face) or a turn vertex (the corner centre).
    pub start_um: [f64; 3],
    /// Electrical centreline end (um), in traversal order.
    pub end_um: [f64; 3],
    /// The two transverse intervals (um), in ascending axis-index order over
    /// the two non-axis coordinates -- the same convention as
    /// `Bar::transverse_um`.
    pub transverse_um: [(f64, f64); 2],
    /// Physical solid (the union of `box_indices`), low corner (um). Can
    /// extend past `start_um`/`end_um` along `axis` by up to half the
    /// neighbour's width where this leg owns a corner, or stop short of it
    /// where the neighbour does.
    pub solid_lo_um: [f64; 3],
    /// Physical solid, high corner (um).
    pub solid_hi_um: [f64; 3],
}

impl PathSegment {
    /// Electrical centreline length (um).
    pub fn length_um(&self) -> f64 {
        (self.end_um[self.axis] - self.start_um[self.axis]).abs()
    }

    /// Rectangular cross-section extents (um), ascending axis-index order.
    pub fn cross_section_um(&self) -> [f64; 2] {
        [
            self.transverse_um[0].1 - self.transverse_um[0].0,
            self.transverse_um[1].1 - self.transverse_um[1].0,
        ]
    }

    /// Cross-sectional area (um^2).
    pub fn area_um2(&self) -> f64 {
        let [w, h] = self.cross_section_um();
        w * h
    }
}

/// A non-branching open chain of rectangular bars, as one series current path
/// from one terminal to the other.
///
/// Corner volume: every corner square of metal belongs to exactly one
/// physical solid (whichever box contains it; `PathSegment::solid_*`), while
/// the *electrical* segments run centreline-to-centreline through the corner
/// vertex. The leg owning the corner is shortened electrically by half the
/// other leg's width and the other leg lengthened by half the owner's width,
/// so `sum(length_um * area_um2)` over the segments equals the summed volume
/// of the physical boxes exactly -- nothing is double-counted or dropped in
/// total (the classic centreline convention of filament PEEC extractors; the
/// local overlap/omission of one corner quadrant each is the documented
/// approximation).
#[derive(Debug, Clone, PartialEq)]
pub struct OrientedPath {
    pub conductor_index: usize,
    pub orientation: PathOrientation,
    /// Series legs in traversal order; consecutive legs share an endpoint
    /// (`segments[i].end_um == segments[i + 1].start_um`).
    pub segments: Vec<PathSegment>,
}

impl OrientedPath {
    /// The terminal the traversal starts from (um).
    pub fn start_terminal_um(&self) -> [f64; 3] {
        self.segments[0].start_um
    }

    /// The terminal the traversal ends at (um).
    pub fn end_terminal_um(&self) -> [f64; 3] {
        self.segments[self.segments.len() - 1].end_um
    }

    /// Every centreline vertex in traversal order: the start terminal, each
    /// turn vertex, then the end terminal (`segments.len() + 1` points).
    pub fn vertices_um(&self) -> Vec<[f64; 3]> {
        let mut out = vec![self.start_terminal_um()];
        out.extend(self.segments.iter().map(|s| s.end_um));
        out
    }

    /// Total electrical centreline length (um).
    pub fn total_length_um(&self) -> f64 {
        self.segments.iter().map(PathSegment::length_um).sum()
    }

    /// The same path traversed from its other terminal: segment order,
    /// endpoints and signs flip, `orientation` toggles. This is the only way
    /// to obtain a non-canonical path, so a deliberate terminal reversal is
    /// always distinguishable from a mere permutation of the input boxes.
    pub fn reversed(&self) -> OrientedPath {
        let count = self.segments.len();
        let segments = self
            .segments
            .iter()
            .rev()
            .enumerate()
            .map(|(segment_index, s)| PathSegment {
                segment_index,
                axis_sign: -s.axis_sign,
                start_um: s.end_um,
                end_um: s.start_um,
                ..s.clone()
            })
            .collect::<Vec<_>>();
        debug_assert_eq!(segments.len(), count);
        OrientedPath {
            conductor_index: self.conductor_index,
            orientation: match self.orientation {
                PathOrientation::Canonical => PathOrientation::Reversed,
                PathOrientation::Reversed => PathOrientation::Canonical,
            },
            segments,
        }
    }
}

/// How one conductor's boxes are interpreted.
#[derive(Debug, Clone, PartialEq)]
pub enum ConductorTopology {
    /// The pre-existing geometry class: every box a bar along one shared axis
    /// and axial span, physically separate boxes allowed (e.g. a coax
    /// shield's north/south walls). Classified first, so nothing that the
    /// parallel-bundle model accepts today is reinterpreted as a path (or
    /// rejected as a "disconnected" one).
    ParallelBundle,
    /// A series winding (see [`classify_oriented_path`]).
    OrientedPath(OrientedPath),
}

/// Decide whether a conductor is an existing parallel bundle or a series
/// winding. Backward compatible by construction: a conductor that satisfies
/// `classify_bars`' within-conductor discipline (individually bar-shaped
/// boxes, one shared axis and `[lo, hi]` span) is a
/// [`ConductorTopology::ParallelBundle`] exactly as before; only otherwise is
/// it classified as an oriented path, with that classifier's explicit
/// rejection if it is not one.
pub fn classify_conductor_topology(
    conductor_index: usize,
    conductor: &ConductorRequest,
) -> Result<ConductorTopology, String> {
    if is_parallel_bundle(conductor) {
        return Ok(ConductorTopology::ParallelBundle);
    }
    classify_oriented_path(conductor_index, conductor).map(ConductorTopology::OrientedPath)
}

fn is_parallel_bundle(conductor: &ConductorRequest) -> bool {
    let mut span: Option<(usize, f64, f64)> = None;
    for b in &conductor.boxes {
        let Ok(bar) = classify_bar(&conductor.name, b) else {
            return false;
        };
        match span {
            None => span = Some((bar.axis, bar.axis_lo_um, bar.axis_hi_um)),
            Some((axis, lo, hi)) => {
                if bar.axis != axis
                    || (bar.axis_lo_um - lo).abs() > super::EPS_UM
                    || (bar.axis_hi_um - hi).abs() > super::EPS_UM
                {
                    return false;
                }
            }
        }
    }
    span.is_some()
}

/// An axis-aligned rectangular solid, normalised so `lo <= hi` per axis.
#[derive(Debug, Clone, Copy, PartialEq)]
struct Solid {
    lo: [f64; 3],
    hi: [f64; 3],
}

impl Solid {
    fn from_box(b: &BoxRequest) -> Solid {
        let (x0, x1) = minmax(b.x0_um, b.x1_um);
        let (y0, y1) = minmax(b.y0_um, b.y1_um);
        let (z0, z1) = minmax(b.z0_um, b.z1_um);
        Solid {
            lo: [x0, y0, z0],
            hi: [x1, y1, z1],
        }
    }

    fn extent(&self, k: usize) -> f64 {
        self.hi[k] - self.lo[k]
    }

    fn center(&self, k: usize) -> f64 {
        0.5 * (self.lo[k] + self.hi[k])
    }

    fn volume(&self) -> f64 {
        self.extent(0) * self.extent(1) * self.extent(2)
    }

    fn union_bbox(&self, other: &Solid) -> Solid {
        let mut out = *self;
        for k in 0..3 {
            out.lo[k] = out.lo[k].min(other.lo[k]);
            out.hi[k] = out.hi[k].max(other.hi[k]);
        }
        out
    }

    fn as_box(&self) -> BoxRequest {
        BoxRequest {
            x0_um: self.lo[0],
            y0_um: self.lo[1],
            x1_um: self.hi[0],
            y1_um: self.hi[1],
            z0_um: self.lo[2],
            z1_um: self.hi[2],
        }
    }

    fn same_interval(&self, other: &Solid, k: usize) -> bool {
        (self.lo[k] - other.lo[k]).abs() <= CONTACT_TOL_UM
            && (self.hi[k] - other.hi[k]).abs() <= CONTACT_TOL_UM
    }
}

/// How two solids' closed regions meet.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Contact {
    /// A gap wider than [`CONTACT_TOL_UM`] on at least one axis.
    Separate,
    /// Positive-volume intersection (overlap beyond tolerance on all three
    /// axes).
    Overlap,
    /// Touching on exactly one axis (`normal`) and overlapping on the other
    /// two: a rectangle of positive area.
    Face { normal: usize },
    /// Touching on two or three axes: a shared edge or corner point only.
    EdgeOrPoint,
}

fn contact(a: &Solid, b: &Solid) -> Contact {
    let mut touching = 0;
    let mut normal = 0;
    for k in 0..3 {
        let overlap = a.hi[k].min(b.hi[k]) - a.lo[k].max(b.lo[k]);
        if overlap < -CONTACT_TOL_UM {
            return Contact::Separate;
        }
        if overlap <= CONTACT_TOL_UM {
            touching += 1;
            normal = k;
        }
    }
    match touching {
        0 => Contact::Overlap,
        1 => Contact::Face { normal },
        _ => Contact::EdgeOrPoint,
    }
}

/// A maximal group of collinear pieces whose union is one rectangular bar.
struct Run {
    boxes: Vec<usize>,
    solid: Solid,
    axis: usize,
}

/// A run end's connection to its neighbour across a turn.
#[derive(Clone, Copy)]
struct Link {
    neighbor: usize,
    neighbor_end: usize,
    vertex: [f64; 3],
}

fn find(parent: &mut [usize], mut i: usize) -> usize {
    while parent[i] != i {
        parent[i] = parent[parent[i]];
        i = parent[i];
    }
    i
}

fn union(parent: &mut [usize], a: usize, b: usize) {
    let (ra, rb) = (find(parent, a), find(parent, b));
    if ra != rb {
        // Smaller root wins: keeps the grouping independent of union order.
        let (lo, hi) = if ra < rb { (ra, rb) } else { (rb, ra) };
        parent[hi] = lo;
    }
}

/// Group `items` (indices `0..n`) by union-find root, each group ascending,
/// groups ordered by their smallest member.
fn groups(parent: &mut [usize]) -> Vec<Vec<usize>> {
    let n = parent.len();
    let mut by_root: Vec<Vec<usize>> = vec![Vec::new(); n];
    for i in 0..n {
        let r = find(parent, i);
        by_root[r].push(i);
    }
    by_root.into_iter().filter(|g| !g.is_empty()).collect()
}

fn lex_less(p: &[f64; 3], q: &[f64; 3]) -> bool {
    for k in 0..3 {
        if (p[k] - q[k]).abs() > CONTACT_TOL_UM {
            return p[k] < q[k];
        }
    }
    false
}

/// Classify one conductor's boxes as a non-branching open chain of
/// rectangular bars and return its canonical [`OrientedPath`].
///
/// Rejected explicitly, each with a conductor-specific message (never by
/// picking an arbitrary traversal): no boxes; a degenerate box (an extent at
/// or below [`CONTACT_TOL_UM`]); overlapping boxes; edge/point-only contact
/// between segments; a collinear group that is not one rectangular bar (e.g.
/// a separately drawn corner block, whose ownership would be ambiguous); a
/// segment that is not bar-shaped; side-by-side contact between parallel
/// segments or a cross-section step; a stacked (via-like) contact normal to
/// both segment axes; a turn that changes the shared out-of-plane extent,
/// overhangs the other segment's end, or meets it mid-length (a T-junction);
/// more than two segments meeting at one end (a branch); a segment whose
/// electrical length collapses to ~0; disconnected pieces; and a closed loop.
pub fn classify_oriented_path(
    conductor_index: usize,
    conductor: &ConductorRequest,
) -> Result<OrientedPath, String> {
    let name = conductor.name.as_str();
    if conductor.boxes.is_empty() {
        return Err(format!(
            "conductor {name:?}: an oriented conductor path needs at least one box (got 0)"
        ));
    }
    let solids: Vec<Solid> = conductor.boxes.iter().map(Solid::from_box).collect();

    for (i, s) in solids.iter().enumerate() {
        for (k, axis_name) in AXIS_NAMES.iter().enumerate() {
            if s.extent(k) <= CONTACT_TOL_UM {
                return Err(format!(
                    "conductor {name:?}: box {i} is degenerate for an oriented conductor path \
                     -- its {axis_name} extent is ~0 (every path segment must be a true 3-D bar)"
                ));
            }
        }
    }

    // Box-level pass: reject overlaps, merge collinear pieces.
    let n = solids.len();
    let mut parent: Vec<usize> = (0..n).collect();
    for i in 0..n {
        for j in (i + 1)..n {
            match contact(&solids[i], &solids[j]) {
                Contact::Overlap => {
                    return Err(format!(
                        "conductor {name:?}: boxes {i} and {j} overlap (positive-volume \
                         intersection) -- an oriented conductor path needs non-overlapping, \
                         tiled boxes so each corner's metal belongs to exactly one segment"
                    ));
                }
                Contact::Face { normal } => {
                    if (0..3)
                        .filter(|&k| k != normal)
                        .all(|k| solids[i].same_interval(&solids[j], k))
                    {
                        union(&mut parent, i, j);
                    }
                }
                Contact::Separate | Contact::EdgeOrPoint => {}
            }
        }
    }

    // Each collinear group must be exactly one rectangular bar.
    let mut runs: Vec<Run> = Vec::new();
    for members in groups(&mut parent) {
        let mut solid = solids[members[0]];
        let mut volume = 0.0;
        for &m in &members {
            solid = solid.union_bbox(&solids[m]);
            volume += solids[m].volume();
        }
        let bbox_volume = solid.volume();
        if (bbox_volume - volume).abs() > 1e-9 * bbox_volume.max(1.0) {
            return Err(format!(
                "conductor {name:?}: boxes {members:?} abut collinearly but do not form one \
                 rectangular bar (e.g. a separately drawn corner block that could merge into \
                 either leg) -- corner ownership is ambiguous; draw each corner square as part \
                 of exactly one leg"
            ));
        }
        let bar = classify_bar(name, &solid.as_box())
            .map_err(|e| format!("{e} (oriented-path segment built from boxes {members:?})"))?;
        runs.push(Run {
            boxes: members,
            solid,
            axis: bar.axis,
        });
    }

    // Run-level pass: resolve every contact between segments into a turn.
    let r = runs.len();
    let mut ends: Vec<[Option<Link>; 2]> = vec![[None, None]; r];
    let mut connected: Vec<usize> = (0..r).collect();
    for i in 0..r {
        for j in (i + 1)..r {
            let (ri, rj) = (&runs[i], &runs[j]);
            let normal = match contact(&ri.solid, &rj.solid) {
                Contact::Separate => continue,
                Contact::Face { normal } => normal,
                Contact::Overlap => {
                    return Err(format!(
                        "conductor {name:?}: segments from boxes {:?} and {:?} overlap -- an \
                         oriented conductor path needs non-overlapping, tiled boxes",
                        ri.boxes, rj.boxes
                    ));
                }
                Contact::EdgeOrPoint => {
                    return Err(format!(
                        "conductor {name:?}: segments from boxes {:?} and {:?} touch only along \
                         an edge or at a point -- not a defined electrical contact for an \
                         oriented conductor path (segments must share a face of positive area)",
                        ri.boxes, rj.boxes
                    ));
                }
            };
            if ri.axis == rj.axis {
                return Err(format!(
                    "conductor {name:?}: segments from boxes {:?} and {:?} both run along {} and \
                     touch (side-by-side parallel contact, or an end-to-end cross-section \
                     step) -- an oriented conductor path only joins segments at right-angle \
                     turns; parallel same-current geometry is a parallel bundle",
                    ri.boxes, rj.boxes, AXIS_NAMES[ri.axis]
                ));
            }
            let (owner, arriving) = if rj.axis == normal {
                (i, j)
            } else if ri.axis == normal {
                (j, i)
            } else {
                return Err(format!(
                    "conductor {name:?}: segments from boxes {:?} and {:?} (along {} and {}) \
                     touch across a {}-normal face -- a stacked, via-like contact normal to \
                     both segment axes is not supported by oriented conductor paths",
                    ri.boxes,
                    rj.boxes,
                    AXIS_NAMES[ri.axis],
                    AXIS_NAMES[rj.axis],
                    AXIS_NAMES[normal]
                ));
            };
            let (owner_end, arriving_end, vertex) =
                resolve_turn(name, &runs[owner], &runs[arriving])?;
            for (run, end, other, other_end) in [
                (owner, owner_end, arriving, arriving_end),
                (arriving, arriving_end, owner, owner_end),
            ] {
                if ends[run][end].is_some() {
                    return Err(format!(
                        "conductor {name:?}: more than two segments meet at the {} end of the \
                         segment from boxes {:?} -- a branched conductor has no single series \
                         path",
                        if end == 0 { "low" } else { "high" },
                        runs[run].boxes
                    ));
                }
                ends[run][end] = Some(Link {
                    neighbor: other,
                    neighbor_end: other_end,
                    vertex,
                });
            }
            union(&mut connected, i, j);
        }
    }

    let pieces = groups(&mut connected);
    if pieces.len() > 1 {
        let described: Vec<Vec<usize>> = pieces
            .iter()
            .map(|g| {
                let mut b: Vec<usize> = g.iter().flat_map(|&ri| runs[ri].boxes.clone()).collect();
                b.sort_unstable();
                b
            })
            .collect();
        return Err(format!(
            "conductor {name:?}: oriented conductor path is disconnected -- its boxes form {} \
             separate pieces (box groups {described:?}) with no face contact between them",
            pieces.len()
        ));
    }

    let free: Vec<(usize, usize)> = (0..r)
        .flat_map(|ri| (0..2).map(move |e| (ri, e)))
        .filter(|&(ri, e)| ends[ri][e].is_none())
        .collect();
    if free.is_empty() {
        return Err(format!(
            "conductor {name:?}: boxes form a closed loop (every segment end is joined to \
             another) -- an oriented conductor path must be open, with two terminals"
        ));
    }
    debug_assert_eq!(
        free.len(),
        2,
        "connected max-degree-2 acyclic graph has 2 free ends"
    );

    let terminal = |(ri, e): (usize, usize)| -> [f64; 3] {
        let run = &runs[ri];
        let mut p = [
            run.solid.center(0),
            run.solid.center(1),
            run.solid.center(2),
        ];
        p[run.axis] = if e == 0 {
            run.solid.lo[run.axis]
        } else {
            run.solid.hi[run.axis]
        };
        p
    };
    let (first, last) = (free[0], free[1]);
    let start = if lex_less(&terminal(last), &terminal(first)) {
        last
    } else {
        first
    };

    let mut segments = Vec::with_capacity(r);
    let (mut current, mut entry_end) = start;
    let mut entry_point = terminal(start);
    loop {
        let run = &runs[current];
        let exit_end = 1 - entry_end;
        let (exit_point, next) = match ends[current][exit_end] {
            Some(link) => (link.vertex, Some((link.neighbor, link.neighbor_end))),
            None => (terminal((current, exit_end)), None),
        };
        let delta = exit_point[run.axis] - entry_point[run.axis];
        if delta.abs() <= CONTACT_TOL_UM {
            return Err(format!(
                "conductor {name:?}: the segment from boxes {:?} has ~0 electrical length \
                 between its endpoints (its turn vertices coincide) -- too short to carry a \
                 defined series current in an oriented conductor path",
                run.boxes
            ));
        }
        let other: Vec<usize> = (0..3).filter(|&k| k != run.axis).collect();
        segments.push(PathSegment {
            conductor_index,
            segment_index: segments.len(),
            box_indices: run.boxes.clone(),
            axis: run.axis,
            axis_sign: delta.signum(),
            start_um: entry_point,
            end_um: exit_point,
            transverse_um: [
                (run.solid.lo[other[0]], run.solid.hi[other[0]]),
                (run.solid.lo[other[1]], run.solid.hi[other[1]]),
            ],
            solid_lo_um: run.solid.lo,
            solid_hi_um: run.solid.hi,
        });
        match next {
            Some((neighbor, neighbor_end)) => {
                current = neighbor;
                entry_end = neighbor_end;
                entry_point = exit_point;
            }
            None => break,
        }
        if segments.len() > r {
            // Unreachable for a connected acyclic graph; a defensive stop
            // rather than an infinite loop if that invariant were ever broken.
            return Err(format!(
                "conductor {name:?}: internal error -- oriented path traversal revisited a \
                 segment"
            ));
        }
    }
    if segments.len() != r {
        return Err(format!(
            "conductor {name:?}: boxes contain a closed loop alongside an open chain -- an \
             oriented conductor path must be a single open chain"
        ));
    }

    Ok(OrientedPath {
        conductor_index,
        orientation: PathOrientation::Canonical,
        segments,
    })
}

/// Resolve a turn where `arriving`'s end face (normal to its own axis `b`)
/// sits on `owner`'s side face. Returns `(owner_end, arriving_end, vertex)`,
/// ends as `0` = low / `1` = high along each run's own axis.
fn resolve_turn(
    name: &str,
    owner: &Run,
    arriving: &Run,
) -> Result<(usize, usize, [f64; 3]), String> {
    let (a, b) = (owner.axis, arriving.axis);
    let c = 3 - a - b;
    let (os, as_) = (&owner.solid, &arriving.solid);

    if !os.same_interval(as_, c) {
        return Err(format!(
            "conductor {name:?}: the turn between segments from boxes {:?} and {:?} changes \
             the {} extent ([{:.4}, {:.4}] vs [{:.4}, {:.4}] um) -- an oriented conductor path \
             needs both legs of a turn to share their out-of-plane extent",
            owner.boxes, arriving.boxes, AXIS_NAMES[c], os.lo[c], os.hi[c], as_.lo[c], as_.hi[c]
        ));
    }
    let inside = as_.lo[a] >= os.lo[a] - CONTACT_TOL_UM && as_.hi[a] <= os.hi[a] + CONTACT_TOL_UM;
    if !inside {
        return Err(format!(
            "conductor {name:?}: the segment from boxes {:?} overhangs the end of the segment \
             from boxes {:?} at their turn -- an oriented conductor path needs tiled corners \
             (each leg's end face flush within the other leg)",
            arriving.boxes, owner.boxes
        ));
    }
    let flush_lo = (as_.lo[a] - os.lo[a]).abs() <= CONTACT_TOL_UM;
    let flush_hi = (as_.hi[a] - os.hi[a]).abs() <= CONTACT_TOL_UM;
    let owner_end = match (flush_lo, flush_hi) {
        (true, false) => 0,
        (false, true) => 1,
        (true, true) => {
            return Err(format!(
                "conductor {name:?}: the segment from boxes {:?} is exactly as long as the \
                 segment from boxes {:?} is wide -- which end it turns at is ambiguous",
                owner.boxes, arriving.boxes
            ));
        }
        (false, false) => {
            return Err(format!(
                "conductor {name:?}: the segment from boxes {:?} meets the segment from boxes \
                 {:?} away from its ends (a T-junction) -- a branched conductor has no single \
                 series path",
                arriving.boxes, owner.boxes
            ));
        }
    };
    let arriving_end = if (as_.lo[b] - os.hi[b]).abs() <= CONTACT_TOL_UM {
        0
    } else if (as_.hi[b] - os.lo[b]).abs() <= CONTACT_TOL_UM {
        1
    } else {
        return Err(format!(
            "conductor {name:?}: internal error -- turn contact between segments from boxes \
             {:?} and {:?} is not at an end face",
            owner.boxes, arriving.boxes
        ));
    };
    let mut vertex = [0.0; 3];
    vertex[a] = as_.center(a);
    vertex[b] = os.center(b);
    vertex[c] = os.center(c);
    Ok((owner_end, arriving_end, vertex))
}

/// One PEEC filament of an oriented path, tagged with the series leg it
/// samples as well as its conductor.
///
/// `filament.start_um -> end_um` follows the traversal direction (so a
/// filament's sign is already folded into its geometry), and every filament
/// of one `segment_index` is a *parallel* cross-section sample of that leg's
/// current, while different `segment_index` values are *series* legs --
/// exactly the distinction a series-L/R consumer (#2729) needs and the
/// conductor index alone cannot express.
#[derive(Debug, Clone, Copy)]
pub struct PathFilament {
    pub segment_index: usize,
    pub filament: Filament,
}

/// Discretise an oriented path's legs into cross-section filament grids
/// (target edge length `filament_size_um`), each filament spanning its leg's
/// electrical centreline length, in traversal order.
pub fn discretize_oriented_path(
    path: &OrientedPath,
    filament_size_um: f64,
) -> Result<Vec<PathFilament>, String> {
    if filament_size_um <= 0.0 {
        return Err(format!(
            "filament_size_um must be positive, got {filament_size_um}"
        ));
    }
    let mut estimated: u64 = 0;
    for s in &path.segments {
        let [w, h] = s.cross_section_um();
        estimated = estimated.saturating_add(
            face_segment_count(w, filament_size_um)
                .saturating_mul(face_segment_count(h, filament_size_um)),
        );
    }
    if estimated > MAX_FILAMENTS as u64 {
        return Err(format!(
            "oriented-path cross-section discretisation would produce more than \
             {MAX_FILAMENTS} filaments -- increase filament_size_um"
        ));
    }

    let mut out = Vec::new();
    for s in &path.segments {
        let (u0, u1) = s.transverse_um[0];
        let (v0, v1) = s.transverse_um[1];
        for (uc, ulen) in subdivide_1d(u0, u1, filament_size_um) {
            for (vc, vlen) in subdivide_1d(v0, v1, filament_size_um) {
                out.push(PathFilament {
                    segment_index: s.segment_index,
                    filament: Filament {
                        conductor_index: s.conductor_index,
                        start_um: to_xyz_on_axis(s.axis, s.start_um[s.axis], uc, vc),
                        end_um: to_xyz_on_axis(s.axis, s.end_um[s.axis], uc, vc),
                        area_um2: ulen * vlen,
                        extent_um: [ulen, vlen],
                    },
                });
            }
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::super::{classify_bars, classify_full_wave_bars, discretize_bars};
    use super::*;

    const Z0: f64 = 0.0;
    const Z1: f64 = 2.0;

    fn bx(x0: f64, y0: f64, x1: f64, y1: f64) -> BoxRequest {
        BoxRequest {
            x0_um: x0,
            y0_um: y0,
            x1_um: x1,
            y1_um: y1,
            z0_um: Z0,
            z1_um: Z1,
        }
    }

    fn conductor(boxes: Vec<BoxRequest>) -> ConductorRequest {
        ConductorRequest {
            name: "winding".to_string(),
            boxes,
            conductivity_s_per_m: None,
        }
    }

    fn classify(boxes: Vec<BoxRequest>) -> Result<OrientedPath, String> {
        classify_oriented_path(0, &conductor(boxes))
    }

    fn err(boxes: Vec<BoxRequest>) -> String {
        classify(boxes).expect_err("expected an explicit rejection")
    }

    /// Order-independent fingerprint of a path's electrical topology: per
    /// segment (axis, sign, start, end, cross-section, physical solid), all
    /// quantised -- deliberately excluding `box_indices`, which permuting the
    /// input legitimately renumbers.
    type Fingerprint = Vec<(usize, i64, [i64; 3], [i64; 3], [i64; 2], [i64; 3], [i64; 3])>;

    fn q(v: f64) -> i64 {
        (v * 1e6).round() as i64
    }

    fn q3(p: [f64; 3]) -> [i64; 3] {
        [q(p[0]), q(p[1]), q(p[2])]
    }

    fn fingerprint(path: &OrientedPath) -> Fingerprint {
        path.segments
            .iter()
            .map(|s| {
                let cs = s.cross_section_um();
                (
                    s.axis,
                    s.axis_sign as i64,
                    q3(s.start_um),
                    q3(s.end_um),
                    [q(cs[0]), q(cs[1])],
                    q3(s.solid_lo_um),
                    q3(s.solid_hi_um),
                )
            })
            .collect()
    }

    /// Box volume and electrical `length * area` totals must agree: the
    /// corner-volume conservation `OrientedPath`'s docs promise.
    fn assert_volume_conserved(path: &OrientedPath, boxes: &[BoxRequest]) {
        let physical: f64 = boxes.iter().map(|b| Solid::from_box(b).volume()).sum();
        let electrical: f64 = path
            .segments
            .iter()
            .map(|s| s.length_um() * s.area_um2())
            .sum();
        assert!(
            (physical - electrical).abs() < 1e-9 * physical,
            "corner volume not conserved: physical {physical} vs electrical {electrical}"
        );
    }

    fn assert_structure(path: &OrientedPath) {
        for (i, s) in path.segments.iter().enumerate() {
            assert_eq!(s.segment_index, i);
            assert_eq!(s.conductor_index, path.conductor_index);
            if i + 1 < path.segments.len() {
                assert_eq!(q3(s.end_um), q3(path.segments[i + 1].start_um));
            }
            // A straight axis-aligned centreline.
            for k in 0..3 {
                if k != s.axis {
                    assert_eq!(q(s.start_um[k]), q(s.end_um[k]));
                }
            }
            assert_eq!(
                s.axis_sign,
                (s.end_um[s.axis] - s.start_um[s.axis]).signum()
            );
        }
    }

    /// A deterministic, non-trivial permutation (reverse, then interleave).
    fn shuffled<T: Clone>(items: &[T]) -> Vec<T> {
        let rev: Vec<T> = items.iter().rev().cloned().collect();
        let (odd, even): (Vec<_>, Vec<_>) =
            rev.into_iter().enumerate().partition(|(i, _)| i % 2 == 1);
        odd.into_iter().chain(even).map(|(_, t)| t).collect()
    }

    fn assert_permutation_invariant(boxes: &[BoxRequest]) {
        let base = classify(boxes.to_vec()).unwrap();
        let reversed: Vec<BoxRequest> = boxes.iter().rev().cloned().collect();
        for permuted in [reversed, shuffled(boxes)] {
            let p = classify(permuted.clone()).unwrap();
            assert_eq!(p.orientation, PathOrientation::Canonical);
            assert_eq!(fingerprint(&p), fingerprint(&base));
            // Ownership survives renumbering: each segment's boxes are the
            // same physical solids.
            for (a, b) in p.segments.iter().zip(&base.segments) {
                let mut pa: Vec<[i64; 3]> = a
                    .box_indices
                    .iter()
                    .map(|&i| q3(Solid::from_box(&permuted[i]).lo))
                    .collect();
                let mut pb: Vec<[i64; 3]> = b
                    .box_indices
                    .iter()
                    .map(|&i| q3(Solid::from_box(&boxes[i]).lo))
                    .collect();
                pa.sort_unstable();
                pb.sort_unstable();
                assert_eq!(pa, pb);
            }
        }
    }

    // --- L / U paths --------------------------------------------------------

    /// East then north, width 2; the corner square [59,61]x[-1,1] belongs to
    /// the east leg (the arriving leg, the spiral fixture's convention).
    fn l_corner_in_first_leg() -> Vec<BoxRequest> {
        vec![bx(0.0, -1.0, 61.0, 1.0), bx(59.0, 1.0, 61.0, 41.0)]
    }

    #[test]
    fn l_path_has_two_signed_segments_meeting_at_the_corner_centre() {
        let boxes = l_corner_in_first_leg();
        let path = classify(boxes.clone()).unwrap();
        assert_structure(&path);
        assert_eq!(path.orientation, PathOrientation::Canonical);
        assert_eq!(path.segments.len(), 2);
        let [s0, s1] = [&path.segments[0], &path.segments[1]];
        assert_eq!((s0.axis, s0.axis_sign), (0, 1.0));
        assert_eq!((s1.axis, s1.axis_sign), (1, 1.0));
        assert_eq!(q3(path.start_terminal_um()), q3([0.0, 0.0, 1.0]));
        assert_eq!(q3(s0.end_um), q3([60.0, 0.0, 1.0]));
        assert_eq!(q3(path.end_terminal_um()), q3([60.0, 41.0, 1.0]));
        assert!((s0.length_um() - 60.0).abs() < 1e-9);
        assert!((s1.length_um() - 41.0).abs() < 1e-9);
        assert_eq!(s0.cross_section_um(), [2.0, 2.0]);
        assert_eq!(s1.cross_section_um(), [2.0, 2.0]);
        assert_eq!(s0.box_indices, vec![0]);
        assert_eq!(s1.box_indices, vec![1]);
        // The physical solid keeps the corner square on the east leg.
        assert_eq!(s0.solid_hi_um[0], 61.0);
        assert_eq!(s1.solid_lo_um[1], 1.0);
        assert_volume_conserved(&path, &boxes);
        assert_permutation_invariant(&boxes);
    }

    #[test]
    fn corner_ownership_does_not_change_the_electrical_path() {
        // Same L, but the corner square is drawn as part of the north leg.
        let boxes = vec![bx(0.0, -1.0, 59.0, 1.0), bx(59.0, -1.0, 61.0, 41.0)];
        let path = classify(boxes.clone()).unwrap();
        let reference = classify(l_corner_in_first_leg()).unwrap();
        let electrical = |p: &OrientedPath| -> Vec<_> {
            p.segments
                .iter()
                .map(|s| (s.axis, s.axis_sign as i64, q3(s.start_um), q3(s.end_um)))
                .collect()
        };
        assert_eq!(electrical(&path), electrical(&reference));
        assert_volume_conserved(&path, &boxes);
        assert_permutation_invariant(&boxes);
    }

    #[test]
    fn unequal_widths_still_conserve_corner_volume() {
        // East leg 2um wide, north leg 4um wide (x [57,61]).
        let boxes = vec![bx(0.0, -1.0, 61.0, 1.0), bx(57.0, 1.0, 61.0, 61.0)];
        let path = classify(boxes.clone()).unwrap();
        assert_eq!(q3(path.segments[0].end_um), q3([59.0, 0.0, 1.0]));
        assert_eq!(path.segments[1].cross_section_um(), [4.0, 2.0]);
        assert_volume_conserved(&path, &boxes);
    }

    /// Vertices (0,40) -> (0,0) -> (50,0) -> (50,40), arriving-leg corners.
    fn u_path() -> Vec<BoxRequest> {
        vec![
            bx(-1.0, -1.0, 1.0, 40.0),
            bx(1.0, -1.0, 51.0, 1.0),
            bx(49.0, 1.0, 51.0, 40.0),
        ]
    }

    #[test]
    fn u_path_traverses_down_across_and_up() {
        let boxes = u_path();
        let path = classify(boxes.clone()).unwrap();
        assert_structure(&path);
        let signed: Vec<(usize, f64)> = path
            .segments
            .iter()
            .map(|s| (s.axis, s.axis_sign))
            .collect();
        assert_eq!(signed, vec![(1, -1.0), (0, 1.0), (1, 1.0)]);
        let vertices: Vec<[i64; 3]> = path.vertices_um().into_iter().map(q3).collect();
        assert_eq!(
            vertices,
            vec![
                q3([0.0, 40.0, 1.0]),
                q3([0.0, 0.0, 1.0]),
                q3([50.0, 0.0, 1.0]),
                q3([50.0, 40.0, 1.0]),
            ]
        );
        assert!((path.total_length_um() - 130.0).abs() < 1e-9);
        assert_volume_conserved(&path, &boxes);
        assert_permutation_invariant(&boxes);
    }

    #[test]
    fn vertical_turn_into_z_is_supported() {
        // An x bar turning up into a z riser of the same y extent.
        let boxes = vec![
            BoxRequest {
                x0_um: 0.0,
                y0_um: -1.0,
                x1_um: 30.0,
                y1_um: 1.0,
                z0_um: 0.0,
                z1_um: 2.0,
            },
            BoxRequest {
                x0_um: 28.0,
                y0_um: -1.0,
                x1_um: 30.0,
                y1_um: 1.0,
                z0_um: 2.0,
                z1_um: 20.0,
            },
        ];
        let path = classify(boxes.clone()).unwrap();
        assert_eq!(path.segments[1].axis, 2);
        assert_eq!(q3(path.segments[0].end_um), q3([29.0, 0.0, 1.0]));
        assert_eq!(q3(path.end_terminal_um()), q3([29.0, 0.0, 20.0]));
        assert_volume_conserved(&path, &boxes);
    }

    // --- the shared spiral benchmark ---------------------------------------

    /// `tests/test_mom_pypeec_cross_validation.py`'s `_spiral_vertices`,
    /// transcribed (TURNS=2, START_SIDE_UM=60, PITCH_GROWTH_UM=15). The native
    /// suite must not import that optional PyPEEC module, so the reference
    /// geometry is reproduced here instead.
    fn spiral_vertices() -> Vec<(f64, f64)> {
        let directions = [(1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0)];
        let (mut x, mut y, mut side) = (0.0, 0.0, 60.0);
        let mut out = vec![(x, y)];
        for leg in 0..8 {
            let (dx, dy) = directions[leg % 4];
            x += dx * side;
            y += dy * side;
            out.push((x, y));
            if leg % 2 == 1 {
                side += 15.0;
            }
        }
        out
    }

    /// `_leg_boxes_and_signs`, transcribed: WIDTH_UM=2 tiled legs (each
    /// interior end pushed forward by half a width) plus each leg's sign.
    fn spiral_boxes_and_signs() -> Vec<(BoxRequest, f64)> {
        let v = spiral_vertices();
        let last = v.len() - 2;
        let step = 1.0;
        let mut out = Vec::new();
        for (index, w) in v.windows(2).enumerate() {
            let ((x0, y0), (x1, y1)) = (w[0], w[1]);
            if (x1 - x0).abs() > (y1 - y0).abs() {
                let sign: f64 = if x1 > x0 { 1.0 } else { -1.0 };
                let a = x0 + if index > 0 { sign * step } else { 0.0 };
                let b = x1 + if index < last { sign * step } else { 0.0 };
                out.push((bx(a.min(b), y0 - step, a.max(b), y0 + step), sign));
            } else {
                let sign: f64 = if y1 > y0 { 1.0 } else { -1.0 };
                let a = y0 + if index > 0 { sign * step } else { 0.0 };
                let b = y1 + if index < last { sign * step } else { 0.0 };
                out.push((bx(x0 - step, a.min(b), x0 + step, a.max(b)), sign));
            }
        }
        out
    }

    #[test]
    fn tiled_two_turn_spiral_matches_the_reference_vertices_and_signs() {
        let (boxes, signs): (Vec<BoxRequest>, Vec<f64>) =
            spiral_boxes_and_signs().into_iter().unzip();
        let path = classify(boxes.clone()).unwrap();
        assert_structure(&path);
        assert_eq!(path.segments.len(), 8);
        assert_volume_conserved(&path, &boxes);

        let reference: Vec<[i64; 3]> = spiral_vertices()
            .into_iter()
            .map(|(x, y)| q3([x, y, 1.0]))
            .collect();

        // Canonical start is the lexicographically smaller terminal: the
        // inner end (-30, -30) sorts before the outer start (0, 0), so the
        // canonical traversal is the reference walked backwards ...
        let canonical: Vec<[i64; 3]> = path.vertices_um().into_iter().map(q3).collect();
        let mut backwards = reference.clone();
        backwards.reverse();
        assert_eq!(canonical, backwards);

        // ... and an explicit reversal reproduces the reference order, leg
        // for leg, with exactly the fixture's signs and box ownership.
        let forward = path.reversed();
        assert_eq!(forward.orientation, PathOrientation::Reversed);
        let fwd: Vec<[i64; 3]> = forward.vertices_um().into_iter().map(q3).collect();
        assert_eq!(fwd, reference);
        for (i, s) in forward.segments.iter().enumerate() {
            assert_eq!(s.box_indices, vec![i], "leg {i} owns its own box");
            assert_eq!(s.axis_sign, signs[i], "leg {i} sign");
            assert_eq!(s.cross_section_um(), [2.0, 2.0]);
        }
        assert_permutation_invariant(&boxes);
    }

    #[test]
    fn explicit_reversal_is_distinguishable_from_permutation() {
        let boxes = u_path();
        let path = classify(boxes.clone()).unwrap();
        let reversed = path.reversed();
        assert_eq!(reversed.orientation, PathOrientation::Reversed);
        assert_ne!(fingerprint(&reversed), fingerprint(&path));
        assert_eq!(reversed.reversed(), path);
        // Reversing the *input* is not a terminal reversal.
        let input_reversed: Vec<BoxRequest> = boxes.into_iter().rev().collect();
        let p = classify(input_reversed).unwrap();
        assert_eq!(p.orientation, PathOrientation::Canonical);
        assert_eq!(fingerprint(&p), fingerprint(&path));
    }

    // --- collinear subdivision ---------------------------------------------

    #[test]
    fn collinear_subdivision_canonicalises_to_the_unsplit_path() {
        let reference = fingerprint(&classify(l_corner_in_first_leg()).unwrap());
        let north = bx(59.0, 1.0, 61.0, 41.0);
        for split in [
            // Lengthwise into two halves.
            vec![bx(0.0, -1.0, 30.0, 1.0), bx(30.0, -1.0, 61.0, 1.0), north],
            // A piece (3x2x2, aspect 1.5) below the individual-bar threshold.
            vec![bx(0.0, -1.0, 58.0, 1.0), bx(58.0, -1.0, 61.0, 1.0), north],
            // Three pieces, enumerated out of order.
            vec![
                bx(40.0, -1.0, 61.0, 1.0),
                north,
                bx(0.0, -1.0, 20.0, 1.0),
                bx(20.0, -1.0, 40.0, 1.0),
            ],
            // Widthwise into two parallel strips.
            vec![bx(0.0, -1.0, 61.0, 0.0), bx(0.0, 0.0, 61.0, 1.0), north],
        ] {
            let path = classify(split.clone()).unwrap();
            assert_eq!(fingerprint(&path), reference, "split {split:?}");
            assert_eq!(path.segments[0].box_indices.len(), split.len() - 1);
            assert_volume_conserved(&path, &split);
            assert_permutation_invariant(&split);
        }
    }

    #[test]
    fn separately_drawn_corner_block_is_rejected_as_ambiguous() {
        let e = err(vec![
            bx(0.0, -1.0, 59.0, 1.0),
            bx(59.0, -1.0, 61.0, 1.0), // the corner square on its own
            bx(59.0, 1.0, 61.0, 41.0),
        ]);
        assert!(e.contains("ambiguous"), "{e}");
    }

    #[test]
    fn merged_segment_must_still_be_bar_shaped() {
        // Two legs that each merge fine but one is a stubby 4x2x2 block.
        let e = err(vec![bx(0.0, -1.0, 61.0, 1.0), bx(59.0, 1.0, 61.0, 3.0)]);
        assert!(e.contains("elongated"), "{e}");
        assert!(e.contains("oriented-path segment"), "{e}");
    }

    // --- explicit rejections -----------------------------------------------

    #[test]
    fn gap_beyond_tolerance_is_disconnected_and_within_tolerance_is_a_contact() {
        let gap = 2.0 * CONTACT_TOL_UM;
        let e = err(vec![
            bx(0.0, -1.0, 61.0, 1.0),
            bx(59.0, 1.0 + gap, 61.0, 41.0),
        ]);
        assert!(e.contains("disconnected"), "{e}");
        let near = 0.5 * CONTACT_TOL_UM;
        let path = classify(vec![
            bx(0.0, -1.0, 61.0, 1.0),
            bx(59.0, 1.0 + near, 61.0, 41.0),
        ])
        .unwrap();
        assert_eq!(path.segments.len(), 2);
    }

    #[test]
    fn overlap_beyond_tolerance_is_rejected() {
        // The overlapping-corner convention of peec.rs' Rust-only spiral
        // fixture: both legs run to the far corner edge.
        let e = err(vec![bx(0.0, -1.0, 61.0, 1.0), bx(59.0, -1.0, 61.0, 41.0)]);
        assert!(e.contains("overlap"), "{e}");
    }

    #[test]
    fn t_junction_branch_is_rejected() {
        let e = err(vec![bx(0.0, -1.0, 61.0, 1.0), bx(29.0, 1.0, 31.0, 41.0)]);
        assert!(e.contains("T-junction"), "{e}");
    }

    #[test]
    fn two_legs_at_one_end_is_a_branch() {
        // North and south stubs both leaving the east leg's far end.
        let e = err(vec![
            bx(0.0, -1.0, 61.0, 1.0),
            bx(59.0, 1.0, 61.0, 41.0),
            bx(59.0, -41.0, 61.0, -1.0),
        ]);
        assert!(e.contains("more than two segments"), "{e}");
    }

    #[test]
    fn zero_extent_box_is_degenerate() {
        let e = err(vec![bx(0.0, -1.0, 61.0, 1.0), bx(59.0, 1.0, 59.0, 41.0)]);
        assert!(e.contains("degenerate"), "{e}");
    }

    #[test]
    fn closed_square_loop_is_rejected() {
        // Four tiled legs closing on themselves.
        let e = err(vec![
            bx(-1.0, -1.0, 41.0, 1.0),
            bx(39.0, 1.0, 41.0, 41.0),
            bx(-1.0, 39.0, 39.0, 41.0),
            bx(-1.0, 1.0, 1.0, 39.0),
        ]);
        assert!(e.contains("closed loop"), "{e}");
    }

    #[test]
    fn edge_only_contact_is_rejected() {
        // Diagonal neighbours sharing only the z-parallel edge at (61, 1).
        let e = err(vec![bx(0.0, -1.0, 61.0, 1.0), bx(61.0, 1.0, 63.0, 41.0)]);
        assert!(e.contains("edge"), "{e}");
    }

    #[test]
    fn stacked_via_like_contact_is_rejected() {
        let e = err(vec![
            bx(0.0, -1.0, 61.0, 1.0),
            BoxRequest {
                x0_um: 59.0,
                y0_um: -1.0,
                x1_um: 61.0,
                y1_um: 40.0,
                z0_um: 2.0,
                z1_um: 4.0,
            },
        ]);
        assert!(e.contains("via-like"), "{e}");
    }

    #[test]
    fn side_by_side_parallel_contact_is_rejected() {
        let e = err(vec![bx(0.0, -1.0, 60.0, 1.0), bx(10.0, 1.0, 80.0, 3.0)]);
        assert!(e.contains("side-by-side"), "{e}");
    }

    #[test]
    fn turn_changing_the_out_of_plane_extent_is_rejected() {
        let e = err(vec![
            bx(0.0, -1.0, 61.0, 1.0),
            BoxRequest {
                x0_um: 59.0,
                y0_um: 1.0,
                x1_um: 61.0,
                y1_um: 41.0,
                z0_um: 0.0,
                z1_um: 3.0,
            },
        ]);
        assert!(e.contains("out-of-plane"), "{e}");
    }

    #[test]
    fn empty_conductor_is_rejected() {
        assert!(err(vec![]).contains("at least one box"));
    }

    // --- backward compatibility and consumers ------------------------------

    #[test]
    fn existing_parallel_bundles_keep_their_classification() {
        // Physically separate, co-axis, co-spanning walls (a coax shield's
        // north/south pair): a parallel bundle, not a "disconnected" path.
        let walls = conductor(vec![bx(0.0, 8.0, 100.0, 9.0), bx(0.0, -9.0, 100.0, -8.0)]);
        assert_eq!(
            classify_conductor_topology(0, &walls).unwrap(),
            ConductorTopology::ParallelBundle
        );
        assert!(classify_bars(std::slice::from_ref(&walls)).is_ok());
        let single = conductor(vec![bx(0.0, 0.0, 100.0, 2.0)]);
        assert_eq!(
            classify_conductor_topology(0, &single).unwrap(),
            ConductorTopology::ParallelBundle
        );
        // Separate parallel boxes that are *not* co-spanning were never a
        // bundle and are not a path either: still rejected.
        let offset = conductor(vec![bx(0.0, 8.0, 100.0, 9.0), bx(30.0, -9.0, 130.0, -8.0)]);
        assert!(classify_conductor_topology(0, &offset).is_err());
    }

    #[test]
    fn winding_is_an_oriented_path_accepted_statically_but_full_wave_still_rejects_it() {
        let (boxes, _): (Vec<BoxRequest>, Vec<f64>) = spiral_boxes_and_signs().into_iter().unzip();
        let spiral = conductor(boxes);
        match classify_conductor_topology(3, &spiral).unwrap() {
            ConductorTopology::OrientedPath(p) => {
                assert_eq!(p.conductor_index, 3);
                assert!(p.segments.iter().all(|s| s.conductor_index == 3));
            }
            other => panic!("expected an oriented path, got {other:?}"),
        }
        // The static PEEC discretisation now accepts it as a series winding
        // (#2729); the full-wave consumer keeps its explicit mixed-axis
        // rejection until #2730 lands.
        let conductors = [spiral];
        let layout = discretize_bars(&conductors, 1.0).unwrap();
        assert_eq!(layout.series_segments.len(), 8);
        let e = classify_full_wave_bars(&conductors).err().unwrap();
        assert!(e.contains("same current-flow axis"), "{e}");
    }

    #[test]
    fn filaments_are_tagged_with_segment_and_follow_the_traversal() {
        let path = classify(u_path()).unwrap();
        let filaments = discretize_oriented_path(&path, 1.0).unwrap();
        // 2x2um cross-section at 1um -> 4 filaments per leg, 3 legs.
        assert_eq!(filaments.len(), 12);
        for s in &path.segments {
            let mine: Vec<&PathFilament> = filaments
                .iter()
                .filter(|f| f.segment_index == s.segment_index)
                .collect();
            assert_eq!(mine.len(), 4);
            let area: f64 = mine.iter().map(|f| f.filament.area_um2).sum();
            assert!((area - s.area_um2()).abs() < 1e-9);
            for f in mine {
                assert_eq!(f.filament.conductor_index, 0);
                assert!((f.filament.length_um() - s.length_um()).abs() < 1e-9);
                let d = f.filament.end_um[s.axis] - f.filament.start_um[s.axis];
                assert_eq!(d.signum(), s.axis_sign);
            }
        }
        assert!(discretize_oriented_path(&path, 0.0).is_err());
        assert!(discretize_oriented_path(&path, 1e-4)
            .unwrap_err()
            .contains("filaments"));
    }
}
