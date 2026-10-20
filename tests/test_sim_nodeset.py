"""Tests for `klt sim`'s initial-guess hints (`options.nodeset`, issue #3036).

A multi-equilibrium circuit needs different `.nodeset` guesses at different
PVT/parameter points. Rules select a per-point `{node: volts}` map after corner
expansion and exclusions and before Monte Carlo cloning; the map is applied as
file-scope `.nodeset` cards (ngspice has no `nodeset` control command). Unit
tests need no simulator; the integration tests run the real `ngspice -b` and
skip without it. Follows `tests/test_sim_params.py`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from klayout_tools import sim, sim_remote

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

HAVE_NGSPICE = shutil.which("ngspice") is not None and (
    os.environ.get("KLT_SKIP_NGSPICE_TESTS") != "1"
)
_SKIP_NO_NGSPICE = pytest.mark.skipif(
    not HAVE_NGSPICE, reason="ngspice is not installed on this machine"
)

#: Two independent bistable latches (`a`/`b` and `c`/`d`): without a hint every
#: node solves to 0 V, with one the latch settles at the hinted sign. The
#: body hints both latches low; `k` is a top-level `.param` so a parameter row
#: forces `alterparam` + `reset` before the hint matters.
_LATCH = (
    ".param k=5\n"
    "Ba a 0 V=tanh({k}*v(b))\n"
    "Bb b 0 V=tanh({k}*v(a))\n"
    "Bc c 0 V=tanh({k}*v(d))\n"
    "Bd d 0 V=tanh({k}*v(c))\n"
)
_BODY_HINTS = ".nodeset v(a)=-1 v(c)=-1\n"
_DIVIDER = "V1 in 0 1\nR1 in out 1k\nR2 out 0 1k\n"


def _points(corners: dict, exclude: list | None = None) -> list[sim.CornerPoint]:
    return sim._expand_corners(corners, exclude or [])


def _rule(match: dict, **values: float) -> dict:
    return {"match": match, "values": values}


def _resolve(corners: dict, rules: list, exclude: list | None = None):
    points = _points(corners, exclude)
    sim._apply_nodeset_rules(points, sim._validate_nodeset_rules(rules))
    return points


def _write_body(tmp_path: Path, text: str) -> None:
    (tmp_path / "body.spice").write_text(text)


def _write_request(tmp_path: Path, request: dict) -> Path:
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    return path


# --------------------------------------------------------------------------- #
# Grammar and validation
# --------------------------------------------------------------------------- #


def test_omitted_null_and_empty_options_resolve_to_no_rules():
    assert sim._validate_nodeset_rules(None) == []
    assert sim._validate_nodeset_rules([]) == []


def test_rule_is_normalized_lowercase_sorted_floats():
    (rule,) = sim._validate_nodeset_rules(
        [
            {
                "match": {"process": "tt", "params": {"Code": 0.0}},
                "values": {"X1.Bias": 1, "OUT": 0.2},
            }
        ]
    )
    assert rule["values"] == {"out": 0.2, "x1.bias": 1.0}
    assert rule["match"] == {"process": "tt", "params": {"code": 0}}


@pytest.mark.parametrize(
    "rules, message",
    [
        ({"match": {}, "values": {"a": 1}}, "must be an array"),
        (["x"], "exactly the keys"),
        ([{"match": {}}], "exactly the keys"),
        ([{"match": {}, "values": {"a": 1}, "extra": 1}], "exactly the keys"),
        ([{"match": [], "values": {"a": 1}}], "match must be an object"),
        ([{"match": {"bogus": 1}, "values": {"a": 1}}], "unknown key"),
        ([{"match": {"process": None}, "values": {"a": 1}}], "process must be"),
        ([{"match": {"process": 1}, "values": {"a": 1}}], "process must be"),
        ([{"match": {"temperature_c": None}, "values": {"a": 1}}], "temperature_c"),
        ([{"match": {"temperature_c": True}, "values": {"a": 1}}], "temperature_c"),
        ([{"match": {"supply_v": 1}, "values": {"a": 1}}], "supply_v must be"),
        ([{"match": {"supply_v": {"vdd": None}}, "values": {"a": 1}}], "vdd"),
        ([{"match": {"params": {"r": True}}, "values": {"a": 1}}], "params.r"),
        ([{"match": {"params": {"mc_x": 1}}, "values": {"a": 1}}], "reserved"),
        ([{"match": {}, "values": {}}], "nonempty"),
        ([{"match": {}, "values": []}], "nonempty"),
        ([{"match": {}, "values": {"a": None}}], "finite number"),
        ([{"match": {}, "values": {"a": True}}], "finite number"),
        ([{"match": {}, "values": {"a": "0.5"}}], "finite number"),
        ([{"match": {}, "values": {"a": float("nan")}}], "finite number"),
        ([{"match": {}, "values": {"a": float("inf")}}], "finite number"),
        ([{"match": {}, "values": {"A": 1, "a": 2}}], "more than once"),
        ([{"match": {"params": {"R": 1, "r": 2}}, "values": {"a": 1}}], "more than"),
    ],
)
def test_malformed_rules_are_rejected(rules, message):
    with pytest.raises(sim.SimError, match=message):
        sim._validate_nodeset_rules(rules)


@pytest.mark.parametrize(
    "node",
    [
        "a b",
        "a\nb",
        "a)\n.control",
        "v(a)",
        "a+b",
        "a;b",
        "a=1",
        "",
        "-a",
        ".a",
        "a b=1\n.include /etc/passwd",
        "é",
    ],
)
def test_node_names_that_could_inject_cards_are_rejected(node):
    with pytest.raises(sim.SimError, match="not a supported node name"):
        sim._validate_nodeset_rules([{"match": {}, "values": {node: 1}}])


@pytest.mark.parametrize("node", ["out", "x1.bias", "X1:a", "_n", "12", "a_b.c9"])
def test_supported_node_spellings_are_accepted(node):
    (rule,) = sim._validate_nodeset_rules([{"match": {}, "values": {node: 0.5}}])
    assert rule["values"] == {node.lower(): 0.5}


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #


def test_one_matching_rule_selects_its_whole_map_per_point():
    points = _resolve(
        {"temperature_c": [27, 85]},
        [
            _rule({"temperature_c": 27}, out=0.2, x=0.1),
            _rule({"temperature_c": 85}, out=0.9),
        ],
    )
    assert [p.nodeset for p in points] == [{"out": 0.2, "x": 0.1}, {"out": 0.9}]


def test_empty_match_applies_to_every_retained_point():
    points = _resolve({"temperature_c": [27, 85]}, [_rule({}, out=0.5)])
    assert [p.nodeset for p in points] == [{"out": 0.5}, {"out": 0.5}]


def test_a_point_no_rule_matches_gets_no_hint():
    points = _resolve(
        {"temperature_c": [27, 85]}, [_rule({"temperature_c": 85}, out=0.5)]
    )
    assert [p.nodeset for p in points] == [{}, {"out": 0.5}]


def test_coordinates_in_one_rule_are_anded_and_supply_is_a_subset_match():
    corners = {
        "process": ["tt", "ss"],
        "supply_v": {"vdd": [1.0, 1.2], "vio": [3.3, 3.3]},
        "temperature_c": [27],
    }
    points = _resolve(
        corners, [_rule({"process": "ss", "supply_v": {"vdd": 1.2}}, out=0.7)]
    )
    assert [p.nodeset for p in points] == [{}, {}, {}, {"out": 0.7}]
    assert [p.corner_id for p in points if p.nodeset] == ["ss/vdd=1.200_vio=3.300V/27C"]


def test_params_selector_matches_numerically_with_folded_names():
    points = _resolve(
        {"params": {"code": [0, 1, 2]}},
        [_rule({"params": {"CODE": 1.0}}, out=0.4)],
    )
    assert [p.nodeset for p in points] == [{}, {"out": 0.4}, {}]


def test_exclusions_run_first_so_a_rule_for_an_excluded_point_is_unused():
    corners = {"temperature_c": [27, 85]}
    exclude = [{"temperature_c": 85}]
    assert _resolve(corners, [_rule({"temperature_c": 27}, a=1)], exclude)
    with pytest.raises(sim.SimError, match="matches no retained corner"):
        _resolve(corners, [_rule({"temperature_c": 85}, a=1)], exclude)


def test_overlapping_rules_are_an_error_even_when_disjoint_or_equal():
    for second in ({"b": 2.0}, {"a": 1.0}):
        with pytest.raises(sim.SimError, match="overlapping rules are never merged"):
            _resolve(
                {"temperature_c": [27]},
                [_rule({}, a=1.0), _rule({"temperature_c": 27}, **second)],
            )


def test_a_rule_matching_no_point_is_an_error():
    with pytest.raises(sim.SimError, match=r"options\.nodeset\[1\] matches no"):
        _resolve(
            {"temperature_c": [27]},
            [_rule({}, a=1), _rule({"process": "ff"}, a=2)],
        )


def test_unknown_parameter_and_rail_names_are_errors():
    with pytest.raises(sim.SimError, match="not a corners.params parameter"):
        _resolve({"params": {"r": [1]}}, [_rule({"params": {"nope": 1}}, a=1)])
    with pytest.raises(sim.SimError, match="which no corner of this request sets"):
        _resolve({"supply_v": {"vdd": [1.0]}}, [_rule({"supply_v": {"vcc": 1.0}}, a=1)])


def test_corner_ids_and_slugs_do_not_carry_the_hint():
    plain = _points({"temperature_c": [27]})
    hinted = _resolve({"temperature_c": [27]}, [_rule({}, a=1)])
    assert [p.corner_id for p in plain] == [p.corner_id for p in hinted]
    assert [p.slug for p in plain] == [p.slug for p in hinted]


def test_monte_carlo_samples_inherit_their_base_points_map():
    points = _resolve(
        {"temperature_c": [27, 85]}, [_rule({"temperature_c": 85}, out=0.9)]
    )
    sampled, _info = sim._expand_monte_carlo(
        points, {"n": 2, "seed": 3, "vary": "both"}
    )
    assert [(p.temperature_c, p.sample_index, p.nodeset) for p in sampled] == [
        (27, 0, {}),
        (27, 1, {}),
        (85, 0, {"out": 0.9}),
        (85, 1, {"out": 0.9}),
    ]


# --------------------------------------------------------------------------- #
# Wire format
# --------------------------------------------------------------------------- #


def test_wire_round_trip_preserves_nodeset_params_and_mc_seed():
    points = _resolve(
        {"temperature_c": [27, 85], "params": {"r": [1, 2]}},
        [_rule({"temperature_c": 85, "params": {"r": 2}}, out=0.9)],
    )
    sampled, _ = sim._expand_monte_carlo(points, {"n": 1, "seed": 3, "vary": "both"})
    wire = json.loads(json.dumps(sim._corner_points_to_wire(sampled)))
    back = sim._corner_points_from_wire(wire)
    assert [(p.corner_id, p.nodeset, p.mc_seed) for p in back] == [
        (p.corner_id, p.nodeset, p.mc_seed) for p in sampled
    ]
    assert sum(1 for p in back if p.nodeset) == 1


def test_wire_omits_the_field_for_a_point_without_a_hint():
    (entry,) = sim._corner_points_to_wire(_points({}))
    assert "nodeset" not in entry


@pytest.mark.parametrize(
    "bad",
    [{}, [], {"a b": 1}, {"a": None}, {"a": True}, {"a": "1"}, {"A": 1, "a": 1}],
)
def test_wire_nodeset_is_revalidated_at_the_receiving_worker(bad):
    entry = sim._corner_points_to_wire(_points({}))[0]
    entry["nodeset"] = bad
    with pytest.raises(sim.SimError, match="request._explicit_points"):
        sim._corner_points_from_wire([entry])


def test_remote_request_ships_selected_maps_in_explicit_points():
    points = _resolve({"temperature_c": [27, 85]}, [_rule({"temperature_c": 85}, a=1)])
    request = {
        "netlist": "n.spice",
        "options": {"nodeset": [_rule({"temperature_c": 85}, a=1)]},
        "corners": {"temperature_c": [27, 85]},
    }
    remote = sim_remote._build_remote_request(
        request,
        timeout_s=10,
        keep_artifacts=False,
        want_waveforms=False,
        explicit_points=points,
    )
    assert [e.get("nodeset") for e in remote["_explicit_points"]] == [None, {"a": 1.0}]


def test_unrun_corner_report_carries_the_hint():
    (point,) = _resolve({}, [_rule({}, a=0.5)])
    report = sim_remote._unrun_corner_report(point, [], "lost_shard")
    assert report["nodeset"] == {"a": 0.5}
    (legacy,) = _points({})
    assert "nodeset" not in sim_remote._unrun_corner_report(legacy, [], "lost_shard")


# --------------------------------------------------------------------------- #
# Deck
# --------------------------------------------------------------------------- #


def _deck_lines(tmp_path: Path, point: sim.CornerPoint) -> list[str]:
    deck = tmp_path / "deck.spice"
    sim._write_corner_deck(
        deck_path=str(deck),
        netlist_path="/n.spice",
        models_lib=None,
        point=point,
        analysis={"kind": "op", "args": ""},
        measurements_spec=[],
        raw_path=None,
    )
    return deck.read_text().splitlines()


def test_nodeset_cards_follow_the_include_at_file_scope_not_in_control(tmp_path):
    (point,) = _resolve(
        {"params": {"k": [5]}}, [_rule({}, **{"x1.bias": 0.7, "out": -0.2})]
    )
    lines = _deck_lines(tmp_path, point)
    control = lines.index(".control")
    include = lines.index(".include /n.spice")
    cards = [i for i, line in enumerate(lines) if line.startswith(".nodeset")]
    assert [lines[i] for i in cards] == [
        ".nodeset v(out)=-0.2",
        ".nodeset v(x1.bias)=0.7",
    ]
    assert all(include < i < control for i in cards)
    assert not any("nodeset" in line for line in lines[control:])


def test_deck_for_a_point_without_a_hint_has_no_nodeset_card(tmp_path):
    lines = _deck_lines(tmp_path, sim.CornerPoint(None, {}, 27))
    assert not any("nodeset" in line for line in lines)
    assert lines == [
        "* klt sim -- generated corner deck, do not edit",
        ".include /n.spice",
        ".temp 27",
        ".control",
        "op ",
        "quit",
        ".endc",
        ".end",
    ]


def test_fail_fast_probe_deck_applies_the_hint(tmp_path, monkeypatch):
    """The probe is a separate dispatch path; it writes its deck from the first
    dispatched point, hint included."""
    (point,) = _resolve({}, [_rule({}, a=0.5)])
    written: list[sim.CornerPoint] = []

    def capture(**kwargs):
        written.append(kwargs["point"])
        raise RuntimeError("stop after the deck is written")

    monkeypatch.setattr(sim, "_write_corner_deck", capture)
    _write_body(tmp_path, _DIVIDER)
    with pytest.raises(RuntimeError, match="stop after"):
        sim._run_fail_fast_probe(
            corner_points=[point],
            netlist_path=str(tmp_path / "body.spice"),
            models_lib=None,
            analysis={"kind": "tran", "args": "1n 1u"},
            timeout_s=60.0,
            artifacts_dir=str(tmp_path / "a"),
            keep_artifacts=False,
            ngspice_binary=None,
            ngspice_init=(),
            osdi_preload=(),
            save_mode=sim.DEFAULT_SAVE_MODE,
            ngspice_options=None,
        )
    assert [p.nodeset for p in written] == [{"a": 0.5}]


def test_xyce_refuses_nodeset():
    with pytest.raises(sim.SimError, match="options.nodeset is not supported"):
        sim._enforce_xyce_support_boundary(
            engine="xyce",
            backend="local",
            corners_spec={},
            monte_carlo_declared=False,
            fail_fast_probe=False,
            nodeset_declared=True,
        )


# --------------------------------------------------------------------------- #
# Diagnostics: unknown nodes
# --------------------------------------------------------------------------- #

_WARNING = (
    "Warning : Nodeset on non-existent node - {node}, ignored\n"
    "   Please check line .nodeset v({node})=1\n"
)


def test_a_requested_node_the_engine_reports_missing_is_an_error_diagnostic():
    (point,) = _resolve({}, [_rule({}, out=1, zzz=1)])
    (diag,) = sim._nodeset_failures(point, _WARNING.format(node="zzz"))
    assert diag["severity"] == "error" and diag["code"] == "netlist"
    assert "'zzz'" in diag["message"]


def test_a_missing_body_hint_node_is_not_the_requests_business():
    (point,) = _resolve({}, [_rule({}, out=1)])
    assert sim._nodeset_failures(point, _WARNING.format(node="elsewhere")) == []
    (legacy,) = _points({})
    assert sim._nodeset_failures(legacy, _WARNING.format(node="zzz")) == []


# --------------------------------------------------------------------------- #
# Checkpoint / resume
# --------------------------------------------------------------------------- #


def _fingerprint(tmp_path: Path, points: list[sim.CornerPoint]) -> str:
    _write_body(tmp_path, _DIVIDER)
    return sim._checkpoint_fingerprint(
        netlist_path=str(tmp_path / "body.spice"),
        models_lib=None,
        analysis={"kind": "op", "args": ""},
        measurements_spec=[],
        corner_points=points,
        timeout_s=10.0,
        engine="ngspice",
    )


def test_a_changed_hint_changes_the_fingerprint_and_a_legacy_one_is_stable(tmp_path):
    corners = {"temperature_c": [27, 85]}
    legacy = _fingerprint(tmp_path, _points(corners))
    one = _fingerprint(tmp_path, _resolve(corners, [_rule({}, a=1.0)]))
    two = _fingerprint(tmp_path, _resolve(corners, [_rule({}, a=-1.0)]))
    moved = _fingerprint(
        tmp_path, _resolve(corners, [_rule({"temperature_c": 85}, a=1.0)])
    )
    assert len({legacy, one, two, moved}) == 4
    assert legacy == _fingerprint(tmp_path, _points(corners))


# --------------------------------------------------------------------------- #
# Off-host worker compatibility
# --------------------------------------------------------------------------- #


def _echo(point: sim.CornerPoint, **over) -> dict:
    corner = {"corner_id": point.corner_id, "status": "pass", "diagnostics": []}
    if point.nodeset:
        corner["nodeset"] = dict(point.nodeset)
    return {**corner, **over}


def test_updated_worker_echo_is_accepted():
    points = _resolve({"temperature_c": [27, 85]}, [_rule({"temperature_c": 85}, a=1)])
    sim._require_nodeset_echo(points, [_echo(p) for p in points])


def test_a_worker_that_drops_the_hint_cannot_return_a_pass():
    points = _resolve({}, [_rule({}, a=1)])
    with pytest.raises(sim.SimError) as raised:
        sim._require_nodeset_echo(points, [_echo(points[0], nodeset=None)])
    assert raised.value.code == sim.NODESET_UNSUPPORTED_WORKER_CODE


def test_a_worker_that_echoes_the_wrong_map_or_count_is_refused():
    points = _resolve({"temperature_c": [27, 85]}, [_rule({}, a=1)])
    with pytest.raises(sim.SimError, match="expected"):
        sim._require_nodeset_echo(
            points, [_echo(points[0]), _echo(points[1], nodeset={"a": 2.0})]
        )
    with pytest.raises(sim.SimError, match="1 corner report"):
        sim._require_nodeset_echo(points, [_echo(points[0])])


def test_requests_without_hints_and_local_backends_are_never_gated():
    points = _points({"temperature_c": [27, 85]})
    sim._require_nodeset_echo(points, [])
    hinted = _resolve({}, [_rule({}, a=1)])
    sim._require_nodeset_echo(hinted, [], backend="local")


@pytest.mark.parametrize("backend", ["remote", "batch"])
def test_run_sim_refuses_an_old_offhost_worker_end_to_end(
    tmp_path, monkeypatch, backend
):
    _write_body(tmp_path, _DIVIDER)

    def old_worker(**kwargs):
        # Ignores the field: honest-looking reports with no `nodeset` echo.
        return (
            [_echo(p, nodeset=None) for p in kwargs["corner_points"]],
            "ngspice-46",
            None,
        )

    monkeypatch.setitem(sim._BACKENDS, backend, old_worker)
    monkeypatch.setattr(sim, "_preflight_staged_model_inputs", lambda *a, **k: None)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "backend": backend,
            "analysis": {"kind": "op", "args": ""},
            "options": {"nodeset": [_rule({}, out=0.2)]},
        },
    )
    with pytest.raises(sim.SimError) as raised:
        sim.run_sim(str(request))
    assert raised.value.code == sim.NODESET_UNSUPPORTED_WORKER_CODE


def test_run_sim_rejects_bad_rules_before_any_dispatch(tmp_path, monkeypatch):
    _write_body(tmp_path, _DIVIDER)

    def never(**kwargs):
        raise AssertionError("dispatched despite an invalid request")

    monkeypatch.setitem(sim._BACKENDS, "local", never)
    for rules, message in (
        ([_rule({}, a=1), _rule({}, a=1)], "overlapping"),
        ([_rule({"process": "ff"}, a=1)], "matches no retained"),
        ([_rule({}, **{"a b": 1})], "not a supported node name"),
    ):
        request = _write_request(
            tmp_path,
            {
                "netlist": "body.spice",
                "backend": "local",
                "analysis": {"kind": "op", "args": ""},
                "options": {"nodeset": rules},
            },
        )
        with pytest.raises(sim.SimError, match=message):
            sim.run_sim(str(request))


def test_run_sim_refuses_nodeset_for_xyce_before_dispatch(tmp_path):
    _write_body(tmp_path, _DIVIDER)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "engine": "xyce",
            "backend": "local",
            "analysis": {"kind": "op", "args": ""},
            "options": {"nodeset": [_rule({}, out=0.2)]},
        },
    )
    with pytest.raises(sim.SimError, match="options.nodeset is not supported"):
        sim.run_sim(str(request))


# --------------------------------------------------------------------------- #
# Integration: the real ngspice binary
# --------------------------------------------------------------------------- #


def _engine_request(tmp_path: Path, measures: list[str], **overrides) -> Path:
    request = {
        "netlist": "body.spice",
        "analysis": {"kind": "op", "args": ""},
        "measurements": [
            {
                "name": m.replace("(", "_").replace(")", "").replace(".", "_"),
                "expr": m,
                "unit": "V",
            }
            for m in measures
        ],
        "backend": "local",
        "options": {"keep_artifacts": True},
        **overrides,
    }
    return _write_request(tmp_path, request)


def _by_corner(report: dict) -> list[dict[str, float]]:
    return [
        {m["name"]: m["value"] for m in corner["measurements"]}
        for corner in report["corners"]
    ]


def _run(tmp_path: Path, request: Path) -> dict:
    report = sim.run_sim(str(request), artifacts_dir=str(tmp_path / "a"))
    assert report["status"] == "pass", [c["diagnostics"] for c in report["corners"]]
    return report


@_SKIP_NO_NGSPICE
def test_real_ngspice_body_hint_alone_selects_the_low_state(tmp_path):
    """Control: with no request hint, the body's own `.nodeset` decides."""
    _write_body(tmp_path, _LATCH + _BODY_HINTS)
    report = _run(tmp_path, _engine_request(tmp_path, ["v(a)", "v(c)"]))
    (row,) = _by_corner(report)
    assert row["v_a"] == pytest.approx(-1.0, abs=1e-3)
    assert row["v_c"] == pytest.approx(-1.0, abs=1e-3)
    assert "nodeset" not in report["corners"][0]


