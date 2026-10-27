"""Tests for `klt sim`'s transient start states (`options.initial_conditions`,
issue #3881).

Same rule grammar and per-point resolution as `options.nodeset`
(`tests/test_sim_nodeset.py`), but the selected map is rendered as file-scope
`.ic v(node)=volts` cards and the `tran` command gains `uic`. Unit tests need no
simulator; the integration tests run the real `ngspice -b` and skip without it.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from klayout_tools import sim

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

HAVE_NGSPICE = shutil.which("ngspice") is not None and (
    os.environ.get("KLT_SKIP_NGSPICE_TESTS") != "1"
)
_SKIP_NO_NGSPICE = pytest.mark.skipif(
    not HAVE_NGSPICE, reason="ngspice is not installed on this machine"
)

#: A source-free RC: the only thing that decides `v(out)` at t=0 is the start
#: state. (Without `uic` the DC solve would put it at 0 V.)
_RC = "R1 out 0 1k\nC1 out 0 1u\n"
_TRAN = {"kind": "tran", "args": "10u 1m"}


def _rule(match: dict, **values: float) -> dict:
    return {"match": match, "values": values}


def _resolve(corners: dict, rules: list):
    points = sim._expand_corners(corners, [])
    sim._apply_nodeset_rules(
        points,
        sim._validate_nodeset_rules(rules, label=sim._IC_LABEL),
        label=sim._IC_LABEL,
        attr="initial_conditions",
    )
    return points


def _write_request(tmp_path: Path, request: dict) -> Path:
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    return path


# --------------------------------------------------------------------------- #
# Validation and resolution
# --------------------------------------------------------------------------- #


def test_errors_name_the_initial_conditions_option():
    with pytest.raises(sim.SimError, match=r"options\.initial_conditions must be"):
        sim._validate_nodeset_rules({}, label=sim._IC_LABEL)
    with pytest.raises(sim.SimError, match=r"options\.initial_conditions\[0\]"):
        sim._validate_nodeset_rules([{"match": {}, "values": {}}], label=sim._IC_LABEL)


@pytest.mark.parametrize(
    "values",
    [{"a b": 1}, {"a)": 1}, {"a": float("nan")}, {"a": True}, {"A": 1, "a": 2}],
)
def test_bad_node_spelling_and_values_are_rejected(values):
    with pytest.raises(sim.SimError):
        sim._validate_nodeset_rules(
            [{"match": {}, "values": values}], label=sim._IC_LABEL
        )


def test_rules_select_per_point_and_do_not_touch_the_nodeset_map():
    points = _resolve(
        {"temperature_c": [27, 85]}, [_rule({"temperature_c": 85}, Out=0.5)]
    )
    assert [p.initial_conditions for p in points] == [{}, {"out": 0.5}]
    assert all(p.nodeset == {} for p in points)
    assert [p.corner_id for p in points] == [
        p.corner_id for p in sim._expand_corners({"temperature_c": [27, 85]}, [])
    ]


def test_overlapping_and_unused_rules_are_errors():
    with pytest.raises(
        sim.SimError, match=r"options\.initial_conditions rules 0 and 1"
    ):
        _resolve({}, [_rule({}, a=1), _rule({}, a=2)])
    with pytest.raises(
        sim.SimError, match=r"options\.initial_conditions\[1\] matches no"
    ):
        _resolve(
            {"temperature_c": [27]}, [_rule({}, a=1), _rule({"temperature_c": 99}, a=2)]
        )


def test_monte_carlo_clones_and_wire_round_trip_keep_the_map():
    points = _resolve(
        {"temperature_c": [27, 85]}, [_rule({"temperature_c": 85}, out=0.5)]
    )
    wire = sim._corner_points_to_wire(points)
    assert "initial_conditions" not in wire[0]
    assert wire[1]["initial_conditions"] == {"out": 0.5}
    back = sim._corner_points_from_wire(json.loads(json.dumps(wire)))
    assert [p.initial_conditions for p in back] == [{}, {"out": 0.5}]


def test_wire_map_is_revalidated_at_the_receiving_worker():
    wire = sim._corner_points_to_wire(_resolve({}, [_rule({}, a=1)]))
    wire[0]["initial_conditions"] = {"a b": 1}
    with pytest.raises(sim.SimError, match="initial_conditions"):
        sim._corner_points_from_wire(wire)


@pytest.mark.parametrize(
    "analysis",
    [
        {"kind": "op", "args": ""},
        {"kind": "ac", "args": "dec 10 1 1k"},
        {"kind": "analysis_steps", "steps": [{"kind": "tran", "args": "1u 1m"}]},
    ],
)
def test_non_transient_analyses_are_refused(analysis):
    with pytest.raises(sim.SimError, match="kind 'tran'"):
        sim._require_plain_tran_for_ic(True, analysis)
    sim._require_plain_tran_for_ic(False, analysis)


def test_xyce_refuses_initial_conditions(tmp_path):
    (tmp_path / "body.spice").write_text(_RC)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "engine": "xyce",
            "analysis": _TRAN,
            "options": {"initial_conditions": [_rule({}, out=1.0)]},
        },
    )
    with pytest.raises(sim.SimError, match="not supported for engine 'xyce'"):
        sim.run_sim(str(request))


# --------------------------------------------------------------------------- #
# Deck rendering
# --------------------------------------------------------------------------- #


def test_uic_is_added_once_and_only_for_a_point_with_a_map():
    with_map, without = _resolve(
        {"temperature_c": [27, 85]}, [_rule({"temperature_c": 27}, out=1)]
    )
    assert sim._ic_analysis_args(with_map, "10u 1m") == "10u 1m uic"
    assert sim._ic_analysis_args(with_map, "10u 1m UIC") == "10u 1m UIC"
    assert sim._ic_analysis_args(without, "10u 1m") == "10u 1m"
    assert sim._ic_cards(with_map) == [".ic v(out)=1.0"]
    assert sim._ic_cards(without) == []


# --------------------------------------------------------------------------- #
# Worker echo
# --------------------------------------------------------------------------- #


def _echo(point: sim.CornerPoint, **over) -> dict:
    corner = {"corner_id": point.corner_id, "status": "pass", "diagnostics": []}
    if point.initial_conditions:
        corner["initial_conditions"] = dict(point.initial_conditions)
    return {**corner, **over}


def _check(points, corners, **kwargs):
    sim._require_nodeset_echo(
        points,
        corners,
        label=sim._IC_LABEL,
        attr="initial_conditions",
        code=sim.IC_UNSUPPORTED_WORKER_CODE,
        **kwargs,
    )


def test_worker_that_drops_the_map_fails_with_its_own_code():
    points = _resolve({}, [_rule({}, out=1)])
    _check(points, [_echo(points[0])])
    with pytest.raises(sim.SimError) as raised:
        _check(points, [_echo(points[0], initial_conditions=None)])
    assert raised.value.code == "initial_conditions_unsupported_worker"
    assert "options.initial_conditions" in str(raised.value)


def test_no_map_or_local_backend_is_never_gated():
    plain = sim._expand_corners({}, [])
    _check(plain, [])
    _check(_resolve({}, [_rule({}, out=1)]), [], backend="local")


# --------------------------------------------------------------------------- #
# Integration: the real ngspice binary
# --------------------------------------------------------------------------- #


def _engine_request(tmp_path: Path, **overrides) -> Path:
    request = {
        "netlist": "body.spice",
        "analysis": _TRAN,
        "measurements": [{"name": "v0", "expr": "v(out)[0]", "unit": "V"}],
        "backend": "local",
        "options": {"keep_artifacts": True},
        **overrides,
    }
    return _write_request(tmp_path, request)


def _assert_v0(report: dict, expected: list[float]) -> None:
    # The first saved sample sits one solver step into the decay, hence the
    # tolerance.
    got = [c["measurements"][0]["value"] for c in report["corners"]]
    assert got == pytest.approx(expected, abs=2e-3)


@_SKIP_NO_NGSPICE
def test_real_ngspice_two_start_states_give_different_transients(tmp_path):
    (tmp_path / "body.spice").write_text(_RC)
    request = _engine_request(
        tmp_path,
        corners={"temperature_c": [27, 85]},
        options={
            "keep_artifacts": True,
            "initial_conditions": [
                _rule({"temperature_c": 27}, out=0.25),
                _rule({"temperature_c": 85}, out=1.5),
            ],
        },
    )
    report = sim.run_sim(str(request), artifacts_dir=str(tmp_path / "a"))
    assert report["status"] == "pass", [c["diagnostics"] for c in report["corners"]]
    _assert_v0(report, [0.25, 1.5])
    assert [c["initial_conditions"] for c in report["corners"]] == [
        {"out": 0.25},
        {"out": 1.5},
    ]
    deck = Path(report["corners"][1]["artifacts"]["deck"]).read_text()
    assert ".ic v(out)=1.5" in deck
    assert "tran 10u 1m uic" in deck


@_SKIP_NO_NGSPICE
def test_real_ngspice_ic_is_a_start_state_not_a_nodeset(tmp_path):
    """`.nodeset` only seeds the DC solve, which `uic` skips; the source-driven
    node starts at the `.ic` value, not at the 1 V operating point."""
    (tmp_path / "body.spice").write_text("V1 in 0 1\nR1 in out 1k\nC1 out 0 1u\n")
    request = _engine_request(
        tmp_path,
        options={"keep_artifacts": True, "initial_conditions": [_rule({}, out=0.0)]},
    )
    report = sim.run_sim(str(request), artifacts_dir=str(tmp_path / "a"))
    assert report["status"] == "pass"
    _assert_v0(report, [0.0])


@_SKIP_NO_NGSPICE
def test_real_ngspice_request_ic_beats_body_ic_and_legacy_deck_is_unchanged(tmp_path):
    (tmp_path / "body.spice").write_text(_RC + ".ic v(out)=0.75\n")
    plain = sim.run_sim(
        str(_engine_request(tmp_path)), artifacts_dir=str(tmp_path / "a0")
    )
    # No request map: no `uic`, so the body `.ic` is not applied and the
    # corner report has no field.
    assert "initial_conditions" not in plain["corners"][0]
    deck = Path(plain["corners"][0]["artifacts"]["deck"]).read_text()
    assert "uic" not in deck
    assert ".ic v(" not in deck

    request = _engine_request(
        tmp_path,
        options={"keep_artifacts": True, "initial_conditions": [_rule({}, out=1.25)]},
    )
    report = sim.run_sim(str(request), artifacts_dir=str(tmp_path / "a1"))
    _assert_v0(report, [1.25])
