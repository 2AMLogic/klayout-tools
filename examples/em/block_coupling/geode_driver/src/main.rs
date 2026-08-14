//! klayout-tools reproducibility-seam driver for issue #958 (Epic #840
//! Phase 3a) — not part of [geode-fem](https://github.com/rjwalters/geode-fem)
//! upstream, and never committed there (geode-fem is a separate, MIT sibling
//! project this repo wraps, not vendors -- see
//! `docs/design/em-field-sim-spike.md` Section 5).
//! `examples/em/block_coupling/generate.py` in *this* repo copies this crate
//! into a local geode-fem checkout's `examples/` directory, drives it with
//! geometry read out of a real gallery block's committed GDS, and converts
//! its output into the site export format -- the same "separate,
//! already-built checkout" pattern `examples/em/patch_antenna/generate.py`
//! (issue #849) and `examples/em/interconnect_coupling/generate.py` (issue
//! #842) established.
//!
//! ## What this generalises
//!
//! `interconnect_coupling`'s driver hard-codes *one* geometry family: two
//! equal-width coplanar rails, symmetric margins, no notion of where the
//! metal actually sits in the process stack. A real analog block's routing
//! is none of those things, so this driver takes:
//!
//!   - **N conductors**, each an arbitrary set of `y` intervals (a net that
//!     crosses the cut line more than once -- a return path on both sides of
//!     a signal, a routing loop -- is *one* electrode with several intervals,
//!     because it is one net).
//!   - **Real widths and spacings**, via a *breakpoint-aligned* structured
//!     grid: every conductor edge is exactly a grid plane, and the requested
//!     `--step-um` only sets how finely the spans *between* breakpoints are
//!     subdivided. A uniform grid would quantise each real GDS width/space to
//!     the step and silently solve a different structure.
//!   - **The real process stackup**: the conductor layer sits at its true
//!     height `--metal-bottom-um` above a grounded substrate plane at `z = 0`
//!     (from the open PDK's own published metal stack), rather than floating
//!     in a symmetric box. Substrate capacitance is the dominant term for
//!     on-chip routing, so this is what makes the extracted matrix comparable
//!     to the PDK's own area/fringe parasitic model.
//!
//! ## Model
//!
//! A 2.5-D extrusion: the cross-section is meshed in `(y, z)` and extruded
//! `--length-um` along `x` (the direction the bundle actually runs uniformly
//! in the source layout, measured from the GDS by `generate.py`). The
//! substrate plane (`z = 0`), the box top (`z = metal_bottom + thickness +
//! top_margin`) and the two side walls are the grounded return conductor; the
//! `x` ends are left natural (Neumann), the same treatment `coax_shell_mesh`
//! (`crates/geode-core/src/mesh/electrostatic_fixtures.rs`) gives its own
//! extrusion axis, so the finite-length result approximates a per-length
//! quantity away from the ends.
//!
//! Emits one JSON document to `--out` (or stdout) with the N x N Maxwell
//! capacitance matrix, the driven potential/field on the mid-length
//! cross-section as a renderable 2-D triangle mesh, and mesh stats.

use std::collections::BTreeMap;
use std::env;
use std::fs;
use std::path::PathBuf;

use geode_core::assembly::electrostatic::{
    ConductorSurface, EPS_0, Electrode, assemble_electrostatic, extract_capacitance,
};
use geode_core::mesh::TetMesh;
use serde::Serialize;

/// One net's cross-section: its name plus every `y` interval (um) it
/// occupies on the cut line.
#[derive(Clone, Debug)]
struct Conductor {
    name: String,
    intervals: Vec<(f64, f64)>,
}

struct Args {
    conductors: Vec<Conductor>,
    /// Height of the conductor layer's bottom face above the grounded
    /// substrate plane, um (open-PDK stackup value).
    metal_bottom_um: f64,
    /// Conductor thickness (z), um.
    thickness_um: f64,
    /// Uniform parallel-run length along the extrusion axis, um.
    length_um: f64,
    /// Relative permittivity of the surrounding dielectric (uniform).
    eps_r: f64,
    /// Distance from the outermost conductor edge to each side wall, um.
    side_margin_um: f64,
    /// Distance from the conductor top face to the box top wall, um.
    top_margin_um: f64,
    /// Target subdivision of each breakpoint-bounded span, um.
    step_um: f64,
    /// Extrusion (x) divisions.
    n_x: usize,
    /// Which conductor is driven to 1 V in the exported field frame.
    drive: Option<String>,
    out: Option<PathBuf>,
}

