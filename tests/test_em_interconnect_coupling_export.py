"""Tests for the interconnect-coupling geode-fem site export (issue #842) --
`examples/em/interconnect_coupling/interconnect_coupling.em-export.json`,
the Electromagnetics gallery's real-fleet-geometry entry (as opposed to
`examples/em/patch_antenna`'s bundled textbook fixture).

Mirrors `tests/test_em_site_export.py`'s schema-self-consistency +
instance-validates + internal-consistency pattern, adapted for the
`capacitance`-shaped export instead of the `s_parameters`-shaped one: no
frequency sweep to check, but a Maxwell-reciprocity hard check and a PDK-
oracle sanity cross-check to verify instead. Does not (and cannot, in CI)
re-run geode-fem itself -- see `examples/em/interconnect_coupling/generate.py`
for the reproducible generation script and
`docs/design/em-site-export-format.md`'s "Addendum: capacitance" section
for the full design rationale.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent
    / "docs"
    / "schemas"
    / "em-site-export.schema.json"
)
EXPORT_PATH = (
    Path(__file__).resolve().parent.parent
    / "examples"
    / "em"
    / "interconnect_coupling"
    / "interconnect_coupling.em-export.json"
)


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def _load_export() -> dict:
    return json.loads(EXPORT_PATH.read_text())


def test_export_exists():
    assert EXPORT_PATH.is_file(), f"missing committed export: {EXPORT_PATH}"


def test_export_validates_against_schema():
    jsonschema.validate(instance=_load_export(), schema=_load_schema())


def test_export_has_no_s_parameters_block():
    """A pure electrostatic-extraction benchmark has no driven-port
    frequency sweep -- confirms the schema's `anyOf` is doing real work
    here, not just being vacuously satisfied by both blocks being present."""
    export = _load_export()
    assert "s_parameters" not in export


# ---------------------------------------------------------------------------
# Internal consistency (beyond what JSON Schema alone can express).
# ---------------------------------------------------------------------------


def test_frames_are_vertex_aligned_with_mesh():
    export = _load_export()
    n = len(export["mesh"]["vertices"])
    assert n > 0
    for frame in export["frames"]:
        if "scalar" in frame:
            assert len(frame["scalar"]) == n
        if "vector" in frame:
            assert len(frame["vector"]) == n


def test_cell_indices_are_in_bounds():
    export = _load_export()
    n = len(export["mesh"]["vertices"])
    for cell in export["mesh"]["cells"]:
        assert len(cell) >= 3
        for idx in cell:
            assert 0 <= idx < n


def test_cells_are_triangles_not_degenerate_and_on_one_plane():
    export = _load_export()
    vertices = export["mesh"]["vertices"]
    for cell in export["mesh"]["cells"]:
        assert len(set(cell)) == len(cell)
        zs = {vertices[i][2] for i in cell}
        assert len(zs) == 1


def test_driven_potential_field_is_bounded_zero_to_one_volt():
    """The exported frame is a driven solve with VGND pinned to 0V and VPWR
    pinned to 1V -- by the maximum principle for this source-free Poisson
    problem, every interior potential must lie in [0, 1] volts. A value
    outside that range would mean the exported field is not the real
    solved potential."""
    export = _load_export()
    scalar = export["frames"][0]["scalar"]
    assert scalar, "expected a non-empty scalar (phi) field"
    assert min(scalar) >= -1e-6
    assert max(scalar) <= 1.0 + 1e-6
    # Both pinned boundary values should actually appear on the exported
    # cross-section (it passes through both rails).
    assert min(scalar) < 0.05
    assert max(scalar) > 0.95


def test_capacitance_matrix_is_square_and_positive_diagonal():
    export = _load_export()
    cap = export["capacitance"]
    n = len(cap["conductors"])
    matrix = cap["matrix_farad"]
    assert len(matrix) == n
    for row in matrix:
        assert len(row) == n
    for i in range(n):
        assert matrix[i][i] > 0, "Maxwell self-capacitance must be positive"


def test_capacitance_matrix_is_symmetric_within_asymmetry_bound():
    """Maxwell reciprocity (`C_ij == C_ji`) is an exact discrete identity of
    the energy-method extraction regardless of mesh quality -- a hard
    correctness check, the same convention
    `benchmarks/electrostatic/results.toml`'s own `two_sphere_box` oracle
    uses for `max_rel_asymmetry`."""
    export = _load_export()
    cap = export["capacitance"]
    assert cap["max_rel_asymmetry"] < 1e-6
    matrix = cap["matrix_farad"]
    n = len(matrix)
    for i in range(n):
        for j in range(n):
            denom = max(abs(matrix[i][j]), abs(matrix[j][i]), 1e-30)
            assert abs(matrix[i][j] - matrix[j][i]) / denom < 1e-6


def test_mutual_capacitance_is_non_positive():
    """Maxwell sign structure: off-diagonal capacitance terms are
    non-positive (mutual capacitance couples charge from one conductor's
    excitation onto the other with the opposite sign convention)."""
    export = _load_export()
    matrix = export["capacitance"]["matrix_farad"]
    n = len(matrix)
    for i in range(n):
        for j in range(n):
            if i != j:
                assert matrix[i][j] <= 1e-30


def test_pdk_oracle_cross_check_is_present_and_within_an_honest_band():
    """A sanity magnitude cross-check against the sky130 open PDK's own
    published `met1` area+edge capacitance model should land within an
    honest, generous band -- not a tight pass/fail bar (see the oracle's
    own `description` field for why), but well short of an order of
    magnitude, which would indicate a real modeling bug."""
    export = _load_export()
    oracle = export["capacitance"].get("oracle")
    assert oracle is not None
    assert oracle["predicted_self_capacitance_farad"] > 0
    assert oracle["fem_self_capacitance_farad"] > 0
    assert oracle["rel_err"] is not None
    assert oracle["rel_err"] < 0.5


def test_provenance_traces_to_a_real_geode_fem_commit():
    export = _load_export()
    commit = export["provenance"]["generator"]["commit"]
    assert len(commit) == 40
    int(commit, 16)
    assert commit != "0" * 40


def test_provenance_traces_to_the_source_klayout_tools_block_and_commit():
    """The acceptance criterion this issue's provenance panel renders: the
    export must name the exact source repo/commit/block the real GDS
    geometry traces to, distinct from the geode-fem generator identity."""
    export = _load_export()
    geometry = export["provenance"]["geometry"]
    assert geometry["source_repo"] == "https://github.com/2AMLogic/klayout-tools"
    source_commit = geometry["source_commit"]
    assert len(source_commit) == 40
    int(source_commit, 16)
    assert geometry["source_block"]
    assert geometry["fixture_sha256"] is not None
    assert len(geometry["fixture_sha256"]) == 64


def test_solve_parameters_document_real_gds_derived_dimensions():
    export = _load_export()
    params = export["provenance"]["solve_parameters"]
    # Real sky130 met1 power-rail dimensions (see generate.py's docstring) --
    # sanity-bound to a plausible on-chip standard-cell scale, not an exact
    # pin (a future re-run against a different/updated block is expected to
    # produce different real numbers within this same rough scale).
    assert 0.05 < params["rail_width_um"] < 5.0
    assert 0.1 < params["rail_length_um"] < 50.0
    assert 0.01 < params["thickness_um"] < 2.0
    assert 0.05 < params["gap_um"] < 50.0


def test_regenerating_with_a_different_block_changes_provenance():
    """Edge case from this issue's own test plan: a re-run after the source
    geometry changes must produce different provenance, not a stale/cached
    value. Exercised here at the extraction-function level (no geode-fem
    checkout available in CI) against two real, differently-sized library
    blocks already committed to this repo."""
    import sys

    sys.path.insert(
        0,
        str(
            Path(__file__).resolve().parent.parent
            / "examples"
            / "em"
            / "interconnect_coupling"
        ),
    )
    import generate as gen  # noqa: E402

    repo_root = Path(__file__).resolve().parent.parent
    nand2_path = (
        repo_root
        / "blocks"
        / "sky130_fd_sc_hd__nand2_2"
        / "output"
        / "sky130_fd_sc_hd__nand2_2.gds"
    )
    buf4_path = (
        repo_root
        / "blocks"
        / "sky130_fd_sc_hd__buf_4"
        / "output"
        / "sky130_fd_sc_hd__buf_4.gds"
    )
    if not (nand2_path.is_file() and buf4_path.is_file()):
        pytest.skip("corpus GDS fixtures not present in this checkout")

    geom_nand2 = gen.extract_rail_geometry(nand2_path)
    geom_buf4 = gen.extract_rail_geometry(buf4_path)
    # Both are real sky130_fd_sc_hd cells sharing the same rail width/gap
    # (fixed by the row-height/rail-drawing rules) but a different cell
    # width -- so `rail_length_um` differs while `rail_width_um`/`gap_um`
    # stay identical. Different GDS -> different provenance, same schema.
    assert geom_nand2["rail_length_um"] != geom_buf4["rail_length_um"]