@_SKIP_NO_NGSPICE
def test_real_ngspice_request_hint_overrides_body_hint_and_keeps_unrelated_ones(
    tmp_path,
):
    _write_body(tmp_path, _LATCH + _BODY_HINTS)
    request = _engine_request(
        tmp_path,
        ["v(a)", "v(b)", "v(c)"],
        options={"keep_artifacts": True, "nodeset": [_rule({}, a=1.0)]},
    )
    report = _run(tmp_path, request)
    (row,) = _by_corner(report)
    # `a` was hinted low in the body and high by the request: the request wins.
    assert row["v_a"] == pytest.approx(1.0, abs=1e-3)
    assert row["v_b"] == pytest.approx(1.0, abs=1e-3)
    # `c` is only hinted by the body and is left alone.
    assert row["v_c"] == pytest.approx(-1.0, abs=1e-3)
    assert report["corners"][0]["nodeset"] == {"a": 1.0}
    deck = Path(report["corners"][0]["artifacts"]["deck"]).read_text()
    assert ".nodeset v(a)=1.0" in deck
    assert report["environment"]["engine_version"]


@_SKIP_NO_NGSPICE
def test_real_ngspice_different_points_get_their_own_hints_without_leakage(tmp_path):
    _write_body(tmp_path, _LATCH)
    request = _engine_request(
        tmp_path,
        ["v(a)", "v(c)"],
        corners={"temperature_c": [27, 85, 125]},
        options={
            "keep_artifacts": True,
            "nodeset": [
                _rule({"temperature_c": 27}, a=1.0, c=-1.0),
                _rule({"temperature_c": 85}, a=-1.0, c=1.0),
            ],
        },
    )
    report = _run(tmp_path, request)
    rows = _by_corner(report)
    assert (round(rows[0]["v_a"]), round(rows[0]["v_c"])) == (1, -1)
    assert (round(rows[1]["v_a"]), round(rows[1]["v_c"])) == (-1, 1)
    # 125 C matched no rule: no hint, so both latches rest at the 0 V solution.
    assert rows[2]["v_a"] == pytest.approx(0.0, abs=1e-6)
    assert [c.get("nodeset") for c in report["corners"]] == [
        {"a": 1.0, "c": -1.0},
        {"a": -1.0, "c": 1.0},
        None,
    ]