fn parse_conductor(spec: &str) -> Conductor {
    // NAME:y0:y1[,y0:y1]...
    let (name, rest) = spec
        .split_once(':')
        .unwrap_or_else(|| panic!("--conductor needs NAME:y0:y1[,y0:y1...], got {spec:?}"));
    let mut intervals = Vec::new();
    for part in rest.split(',') {
        let (a, b) = part
            .split_once(':')
            .unwrap_or_else(|| panic!("--conductor interval needs y0:y1, got {part:?}"));
        let y0: f64 = a.parse().unwrap_or_else(|_| panic!("bad y0 in {part:?}"));
        let y1: f64 = b.parse().unwrap_or_else(|_| panic!("bad y1 in {part:?}"));
        assert!(y1 > y0, "--conductor interval must have y1 > y0, got {part:?}");
        intervals.push((y0, y1));
    }
    assert!(!intervals.is_empty(), "--conductor {name:?} has no intervals");
    Conductor { name: name.to_string(), intervals }
}

impl Args {
    fn parse() -> Self {
        let mut a = Args {
            conductors: Vec::new(),
            metal_bottom_um: 1.3761,
            thickness_um: 0.36,
            length_um: 10.0,
            eps_r: 4.0,
            side_margin_um: 2.0,
            top_margin_um: 1.5,
            step_um: 0.12,
            n_x: 6,
            drive: None,
            out: None,
        };
        let argv: Vec<String> = env::args().collect();
        let mut i = 1;
        while i < argv.len() {
            let flag = argv[i].as_str();
            let mut next = || {
                i += 1;
                argv.get(i).unwrap_or_else(|| panic!("{flag} needs a value")).clone()
            };
            match flag {
                "--conductor" => a.conductors.push(parse_conductor(&next())),
                "--metal-bottom-um" => a.metal_bottom_um = next().parse().unwrap(),
                "--thickness-um" => a.thickness_um = next().parse().unwrap(),
                "--length-um" => a.length_um = next().parse().unwrap(),
                "--eps-r" => a.eps_r = next().parse().unwrap(),
                "--side-margin-um" => a.side_margin_um = next().parse().unwrap(),
                "--top-margin-um" => a.top_margin_um = next().parse().unwrap(),
                "--step-um" => a.step_um = next().parse().unwrap(),
                "--n-x" => a.n_x = next().parse().unwrap(),
                "--drive" => a.drive = Some(next()),
                "--out" => a.out = Some(PathBuf::from(next())),
                other => panic!("unknown flag {other}"),
            }
            i += 1;
        }
        assert!(
            a.conductors.len() >= 2,
            "need at least two --conductor specs to extract a coupling matrix"
        );
        a
    }
}

#[derive(Serialize)]
struct Output {
    mesh_stats: MeshStats,
    capacitance: CapacitanceOut,
    slice: SliceOut,
    params: ParamsOut,
}

#[derive(Serialize)]
struct MeshStats {
    n_nodes: usize,
    n_tets: usize,
    n_y: usize,
    n_z: usize,
    n_x: usize,
    n_ground_nodes: usize,
    /// Per-conductor electrode node counts, in `--conductor` order.
    n_electrode_nodes: Vec<usize>,
}

#[derive(Serialize)]
struct CapacitanceOut {
    /// Electrode name order (as given on the command line).
    names: Vec<String>,
    /// N x N Maxwell capacitance matrix, farads.
    c_farad: Vec<Vec<f64>>,
    /// Surface-flux cross-check of the diagonal, farads (None entries
    /// serialize as `null`).
    c_flux_diag_farad: Vec<Option<f64>>,
    max_rel_asymmetry: f64,
}

