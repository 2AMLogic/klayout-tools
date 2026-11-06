"""Tests for `klt sim`'s per-corner analysis windows (`analysis_overrides`,
issue #4970). Unit tests need no simulator; the integration test runs the real
`ngspice -b` and skips without it."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from klayout_tools import sim, sim_steps

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

HAVE_NGSPICE = shutil.which("ngspice") is not None and (
    os.environ.get("KLT_SKIP_NGSPICE_TESTS") != "1"
)

_TRAN = {"kind": "tran", "args": "1u 1m"}
_CORNERS = {"temperature_c": [27, 85, 125]}


def _rule(match: dict, args: str) -> dict:
    return {"match": match, "args": args}


def _resolve(rules, corners=None, analysis=None, engine="ngspice"):
    points = sim._expand_corners(corners or _CORNERS, [])
    validated = sim._validate_analysis_overrides(rules, analysis or _TRAN, engine)
    sim._apply_nodeset_rules(
        points, validated, label=sim._AO_LABEL, attr="analysis_args"
    )
    return points


def test_omitted_or_empty_is_a_no_op():
    assert sim._validate_analysis_overrides(None, _TRAN, "ngspice") == []
    assert sim._validate_analysis_overrides([], _TRAN, "ngspice") == []


def test_rules_select_per_point_and_unmatched_keep_base_args():
    points = _resolve(
        [_rule({"temperature_c": 85}, "10n 5u"), _rule({"temperature_c": 125}, "1n 1u")]
    )
    assert [p.analysis_args for p in points] == [None, "10n 5u", "1n 1u"]
    assert [sim._effective_analysis_args(p, _TRAN) for p in points] == [
        "1u 1m",
        "10n 5u",
        "1n 1u",
    ]
    assert [p.corner_id for p in points] == [
        p.corner_id for p in sim._expand_corners(_CORNERS, [])
    ]


def test_empty_match_applies_to_every_point():
    points = _resolve([_rule({}, "2u 2m")])
    assert {p.analysis_args for p in points} == {"2u 2m"}


@pytest.mark.parametrize(
    "rules,message",
    [
        ({}, r"analysis_overrides must be an array"),
        ([{"match": {}}], r"analysis_overrides\[0\] must be an object"),
        ([_rule({}, "")], r"analysis_overrides\[0\]\.args"),
        ([_rule({}, "1u {stop}")], r"brace"),
        ([_rule({"bogus": 1}, "1u 1m")], r"analysis_overrides\[0\]\.match"),
    ],
)
def test_invalid_rules_name_the_rule_index(rules, message):
    with pytest.raises(sim.SimError, match=message):
        sim._validate_analysis_overrides(rules, _TRAN, "ngspice")


def test_overlap_and_unused_rules_are_errors():
    with pytest.raises(sim.SimError, match=r"analysis_overrides rules 0 and 1"):
        _resolve([_rule({}, "1u 1m"), _rule({"temperature_c": 27}, "2u 2m")])
    with pytest.raises(sim.SimError, match=r"analysis_overrides\[1\] matches no"):
        _resolve(
            [
                _rule({"temperature_c": 27}, "1u 1m"),
                _rule({"temperature_c": 1}, "2u 2m"),
            ]
        )


def test_refused_with_steps_and_without_scalar_analysis():
    steps = {"kind": sim_steps.STEPS_KIND, "steps": [{"kind": "tran", "args": "1u 1m"}]}
    with pytest.raises(sim.SimError, match="analysis_steps"):
        sim._validate_analysis_overrides([_rule({}, "1u 1m")], steps, "ngspice")
    with pytest.raises(sim.SimError, match="scalar `analysis`"):
        sim._validate_analysis_overrides([_rule({}, "1u 1m")], {}, "ngspice")


def test_brace_args_allowed_for_xyce_like_the_base_analysis():
    sim._validate_analysis_overrides([_rule({}, "1u {stop}")], _TRAN, "xyce")


def test_wire_round_trip_and_mc_clone_keep_the_window():
    points = _resolve([_rule({"temperature_c": 85}, "10n 5u")])
    wire = sim._corner_points_to_wire(points)
    assert "analysis_args" not in wire[0]
    assert wire[1]["analysis_args"] == "10n 5u"
    back = sim._corner_points_from_wire(json.loads(json.dumps(wire)))
    assert [p.analysis_args for p in back] == [None, "10n 5u", None]
    wire[0]["analysis_args"] = ""
    with pytest.raises(sim.SimError, match="analysis_args"):
        sim._corner_points_from_wire(wire)


def test_report_field_is_additive():
    plain, hit = _resolve([_rule({"temperature_c": 85}, "10n 5u")])[:2]
    assert sim._ao_report_field(plain) == {}
    assert sim._ao_report_field(hit) == {"analysis_args": "10n 5u"}


def test_worker_that_drops_the_window_fails_with_its_own_code():
    points = _resolve([_rule({}, "2u 2m")], corners={"temperature_c": [27]})

    def check(corners):
        sim._require_nodeset_echo(
            points,
            corners,
            label=sim._AO_LABEL,
            attr="analysis_args",
            code=sim.AO_UNSUPPORTED_WORKER_CODE,
        )

    base = {"corner_id": points[0].corner_id}
    check([{**base, "analysis_args": "2u 2m"}])
    with pytest.raises(sim.SimError) as raised:
        check([base])
    assert raised.value.code == "analysis_overrides_unsupported_worker"


def test_nodeset_echo_check_still_handles_dict_maps():
    points = sim._expand_corners({}, [])
    points[0].nodeset = {"a": 1.0}
    sim._require_nodeset_echo(points, [{"nodeset": {"a": 1.0}}], backend="remote")


@pytest.mark.skipif(not HAVE_NGSPICE, reason="ngspice is not installed")
def test_real_run_uses_a_different_tran_card_per_corner(tmp_path: Path):
    (tmp_path / "body.spice").write_text("V1 in 0 1\nR1 in out 1k\nC1 out 0 1u\n")
    request = {
        "netlist": "body.spice",
        "corners": {"temperature_c": [27, 85]},
        "analysis": _TRAN,
        "analysis_overrides": [_rule({"temperature_c": 85}, "5u 2m")],
        "measurements": [],
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    report = sim.run_sim(str(path))
    by_t = {c["temperature_c"]: c for c in report["corners"]}
    assert "analysis_args" not in by_t[27]
    assert by_t[85]["analysis_args"] == "5u 2m"