@_SKIP_NO_NGSPICE
def test_real_ngspice_hint_survives_the_parameter_reset(tmp_path):
    """`alterparam` + `reset` re-parses the circuit; the file-scope card must
    still apply to the re-solved latch, whose gain the row changes."""
    _write_body(tmp_path, _LATCH)
    request = _engine_request(
        tmp_path,
        ["v(a)", "v(c)"],
        corners={"params": {"k": [5, 8]}},
        options={
            "keep_artifacts": True,
            "nodeset": [
                _rule({"params": {"k": 5}}, a=1.0, c=1.0),
                _rule({"params": {"k": 8}}, a=-1.0, c=1.0),
            ],
        },
    )
    report = _run(tmp_path, request)
    rows = _by_corner(report)
    assert (round(rows[0]["v_a"]), round(rows[0]["v_c"])) == (1, 1)
    assert (round(rows[1]["v_a"]), round(rows[1]["v_c"])) == (-1, 1)
    # tanh(8) is closer to its rail than tanh(5): proof the row really applied.
    assert abs(rows[1]["v_a"]) > abs(rows[0]["v_a"])


@_SKIP_NO_NGSPICE
def test_real_ngspice_hint_reaches_local_parallel_monte_carlo_samples(tmp_path):
    _write_body(tmp_path, _LATCH)
    request = _engine_request(
        tmp_path,
        ["v(a)"],
        backend="local-parallel",
        corners={"temperature_c": [27, 85]},
        monte_carlo={"n": 2, "seed": 5, "vary": "both"},
        options={
            "keep_artifacts": True,
            "max_workers": 2,
            "nodeset": [
                _rule({"temperature_c": 27}, a=1.0),
                _rule({"temperature_c": 85}, a=-1.0),
            ],
        },
    )
    report = _run(tmp_path, request)
    assert [round(r["v_a"]) for r in _by_corner(report)] == [1, 1, -1, -1]
    assert [c["nodeset"] for c in report["corners"]] == [
        {"a": 1.0},
        {"a": 1.0},
        {"a": -1.0},
        {"a": -1.0},
    ]