#[derive(Serialize)]
struct SliceOut {
    /// x position (um) the cross-section was taken at.
    x_um: f64,
    /// Which conductor was held at 1 V for this field frame.
    driven: String,
    /// `[y, z]` per vertex, um.
    vertices_yz_um: Vec<[f64; 2]>,
    /// Triangle indices into `vertices_yz_um`.
    cells: Vec<[u32; 3]>,
    /// Driven potential, volts, per vertex.
    phi_v: Vec<f64>,
    /// |E| = |grad phi| magnitude, V/um, per vertex (nodal average of the
    /// adjacent tets' constant-gradient E field).
    e_mag_v_per_um: Vec<f64>,
    /// E vector (V/um) `[Ex, Ey, Ez]` per vertex, same nodal-average basis.
    e_vec_v_per_um: Vec<[f64; 3]>,
}

#[derive(Serialize)]
struct ParamsOut {
    conductors: Vec<ConductorOut>,
    metal_bottom_um: f64,
    thickness_um: f64,
    length_um: f64,
    eps_r: f64,
    side_margin_um: f64,
    top_margin_um: f64,
    step_um: f64,
    n_x: usize,
    y_min_um: f64,
    y_max_um: f64,
    z_min_um: f64,
    z_max_um: f64,
    eps_0_f_per_m: f64,
}

#[derive(Serialize)]
struct ConductorOut {
    name: String,
    intervals_um: Vec<[f64; 2]>,
    /// Total cross-section width (sum of interval widths), um.
    width_um: f64,
}

/// Subdivide the sorted, deduplicated `breaks` so every resulting span is at
/// most `step` long, keeping each breakpoint exactly on a grid plane.
fn graded_axis(breaks: &[f64], step: f64) -> Vec<f64> {
    assert!(breaks.len() >= 2, "need at least two breakpoints");
    let mut axis = vec![breaks[0]];
    for w in breaks.windows(2) {
        let (lo, hi) = (w[0], w[1]);
        let span = hi - lo;
        if span <= 0.0 {
            continue;
        }
        let n = ((span / step).ceil() as usize).max(1);
        for i in 1..=n {
            axis.push(lo + span * i as f64 / n as f64);
        }
    }
    axis
}

fn dedup_sorted(mut v: Vec<f64>, tol: f64) -> Vec<f64> {
    v.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let mut out: Vec<f64> = Vec::with_capacity(v.len());
    for x in v {
        if out.last().is_none_or(|last| (x - last).abs() > tol) {
            out.push(x);
        }
    }
    out
}

fn push_hex_tets(tets: &mut Vec<[u32; 4]>, c: &[u32; 8], nodes: &[[f64; 3]]) {
    // Same c[0]->c[6] main-diagonal 6-tet split geode-core's own
    // `two_sphere_box_mesh` uses (`crates/geode-core/src/mesh/electrostatic_fixtures.rs`),
    // reimplemented here (this crate has no access to that module's private
    // helper) -- a globally-consistent corner-ordering split is conforming
    // across a structured grid.
    const SPLIT: [[usize; 4]; 6] = [
        [0, 1, 2, 6],
        [0, 2, 3, 6],
        [0, 3, 7, 6],
        [0, 7, 4, 6],
        [0, 4, 5, 6],
        [0, 5, 1, 6],
    ];
    for s in &SPLIT {
        tets.push(oriented_tet([c[s[0]], c[s[1]], c[s[2]], c[s[3]]], nodes));
    }
}

fn oriented_tet(t: [u32; 4], nodes: &[[f64; 3]]) -> [u32; 4] {
    let v0 = nodes[t[0] as usize];
    let e1 = sub3(nodes[t[1] as usize], v0);
    let e2 = sub3(nodes[t[2] as usize], v0);
    let e3 = sub3(nodes[t[3] as usize], v0);
    let det = dot3(e1, cross3(e2, e3));
    if det < 0.0 { [t[0], t[1], t[3], t[2]] } else { t }
}

