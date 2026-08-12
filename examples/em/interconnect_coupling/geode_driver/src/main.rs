//! klayout-tools reproducibility-seam driver for issue #842 — not part of
//! [geode-fem](https://github.com/rjwalters/geode-fem) upstream, and never
//! committed there (geode-fem is a separate, MIT sibling project this repo
//! wraps, not vendors -- see `docs/design/em-field-sim-spike.md` Section 5).
//! `examples/em/interconnect_coupling/generate.py` in *this* repo copies
//! this crate into a local geode-fem checkout's `examples/` directory,
//! drives it with real GDS-derived geometry, and converts its output into
//! the site export format -- the same "separate, already-built checkout"
//! pattern `examples/em/patch_antenna/generate.py` established for the
//! patch-antenna benchmark (issue #849).
//!
//! Electrostatic coupling-capacitance extraction on a hand-authored
//! structured-grid tet mesh of two coplanar rectangular PEC "rails"
//! (real sky130 met1 power-rail dimensions, passed in via CLI flags) inside
//! a grounded box. Mirrors `two_sphere_box_mesh`
//! (`crates/geode-core/src/mesh/electrostatic_fixtures.rs`) closely: same
//! uniform-grid + geometric-membership-classification approach, same
//! `assemble_electrostatic` / `extract_capacitance` pipeline, just box
//! conductors instead of spheres.
//!
//! Emits one JSON document to `--out` (or stdout) with:
//!   - the driven potential field (VPWR at 1V, VGND at 0V, rho=0) sampled on
//!     the mid-length (x = length/2) cross-section, as a renderable 2-D
//!     triangle mesh (vertices + cells + per-vertex `phi_v`, `|E|`, `E`
//!     vector) — a genuine solved-and-sliced field, the same operation
//!     `examples/em/patch_antenna/generate.py` performs on geode-fem's own
//!     volumetric output.
//!   - the full N=2 Maxwell capacitance matrix (self + mutual).
//!   - basic mesh stats.

use std::collections::BTreeMap;
use std::env;
use std::fs;
use std::path::PathBuf;

use geode_core::assembly::electrostatic::{
    ConductorSurface, EPS_0, Electrode, assemble_electrostatic, extract_capacitance,
};
use geode_core::mesh::TetMesh;
use serde::Serialize;

struct Args {
    /// In-plane rail width (extent perpendicular to the rail's length), um.
    rail_width_um: f64,
    /// Rail length (extrusion axis, x), um.
    rail_length_um: f64,
    /// Metal thickness (z), um.
    thickness_um: f64,
    /// Edge-to-edge gap between the two rails, um.
    gap_um: f64,
    /// Relative permittivity of the surrounding dielectric (uniform).
    eps_r: f64,
    /// Margin added around the two-rail cross-section before the outer
    /// grounded box wall, um.
    margin_um: f64,
    /// Grid step target, um (uniform in y/z; x uses a coarser step).
    step_um: f64,
    /// Extrusion (x) divisions.
    n_x: usize,
    out: Option<PathBuf>,
}

impl Args {
    fn parse() -> Self {
        let mut a = Args {
            rail_width_um: 0.48,
            rail_length_um: 2.3,
            thickness_um: 0.35,
            gap_um: 2.24,
            eps_r: 4.0,
            margin_um: 1.0,
            step_um: 0.08,
            n_x: 6,
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
                "--rail-width-um" => a.rail_width_um = next().parse().unwrap(),
                "--rail-length-um" => a.rail_length_um = next().parse().unwrap(),
                "--thickness-um" => a.thickness_um = next().parse().unwrap(),
                "--gap-um" => a.gap_um = next().parse().unwrap(),
                "--eps-r" => a.eps_r = next().parse().unwrap(),
                "--margin-um" => a.margin_um = next().parse().unwrap(),
                "--step-um" => a.step_um = next().parse().unwrap(),
                "--n-x" => a.n_x = next().parse().unwrap(),
                "--out" => a.out = Some(PathBuf::from(next())),
                other => panic!("unknown flag {other}"),
            }
            i += 1;
        }
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
    n_vgnd_nodes: usize,
    n_vpwr_nodes: usize,
    n_ground_nodes: usize,
}

#[derive(Serialize)]
struct CapacitanceOut {
    /// Electrode name order: ["vgnd", "vpwr"].
    names: Vec<String>,
    /// 2x2 Maxwell capacitance matrix, farads.
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
    /// `[y, z]` per vertex, um.
    vertices_yz_um: Vec<[f64; 2]>,
    /// Triangle indices into `vertices_yz_um`.
    cells: Vec<[u32; 3]>,
    /// Driven potential (VPWR=1V, VGND=0V), volts, per vertex.
    phi_v: Vec<f64>,
    /// |E| = |grad phi| magnitude, V/um, per vertex (nodal average of the
    /// adjacent tets' constant-gradient E field).
    e_mag_v_per_um: Vec<f64>,
    /// E vector (V/um) `[Ex, Ey, Ez]` per vertex, same nodal-average basis.
    e_vec_v_per_um: Vec<[f64; 3]>,
}

#[derive(Serialize)]
struct ParamsOut {
    rail_width_um: f64,
    rail_length_um: f64,
    thickness_um: f64,
    gap_um: f64,
    eps_r: f64,
    margin_um: f64,
    step_um: f64,
    n_x: usize,
    eps_0_f_per_m: f64,
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

    // y layout: [y_min, 0] margin | [0, w] VGND | [w, w+gap] gap
    //   | [w+gap, 2w+gap] VPWR | [2w+gap, y_max] margin.
    let w = args.rail_width_um;
    let gap = args.gap_um;
    let y_min = -args.margin_um;
    let y_max = 2.0 * w + gap + args.margin_um;
    let z_min = -args.margin_um;
    let z_max = args.thickness_um + args.margin_um;
    let x_max = args.rail_length_um;