@_SKIP_NO_NGSPICE
def test_real_ngspice_hint_reaches_every_analysis_step_including_a_reset_step(
    tmp_path,
):
    _write_body(tmp_path, _LATCH)
    step = {
        "analysis": {"kind": "op", "args": ""},
        "measurements": [{"name": "va", "expr": "v(a)", "unit": "V"}],
    }
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "backend": "local",
            "analysis_steps": [
                {"name": "first", **step},
                {"name": "again", "reset": True, **step},
            ],
            "options": {"nodeset": [_rule({}, a=1.0)]},
        },
    )
    report = _run(tmp_path, request)
    (row,) = _by_corner(report)
    assert row["first.va"] == pytest.approx(1.0, abs=1e-3)
    assert row["again.va"] == pytest.approx(1.0, abs=1e-3)


@_SKIP_NO_NGSPICE
def test_real_ngspice_nodeset_is_only_an_initial_guess(tmp_path):
    """A deliberately wrong guess does not move a linear circuit's solution:
    the 1 V / 1 k / 1 k divider solves to 0.5 V whatever `out` is hinted at.
    A nodeset is a starting point, never an asserted answer."""
    _write_body(tmp_path, _DIVIDER)
    request = _engine_request(
        tmp_path,
        ["v(out)"],
        options={"keep_artifacts": True, "nodeset": [_rule({}, out=0.2)]},
    )
    report = _run(tmp_path, request)
    (row,) = _by_corner(report)
    assert row["v_out"] == pytest.approx(0.5, abs=1e-9)
    assert report["corners"][0]["nodeset"] == {"out": 0.2}
    # The hint really reached the engine (not merely the report echo): the
    # same deck, run by hand, shows ngspice parsing it with no complaint.
    deck = Path(report["corners"][0]["artifacts"]["deck"])
    assert ".nodeset v(out)=0.2" in deck.read_text()
    log = Path(report["corners"][0]["artifacts"]["log"]).read_text()
    assert "non-existent node" not in log


