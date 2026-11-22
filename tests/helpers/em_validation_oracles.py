"""Shared fixtures and oracles for ``tests/test_em_validation.py`` (issue
#5618, brief 1.C of Epic #708 Phase 1).

A **test-only** helper: nothing in ``src/klayout_tools/`` imports it. It
holds the engine gates, the GDS fixture and the closed-form oracles the
validation module's tests share, split out of that module so it stays under
the per-file size ratchet (``scripts/check_file_size_baseline.py``, 500
lines). The assertions, tolerance bands and measured numbers stay in the
test module, where each check names its oracle
(``docs/design/em-validation.md``).

Every engine gate skips (never passes silently) when its engine is absent:
``geode`` (``$KLT_GEODE_BINARY`` or ``PATH``, see ``docs/cli/em.md``) and
Gmsh for meshing.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.em import run_em

#: Vacuum permittivity, F/m (CODATA 2018) -- the same value
#: ``native/mom/src/solver.rs`` uses, so an oracle mismatch can only come
#: from the solve, never from a constant disagreeing across the codebase.
EPS0_F_PER_M = 8.854_187_812_8e-12

# ---------------------------------------------------------------------------
# Environment: which live engines are present?
# ---------------------------------------------------------------------------


def geode_or_skip() -> str:
    """Resolve the geode binary exactly as ``run_em`` does (options -> env ->
    PATH), or skip the test: a live validation must never pass silently by
    accident of an absent engine."""
    env = os.environ.get("KLT_GEODE_BINARY")
    if env:
        if shutil.which(env) or (os.path.isfile(env) and os.access(env, os.X_OK)):
            return env
        pytest.skip(f"$KLT_GEODE_BINARY={env!r} does not name a runnable binary")
    found = shutil.which("geode")
    if found:
        return found
    pytest.skip(
        "geode binary not found: set KLT_GEODE_BINARY or put geode on PATH "
        "(docs/cli/em.md 'Resolving geode')"
    )


def gmsh_or_skip() -> None:
    """``geode mesh`` shells out to Gmsh (``$GEODE_GMSH`` or ``PATH``)."""
    if shutil.which(os.environ.get("GEODE_GMSH", "gmsh")) is None:
        pytest.skip("gmsh binary not found (geode mesh shells out to it)")


# ---------------------------------------------------------------------------
# Fixture geometry and oracles
# ---------------------------------------------------------------------------


def plate_pair_gds(path: Path, side_um: float, lower: str, upper: str) -> None:
    """Two coincident square plates on a sky130 layer pair: the section-1
    parallel-plate fixture of ``docs/design/mom-validation.md``, realized as
    a GDS on PDK conductors (zero-thickness plates become the stackup's own
    metal z-extents; the gap and eps_r come from the stackup)."""
    layers = {
        "li1": (67, 20),
        "met1": (68, 20),
        "met2": (69, 20),
    }
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    half = int(side_um * 500)
    box = kdb.Box(0, 0, int(side_um * 1000), int(side_um * 1000))
    for name in (lower, upper):
        layer, datatype = layers[name]
        top.shapes(layout.layer(layer, 20)).insert(box)
        top.shapes(layout.layer(layer, 5)).insert(
            kdb.Text("TOPP" if name == upper else "BOT", half, half)
        )
    layout.write(str(path))


def layer_pair_geometry(stack: dict, lower: str, upper: str) -> dict:
    """The facing gap, the uniform eps between the plates and the plates'
    z-extents, all derived from the curated stackup (never a duplicated
    table -- issue #5617's discipline). Raises if the inter-plate dielectric
    is not uniform: the parallel-plate oracle needs one eps_r."""
    conductors = {c["name"]: c for c in stack["conductors"]}
    lo, hi = conductors[lower], conductors[upper]
    lo_top = lo["z0_um"] + lo["thickness_um"]
    hi_bottom = hi["z0_um"]
    gap = hi_bottom - lo_top
    eps = {
        d["permittivity"]
        for d in stack["dielectrics"]
        if d["z0_um"] < hi_bottom
        and d["z0_um"] + d["thickness_um"] > lo_top
        and d["permittivity"] is not None
    }
    assert gap > 0, (lower, upper, gap)
    assert len(eps) == 1, f"non-uniform dielectric between {lower} and {upper}: {eps}"
    return {
        "gap_um": gap,
        "eps_r": next(iter(eps)),
        "lower": {"name": lower, **lo},
        "upper": {"name": upper, **hi},
    }


def parallel_plate_ff(area_um2: float, gap_um: float, eps_r: float) -> float:
    """Oracle: the ideal parallel-plate capacitance ``eps_r eps_0 A / d`` in
    femtofarads. For finite plates a strict lower bound on any solve."""
    # um^2 -> m^2 is 1e-12, um -> m is 1e-6, F -> fF is 1e15.
    return eps_r * EPS0_F_PER_M * (area_um2 / gap_um) * 1e9


def mutual_ff(response: dict) -> float:
    """``-C_01`` of the two-terminal Maxwell matrix, the pair capacitance in
    the usual (positive) SPICE sense."""
    return -response["results"]["c"]["matrix"][0][1] * 1e15


def run_layout_request(tmp_path: Path, name: str, gds: Path) -> dict:
    request = tmp_path / f"{name}.json"
    request.write_text(
        json.dumps(
            {
                "layout": os.path.relpath(gds, tmp_path),
                "region": {"cell": "TOP"},
                "stackup": "sky130A",
                "options": {"keep_artifacts": False},
            }
        )
    )
    return run_em(str(request))


# ---------------------------------------------------------------------------
# Coax-annulus mesh (exact closed-form oracle, mesh-refinement study)
# ---------------------------------------------------------------------------

#: Coax geometry: upstream geode-fem's own oracle-1 dimensions
#: (benchmarks/electrostatic/results.toml; a = 1, b = 2.5, L = 1, SI metres,
#: end faces left untagged so they carry the natural symmetry condition --
#: the continuum field is exactly the 2-D radial one).
COAX_A_M = 1.0
COAX_B_M = 2.5
COAX_LENGTH_M = 1.0


def build_coax_mesh(path: Path, h: float) -> None:
    """A tagged coax-annulus tetrahedral mesh from the gmsh Python API
    (OpenCASCADE): volume ``diel``, terminal surface ``S_in`` (inner
    cylinder), ground surface ``S_out`` (outer cylinder). The end annuli are
    left untagged so they carry the natural (zero normal displacement)
    condition -- for the radial field they are exact symmetry planes."""
    gmsh = pytest.importorskip(
        "gmsh", reason="gmsh Python module needed to build meshes"
    )
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("coax")
        outer = gmsh.model.occ.add_cylinder(
            0, 0, -COAX_LENGTH_M / 2, 0, 0, COAX_LENGTH_M, COAX_B_M
        )
        inner = gmsh.model.occ.add_cylinder(
            0, 0, -COAX_LENGTH_M / 2, 0, 0, COAX_LENGTH_M, COAX_A_M
        )
        out, _removed = gmsh.model.occ.cut([(3, outer)], [(3, inner)])
        assert len(out) == 1, out
        vol_dim, vol_tag = out[0]
        gmsh.model.occ.synchronize()
        surfaces = gmsh.model.get_boundary([(3, vol_tag)], oriented=False)
        inner_faces, outer_faces = [], []
        for _dim, tag in surfaces:
            xmin, ymin, zmin, xmax, ymax, zmax = gmsh.model.get_bounding_box(2, tag)
            if abs(zmax - zmin) < 1e-6:
                continue  # end annulus: symmetry plane, deliberately untagged
            radius = max(abs(xmin), xmax, abs(ymin), ymax)
            if abs(radius - COAX_A_M) < 1e-6:
                inner_faces.append(tag)
            elif abs(radius - COAX_B_M) < 1e-6:
                outer_faces.append(tag)
        assert len(inner_faces) == 1 and len(outer_faces) == 1
        gmsh.model.add_physical_group(3, [vol_tag], name="diel")
        gmsh.model.add_physical_group(2, inner_faces, name="S_in")
        gmsh.model.add_physical_group(2, outer_faces, name="S_out")
        gmsh.option.setNumber("Mesh.CharacteristicLengthMin", h)
        gmsh.option.setNumber("Mesh.CharacteristicLengthMax", h)
        gmsh.option.setNumber("Mesh.Algorithm", 6)
        gmsh.model.mesh.generate(3)
        gmsh.write(str(path))
    finally:
        gmsh.finalize()
