"""Tests for ``measurements[].search`` -- bounded bisection graded through
``klt sim`` (issue #4121).

The ``real_ngspice`` tests run the engine on a resistor driven by the searched
current source, so the boundary of ``v(x) > 0.3`` is known analytically
(``0.3 / rload``).
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest

from klayout_tools import sim, sim_batch, sim_search
from klayout_tools.sim_errors import SimError

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

_SKIP_NO_NGSPICE = pytest.mark.skipif(
    shutil.which("ngspice") is None, reason="ngspice not installed"
)

_SEARCH = {
    "source": "Ib",
    "lo": 0.0,
    "hi": 1e-3,
    "tol": 1e-6,
    "max_iter": 20,
    "analysis": {"kind": "op", "args": ""},
    "predicate": {"expr": "v(x) > 0.3", "at": "end"},
}
_DECL = {"name": "trip", "search": _SEARCH, "unit": "A"}


def _body(tmp_path: Path, text: str | None = None) -> None:
    (tmp_path / "body.spice").write_text(
        text or ".param rload=1000\nV1 in 0 1\nR0 in 0 1k\nIb 0 x 0\nR1 x 0 {rload}\n"
    )


def _request(tmp_path: Path, measurements: list[dict], **extra) -> Path:
    _body(tmp_path)
    path = tmp_path / "request.json"
    path.write_text(
        json.dumps(
            {
                "netlist": "body.spice",
                "backend": "local",
                "analysis": {"kind": "op", "args": ""},
                "measurements": measurements,
                "options": {"keep_artifacts": False},
                **extra,
            }
        )
    )
    return path


def _spec(**overrides) -> dict:
    body = copy.deepcopy(_SEARCH)
    body.update(overrides)
    return {"name": "trip", "search": body}


def _only(report):
    (corner,) = report["corners"]
    return corner, {m["name"]: m for m in corner["measurements"]}


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "overrides, match",
    [
        ({"source": "Rload"}, "independent V or I source"),
        ({"lo": 1e-3}, "must be greater than"),
        ({"hi": 0.0}, "must be greater than"),
        ({"tol": 0}, "must be > 0"),
        ({"tol": 1.0}, "smaller than"),
        ({"tol": 1e-12}, "6-significant-digit"),
        ({"max_iter": 0}, "max_iter must be in"),
        ({"max_iter": sim_search.MAX_ITER_CAP + 1}, "max_iter must be in"),
        ({"max_iter": 3}, "can never reach tol"),
        ({"max_iter": 2.5}, "must be an integer"),
        ({"lo": float("nan")}, "finite number"),
        ({"direction": "up"}, "direction"),
        ({"report": "mean"}, "report"),
        ({"bogus": 1}, "unknown search key"),
        ({"analysis": {"kind": "ac", "args": "dec 1 1 10"}}, "analysis.kind"),
        ({"analysis": {"kind": "tran", "args": ""}}, "args is required"),
        ({"analysis": {"kind": "tran", "args": "1n {t}"}}, "brace"),
        ({"predicate": {"expr": "x = 1"}}, "truth value"),
        ({"predicate": {"expr": "v(x) > 0", "at": "start"}}, "at must be"),
    ],
)
def test_bad_specs_fail_validation_with_a_named_error(overrides, match):
    with pytest.raises(SimError, match=match):
        sim_search.validate_form_exclusive(_spec(**overrides))


def test_missing_required_keys_are_named():
    spec = _spec()
    del spec["search"]["max_iter"]
    with pytest.raises(SimError, match="missing \\['max_iter'\\]"):
        sim_search.validate_form_exclusive(spec)


def test_search_is_exclusive_with_the_other_forms():
    spec = {**_spec(), "expr": "v(x)"}
    with pytest.raises(SimError, match="alternative forms"):
        sim_search.validate_form_exclusive(spec)
    assert sim_search.validate_form_exclusive({"name": "a", "expr": "v(x)"}) is False


def test_predicate_operators_become_ngspice_words():
    assert sim_search.predicate_expr("v(q) > 0.9*vdd") == "v(q)  gt  0.9*vdd"
    assert " ge " in sim_search.predicate_expr("a >= b")
    assert " ne " in sim_search.predicate_expr("a != b")


def test_deck_has_no_search_lines_without_a_search_entry():
    assert sim_search.control_lines([{"name": "a", "expr": "v(x)"}]) == []


def test_deck_lines_carry_a_while_loop():
    lines = sim_search.control_lines([_DECL])
    assert "while klt_s0_run eq 1" in lines
    assert [line.strip() for line in lines].count("op") == 3
    assert not any(">" in line for line in lines)


def test_duplicate_names_and_structured_analyses_are_refused():
    with pytest.raises(SimError, match="duplicate measurement name"):
        sim_search.validate_search_context([_DECL, _DECL], {"kind": "op"})
    with pytest.raises(SimError, match="passes"):
        sim_search.validate_search_context([_DECL], {"kind": "ac", "passes": []})


def test_alter_targets_with_no_effect_are_refused():
    with pytest.raises(SimError, match="corners.supply_v"):
        sim_search.validate_search_targets([_DECL], {}, {"ib"})
    with pytest.raises(SimError, match="PULSE waveform"):
        sim_search.validate_search_targets([_DECL], {"ib": "pulse"}, set())
    sim_search.validate_search_targets([_DECL], {}, set())


def test_refused_under_analysis_steps(tmp_path):
    request = _request(tmp_path, [])
    document = json.loads(request.read_text())
    del document["analysis"]
    document["analysis_steps"] = [
        {"name": "s", "analysis": {"kind": "op", "args": ""}, "measurements": []}
    ]
    document["measurements"] = [_DECL]
    request.write_text(json.dumps(document))
    with pytest.raises(SimError, match="'search' is not supported with analysis_steps"):
        sim.run_sim(str(request))


def test_refused_for_engine_xyce(tmp_path):
    request = _request(tmp_path, [_DECL], engine="xyce")
    with pytest.raises(SimError, match="measurements\\[\\].search is not supported"):
        sim.run_sim(str(request))


# --------------------------------------------------------------------------- #
# Real ngspice
# --------------------------------------------------------------------------- #


@_SKIP_NO_NGSPICE
def test_real_ngspice_converges_to_the_analytic_threshold(tmp_path):
    request = _request(tmp_path, [{**_DECL, "limits": {"min": 1e-4, "max": 5e-4}}])

    corner, by_name = _only(sim.run_sim(str(request)))

    trip = by_name["trip"]
    assert trip["value"] == pytest.approx(0.3e-3, abs=_SEARCH["tol"])
    assert trip["status"] == "pass"
    info = trip["search"]
    assert info["converged"] is True
    assert 0 < info["iterations"] <= _SEARCH["max_iter"]
    assert info["hi"] - info["lo"] <= _SEARCH["tol"]
    assert corner["status"] == "pass"


@_SKIP_NO_NGSPICE
def test_real_ngspice_transient_probe_and_failing_limit(tmp_path):
    spec = {
        "name": "trip",
        "search": {
            **_SEARCH,
            "analysis": {"kind": "tran", "args": "10p 1n"},
        },
        "limits": {"min": 0.5e-3},
    }
    request = _request(tmp_path, [spec])

    _, by_name = _only(sim.run_sim(str(request)))

    assert by_name["trip"]["value"] == pytest.approx(0.3e-3, abs=_SEARCH["tol"])
    assert by_name["trip"]["status"] == "fail"


@_SKIP_NO_NGSPICE
def test_real_ngspice_descending_direction(tmp_path):
    spec = _spec(
        direction="falling",
        predicate={"expr": "v(x) < 0.3"},
    )
    request = _request(tmp_path, [spec])

    _, by_name = _only(sim.run_sim(str(request)))

    assert by_name["trip"]["value"] == pytest.approx(0.3e-3, abs=_SEARCH["tol"])


@_SKIP_NO_NGSPICE
def test_real_ngspice_invalid_bracket_is_an_error_without_a_value(tmp_path):
    request = _request(tmp_path, [_spec(hi=2e-4)])

    corner, by_name = _only(sim.run_sim(str(request)))

    assert by_name["trip"]["status"] == "error"
    assert by_name["trip"]["value"] is None
    assert [d["message"][:24] for d in corner["diagnostics"]] == [
        "[search_bracket_invalid]"
    ]


@_SKIP_NO_NGSPICE
def test_real_ngspice_failed_probe_is_an_error_without_a_value(tmp_path):
    spec = _spec(predicate={"expr": "v(nope) > 0.3"})
    request = _request(tmp_path, [spec])

    corner, by_name = _only(sim.run_sim(str(request)))

    assert by_name["trip"]["status"] == "error"
    assert by_name["trip"]["value"] is None
    assert [d["message"][:22] for d in corner["diagnostics"]] == [
        "[search_probe_failed] "
    ]


def test_not_converged_state_is_a_strict_error_with_the_bracket():
    log = (
        "klt_s0_lo = 1.0e-04\nklt_s0_hi = 2.0e-04\n"
        "klt_s0_n = 4\nklt_s0_st = 3\nklt_s0_x = 1.5e-04\n"
    )
    outcomes = sim_search.extract_corner_searches([_DECL], log)
    outcome = outcomes[0]
    assert outcome.value is None
    assert outcome.code == "search_not_converged"
    assert (outcome.iterations, outcome.lo, outcome.hi) == (4, 1e-4, 2e-4)


def test_missing_log_state_is_search_no_result():
    outcome = sim_search.extract_corner_searches([_DECL], "nothing here")[0]
    assert outcome.value is None and outcome.code == "search_no_result"


@_SKIP_NO_NGSPICE
def test_real_ngspice_composes_with_param_axis_and_monte_carlo(tmp_path):
    request = _request(
        tmp_path,
        [_DECL],
        corners={"params": {"rload": [1000, 2000]}},
        monte_carlo={"n": 2, "seed": 5, "vary": "both"},
    )

    report = sim.run_sim(str(request))

    assert report["status"] == "pass", [c["diagnostics"] for c in report["corners"]]
    values = [c["measurements"][0]["value"] for c in report["corners"]]
    assert values[0] == pytest.approx(0.3e-3, abs=2e-6)
    assert values[2] == pytest.approx(0.15e-3, abs=2e-6)
    assert all(c["measurements"][0]["search"]["converged"] for c in report["corners"])


# --------------------------------------------------------------------------- #
# Mandatory batch runner feature
# --------------------------------------------------------------------------- #

_SEARCH_REQUEST = {
    "models": {"pdk": "sky130A"},
    "options": {},
    "measurements": [_DECL],
}


def test_search_is_a_registered_mandatory_runner_feature():
    assert sim_batch._request_features(_SEARCH_REQUEST) == ["search"]
    assert (
        sim_batch._request_features({"measurements": [{"name": "x", "spice": "."}]})
        == []
    )
    assert "search" in sim_batch._MANDATORY_RUNNER_FEATURES
    assert sim_batch._missing_runner_features(["search"], "0.7.0") == ["search"]
    assert sim_batch._missing_runner_features(["search"], "0.7.1") == []
    assert sim_batch._missing_runner_features(["search"], None) is None
    assert sim_batch.REQUEST_FEATURE_MIN_RUNNER_VERSION["measurements[].search"] is None