@_SKIP_NO_NGSPICE
def test_real_ngspice_applied_hint_is_visible_to_the_engine(tmp_path):
    """Simulator introspection: ngspice's own `.nodeset` bookkeeping, not the
    solved voltage, shows which hints were accepted. A hinted node that does
    not exist is reported by the engine; a real one (including a hierarchical
    name) is not."""
    deck = tmp_path / "d.cir"
    deck.write_text(
        "* introspect\n.subckt s in out\nR1 in m 1k\nR2 m out 1k\n.ends\n"
        "V1 in 0 1\nX1 in 0 s\n"
        ".nodeset v(x1.m)=0.3 v(in)=0.9 v(nosuch)=1\n"
        ".control\nop\nquit\n.endc\n.end\n"
    )
    log = tmp_path / "d.log"
    subprocess.run(
        ["ngspice", "-b", "-o", str(log), str(deck)],
        check=True,
        capture_output=True,
        timeout=60,
    )
    missing = re.findall(r"non-existent node - (\S+?), ignored", log.read_text())
    assert missing == ["nosuch"]


@_SKIP_NO_NGSPICE
def test_real_ngspice_unknown_node_fails_the_corner_visibly(tmp_path):
    _write_body(tmp_path, _DIVIDER)
    request = _engine_request(
        tmp_path,
        ["v(out)"],
        options={"keep_artifacts": True, "nodeset": [_rule({}, out=0.2, nosuch=1.0)]},
    )
    report = sim.run_sim(str(request), artifacts_dir=str(tmp_path / "a"))
    assert report["status"] == "error"
    (corner,) = report["corners"]
    assert corner["status"] == "error"
    assert any(
        d["severity"] == "error" and "'nosuch'" in d["message"]
        for d in corner["diagnostics"]
    )