    let n_y = ((y_max - y_min) / args.step_um).round().max(4.0) as usize;
    let n_z = ((z_max - z_min) / args.step_um).round().max(4.0) as usize;
    let n_x = args.n_x.max(1);

    let nx1 = n_x + 1;
    let ny1 = n_y + 1;
    let nz1 = n_z + 1;
    let node_idx = |i: usize, j: usize, k: usize| -> u32 { (i + j * nx1 + k * nx1 * ny1) as u32 };

    // `EPS_0` is the literal SI vacuum permittivity (F/m) -- see its own
    // doc comment -- so node coordinates MUST be in meters for the
    // extracted capacitance to be a physically meaningful on-chip value.
    // Everything above this point is computed in micrometers (the natural
    // unit for GDS-derived dimensions); convert once here.
    const UM_TO_M: f64 = 1e-6;
    let mut nodes: Vec<[f64; 3]> = Vec::with_capacity(nx1 * ny1 * nz1);
    for k in 0..nz1 {
        let z = z_min + (z_max - z_min) * k as f64 / n_z as f64;
        for j in 0..ny1 {
            let y = y_min + (y_max - y_min) * j as f64 / n_y as f64;
            for i in 0..nx1 {
                let x = x_max * i as f64 / n_x as f64;
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

    // `nodes` is now in meters; classify against meter-scale bounds
    // (`eps` well below the grid spacing, ~1e-7--1e-8 m, but far above
    // f64 rounding noise at this magnitude).
    let eps = 1e-15;
    let in_range = |v: f64, lo: f64, hi: f64| v >= lo - eps && v <= hi + eps;
    let thickness_m = args.thickness_um * UM_TO_M;
    let w_m = w * UM_TO_M;
    let gap_m = gap * UM_TO_M;

    let mut vgnd = Vec::new();
    let mut vpwr = Vec::new();
    let mut ground = Vec::new();
    for k in 0..nz1 {
        for j in 0..ny1 {
            for i in 0..nx1 {
                let idx = node_idx(i, j, k);
                let p = nodes[idx as usize];
                let (_x, y, z) = (p[0], p[1], p[2]);
                let in_z = in_range(z, 0.0, thickness_m);
                if in_z && in_range(y, 0.0, w_m) {
                    vgnd.push(idx);
                } else if in_z && in_range(y, w_m + gap_m, 2.0 * w_m + gap_m) {
                    vpwr.push(idx);
                } else if j == 0 || j == n_y || k == 0 || k == n_z {
                    // Outer y/z box walls are the return conductor
                    // ("ground"); x=0/x=n_x ends are left natural
                    // (Neumann), matching `coax_shell_mesh`'s treatment of
                    // its extrusion axis so the finite-length result
                    // approximates a per-length quantity away from the
                    // ends.
                    ground.push(idx);
                }
            }
        }
    }

    let n_tets = tets.len();
    let mesh = TetMesh { nodes: nodes.clone(), tets: tets.clone(), physical_groups: Default::default() };
    let eps_r_per_tet = vec![args.eps_r; n_tets];
    let rho = vec![0.0_f64; n_tets];

    let electrodes = vec![
        Electrode { name: "vgnd".into(), nodes: vgnd.clone(), voltage: 0.0 },
        Electrode { name: "vpwr".into(), nodes: vpwr.clone(), voltage: 1.0 },
    ];

    let sys = assemble_electrostatic(&mesh, &eps_r_per_tet, &rho, &electrodes, &ground)
        .expect("assemble_electrostatic failed");

    let cm = extract_capacitance(&sys, &mesh, &eps_r_per_tet, &electrodes, &ground, &[] as &[ConductorSurface])
        .expect("extract_capacitance failed");

    // Driven forward solve (VPWR=1V, VGND=0V, rho=0) for the field picture.
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
        .map(|(s, &n)| if n > 0 { [s[0] / n as f64, s[1] / n as f64, s[2] / n as f64] } else { [0.0; 3] })
        .collect();

    // Mid-length cross-section slice (structured grid: just the i = n_x/2
    // node layer -- already grid-aligned, no marching-tetrahedra needed).
    let i_mid = n_x / 2;
    let x_um = x_max * i_mid as f64 / n_x as f64;
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
            // `nodes`/`e_nodal` are in meters / V-per-meter; convert back
            // to micrometers / V-per-micrometer for the exported slice.
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
            n_vgnd_nodes: vgnd.len(),
            n_vpwr_nodes: vpwr.len(),
            n_ground_nodes: ground.len(),
        },
        capacitance: CapacitanceOut {
            names: cm.names.clone(),
            c_farad: cm.c.clone(),
            c_flux_diag_farad: cm.c_flux_diag.clone(),
            max_rel_asymmetry: cm.max_rel_asymmetry(),
        },
        slice: SliceOut { x_um, vertices_yz_um, cells, phi_v, e_mag_v_per_um: e_mag_v, e_vec_v_per_um: e_vec_v },
        params: ParamsOut {
            rail_width_um: args.rail_width_um,
            rail_length_um: args.rail_length_um,
            thickness_um: args.thickness_um,
            gap_um: args.gap_um,
            eps_r: args.eps_r,
            margin_um: args.margin_um,
            step_um: args.step_um,
            n_x: args.n_x,
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

/// Constant gradient of the P1 basis over one tet (cofactor-vector form,
/// same closed form the module doc of `assembly::electrostatic` cites),
/// returning `E = -grad(phi)`.
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
