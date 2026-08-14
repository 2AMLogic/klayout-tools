"""Tests for the per-gallery-block geode-fem site exports (issue #958, Epic
#840 Phase 3a) -- `blocks/<slug>/output/<slug>.em-export.json`, field data
generated from each block's *own committed GDS* rather than from a benchmark
shape.

Mirrors `tests/test_em_site_export.py` / `tests/test_em_interconnect_coupling
_export.py`'s schema-validates + internal-consistency pattern, and adds the
two checks that only a *block-derived* export can make:

- the recorded geometry hash still matches the block's committed GDS on disk
  (an export can never silently outlive the layout it claims to describe);
- re-running the committed search parameters against that GDS re-derives the
  same conductors (the artifact is regenerable, not a one-off).

Every `blocks/*/output/*.em-export.json` is discovered automatically, so
adding a block needs no test edit. As with the other EM export tests, this
does not (and cannot, in CI) re-run geode-fem itself -- see
`examples/em/block_coupling/README.md` for the reproducible generation
recipe.
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "docs" / "schemas" / "em-site-export.schema.json"
BLOCKS_DIR = REPO_ROOT / "blocks"
GENERATOR_DIR = REPO_ROOT / "examples" / "em" / "block_coupling"

EXPORTS = sorted(BLOCKS_DIR.glob("*/output/*.em-export.json"))
EXPORT_IDS = [p.parent.parent.name for p in EXPORTS]


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def _load_generator():
    """Import `examples/em/block_coupling/generate.py` under a *unique*
    module name, loaded by explicit path.

    Deliberately not `sys.path.insert(...); import generate`: several
    `examples/em/*/` directories each ship a module literally named
    `generate`, so a `sys.path` entry plus a bare import makes the first
    test file to run win `sys.modules["generate"]` for the whole session --
    which is exactly how this file's first draft broke
    `tests/test_em_interconnect_coupling_export.py` (it got *this*
    directory's `generate` and failed on a missing attribute).
    """
    name = "em_block_coupling_generate"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, GENERATOR_DIR / "generate.py")
    module = importlib.util.module_from_spec(spec)
    # Register before executing: `@dataclass` resolves its own annotations
    # through `sys.modules[cls.__module__]`, which is absent for a module
    # that was only ever `module_from_spec`'d.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(params=EXPORTS, ids=EXPORT_IDS)
def export(request) -> dict:
    return json.loads(Path(request.param).read_text())


@pytest.fixture(params=EXPORTS, ids=EXPORT_IDS)
def export_path(request) -> Path:
    return Path(request.param)


def test_at_least_one_block_export_ships():
    """Phase 3a's headline acceptance criterion: a real gallery block has
    field data generated from its actual committed GDS."""
    assert EXPORTS, "expected at least one blocks/<slug>/output/*.em-export.json"


def test_export_validates_against_schema(export):
    jsonschema.validate(instance=export, schema=_schema())


def test_export_is_capacitance_shaped_not_s_parameters(export):
    """A pure electrostatic extraction has no driven-port frequency sweep --
    confirms the schema's `anyOf` is doing real work here rather than being
    vacuously satisfied by both blocks being present."""
    assert "capacitance" in export
    assert "s_parameters" not in export


def test_filename_slug_matches_block_and_provenance(export_path):
    export = json.loads(export_path.read_text())
    slug = export_path.parent.parent.name
    assert export_path.name == f"{slug}.em-export.json"
    assert export["provenance"]["geometry"]["source_block"] == slug


# ---------------------------------------------------------------------------
# Internal consistency (beyond what JSON Schema alone can express).
# ---------------------------------------------------------------------------


def test_frames_are_vertex_aligned_with_mesh(export):
    n = len(export["mesh"]["vertices"])
    assert n > 0
    for frame in export["frames"]:
        if "scalar" in frame:
            assert len(frame["scalar"]) == n
        if "vector" in frame:
            assert len(frame["vector"]) == n


def test_cell_indices_are_in_bounds(export):
    n = len(export["mesh"]["vertices"])
    for cell in export["mesh"]["cells"]:
        assert len(cell) >= 3
        for idx in cell:
            assert 0 <= idx < n


def test_cells_are_non_degenerate_and_on_one_plane(export):
    vertices = export["mesh"]["vertices"]
    for cell in export["mesh"]["cells"]:
        assert len(set(cell)) == len(cell)
        assert len({vertices[i][2] for i in cell}) == 1


def test_driven_potential_field_is_bounded_zero_to_one_volt(export):
    """The exported frame is a driven solve with one conductor pinned to 1 V
    and every other conductor plus the box pinned to 0 V -- by the maximum
    principle for this source-free Poisson problem every interior potential
    must lie in [0, 1] V, and both pinned values must actually appear on a
    cross-section that passes through the driven conductor."""
    scalar = export["frames"][0]["scalar"]
    assert scalar
    assert min(scalar) >= -1e-6
    assert max(scalar) <= 1.0 + 1e-6
    assert min(scalar) < 0.05
    assert max(scalar) > 0.95


def test_capacitance_matrix_is_square_with_positive_diagonal(export):
    cap = export["capacitance"]
    n = len(cap["conductors"])
    assert n >= 2, "a coupling matrix needs at least two conductors"
    matrix = cap["matrix_farad"]
    assert len(matrix) == n
    for row in matrix:
        assert len(row) == n
    for i in range(n):
        assert matrix[i][i] > 0, "Maxwell self-capacitance must be positive"


def test_capacitance_matrix_is_symmetric_within_asymmetry_bound(export):
    """Maxwell reciprocity (`C_ij == C_ji`) is an exact discrete identity of
    the energy-method extraction regardless of mesh quality -- a hard
    correctness check, the same convention `benchmarks/electrostatic/
    results.toml`'s `two_sphere_box` oracle uses for `max_rel_asymmetry`."""
    cap = export["capacitance"]
    assert cap["max_rel_asymmetry"] < 1e-6
    matrix = cap["matrix_farad"]
    for i, row in enumerate(matrix):
        for j, value in enumerate(row):
            denom = max(abs(value), abs(matrix[j][i]), 1e-30)
            assert abs(value - matrix[j][i]) / denom < 1e-6


def test_mutual_capacitance_is_non_positive(export):
    """Maxwell sign structure: off-diagonal terms are non-positive."""
    matrix = export["capacitance"]["matrix_farad"]
    for i, row in enumerate(matrix):
        for j, value in enumerate(row):
            if i != j:
                assert value <= 1e-30


def test_adjacent_conductors_couple_more_than_distant_ones(export):
    """A real bundle's coupling must fall off with separation: the conductor
    immediately next to the driven one has to couple to it more strongly than
    the farthest one in the cross-section. A mesh or classification bug that
    smeared the conductors together would break this ordering while leaving
    the reciprocity check above perfectly happy."""
    cap = export["capacitance"]
    names = cap["conductors"]
    if len(names) < 3:
        pytest.skip("needs at least three conductors to compare separations")
    driven = export["provenance"]["geometry"]["driven_conductor"]
    i = names.index(driven)
    matrix = cap["matrix_farad"]
    # `provenance.geometry.conductors` is in cross-section order.
    order = [c["name"] for c in export["provenance"]["geometry"]["conductors"]]
    pos = order.index(driven)
    by_distance = sorted((abs(order.index(n) - pos), n) for n in names if n != driven)
    nearest = names.index(by_distance[0][1])
    farthest = names.index(by_distance[-1][1])
    assert abs(matrix[i][nearest]) > abs(matrix[i][farthest])


def test_pdk_oracle_cross_check_is_within_an_honest_band(export):
    """A magnitude cross-check against this repo's own curated open-PDK
    area+fringe parasitics table. Deliberately a wide band, not a tight bar:
    that model is capacitance to the substrate alone, while the FEM solve
    additionally sees a grounded box top/side walls at a finite modeled
    distance and the neighbouring conductors held at 0 V (see the oracle's
    own `description`). An order-of-magnitude miss would mean a real modeling
    bug -- a factor of two would not."""
    oracle = export["capacitance"].get("oracle")
    assert oracle is not None
    predicted = oracle["predicted_self_capacitance_farad"]
    fem = oracle["fem_self_capacitance_farad"]
    assert predicted > 0
    assert fem > 0
    assert 0.1 < fem / predicted < 10.0


# ---------------------------------------------------------------------------
# Provenance: the reality-grounding discipline Epic #840 asks for.
# ---------------------------------------------------------------------------


def test_provenance_traces_to_a_real_geode_fem_commit(export):
    generator = export["provenance"]["generator"]
    commit = generator["commit"]
    assert len(commit) == 40
    int(commit, 16)
    assert commit != "0" * 40
    assert generator["repo"] == "https://github.com/rjwalters/geode-fem"


def test_provenance_traces_to_the_source_block_and_commit(export):
    geometry = export["provenance"]["geometry"]
    assert geometry["source_repo"] == "https://github.com/2AMLogic/klayout-tools"
    source_commit = geometry["source_commit"]
    assert len(source_commit) == 40
    int(source_commit, 16)
    assert len(geometry["fixture_sha256"]) == 64


def test_geometry_hash_still_matches_the_committed_gds(export):
    """The export must describe the layout that is actually checked in right
    now -- not one the block has since moved past. This is the check that
    keeps a stale artifact from quietly claiming to be current."""
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from klayout_tools._provenance import sha256_file

    fixture = REPO_ROOT / export["provenance"]["geometry"]["fixture"]
    assert fixture.is_file(), f"missing source GDS: {fixture}"
    assert (
        sha256_file(str(fixture)) == export["provenance"]["geometry"]["fixture_sha256"]
    )


def test_solve_parameters_document_a_real_extruded_cross_section(export):
    params = export["provenance"]["solve_parameters"]
    assert params["run_axis"] in {"x", "y"}
    assert params["cross_section_axis"] in {"x", "y"}
    assert params["cross_section_axis"] != params["run_axis"]
    assert params["length_um"] > 0
    assert math.isclose(
        params["length_um"],
        params["run_end_um"] - params["run_start_um"],
        rel_tol=1e-9,
    )
    # The extrusion is only honest if the bundle really runs that way.
    conductors = export["provenance"]["geometry"]["conductors"]
    lows = [iv[0] for c in conductors for iv in c["intervals_um"]]
    highs = [iv[1] for c in conductors for iv in c["intervals_um"]]
    span = max(highs) - min(lows)
    assert params["length_um"] > 3.0 * span
    # Real, PDK-sourced stackup rather than an arbitrary symmetric box.
    assert params["metal_bottom_um"] > 0
    assert 0.01 < params["thickness_um"] < 2.0
    assert params["stackup_source"] in {"installed-pdk", "transcribed-fallback"}
    assert params["stackup_source_file"]


def test_conductors_are_gds_derived_and_at_least_one_is_labeled(export):
    geometry = export["provenance"]["geometry"]
    conductors = geometry["conductors"]
    assert [c["name"] for c in conductors] == sorted(
        [c["name"] for c in conductors],
        key=lambda name: min(
            iv[0] for c in conductors if c["name"] == name for iv in c["intervals_um"]
        ),
    ), "conductors must be recorded in cross-section order"
    assert any(c["gds_labeled"] for c in conductors), (
        "at least one conductor must be a net named by the GDS's own labels"
    )
    assert set(geometry["named_nets"]) == {
        c["name"] for c in conductors if c["gds_labeled"]
    }
    for c in conductors:
        assert c["intervals_um"]
        for lo, hi in c["intervals_um"]:
            assert hi > lo
        assert math.isclose(
            c["width_um"], sum(hi - lo for lo, hi in c["intervals_um"]), rel_tol=1e-9
        )
    assert set(export["capacitance"]["conductors"]) == {c["name"] for c in conductors}
    assert geometry["driven_conductor"] in {c["name"] for c in conductors}


# ---------------------------------------------------------------------------
# Regenerability: the committed search parameters still find this bundle.
# ---------------------------------------------------------------------------


def test_recorded_search_re_derives_the_same_bundle_from_the_gds(export):
    """The export records the exact bundle-search parameters it was produced
    with (`provenance.geometry.selection`). Replaying them against the
    block's committed GDS must re-derive the same conductors -- i.e. the
    artifact is reproducible from the committed script, not a hand-tuned
    one-off. (The FEM solve itself needs a geode-fem checkout and is out of
    scope for CI; this covers everything upstream of it.)"""
    pytest.importorskip("pya")
    gen = _load_generator()

    geometry = export["provenance"]["geometry"]
    slug = geometry["source_block"]
    profile = gen.profile_for_slug(slug)
    gds_path = REPO_ROOT / geometry["fixture"]
    nets = gen.read_nets(gds_path, profile)

    selection = gen.search_bundle(nets, geometry["selection"])
    assert selection is not None, "recorded search found no bundle on re-run"
    conductors = gen.conductors_from_window(selection.window)

    assert [c["name"] for c in conductors] == [
        c["name"] for c in geometry["conductors"]
    ]
    for fresh, recorded in zip(conductors, geometry["conductors"], strict=True):
        assert fresh["named"] == recorded["gds_labeled"]
        assert len(fresh["intervals"]) == len(recorded["intervals_um"])
        for (lo, hi), (rec_lo, rec_hi) in zip(
            fresh["intervals"], recorded["intervals_um"], strict=True
        ):
            assert math.isclose(lo, rec_lo, abs_tol=1e-6)
            assert math.isclose(hi, rec_hi, abs_tol=1e-6)

    params = export["provenance"]["solve_parameters"]
    assert selection.run_axis == params["run_axis"]
    assert math.isclose(selection.run_length_um, params["length_um"], abs_tol=1e-6)