@_SKIP_NO_NGSPICE
def test_real_ngspice_hierarchical_node_hint_is_accepted(tmp_path):
    _write_body(
        tmp_path,
        ".subckt half in out\nR1 in m 1k\nR2 m out 1k\n.ends\n"
        "V1 in 0 1\nX1 in 0 half\n",
    )
    request = _engine_request(
        tmp_path,
        ["v(x1.m)"],
        options={"keep_artifacts": True, "nodeset": [_rule({}, **{"x1.m": 0.9})]},
    )
    report = _run(tmp_path, request)
    assert _by_corner(report)[0]["v_x1_m"] == pytest.approx(0.5, abs=1e-9)


@_SKIP_NO_NGSPICE
def test_real_ngspice_legacy_request_is_unchanged(tmp_path):
    _write_body(tmp_path, _DIVIDER)
    plain = _run(tmp_path, _engine_request(tmp_path, ["v(out)"]))
    empty = _run(
        tmp_path,
        _engine_request(
            tmp_path,
            ["v(out)"],
            options={"keep_artifacts": True, "nodeset": []},
        ),
    )
    assert [c["corner_id"] for c in plain["corners"]] == [
        c["corner_id"] for c in empty["corners"]
    ]
    assert all("nodeset" not in c for c in empty["corners"])
    assert "nodeset" not in Path(empty["corners"][0]["artifacts"]["deck"]).read_text()


@_SKIP_NO_NGSPICE
def test_real_ngspice_resume_does_not_reuse_a_result_across_a_changed_hint(tmp_path):
    _write_body(tmp_path, _LATCH)

    def run(volts: float) -> dict:
        request = _engine_request(
            tmp_path,
            ["v(a)"],
            options={
                "keep_artifacts": True,
                "resume": True,
                "nodeset": [_rule({}, a=volts)],
            },
        )
        return _run(tmp_path, request)

    first = run(1.0)
    second = run(-1.0)
    assert _by_corner(first)[0]["v_a"] == pytest.approx(1.0, abs=1e-3)
    assert _by_corner(second)[0]["v_a"] == pytest.approx(-1.0, abs=1e-3)
    # Identical hint again: now it may (and does) come from the checkpoint.
    assert _by_corner(run(-1.0))[0]["v_a"] == pytest.approx(-1.0, abs=1e-3)
