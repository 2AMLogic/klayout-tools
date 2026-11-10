"""Smoke tests for examples/asap7-parallel-line-fidelity (issues #5201, #5202)."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys

import pytest

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
    pytest.importorskip("klt_mom_native", reason="klt_mom_native is not built")
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


def _phase1_point(gm, point_id):
    with open(os.path.join(gm.HERE, "results.json"), encoding="utf-8") as handle:
        return next(p for p in json.load(handle)["points"] if p["id"] == point_id)


@pytest.fixture(scope="module")
def circuit_one_point(gm, tmp_path_factory):
    """The min-pitch (coupling-dominated) M2 point through the circuit stage,
    on the local backend (a debug probe of 4 single-corner decks)."""
    if shutil.which("ngspice") is None:
        pytest.skip("ngspice is not installed")
    point = _phase1_point(gm, "M2-p1x-sparse-L0.36")
    work = str(tmp_path_factory.mktemp("circuit"))
    circuit, report = gm.circuit_stage([point], work, backend="local")
    return point, circuit, report


def test_reference_network_uses_klt_mom_values(gm):
    point = _phase1_point(gm, "M2-p1x-sparse-L0.36")
    ports, body = gm.reference_subckt(point)
    assert ports == ["N0", "N1", "N2"]
    text = "\n".join(body)
    near = point["reference_klt_mom"]["coupling_each_ff"]["N0"]
    assert f"Ccc_N1_N0 N1__par N0__par {near * 1e-15:.6g}" in text


def test_circuit_decks_pin_quiet_and_opposite_neighbours(gm):
    point = _phase1_point(gm, "M2-p1x-sparse-L0.36")
    ports, body = gm.reference_subckt(point)
    quiet = gm.circuit_deck(ports, body, "REF", "N1", "quiet")
    opposite = gm.circuit_deck(ports, body, "REF", "N1", "opposite")
    assert "VN0 sN0 0 DC 0" in quiet
    assert "VN0 sN0 0 PULSE(1.0 0" in opposite
    assert "PULSE(0 1.0" in quiet and "PULSE(0 1.0" in opposite  # victim rises


def test_circuit_stage_one_point_matches_hand_rc(circuit_one_point):
    point, circuit, _ = circuit_one_point
    row = circuit["points"][0]
    ref = point["reference_klt_mom"]
    each = ref["coupling_each_ff"]
    c_total_fF = (
        ref["c_ground_ff"]
        + ref["c_adjacent_levels_ff"]
        + sum(v for k, v in each.items() if k in ("N0", "N2"))
    )
    rc_ps = 1e4 * c_total_fF * 1e-15 * 1e12  # driver ohm x total C
    # single-pole 50% delay is 0.69 RC; the neighbours load the victim through
    # their own drivers and the 1 ps edge trims it -- a loose hand bracket
    assert 0.3 * rc_ps < row["reference"]["quiet"]["delay_ps"] < 0.9 * rc_ps


def test_coupling_dominated_case_has_miller_penalty_and_deck_misses_it(
    circuit_one_point,
):
    _, circuit, _ = circuit_one_point
    row = circuit["points"][0]
    ref, deck = row["reference"], row["deck"]
    assert ref["opposite"]["delay_ps"] > 1.5 * ref["quiet"]["delay_ps"]
    assert ref["opposite"]["energy_fj"] > 1.5 * ref["quiet"]["energy_fj"]
    # phase 1: the deck has no same-level coupling -> far too fast and cheap
    assert row["rel_err_pct"]["deck"]["delay_opposite"] < -50
    assert deck["opposite"]["delay_ps"] < ref["opposite"]["delay_ps"]
    assert row["critical_net"] == "unsupported"
    assert row["rel_err_pct"]["critical_net"] == "unsupported"


def test_circuit_evidence_record_is_append_only_and_well_formed(
    gm, circuit_one_point, tmp_path, monkeypatch
):
    sky130 = os.path.join(
        os.path.dirname(__file__),
        "..",
        "evidence",
        "sim",
        "sky130-critical-net-fixture",
        "mom-coupling-fidelity",
    )
    with open(os.path.join(sky130, "HEAD"), encoding="utf-8") as handle:
        template = json.load(open(os.path.join(sky130, handle.read().strip())))

    _, circuit, report = circuit_one_point
    monkeypatch.setattr(gm, "EVIDENCE_DIR", str(tmp_path))
    first = gm.write_circuit_evidence(circuit, report, "0" * 64)
    scope = os.path.dirname(first)
    with open(first, encoding="utf-8") as handle:
        record = json.load(handle)
    assert set(record) == set(template)  # the sky130 record's top-level shape
    assert record["schema"] == template["schema"] == "evidence-record/1"
    assert record["supersedes"] == []
    assert open(os.path.join(scope, "HEAD")).read().strip() == os.path.basename(first)
    monkeypatch.setattr(
        gm,
        "datetime",
        type(
            "D",
            (),
            {
                "now": staticmethod(
                    lambda tz=None: __import__("datetime").datetime(
                        2030, 1, 1, tzinfo=tz
                    )
                )
            },
        ),
    )
    second = gm.write_circuit_evidence(circuit, report, "0" * 64)
    assert json.load(open(second))["supersedes"] == [os.path.basename(first)]
    assert open(os.path.join(scope, "HEAD")).read().strip() == os.path.basename(second)
