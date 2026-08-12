#!/usr/bin/env python3
"""Generate the geode-fem site export for the spiral-inductor benchmark
(issue #851, Phase 1c of Epic #840 "browser E&M results" -- the second
validated benchmark the gallery's acceptance criteria requires, alongside
`examples/em/patch_antenna/`).

**This script produces no data on its own.** It drives a *separate,
already-built* checkout of [rjwalters/geode-fem](https://github.com/rjwalters/geode-fem)
(MIT, a sibling in-house project -- see `docs/design/em-field-sim-spike.md`
and `docs/ARCHITECTURE.md` "In-house prior art") through its own `cargo run
-p spiral_inductor --release` benchmark binary, then converts that binary's
real solver output (`results.toml`, `E_spiral.vtu`) into the JSON export
format documented by `docs/schemas/em-site-export.schema.json` /
`docs/design/em-site-export-format.md`. geode-fem is **not** vendored into
this repo (CLAUDE.md: this repo wraps external engines, it does not absorb
their numerics), so this script is the reproducibility seam: a future CI
run or another engineer with a local geode-fem checkout can re-run it to
regenerate `spiral_inductor.em-export.json`, rather than a one-off manual
conversion nobody can repeat.

Usage (from a sibling geode-fem checkout built once with `cargo build -p
spiral_inductor --release`):

    uv run python3 examples/em/spiral_inductor/generate.py \\
        --geode-fem-dir /path/to/geode-fem

Pass `--skip-run` to re-convert already-generated geode-fem output (skips
the two `cargo run` invocations below) -- useful for iterating on the JSON
shape without re-solving.

What this script actually invokes in the geode-fem checkout (section
references are to that repo's `examples/spiral_inductor/src/main.rs`):

1. `cargo run -p spiral_inductor --release` (the default `benchmark` mode)
   -- solves the bundled 3.5-turn generic square spiral fixture
   (`tests/fixtures/spiral_3p5.msh`, 54,428 edges) across a 13-point
   0.1-40 GHz port-driven frequency sweep and writes
   `benchmarks/spiral_inductor/results.toml`: `Z(f)`, `L(f)`, `R(f)`,
   `Q(f)`, `|S11|(f)`, the self-resonant frequency (SRF, the `Im Z = 0`
   crossing), and the Mohan analytic + mom PEEC oracle comparisons.
2. `cargo run -p spiral_inductor --release -- --export-field --out-dir <tmp>`
   -- solves the identical fixture once more at the benchmark's low-
   frequency reference point (1 GHz, the point the oracle comparison in
   `results.toml` is keyed to) and dumps the per-node driven near field
   `E(r)` (real + imaginary parts, `|E|`) over the full tetrahedral volume
   mesh to `<tmp>/E_spiral.vtu`.

Both runs solve the **identical** fixture
(`tests/fixtures/spiral_3p5.msh`, sha256 pinned in the emitted
`provenance.geometry.fixture_sha256`), so the mesh and the L/R/Q/S11 sweep
in one export are facets of one physically consistent inductor, not
stitched together from mismatched runs. The field-export solve omits the
extraction sweep's Leontovich conductor-surface-impedance loss term (that
public API takes no surface BC -- see `export_field`'s own docstring in
`main.rs`), so the rendered near field is a debugging-grade visualization
of the driven mode shape, not the lossy operator the L/R/Q sweep itself
uses; this is documented in the emitted `provenance.solve_parameters` note
below, not silently glossed over.

# From volumetric solve to a browser-sized surface mesh

Like the patch-antenna export, this script derives a genuine planar
cross-section from the full volumetric solve (8548 nodes / 42341 tets) via
a marching-tetrahedra slice, linearly interpolating the real, solved
per-node field onto the cut -- never fabricated data. The spiral's stack
(`crates/geode-core/tests/fixtures/spiral_3p5.provenance.txt`) places the
top (radiating) conductor layer at z in [5, 8] um (`z2_bot=5.0`,
`t2=3.0`), so the slice cuts through the middle of that layer
(`z = 6.5 um`) -- the plane that actually shows the spiral trace's driven
near field, analogous to the patch antenna's mid-substrate cut. The crop
window (`|x| <= 90um, |y| <= 120um`) keeps the full spiral + feed/stub
geometry (measured node extent within the slice band: x in
[-70.1, 70.1]um, y in [-100.6, 70.5]um) with a small margin, while
dropping the coarsely-meshed outer PEC-box/air region beyond it.
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
from math import log10
from pathlib import Path
from typing import Any

_DIR = Path(__file__).resolve().parent
_OUT_PATH = _DIR / "spiral_inductor.em-export.json"

SCHEMA_VERSION = 1
BENCHMARK = "spiral_inductor"

# Mid-top-conductor-layer cross-section (see module docstring) and a crop
# window around the spiral + feed/stub geometry (measured extent within
# the slice band: x in [-70.1, 70.1], y in [-100.6, 70.5], microns).
SLICE_Z_UM = 6.5
CROP_HALF_X_UM = 90.0
CROP_HALF_Y_UM = 120.0

# The oracle comparison / field export are both keyed to this reference
# frequency (`L_REF_GHZ` in geode-fem's `main.rs`).
FIELD_REF_GHZ = 1.0


# --------------------------------------------------------------------------
# Driving geode-fem's own benchmark binary
# --------------------------------------------------------------------------


def run_geode_fem(geode_fem_dir: Path, export_dir: Path) -> None:
    """Run the two real solver invocations described in the module
    docstring inside `geode_fem_dir`. Raises `subprocess.CalledProcessError`
    on any failure -- this script never falls back to synthesizing data."""
    common = ["cargo", "run", "-p", "spiral_inductor", "--release", "--"]
    print(f"[geode-fem] {geode_fem_dir}: solving benchmark sweep (results.toml)...")
    subprocess.run(common[:-1], cwd=geode_fem_dir, check=True)
    print(
        f"[geode-fem] solving + exporting near field to {export_dir}/E_spiral.vtu ..."
    )
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
# Minimal TOML reader (same subset as `examples/em/patch_antenna/generate.py`
# -- see that module for the full rationale. Handles the `[section]` /
# `[section.sub]` / `[point_<i>]` shape geode-fem's own hand-rolled TOML
# `Display` writer emits.)
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
        s11_mag = p["s11_mag"]
        points.append(
            {
                "frequency_hz": p["f_ghz"] * 1e9,
                "z_re_ohm": p["z_re_ohm"],
                "z_im_ohm": p["z_im_ohm"],
                # `s11_db` is required by the export schema; geode-fem's
                # spiral_inductor benchmark only emits `|S11|`, so this is
                # a direct, lossless 20*log10 conversion of that real
                # solved value -- not a fabricated field.
                "s11_db": 20.0 * log10(s11_mag) if s11_mag > 0 else float("-inf"),
                "solve_residual_rel": p["solve_residual_rel"],
                # Extra, benchmark-specific columns the schema's
                # `additionalProperties: true` on `s_parameters.points[]`
                # items allows -- the headline L/R/Q figures this
                # benchmark exists to report, copied verbatim from
                # `results.toml`.
                "l_nh": p["l_nh"],
                "r_ohm": p["r_ohm"],
                "q": p["q"],
            }
        )
        i += 1

    # Self-resonant frequency (Im Z = 0, a parallel anti-resonance) is this
    # benchmark's headline resonance figure -- a different physical concept
    # than the patch antenna's S11-dip matching resonance, but the schema's
    # `resonance` object only requires `f_res_hz`/`s11_dip_db`, so the S11
    # dip actually observed in this sweep (not a match, just the sweep's
    # minimum |S11|) is recorded honestly alongside the SRF.
    srf_ghz = meta.get("srf_ghz")
    dip = min(points, key=lambda p: p["s11_db"])
    return {
        "ports": ["p1"],
        "reference_impedance_ohm": meta["port_resistance_ohm"],
        "points": points,
        "resonance": {
            "f_res_hz": (srf_ghz * 1e9) if srf_ghz is not None else None,
            "s11_dip_db": dip["s11_db"],
            "s11_dip_frequency_hz": dip["frequency_hz"],
            "bandwidth_10db_hz": None,
            "bandwidth_10db_note": (
                "not applicable: this is a spiral-inductor L/Q benchmark "
                "(one-port reflection, no external match), not an antenna "
                "return-loss bandwidth -- `f_res_hz` above is the "
                "self-resonant frequency (Im Z = 0 crossing)."
            ),
        },
        "oracles": {
            "mohan": results["oracles"]["mohan"],
            "mom_peec": results["oracles"]["mom_peec"],
        },
        "comparison": results["comparison"],
    }


def build_export(
    *,
    geode_fem_dir: Path,
    results: dict[str, Any],
    vtu_path: Path,
) -> dict[str, Any]:
    meta = results["meta"]
    nodes, tets, fields = parse_vtu(vtu_path)
    vertices, scalars, vectors, cells = slice_mesh(
        nodes,
        tets,
        fields["E_real"],
        fields["|E|"],
        SLICE_Z_UM,
        CROP_HALF_X_UM,
        CROP_HALF_Y_UM,
    )
    vertices, scalars, vectors, cells = _compact_mesh(vertices, scalars, vectors, cells)

    fixture_path = geode_fem_dir / "crates" / "geode-core" / meta["fixture"]
    commit = geode_fem_commit(geode_fem_dir)

    backend_note = (
        "burn::backend::NdArray<f64, i32> (CPU; no GPU feature compiled -- "
        "see geode-core's testing::TestBackend cfg fallback)"
    )
    geometry_description = (
        "Port-driven 3.5-turn generic square spiral inductor (w=6um, "
        "s=4um, d_in=60um), copper conductor on the top metal layer "
        "(z in [5,8]um) with a Leontovich good-conductor surface "
        "impedance, PEC outer box, 50-ohm lumped port at the feed."
    )
    reduction_method = (
        "marching-tetrahedra planar slice of the full volumetric solve at "
        "the mid-top-conductor-layer height, linearly interpolating the "
        "solved nodal E field onto the cut"
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "benchmark": BENCHMARK,
        "mesh": {"vertices": vertices, "cells": cells},
        "frames": [
            {
                "label": f"{FIELD_REF_GHZ:g} GHz",
                "frequency_hz": FIELD_REF_GHZ * 1e9,
                "scalar": scalars,
                "vector": vectors,
            }
        ],
        "s_parameters": build_s_parameters(results),
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
                "conductor_model": meta["conductor_model"],
                "conductor_sigma_s_m": meta["conductor_sigma_s_m"],
                "outer_boundary": meta["outer_boundary"],
                "field_export_note": (
                    "the rendered near field (--export-field) omits the "
                    "L/R/Q sweep's Leontovich conductor-surface-impedance "
                    "loss term (that solve path takes no surface BC) -- a "
                    "debugging-grade near-field visualization of the "
                    "driven mode shape, not the lossy operator the L/R/Q "
                    "sweep itself uses."
                ),
            },
            "mesh_reduction": {
                "method": reduction_method,
                "slice_z_um": SLICE_Z_UM,
                "slice_plane_note": (
                    "mid-top-conductor-layer (z2_bot=5.0um, t2=3.0um -- "
                    "see the fixture provenance)"
                ),
                "crop_bbox_um": {
                    "x": [-CROP_HALF_X_UM, CROP_HALF_X_UM],
                    "y": [-CROP_HALF_Y_UM, CROP_HALF_Y_UM],
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
        "`cargo build -p spiral_inductor --release`.",
    )
    parser.add_argument(
        "--skip-run",
        action="store_true",
        help="Skip the two `cargo run` invocations and re-convert "
        "benchmarks/spiral_inductor/results.toml + --export-dir's "
        "E_spiral.vtu already present from a prior run.",
    )
    parser.add_argument(
        "--export-dir",
        type=Path,
        default=None,
        help="Where `--export-field` writes E_spiral.vtu (default: a fresh temp dir).",
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

        results_path = geode_fem_dir / "benchmarks" / "spiral_inductor" / "results.toml"
        vtu_path = export_dir / "E_spiral.vtu"
        for p in (results_path, vtu_path):
            if not p.is_file():
                print(
                    f"error: expected geode-fem output not found: {p}", file=sys.stderr
                )
                return 1

        results = parse_toml_lite(results_path.read_text(encoding="utf-8"))
        export = build_export(
            geode_fem_dir=geode_fem_dir,
            results=results,
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
