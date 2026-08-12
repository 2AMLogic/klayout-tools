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
import ast
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DIR = Path(__file__).resolve().parent
_OUT_PATH = _DIR / "patch_antenna.em-export.json"

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
    """`workspace.package.version` out of the top-level `Cargo.toml`."""
    text = (geode_fem_dir / "Cargo.toml").read_text(encoding="utf-8")
    m = re.search(r'^\s*version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return m.group(1) if m else None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# Minimal TOML reader (avoids adding a `tomllib`/`tomli` dependency for one
# generator script; `requires-python = ">=3.10"` predates stdlib `tomllib`,
# which landed in 3.11). Handles exactly the subset geode-fem's own
# hand-rolled TOML `Display` writer emits: `[section]` / `[section.sub]`
# headers, scalar/array values whose literal syntax is a strict subset of
# Python's (quoted strings, ints, floats incl. scientific notation, and
# `[...]` arrays of those -- including ones that wrap across lines).
# --------------------------------------------------------------------------


def parse_toml_lite(text: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    lines = text.splitlines()
    current = data
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        header = re.match(r"^\[([^\]]+)\]$", stripped)
        if header:
            current = data
            for part in header.group(1).split("."):
                current = current.setdefault(part, {})
            i += 1
            continue
        kv = re.match(r"^([A-Za-z0-9_]+)\s*=\s*(.*)$", stripped)
        if kv:
            key, val = kv.group(1), kv.group(2)
            while val.count("[") > val.count("]"):
                i += 1
                val += " " + lines[i].strip()
            current[key] = _parse_toml_value(val.strip())
        i += 1
    return data


def _parse_toml_value(val: str) -> Any:
    if val in ("true", "false"):
        return val == "true"
    try:
        return ast.literal_eval(val)
    except (ValueError, SyntaxError):
        return val.strip('"')


# --------------------------------------------------------------------------
# VTU parsing (ASCII XML UnstructuredGrid -- see geode-fem's
# `crates/geode-core/src/postproc/viz.rs` for the exact writer this reads).
# --------------------------------------------------------------------------


def parse_vtu(path: Path) -> tuple[list[list[float]], list[list[int]], dict[str, list]]:
    root = ET.parse(path).getroot()
    piece = root.find(".//Piece")
    assert piece is not None
    n_points = int(piece.get("NumberOfPoints", "0"))
    n_cells = int(piece.get("NumberOfCells", "0"))

    points_arr = piece.find("./Points/DataArray")
    assert points_arr is not None and points_arr.text is not None
    coords = [float(x) for x in points_arr.text.split()]
    nodes = [coords[3 * i : 3 * i + 3] for i in range(n_points)]

    cells_el = piece.find("./Cells")
    assert cells_el is not None
    conn: list[int] = []
    for da in cells_el.findall("./DataArray"):
        if da.get("Name") == "connectivity":
            assert da.text is not None
            conn = [int(x) for x in da.text.split()]
    tets = [conn[4 * i : 4 * i + 4] for i in range(n_cells)]

    fields: dict[str, list] = {}
    pdata = piece.find("./PointData")
    assert pdata is not None
    for da in pdata.findall("./DataArray"):
        name = da.get("Name")
        assert name is not None and da.text is not None
        ncomp = int(da.get("NumberOfComponents", "1"))
        vals = [float(x) for x in da.text.split()]
        fields[name] = (
            vals
            if ncomp == 1
            else [vals[ncomp * i : ncomp * i + ncomp] for i in range(n_points)]
        )
    return nodes, tets, fields


def slice_mesh(
    nodes: list[list[float]],
    tets: list[list[int]],
    e_real: list[list[float]],
    mag: list[float],
    z_cut: float,
    crop_half_x: float,
    crop_half_y: float,
) -> tuple[list[list[float]], list[float], list[list[float]], list[list[int]]]:
    """Marching-tetrahedra cut of the volumetric solve at `z = z_cut`,
    linearly interpolating `e_real`/`mag` onto the new cut vertices, cropped
    to `|x| <= crop_half_x, |y| <= crop_half_y` (see module docstring).
    Returns `(vertices, scalar, vector, cells)` -- a `FieldMesh` + one
    `FieldFrame`'s worth of data (`site/src/components/field/types.ts`).
    """
    vert_cache: dict[tuple[int, int], int] = {}
    vertices: list[list[float]] = []
    scalars: list[float] = []
    vectors: list[list[float]] = []

    def cut_edge(ia: int, ib: int) -> int:
        key = (min(ia, ib), max(ia, ib))
        cached = vert_cache.get(key)
        if cached is not None:
            return cached
        za, zb = nodes[ia][2], nodes[ib][2]
        t = (z_cut - za) / (zb - za)
        p = [
            nodes[ia][0] + t * (nodes[ib][0] - nodes[ia][0]),
            nodes[ia][1] + t * (nodes[ib][1] - nodes[ia][1]),
            z_cut,
        ]
        idx = len(vertices)
        vertices.append(p)
        scalars.append(mag[ia] + t * (mag[ib] - mag[ia]))
        vectors.append(
            [e_real[ia][k] + t * (e_real[ib][k] - e_real[ia][k]) for k in range(3)]
        )
        vert_cache[key] = idx
        return idx

    raw_cells: list[list[int]] = []
    for tet in tets:
        above = [nodes[v][2] >= z_cut for v in tet]
        n_above = sum(above)
        if n_above in (0, 4):
            continue
        above_v = [v for v, a in zip(tet, above, strict=True) if a]
        below_v = [v for v, a in zip(tet, above, strict=True) if not a]
        if len(above_v) == 1 or len(below_v) == 1:
            singleton = above_v[0] if len(above_v) == 1 else below_v[0]
            others = below_v if len(above_v) == 1 else above_v
            raw_cells.append([cut_edge(singleton, o) for o in others])
        else:
            a0, a1 = above_v
            b0, b1 = below_v
            p00 = cut_edge(a0, b0)
            p01 = cut_edge(a0, b1)
            p10 = cut_edge(a1, b0)
            p11 = cut_edge(a1, b1)
            raw_cells.append([p00, p01, p11])
            raw_cells.append([p00, p11, p10])

    def in_crop(idx: int) -> bool:
        x, y, _ = vertices[idx]
        return abs(x) <= crop_half_x and abs(y) <= crop_half_y

    cells = [c for c in raw_cells if all(in_crop(v) for v in c)]
    return vertices, scalars, vectors, cells


def _compact_mesh(
    vertices: list[list[float]],
    scalars: list[float],
    vectors: list[list[float]],
    cells: list[list[int]],
) -> tuple[list[list[float]], list[float], list[list[float]], list[list[int]]]:
    """Drop unreferenced vertices (post-crop) and remap cell indices, then
    round to a compact-but-lossless-for-visualization precision so the
    committed JSON stays a reasonable size (see module docstring)."""
    used = sorted({v for c in cells for v in c})
    remap = {old: new for new, old in enumerate(used)}
    new_vertices = [[round(c, 4) for c in vertices[i]] for i in used]
    new_scalars = [float(f"{scalars[i]:.6g}") for i in used]
    new_vectors = [[float(f"{c:.6g}") for c in vectors[i]] for i in used]
    new_cells = [[remap[v] for v in c] for c in cells]
    return new_vertices, new_scalars, new_vectors, new_cells


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
                "fixture_sha256": (
                    sha256_file(fixture_path) if fixture_path.exists() else None
                ),
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
