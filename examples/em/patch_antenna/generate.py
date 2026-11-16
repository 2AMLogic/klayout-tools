#!/usr/bin/env python3
"""Generate the geode-fem site export for the patch-antenna benchmark
(issue #849, Phase 1a of Epic #840 "browser E&M results").

**This script produces no data on its own.** It drives a *separate,
already-built* checkout of [rjwalters/geode-fem](https://github.com/rjwalters/geode-fem)
(MIT, a sibling in-house project -- see `docs/design/em-field-sim-spike.md`
and `docs/ARCHITECTURE.md` "In-house prior art") through its own `cargo run
-p patch_antenna --release` benchmark binary, then converts that binary's
real solver output (`results.toml`, `pattern.toml`, `E_patch.vtu`) into the
JSON export format documented by
`docs/schemas/em-site-export.schema.json` / `docs/design/em-site-export-format.md`.
geode-fem is **not** vendored into this repo (CLAUDE.md: this repo wraps
external engines, it does not absorb their numerics -- see the spike doc's
Section 5 "Wrap or build?"), so this script is the reproducibility seam: a
future CI run or another engineer with a local geode-fem checkout can
re-run it to regenerate `patch_antenna.em-export.json` byte-for-byte
(modulo the pinned geode-fem commit and solver floating-point
associativity), rather than a one-off manual conversion nobody can repeat.

Usage (from a sibling geode-fem checkout built once with `cargo build -p
patch_antenna --release`):

    uv run python3 examples/em/patch_antenna/generate.py \\
        --geode-fem-dir /path/to/geode-fem

Pass `--skip-run` to re-convert already-generated geode-fem output (skips
the three `cargo run` invocations below) -- useful for iterating on the
JSON shape without re-solving.

What this script actually invokes in the geode-fem checkout (Section
references are to that repo's `examples/patch_antenna/src/main.rs`):

1. `cargo run -p patch_antenna --release` (`Mode::Benchmark`, the default) --
   solves the bundled FR-4 probe-fed patch fixture (`tests/fixtures/patch_2g4.msh`)
   across a 13-point 2.0-3.0 GHz sweep and writes
   `benchmarks/patch_antenna/results.toml`: `Z_in(f)`, `S11(f)`, `P_in`/`P_rad`/
   efficiency, the FEM resonant frequency, and the -10 dB return-loss
   bandwidth (`null` here -- this coarse 13-point sweep's shallow -6 dB dip
   does not bracket both -10 dB crossings; the file says so in its own
   comment. Not a defect in this export: `docs/schemas/em-site-export.schema.json`
   models `bandwidth_10db_hz` as nullable for exactly this documented case).
2. `cargo run -p patch_antenna --release -- pattern` (`Mode::Pattern`) --
   the near-to-far-field transform (Love surface equivalence NTFF) on the
   same fixture at the committed FEM resonance, writing
   `benchmarks/patch_antenna/pattern.toml`: broadside directivity, gain,
   and the E-/H-plane principal-plane radiation cuts.
3. `cargo run -p patch_antenna --release -- --export-field --out-dir <tmp>` --
   solves the same fixture once more at the same resonance and dumps the
   per-node driven near field `E(r)` (real + imaginary parts, `|E|`) over
   the full tetrahedral volume mesh to `<tmp>/E_patch.vtu`.

All three runs solve the **identical** fixture
(`tests/fixtures/patch_2g4.msh`, sha256 pinned in the emitted
`provenance.geometry.fixture_sha256`) at the **identical** FEM-resonant
frequency (2.27453 GHz) -- so the mesh, the S-parameter sweep, and the NTFF
pattern in one export are all facets of one physically consistent
antenna, not stitched together from mismatched runs.

# From volumetric solve to a browser-sized surface mesh

geode-fem's own `E_patch.vtu` is the *volumetric* tetrahedral solve (4750
nodes / 25452 tets over the whole air+UPML+substrate domain) -- useful for
ParaView, far too large and not directly renderable as a `FieldViewer`
surface mesh (`FieldMesh.cells` are polygons; a tet's 4 vertices are not
coplanar). This script derives a genuine planar cross-section: a
marching-tetrahedra slice at `z = 0.8 mm` (mid-substrate, between the
ground plane at z=0 and the patch at z=h=1.6mm -- the height geode-fem's
own README highlights as resolving "the TM01 cavity mode: field maxima at
the patch's two radiating edges"), linearly interpolating the real, solved
per-node field onto the cut. This is a **derived measurement of the real
solve** (the same operation ParaView's own Slice filter performs, which is
how geode-fem's own tearsheet near-field images are made -- see the
`--export-field` docstring in geode-fem's `main.rs`), not fabricated data:
every scalar/vector sample in the export traces to a linear interpolation
between two real, solved nodal E-field values.

The slice is cropped to `|x| <= 25mm, |y| <= 20mm` (the patch is
38x29mm, so this keeps a 6-7mm near-field margin beyond every edge) purely
to keep the committed JSON a reasonable size -- the outer air/UPML region
beyond that margin carries near-zero field (the whole point of the matched
absorbing boundary) and is coarsely meshed, so cropping it loses
negligible information while cutting the vertex/triangle count roughly in
half. This is a windowing choice, not a data transformation -- documented
here and in `provenance.mesh_reduction.crop_bbox_mm` on the emitted file.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DIR.parent.parent.parent
_OUT_PATH = _DIR / "patch_antenna.em-export.json"

sys.path.insert(0, str(_DIR.parent))
sys.path.insert(0, str(_REPO_ROOT / "src"))
from _geode_fem import (  # noqa: E402
    _compact_mesh,
    geode_fem_commit,
    geode_fem_version,
    parse_toml_lite,
    parse_vtu,
    slice_mesh,
)

from klayout_tools._provenance import sha256_file  # noqa: E402

SCHEMA_VERSION = 1
BENCHMARK = "patch_antenna"

# Mid-substrate cross-section (ground z=0, patch z=h=1.6mm -- see the
# geode-fem `patch_antenna.geo` layout diagram) and a crop window around
# the patch (patch is 38x29mm) -- see the module docstring.
SLICE_Z_MM = 0.8
CROP_HALF_X_MM = 25.0
CROP_HALF_Y_MM = 20.0


# --------------------------------------------------------------------------
# Driving geode-fem's own benchmark binary
# --------------------------------------------------------------------------


def run_geode_fem(geode_fem_dir: Path, export_dir: Path) -> None:
    """Run the three real solver invocations described in the module
    docstring inside `geode_fem_dir`. Raises `subprocess.CalledProcessError`
    on any failure -- this script never falls back to synthesizing data."""
    common = ["cargo", "run", "-p", "patch_antenna", "--release", "--"]
    print(f"[geode-fem] {geode_fem_dir}: solving benchmark sweep (results.toml)...")
    subprocess.run(common[:-1], cwd=geode_fem_dir, check=True)
    print("[geode-fem] solving NTFF radiation pattern (pattern.toml)...")
    subprocess.run([*common, "pattern"], cwd=geode_fem_dir, check=True)
    print(f"[geode-fem] solving + exporting near field to {export_dir}/E_patch.vtu ...")
    subprocess.run(
        [*common, "--export-field", "--out-dir", str(export_dir)],
        cwd=geode_fem_dir,
        check=True,
    )


# --------------------------------------------------------------------------
# Minimal TOML reader, VTU parsing and the marching-tetrahedra slice live in
# the sibling `examples/em/_geode_fem.py`, shared by all four EM benchmark
# drivers (imported above).
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Assembling the export
# --------------------------------------------------------------------------


def build_s_parameters(results: dict[str, Any]) -> dict[str, Any]:
    meta = results["meta"]
    points = []
    i = 0
    while f"point_{i}" in results:
        p = results[f"point_{i}"]
        points.append(
            {
                "frequency_hz": p["f_ghz"] * 1e9,
                "z_re_ohm": p["z_re_ohm"],
                "z_im_ohm": p["z_im_ohm"],
                "s11_re": p["s11_re"],
                "s11_im": p["s11_im"],
                "s11_db": p["s11_db"],
                "p_in": p["p_in"],
                "p_rad": p["p_rad"],
                "efficiency": p["efficiency"],
                "solve_residual_rel": p["solve_residual_rel"],
            }
        )
        i += 1
    bw = meta.get("bw_10db_ghz")
    bandwidth_note = (
        None
        if bw is not None
        else "not bracketed by this sweep's frequency grid (see results.toml comment)"
    )
    return {
        "ports": ["p1"],
        "reference_impedance_ohm": meta["port_resistance_ohm"],
        "points": points,
        "resonance": {
            "f_res_hz": meta["f_res_fem_ghz"] * 1e9,
            "s11_dip_db": meta["s11_dip_db"],
            "s11_dip_frequency_hz": meta["s11_dip_f_ghz"] * 1e9,
            "bandwidth_10db_hz": (bw * 1e9) if bw is not None else None,
            "bandwidth_10db_note": bandwidth_note,
        },
        "oracle_cavity_model": {
            "f_res_hz": results["oracles"]["cavity_model"]["f_res_ghz"] * 1e9,
            "f_res_rel_err": results["comparison"]["f_res_rel_err"],
        },
    }


def build_radiation_pattern(pattern: dict[str, Any]) -> dict[str, Any]:
    r = pattern["results"]
    cuts = {}
    for name in ("e_plane", "h_plane"):
        cut = pattern["cut"][name]
        cuts[name] = {"theta_deg": cut["theta_deg"], "e_norm": cut["e_norm"]}
    return {
        "frequency_hz": pattern["meta"]["f_res_ghz"] * 1e9,
        "efficiency": r["efficiency"],
        "directivity_max_dbi": r["directivity_max_dbi"],
        "directivity_broadside_dbi": r["directivity_broadside_dbi"],
        "gain_broadside_dbi": r["gain_broadside_dbi"],
        "oracle_cavity_model_directivity_broadside_dbi": pattern["oracles"][
            "cavity_model"
        ]["directivity_broadside_dbi"],
        "cuts": cuts,
    }


def build_export(
    *,
    geode_fem_dir: Path,
    results: dict[str, Any],
    pattern: dict[str, Any],
    vtu_path: Path,
) -> dict[str, Any]:
    meta = results["meta"]
    nodes, tets, fields = parse_vtu(vtu_path)
    vertices, scalars, vectors, cells = slice_mesh(
        nodes,
        tets,
        fields["E_real"],
        fields["|E|"],
        SLICE_Z_MM,
        CROP_HALF_X_MM,
        CROP_HALF_Y_MM,
    )
    vertices, scalars, vectors, cells = _compact_mesh(vertices, scalars, vectors, cells)

    fixture_path = geode_fem_dir / "crates" / "geode-core" / meta["fixture"]
    commit = geode_fem_commit(geode_fem_dir)

    backend_note = (
        "burn::backend::NdArray<f64, i32> (CPU; no GPU feature compiled -- "
        "see geode-core's testing::TestBackend cfg fallback)"
    )
    geometry_description = (
        "Probe-fed FR-4 rectangular microstrip patch (W=38mm, L=29mm, "
        "h=1.6mm substrate, eps_r=4.4, tan_delta=0.02); PEC patch+ground+"
        "outer walls, matched box-UPML absorbing shell (sigma_0=25, "
        "thickness=25mm), Palace-style 50-ohm lumped coax-probe port."
    )
    reduction_method = (
        "marching-tetrahedra planar slice of the full volumetric solve, "
        "linearly interpolating the solved nodal E field onto the cut"
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "benchmark": BENCHMARK,
        "mesh": {"vertices": vertices, "cells": cells},
        "frames": [
            {
                "label": f"{meta['f_res_fem_ghz']:.4g} GHz",
                "frequency_hz": meta["f_res_fem_ghz"] * 1e9,
                "scalar": scalars,
                "vector": vectors,
            }
        ],
        "s_parameters": build_s_parameters(results),
        "radiation_pattern": build_radiation_pattern(pattern),
        "provenance": {
            "generator": {
                "repo": "https://github.com/rjwalters/geode-fem",
                "license": "MIT",
                "commit": commit,
                "version": geode_fem_version(geode_fem_dir),
                "backend": backend_note,
                "build_profile": "release",
            },
            "geometry": {
                "fixture": meta["fixture"],
                "fixture_sha256": sha256_file(str(fixture_path)),
                "description": geometry_description,
            },
            "solve_parameters": {
                "port_resistance_ohm": meta["port_resistance_ohm"],
                "conductors": meta["conductors"],
                "outer_boundary": meta["outer_boundary"],
                "absorber": meta["absorber"],
                "upml_sigma_0": meta["upml_sigma_0"],
                "upml_thick_mm": meta["upml_thick_mm"],
                "substrate": meta["substrate"],
                "eps_r": meta["eps_r"],
                "tan_delta": meta["tan_delta"],
            },
            "mesh_reduction": {
                "method": reduction_method,
                "slice_z_mm": SLICE_Z_MM,
                "slice_plane_note": "mid-substrate (ground at z=0, patch at z=h=1.6mm)",
                "crop_bbox_mm": {
                    "x": [-CROP_HALF_X_MM, CROP_HALF_X_MM],
                    "y": [-CROP_HALF_Y_MM, CROP_HALF_Y_MM],
                },
                "source_volumetric_mesh": {"nodes": len(nodes), "tets": len(tets)},
            },
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--geode-fem-dir",
        type=Path,
        required=True,
        help="Path to a local rjwalters/geode-fem checkout, already built with "
        "`cargo build -p patch_antenna --release`.",
    )
    parser.add_argument(
        "--skip-run",
        action="store_true",
        help="Skip the three `cargo run` invocations and re-convert "
        "benchmarks/patch_antenna/{results,pattern}.toml + --export-dir's "
        "E_patch.vtu already present from a prior run.",
    )
    parser.add_argument(
        "--export-dir",
        type=Path,
        default=None,
        help="Where `--export-field` writes E_patch.vtu (default: a fresh temp dir).",
    )
    parser.add_argument("--out", type=Path, default=_OUT_PATH, help="Output JSON path.")
    args = parser.parse_args()

    geode_fem_dir = args.geode_fem_dir.resolve()
    if not (geode_fem_dir / "Cargo.toml").is_file():
        print(
            f"error: {geode_fem_dir} does not look like a geode-fem checkout "
            "(no Cargo.toml)",
            file=sys.stderr,
        )
        return 1

    export_dir = args.export_dir
    tmp_ctx = None
    if export_dir is None:
        tmp_ctx = tempfile.TemporaryDirectory(prefix="geode-fem-export-")
        export_dir = Path(tmp_ctx.name)

    try:
        if not args.skip_run:
            run_geode_fem(geode_fem_dir, export_dir)

        results_path = geode_fem_dir / "benchmarks" / "patch_antenna" / "results.toml"
        pattern_path = geode_fem_dir / "benchmarks" / "patch_antenna" / "pattern.toml"
        vtu_path = export_dir / "E_patch.vtu"
        for p in (results_path, pattern_path, vtu_path):
            if not p.is_file():
                print(
                    f"error: expected geode-fem output not found: {p}", file=sys.stderr
                )
                return 1

        results = parse_toml_lite(results_path.read_text(encoding="utf-8"))
        pattern = parse_toml_lite(pattern_path.read_text(encoding="utf-8"))
        export = build_export(
            geode_fem_dir=geode_fem_dir,
            results=results,
            pattern=pattern,
            vtu_path=vtu_path,
        )
    finally:
        if tmp_ctx is not None:
            tmp_ctx.cleanup()

    args.out.write_text(json.dumps(export, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.out} ({args.out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
