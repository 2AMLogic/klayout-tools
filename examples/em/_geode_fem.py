"""Shared helpers for the `examples/em/*/generate.py` geode-fem export
drivers.

The four benchmark drivers (`block_coupling`, `interconnect_coupling`,
`patch_antenna`, `spiral_inductor`) each used to carry their own copy of the
helpers here -- the geode-fem provenance readers (`geode_fem_commit`,
`geode_fem_version`), the minimal TOML reader for geode-fem's own hand-rolled
TOML writer output (`parse_toml_lite`), and the VTU -> browser mesh pipeline
(`parse_vtu`, `slice_mesh`, `_compact_mesh`). The copies were byte-identical
when consolidated (the only drift: `interconnect_coupling`'s
`geode_fem_version` carried a function-local `import re` and only
`patch_antenna`'s had a docstring), so they are consolidated here -- the same
single-shared-home rule `src/klayout_tools/_provenance.py` records for
`sha256_file` and the yosys-version helpers.

The module name deliberately avoids `generate`: every benchmark directory
ships a module by that name, and the tests load each driver through
`importlib.util.spec_from_file_location` under a unique name precisely so a
bare import cannot let one directory's `generate` win `sys.modules` for the
whole session (see `tests/test_em_block_exports.py::_load_generator`). A
driver makes this importable with a
`sys.path.insert(0, str(_DIR.parent))` beside its existing
`sys.path.insert(0, str(_REPO_ROOT / "src"))`.
"""

from __future__ import annotations

import ast
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


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
    to `|x| <= crop_half_x, |y| <= crop_half_y` (see the calling driver's
    module docstring for that benchmark's slice plane and crop window).
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
    committed JSON stays a reasonable size (see the calling driver's module
    docstring)."""
    used = sorted({v for c in cells for v in c})
    remap = {old: new for new, old in enumerate(used)}
    new_vertices = [[round(c, 4) for c in vertices[i]] for i in used]
    new_scalars = [float(f"{scalars[i]:.6g}") for i in used]
    new_vectors = [[float(f"{c:.6g}") for c in vectors[i]] for i in used]
    new_cells = [[remap[v] for v in c] for c in cells]
    return new_vertices, new_scalars, new_vectors, new_cells
