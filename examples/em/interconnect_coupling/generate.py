#!/usr/bin/env python3
"""Generate the geode-fem site export for the interconnect-coupling
benchmark (issue #842) -- the Electromagnetics gallery's *real fleet
geometry* entry, extending the export format issue #849 shipped (only the
patch-antenna benchmark) with a second, `capacitance`-shaped instance.

**Geometry: not a hand-picked textbook shape.** Unlike the patch antenna
(geode-fem's own bundled benchmark fixture), this script reads the actual
committed GDS of a real klayout-tools gallery block -- by default
`blocks/sky130_fd_sc_hd__nand2_2/output/sky130_fd_sc_hd__nand2_2.gds` -- and
extracts its two real `met1` (sky130 GDS layer 68/20) power rails (VGND,
VPWR). As of this issue (2026-08-12), no fleet-designed *analog* canary
(`gf180-bandgap`, `sky130-bandgap`) has a GDS layout yet (both are
pre-layout, `status: "in design -- simulation evidence"`) and
`sky130-ota-5t` has no `output/layout.json` at all (#104) -- see this
issue's own Curator comment. The seven `status: "ok"` blocks are sky130/
gf180 standard-cell library cells from the #4 test corpus, not agent-
designed analog blocks, but they carry real, DRC-clean metal routing, and
the issue's own scope explicitly allows "an on-chip interconnect coupling
case" / "a canary's power/ground mesh" as a qualifying geometry -- exactly
what a standard cell's own VPWR/VGND straps are. When a fleet-designed
analog canary reaches layout, re-run this script with `--block <its
slug>` and it re-derives everything (rail dimensions, gap, provenance)
from that block's own GDS.

**No general GDS-to-mesh pipeline.** Per this issue's own Curator
guidance, this script does not attempt a general layer-geometry-to-tet-
mesh converter (that's MoM #701 / FEM #708 epic territory). It reads
exactly the two axis-aligned `met1` rectangles this benchmark needs
(bounding box per shape) and hands their real width/length/gap to a
hand-authored structured-grid mesh generator
(`geode_driver/src/main.rs`) shaped for this one geometry family (two
coplanar rectangular conductors + a grounded box) -- the same "hand-author
a mesh generator for one known geometry" pattern geode-fem's own
`coax_shell_mesh`/`two_sphere_box_mesh` already use for their oracle
fixtures (`crates/geode-core/src/mesh/electrostatic_fixtures.rs`).

**What this script actually invokes.** It drives a *separate, already-
built* checkout of [rjwalters/geode-fem](https://github.com/rjwalters/geode-fem)
(MIT; not vendored into this repo -- same "wrap the numerics" precedent as
`examples/em/patch_antenna/generate.py`) through a small Rust driver
crate committed *here*, `geode_driver/` (this repo's own reproducibility
seam, since geode-fem has no built-in "run on arbitrary geometry" CLI):

    1. Copies `geode_driver/` into `<geode-fem-dir>/examples/interconnect_coupling/`
       (a workspace member glob match -- `examples/*` -- so it reuses that
       checkout's already-resolved `Cargo.lock`/`target/`; never committed
       upstream, geode-fem is not touched).
    2. `cargo run -p interconnect_coupling --release -- <geometry flags>
       --out <result.json>` -- a *real* solve: geode-core's actual
       production `assemble_electrostatic`/`extract_capacitance`
       electrostatic FEM assembler (the same code path
       `benchmarks/electrostatic/results.toml`'s own oracles exercise) on
       a structured-grid tet mesh sized to the real GDS dimensions. Emits
       the N=2 Maxwell capacitance matrix and a driven-field
       (VPWR=1V/VGND=0V) mid-length cross-section slice.
    3. Converts that JSON into `docs/schemas/em-site-export.schema.json`'s
       `capacitance`-shaped export (the `s_parameters`-shaped export
       stays patch-antenna-only; this schema addendum -- issue #842 --
       made `s_parameters` optional and added `capacitance` as its
       sibling, see `docs/design/em-site-export-format.md`).

Usage (from a sibling geode-fem checkout, per `docs/design/em-site-export-format.md`):

    git clone https://github.com/rjwalters/geode-fem.git /tmp/geode-fem
    uv run python3 examples/em/interconnect_coupling/generate.py \\
        --geode-fem-dir /tmp/geode-fem

Pass `--skip-run` to re-convert an already-generated driver result
(`<geode-fem-dir>/target/interconnect_coupling_result.json` by default) --
useful for iterating on the JSON packaging without re-solving.

Public, non-NDA process constants
----------------------------------
GDS polygons carry no z-height/thickness -- those come from the open
sky130 PDK's own published technology LEF (`sky130_fd_sc_hd__nom.tlef`,
Apache-2.0, part of the public SkyWater/`sky130A` distribution this repo
already depends on via `volare` for DRC/LVS/place-and-route -- see
CLAUDE.md's "Open PDKs only" rule, which this satisfies: a numeric citation
to a public, freely-redistributable PDK parameter, not vendored NDA'd
data). `MET1_THICKNESS_UM`, `MET1_EDGECAPACITANCE_F_PER_UM`, and
`MET1_CAPACITANCE_F_PER_UM2` below are transcribed from that file's `LAYER
met1` block (`THICKNESS`/`EDGECAPACITANCE`/`CAPACITANCE CPERSQDIST`) and
used two ways: as a fixed z-thickness for the mesh, and as an independent
sanity cross-check oracle for the FEM self-capacitance (see
`_pdk_self_capacitance_oracle_farad`).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DIR.parent.parent.parent
_DRIVER_SRC = _DIR / "geode_driver"
_OUT_PATH = _DIR / "interconnect_coupling.em-export.json"

sys.path.insert(0, str(_REPO_ROOT / "src"))
from klayout_tools._provenance import sha256_file  # noqa: E402

SCHEMA_VERSION = 1
BENCHMARK = "interconnect_coupling"

DEFAULT_BLOCK = "sky130_fd_sc_hd__nand2_2"
MET1_LAYER = (68, 20)  # sky130 GDS `met1` drawn layer/datatype.

# -- Public sky130A open-PDK tech-LEF constants (see module docstring). --
# `sky130_fd_sc_hd__nom.tlef`, `LAYER met1` block.
MET1_THICKNESS_UM = 0.35
MET1_EDGECAPACITANCE_F_PER_UM = 40.567e-6 * 1e-12  # pF/um -> F/um
MET1_CAPACITANCE_F_PER_UM2 = 25.7784e-6 * 1e-12  # pF/um^2 -> F/um^2

DEFAULT_EPS_R = 4.0  # Typical sky130 inter-metal SiO2 dielectric (approx).


class GenerateError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Real geometry, read from the block's own committed GDS.
# --------------------------------------------------------------------------


def extract_rail_geometry(gds_path: Path) -> dict[str, float]:
    """Read the two `met1` power-rail rectangles out of `gds_path` and
    return their real width/length/gap (um). Raises `GenerateError` if the
    block's `met1` layer doesn't have exactly two axis-aligned rectangles
    (the standard-cell power-rail shape this benchmark expects)."""
    import pya

    layout = pya.Layout()
    layout.read(str(gds_path))
    top = layout.top_cell()
    dbu = layout.dbu
    layer_index = layout.layer(*MET1_LAYER)

    boxes = [shape.bbox().to_dtype(dbu) for shape in top.each_shape(layer_index)]
    if len(boxes) != 2:
        raise GenerateError(
            f"{gds_path}: expected exactly 2 met1 ({MET1_LAYER[0]}/{MET1_LAYER[1]}) "
            f"shapes (VGND + VPWR power rails), found {len(boxes)}"
        )
    boxes.sort(key=lambda b: b.bottom)
    lower, upper = boxes

    width_lower = lower.top - lower.bottom
    width_upper = upper.top - upper.bottom
    if abs(width_lower - width_upper) > 1e-6:
        raise GenerateError(
            f"{gds_path}: the two met1 rails have different widths "
            f"({width_lower} vs {width_upper} um) -- not the expected symmetric "
            "power-rail pair"
        )
    length_lower = lower.right - lower.left
    length_upper = upper.right - upper.left
    gap = upper.bottom - lower.top
    if gap <= 0:
        raise GenerateError(f"{gds_path}: met1 rails overlap or touch (gap={gap} um)")

    return {
        "rail_width_um": width_lower,
        "rail_length_um": min(length_lower, length_upper),
        "gap_um": gap,
    }


# --------------------------------------------------------------------------
# Driving the geode-fem checkout.
# --------------------------------------------------------------------------


def stage_driver(geode_fem_dir: Path) -> Path:
    dest = geode_fem_dir / "examples" / "interconnect_coupling"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(_DRIVER_SRC, dest)
    return dest


def run_driver(
    geode_fem_dir: Path,
    *,
    rail_width_um: float,
    rail_length_um: float,
    gap_um: float,
    eps_r: float,
    margin_um: float,
    step_um: float,
    n_x: int,
    result_path: Path,
) -> None:
    args = [
        "cargo",
        "run",
        "-p",
        "interconnect_coupling",
        "--release",
        "--",
        "--rail-width-um",
        str(rail_width_um),
        "--rail-length-um",
        str(rail_length_um),
        "--thickness-um",
        str(MET1_THICKNESS_UM),
        "--gap-um",
        str(gap_um),
        "--eps-r",
        str(eps_r),
        "--margin-um",
        str(margin_um),
        "--step-um",
        str(step_um),
        "--n-x",
        str(n_x),
        "--out",
        str(result_path),
    ]
    print(f"[geode-fem] {geode_fem_dir}: {' '.join(args)}")
    subprocess.run(args, cwd=geode_fem_dir, check=True)


def geode_fem_commit(geode_fem_dir: Path) -> str:
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=geode_fem_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def geode_fem_version(geode_fem_dir: Path) -> str | None:
    import re

    text = (geode_fem_dir / "Cargo.toml").read_text(encoding="utf-8")
    m = re.search(r'^\s*version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return m.group(1) if m else None


# --------------------------------------------------------------------------
# PDK oracle cross-check.
# --------------------------------------------------------------------------


def pdk_self_capacitance_oracle_farad(
    rail_width_um: float, rail_length_um: float
) -> float:
    """Independent sanity cross-check for one rail's FEM self-capacitance:
    the sky130 tech LEF's own simplified area+edge parasitic-capacitance
    model (`CAPACITANCE CPERSQDIST` * area + `EDGECAPACITANCE` * perimeter),
    the same two-term model an RC extractor built directly on the PDK's own
    published assumptions would use. This models capacitance to the
    *substrate reference plane* at the PDK's own assumed process distance,
    while the FEM run here models capacitance to a modeled grounded box
    `margin_um` away (a different, non-physical reference distance) -- so
    this is an honest order-of-magnitude sanity cross-check, not a strict
    physical oracle (mirrors the "honest, box-truncation-dominated" band
    convention `benchmarks/electrostatic/results.toml`'s own
    `two_sphere_box` oracle already documents for the identical reason)."""
    area_um2 = rail_width_um * rail_length_um
    perimeter_um = 2.0 * (rail_width_um + rail_length_um)
    return (
        MET1_CAPACITANCE_F_PER_UM2 * area_um2
        + MET1_EDGECAPACITANCE_F_PER_UM * perimeter_um
    )


# --------------------------------------------------------------------------
# Assembling the export.
# --------------------------------------------------------------------------


def build_export(
    *,
    geode_fem_dir: Path,
    driver_result: dict[str, Any],
    geometry: dict[str, float],
    eps_r: float,
    margin_um: float,
    step_um: float,
    block: str,
    gds_path: Path,
    source_commit: str,
) -> dict[str, Any]:
    slice_ = driver_result["slice"]
    # Round to a compact-but-lossless-for-visualization precision, the same
    # convention `examples/em/patch_antenna/generate.py`'s `_compact_mesh`
    # uses, to keep the committed JSON a reasonable size.
    vertices = [[round(v[0], 4), round(v[1], 4), 0.0] for v in slice_["vertices_yz_um"]]
    phi_v = [float(f"{v:.6g}") for v in slice_["phi_v"]]
    e_vec_v = [[float(f"{c:.6g}") for c in v] for v in slice_["e_vec_v_per_um"]]

    cap = driver_result["capacitance"]
    names = cap["names"]
    c = cap["c_farad"]
    self_cap = {names[i]: c[i][i] for i in range(len(names))}

    oracle_self_farad = pdk_self_capacitance_oracle_farad(
        geometry["rail_width_um"], geometry["rail_length_um"]
    )
    fem_self_farad = self_cap.get("vgnd")
    oracle_rel_err = (
        abs(fem_self_farad - oracle_self_farad) / oracle_self_farad
        if fem_self_farad is not None
        else None
    )

    commit = geode_fem_commit(geode_fem_dir)
    version = geode_fem_version(geode_fem_dir)
    gds_sha256 = sha256_file(str(gds_path))

    return {
        "schema_version": SCHEMA_VERSION,
        "benchmark": BENCHMARK,
        "mesh": {"vertices": vertices, "cells": slice_["cells"]},
        "frames": [
            {
                "label": "VPWR driven 1V, VGND 0V (mid-length cross-section)",
                "frequency_hz": None,
                "scalar": phi_v,
                "vector": e_vec_v,
            }
        ],
        "capacitance": {
            "conductors": names,
            "matrix_farad": c,
            "matrix_flux_diag_farad": cap["c_flux_diag_farad"],
            "max_rel_asymmetry": cap["max_rel_asymmetry"],
            "oracle": {
                "description": (
                    "sky130 open-PDK tech-LEF area+edge parasitic-capacitance "
                    "model (sky130_fd_sc_hd__nom.tlef LAYER met1: CAPACITANCE "
                    "CPERSQDIST + EDGECAPACITANCE), applied to the vgnd rail's "
                    "real area/perimeter -- an honest sanity cross-check against "
                    "a published PDK parameter, not a strict physical oracle "
                    "(different reference-plane distance than this solve's "
                    "grounded box; see generate.py's own docstring)."
                ),
                "source": (
                    "sky130_fd_sc_hd__nom.tlef LAYER met1 "
                    "(public, Apache-2.0 sky130A open PDK)"
                ),
                "conductor": "vgnd",
                "predicted_self_capacitance_farad": oracle_self_farad,
                "fem_self_capacitance_farad": fem_self_farad,
                "rel_err": oracle_rel_err,
            },
        },
        "provenance": {
            "generator": {
                "repo": "https://github.com/rjwalters/geode-fem",
                "license": "MIT",
                "commit": commit,
                "version": version,
                "backend": (
                    "burn::backend::NdArray<f64, i32> (CPU; no GPU feature compiled -- "
                    "see geode-core's testing::TestBackend cfg fallback)"
                ),
                "build_profile": "release",
            },
            "geometry": {
                "fixture": f"blocks/{block}/output/{block}.gds",
                "fixture_sha256": gds_sha256,
                "description": (
                    f"Real met1 (sky130 GDS layer {MET1_LAYER[0]}/{MET1_LAYER[1]}) "
                    f"VGND/VPWR power-rail pair of the `{block}` gallery block -- "
                    "a genuine, DRC-clean standard-cell layout from the #4 test "
                    "corpus (not a hand-drawn textbook benchmark shape). "
                    "Cross-section extruded along the rail length; hand-authored "
                    "structured-grid tet mesh (see mesh_reduction below), not a "
                    "general GDS-to-mesh pipeline."
                ),
                "source_repo": "https://github.com/2AMLogic/klayout-tools",
                "source_commit": source_commit,
                "source_block": block,
                "source_layer": f"met1 ({MET1_LAYER[0]}/{MET1_LAYER[1]})",
            },
            "solve_parameters": {
                "rail_width_um": geometry["rail_width_um"],
                "rail_length_um": geometry["rail_length_um"],
                "thickness_um": MET1_THICKNESS_UM,
                "gap_um": geometry["gap_um"],
                "eps_r": eps_r,
                "eps_r_note": (
                    "Typical sky130 inter-metal SiO2 dielectric (approximate, uniform)."
                ),
                "margin_um": margin_um,
                "step_um": step_um,
                "n_nodes": driver_result["mesh_stats"]["n_nodes"],
                "n_tets": driver_result["mesh_stats"]["n_tets"],
            },
            "mesh_reduction": {
                "method": (
                    "hand-authored structured-grid tet mesh (two rectangular rail "
                    "conductors + a grounded outer box, uniform Cartesian grid, "
                    "geode_driver/src/main.rs); the exported 2-D slice is the "
                    "already-grid-aligned mid-length (x = rail_length/2) node layer, "
                    "not a marching-tetrahedra cut (unlike patch_antenna's volumetric "
                    "solve, this mesh is grid-structured along the extrusion axis, so "
                    "no interpolation is needed to read off a cross-section)."
                ),
                "slice_x_um": slice_["x_um"],
                "vertex_axes_note": (
                    "mesh.vertices are exported as (x=y_um, y=z_um, z=0.0): the "
                    "cross-section's real in-plane (y) and vertical (z) coordinates, "
                    "flattened onto a single Z=0 plane so the export renders as a flat "
                    "2-D slice; not the original 3-D (x,y,z) axis labels."
                ),
                "source_volumetric_mesh": {
                    "n_nodes": driver_result["mesh_stats"]["n_nodes"],
                    "n_tets": driver_result["mesh_stats"]["n_tets"],
                },
            },
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--geode-fem-dir", type=Path, required=True)
    parser.add_argument(
        "--block",
        default=DEFAULT_BLOCK,
        help="Gallery block slug to read real GDS geometry from.",
    )
    parser.add_argument(
        "--blocks-dir",
        type=Path,
        default=_REPO_ROOT / "blocks",
        help="Root of the gallery's blocks/ tree (default: repo blocks/).",
    )
    parser.add_argument("--eps-r", type=float, default=DEFAULT_EPS_R)
    parser.add_argument("--margin-um", type=float, default=1.0)
    parser.add_argument("--step-um", type=float, default=0.08)
    parser.add_argument("--n-x", type=int, default=6)
    parser.add_argument(
        "--skip-run",
        action="store_true",
        help="Reuse an already-generated driver result instead of re-running cargo.",
    )
    parser.add_argument(
        "--result-json", type=Path, default=None, help="Driver result JSON path."
    )
    parser.add_argument("--out", type=Path, default=_OUT_PATH)
    args = parser.parse_args()

    geode_fem_dir = args.geode_fem_dir.resolve()
    if not (geode_fem_dir / "Cargo.toml").is_file():
        print(
            f"error: {geode_fem_dir} does not look like a geode-fem checkout "
            "(no Cargo.toml)",
            file=sys.stderr,
        )
        return 1

    gds_path = args.blocks_dir / args.block / "output" / f"{args.block}.gds"
    if not gds_path.is_file():
        print(
            f"error: no GDS found for block {args.block!r} at {gds_path}",
            file=sys.stderr,
        )
        return 1

    try:
        geometry = extract_rail_geometry(gds_path)
    except GenerateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    result_path = args.result_json or (
        geode_fem_dir / "target" / "interconnect_coupling_result.json"
    )

    try:
        if not args.skip_run:
            stage_driver(geode_fem_dir)
            run_driver(
                geode_fem_dir,
                rail_width_um=geometry["rail_width_um"],
                rail_length_um=geometry["rail_length_um"],
                gap_um=geometry["gap_um"],
                eps_r=args.eps_r,
                margin_um=args.margin_um,
                step_um=args.step_um,
                n_x=args.n_x,
                result_path=result_path,
            )
        if not result_path.is_file():
            print(
                f"error: expected driver output not found: {result_path}",
                file=sys.stderr,
            )
            return 1
        driver_result = json.loads(result_path.read_text(encoding="utf-8"))

        source_commit = subprocess.run(
            [
                "git",
                "log",
                "-1",
                "--format=%H",
                "--",
                str(gds_path.relative_to(_REPO_ROOT)),
            ],
            cwd=_REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        export = build_export(
            geode_fem_dir=geode_fem_dir,
            driver_result=driver_result,
            geometry=geometry,
            eps_r=args.eps_r,
            margin_um=args.margin_um,
            step_um=args.step_um,
            block=args.block,
            gds_path=gds_path,
            source_commit=source_commit,
        )
    except subprocess.CalledProcessError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    args.out.write_text(json.dumps(export, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.out} ({args.out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