fn sub3(a: [f64; 3], b: [f64; 3]) -> [f64; 3] {
    [a[0] - b[0], a[1] - b[1], a[2] - b[2]]
}
fn cross3(a: [f64; 3], b: [f64; 3]) -> [f64; 3] {
    [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]
}
fn dot3(a: [f64; 3], b: [f64; 3]) -> f64 {
    a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
}

fn main() {
    let args = Args::parse();

    let y_lo = args
        .conductors
        .iter()
        .flat_map(|c| c.intervals.iter().map(|i| i.0))
        .fold(f64::INFINITY, f64::min);
    let y_hi = args
        .conductors
        .iter()
        .flat_map(|c| c.intervals.iter().map(|i| i.1))
        .fold(f64::NEG_INFINITY, f64::max);

    let y_min = y_lo - args.side_margin_um;
    let y_max = y_hi + args.side_margin_um;
    let z_min = 0.0;
    let z_metal_lo = args.metal_bottom_um;
    let z_metal_hi = args.metal_bottom_um + args.thickness_um;
    let z_max = z_metal_hi + args.top_margin_um;

    // Breakpoint-aligned axes: every real conductor edge is a grid plane, so
    // the solved widths/spacings are the GDS's own, not step-quantised ones.
    let mut y_breaks = vec![y_min, y_max];
    for c in &args.conductors {
        for (a, b) in &c.intervals {
            y_breaks.push(*a);
            y_breaks.push(*b);
        }
    }
    let y_axis = graded_axis(&dedup_sorted(y_breaks, 1e-9), args.step_um);
    let z_axis = graded_axis(
        &dedup_sorted(vec![z_min, z_metal_lo, z_metal_hi, z_max], 1e-9),
        args.step_um,
    );

    let n_y = y_axis.len() - 1;
    let n_z = z_axis.len() - 1;
    let n_x = args.n_x.max(1);
    let nx1 = n_x + 1;
    let ny1 = y_axis.len();
    let nz1 = z_axis.len();
    let node_idx = |i: usize, j: usize, k: usize| -> u32 { (i + j * nx1 + k * nx1 * ny1) as u32 };

    // `EPS_0` is the literal SI vacuum permittivity (F/m) -- see its own doc
    // comment -- so node coordinates MUST be in meters for the extracted
    // capacitance to be a physically meaningful on-chip value. Everything
    // above this point is in micrometers (the natural unit for GDS-derived
    // dimensions); convert once here.
    const UM_TO_M: f64 = 1e-6;
    let mut nodes: Vec<[f64; 3]> = Vec::with_capacity(nx1 * ny1 * nz1);
    for &z in &z_axis {
        for &y in &y_axis {
            for i in 0..nx1 {
                let x = args.length_um * i as f64 / n_x as f64;
                nodes.push([x * UM_TO_M, y * UM_TO_M, z * UM_TO_M]);
            }
        }
    }

    let mut tets: Vec<[u32; 4]> = Vec::new();
    for k in 0..n_z {
        for j in 0..n_y {
            for i in 0..n_x {
                let c = [
                    node_idx(i, j, k),
                    node_idx(i + 1, j, k),
                    node_idx(i + 1, j + 1, k),
                    node_idx(i, j + 1, k),
                    node_idx(i, j, k + 1),
                    node_idx(i + 1, j, k + 1),
                    node_idx(i + 1, j + 1, k + 1),
                    node_idx(i, j + 1, k + 1),
                ];
                push_hex_tets(&mut tets, &c, &nodes);
            }
        }
    }

    // Classify in micrometers against the same breakpoints the axes were
    // built from; `eps` is far below the finest real feature (0.24 um
    // spacings in the source layouts) and far above f64 rounding noise.
    let eps = 1e-9;
    let in_range = |v: f64, lo: f64, hi: f64| v >= lo - eps && v <= hi + eps;

    let mut electrode_nodes: Vec<Vec<u32>> = vec![Vec::new(); args.conductors.len()];
    let mut ground: Vec<u32> = Vec::new();
    for (k, &z) in z_axis.iter().enumerate() {
        let in_metal_z = in_range(z, z_metal_lo, z_metal_hi);
        for (j, &y) in y_axis.iter().enumerate() {
            let owner = if in_metal_z {
                args.conductors
                    .iter()
                    .position(|c| c.intervals.iter().any(|(a, b)| in_range(y, *a, *b)))
            } else {
                None
            };
            for i in 0..nx1 {
                let idx = node_idx(i, j, k);
                match owner {
                    Some(c) => electrode_nodes[c].push(idx),
                    // Substrate plane (k == 0), box top and the two side
                    // walls are the grounded return conductor; the x ends
                    // stay natural (Neumann).
                    None if j == 0 || j == n_y || k == 0 || k == n_z => ground.push(idx),
                    None => {}
                }
            }
        }
    }
    for (c, n) in args.conductors.iter().zip(&electrode_nodes) {
        assert!(
            !n.is_empty(),
            "conductor {:?} captured no mesh nodes -- reduce --step-um",
            c.name
        );
    }

    let n_tets = tets.len();
    let mesh =
        TetMesh { nodes: nodes.clone(), tets: tets.clone(), physical_groups: Default::default() };
    let eps_r_per_tet = vec![args.eps_r; n_tets];
    let rho = vec![0.0_f64; n_tets];

    let driven = args.drive.clone().unwrap_or_else(|| args.conductors[0].name.clone());
    assert!(
        args.conductors.iter().any(|c| c.name == driven),
        "--drive {driven:?} is not one of the --conductor names"
    );

    let electrodes: Vec<Electrode> = args
        .conductors
        .iter()
        .zip(&electrode_nodes)
        .map(|(c, n)| Electrode {
            name: c.name.clone(),
            nodes: n.clone(),
            voltage: if c.name == driven { 1.0 } else { 0.0 },
        })
        .collect();

    let sys = assemble_electrostatic(&mesh, &eps_r_per_tet, &rho, &electrodes, &ground)
        .expect("assemble_electrostatic failed");

    let cm = extract_capacitance(
        &sys,
        &mesh,
        &eps_r_per_tet,
        &electrodes,
        &ground,
        &[] as &[ConductorSurface],
    )
    .expect("extract_capacitance failed");

    // Driven forward solve (driven conductor at 1 V, every other conductor
    // and the box at 0 V, rho = 0) for the field picture.
    let phi = sys.solve().expect("forward solve failed");

    // Per-node E = -grad(phi), nodal-averaged over incident tets (constant
    // per tet for P1 elements).
    let mut e_sum = vec![[0.0_f64; 3]; nodes.len()];
    let mut e_count = vec![0u32; nodes.len()];
    for tet in &tets {
        let v = tet.map(|i| nodes[i as usize]);
        let phi_t = tet.map(|i| phi[i as usize]);
        let e = neg_grad_p1(&v, &phi_t);
        for &i in tet {
            let idx = i as usize;
            e_sum[idx][0] += e[0];
            e_sum[idx][1] += e[1];
            e_sum[idx][2] += e[2];
            e_count[idx] += 1;
        }
    }
    let e_nodal: Vec<[f64; 3]> = e_sum
        .iter()
        .zip(&e_count)
        .map(|(s, &n)| {
            if n > 0 {
                [s[0] / n as f64, s[1] / n as f64, s[2] / n as f64]
            } else {
                [0.0; 3]
            }
        })
        .collect();

    // Mid-length cross-section slice (structured grid: just the i = n_x/2
    // node layer -- already grid-aligned, no marching-tetrahedra needed).
    let i_mid = n_x / 2;
    let x_um = args.length_um * i_mid as f64 / n_x as f64;
    let mut slice_index: BTreeMap<(usize, usize), u32> = BTreeMap::new();
    let mut vertices_yz_um = Vec::new();
    let mut phi_v = Vec::new();
    let mut e_mag_v = Vec::new();
    let mut e_vec_v = Vec::new();
    for k in 0..nz1 {
        for j in 0..ny1 {
            let idx = node_idx(i_mid, j, k);
            let p = nodes[idx as usize];
            slice_index.insert((j, k), vertices_yz_um.len() as u32);
            // `nodes`/`e_nodal` are in meters / V-per-meter; convert back to
            // micrometers / V-per-micrometer for the exported slice.
            vertices_yz_um.push([p[1] / UM_TO_M, p[2] / UM_TO_M]);
            phi_v.push(phi[idx as usize]);
            let e_m = e_nodal[idx as usize];
            let e = [e_m[0] * UM_TO_M, e_m[1] * UM_TO_M, e_m[2] * UM_TO_M];
            e_mag_v.push((e[0] * e[0] + e[1] * e[1] + e[2] * e[2]).sqrt());
            e_vec_v.push(e);
        }
    }
    let mut cells = Vec::new();
    for k in 0..n_z {
        for j in 0..n_y {
            let p00 = slice_index[&(j, k)];
            let p10 = slice_index[&(j + 1, k)];
            let p01 = slice_index[&(j, k + 1)];
            let p11 = slice_index[&(j + 1, k + 1)];
            cells.push([p00, p10, p11]);
            cells.push([p00, p11, p01]);
        }
    }

    let out = Output {
        mesh_stats: MeshStats {
            n_nodes: nodes.len(),
            n_tets: tets.len(),
            n_y,
            n_z,
            n_x,
            n_ground_nodes: ground.len(),
            n_electrode_nodes: electrode_nodes.iter().map(Vec::len).collect(),
        },
        capacitance: CapacitanceOut {
            names: cm.names.clone(),
            c_farad: cm.c.clone(),
            c_flux_diag_farad: cm.c_flux_diag.clone(),
            max_rel_asymmetry: cm.max_rel_asymmetry(),
        },
        slice: SliceOut {
            x_um,
            driven: driven.clone(),
            vertices_yz_um,
            cells,
            phi_v,
            e_mag_v_per_um: e_mag_v,
            e_vec_v_per_um: e_vec_v,
        },
        params: ParamsOut {
            conductors: args
                .conductors
                .iter()
                .map(|c| ConductorOut {
                    name: c.name.clone(),
                    intervals_um: c.intervals.iter().map(|(a, b)| [*a, *b]).collect(),
                    width_um: c.intervals.iter().map(|(a, b)| b - a).sum(),
                })
                .collect(),
            metal_bottom_um: args.metal_bottom_um,
            thickness_um: args.thickness_um,
            length_um: args.length_um,
            eps_r: args.eps_r,
            side_margin_um: args.side_margin_um,
            top_margin_um: args.top_margin_um,
            step_um: args.step_um,
            n_x,
            y_min_um: y_min,
            y_max_um: y_max,
            z_min_um: z_min,
            z_max_um: z_max,
            eps_0_f_per_m: EPS_0,
        },
    };

    let json = serde_json::to_string_pretty(&out).unwrap();
    match args.out {
        Some(path) => {
            fs::write(&path, json).unwrap();
            eprintln!("wrote {}", path.display());
        }
        None => println!("{json}"),
    }
}

/// Constant gradient of the P1 basis over one tet (cofactor-vector form, same
/// closed form the module doc of `assembly::electrostatic` cites), returning
/// `E = -grad(phi)`.
fn neg_grad_p1(v: &[[f64; 3]; 4], phi: &[f64; 4]) -> [f64; 3] {
    let e1 = sub3(v[1], v[0]);
    let e2 = sub3(v[2], v[0]);
    let e3 = sub3(v[3], v[0]);
    let g1 = cross3(e2, e3);
    let g2 = cross3(e3, e1);
    let g3 = cross3(e1, e2);
    let g0 = [-(g1[0] + g2[0] + g3[0]), -(g1[1] + g2[1] + g3[1]), -(g1[2] + g2[2] + g3[2])];
    let det = dot3(e1, g1);
    let grads = [g0, g1, g2, g3];
    let mut grad_phi = [0.0_f64; 3];
    for a in 0..4 {
        let coeff = phi[a] / det;
        grad_phi[0] += coeff * grads[a][0];
        grad_phi[1] += coeff * grads[a][1];
        grad_phi[2] += coeff * grads[a][2];
    }
    [-grad_phi[0], -grad_phi[1], -grad_phi[2]]
}
