"""Smoke test for examples/asap7-parallel-line-fidelity (issue #5201)."""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

pytest.importorskip("klt_mom_native", reason="klt_mom_native is not built")

_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "examples",
    "asap7-parallel-line-fidelity",
    "generate_and_measure.py",
)


@pytest.fixture(scope="module")
def gm():
    spec = importlib.util.spec_from_file_location("asap7_fidelity_gm", _PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_stack_is_contiguous_and_cites_m1_thickness(gm):
    stack = gm.STACK
    assert stack[1]["thickness_um"] == 0.036
    for lower, upper in zip(range(1, 5), range(2, 6), strict=True):
        assert stack[upper]["z0_um"] > stack[lower]["z1_um"]
    spec = gm.stackup_spec()
    assert {e["conductor"] for e in spec["stackup"]} == {f"M{i}" for i in range(1, 6)}


def test_point_geometry_min_pitch_with_orthogonal_neighbours(gm):
    geometry = gm.point_geometry(2, 1, "dense", 0.36)
    shapes = geometry["shapes"]
    assert geometry["victim"] == "N2"
    assert sorted(n for n in shapes if n.startswith("N")) == [f"N{i}" for i in range(5)]
    assert {shapes[n]["level"] for n in shapes if n[0] == "D"} == {1}
    assert {shapes[n]["level"] for n in shapes if n[0] == "U"} == {3}
    gap = shapes["N1"]["y0"] - shapes["N0"]["y1"]
    assert gap == pytest.approx(0.018)


def test_one_point_reports_critical_net_as_unsupported(gm, tmp_path):
    point = gm.run_point(2, 1, "sparse", 0.18, str(tmp_path), panel_divisors=(1.0,))
    crit = point["b_critical_net"]
    assert crit["status"] == "unsupported"
    assert "--critical-net" in crit["reason"]
    assert crit["c_same_level_ff"] == "unsupported"
    ref = point["reference_klt_mom"]
    assert ref["c_same_level_ff"] > 0 and ref["c_ground_ff"] > 0
    # the deck models no lateral coupling without --critical-net
    assert point["a_parasitics"]["c_same_level_ff"] == 0
    assert point["a_parasitics"]["rel_err_pct"]["c_same_level"] == -100.0
