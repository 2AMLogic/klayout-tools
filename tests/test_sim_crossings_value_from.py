"""Tests for ``measurements[].crossings.value_from`` (issue #6913): a
transient crossings entry whose graded ``value`` is a period/frequency
reduction of the series instead of the crossing count.

Validation and grading run against a stubbed ``ngspice`` (the same
``_stub_ngspice`` harness ``tests/test_sim_crossings.py`` uses), so no real
simulator is needed; the batch-path fake mirrors
``test_sim_batch.py``'s crossings inline-carry test.
"""

from __future__ import annotations

import json

import pytest

from helpers.subprocess_fakes import fake_completed, uploaded_request_of
from klayout_tools import sim, sim_crossings
from klayout_tools import sim_batch as sb
from klayout_tools.sim_crossings_reduce import graded_value_from
from test_sim_batch import (
    _batch_request,
    _FakeRunner,
    _install_fake_batch_transport,
    _sim_report,
    _status,
)
from test_sim_batch import (
    _write_body as _write_batch_body,
)
from test_sim_batch import (
    _write_request as _write_batch_request,
)
from test_sim_crossings import (
    _TRIANGLE,
    _rawfile,
    _stub_ngspice,
    _write_body,
    _write_request,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

_DECL = {
    "name": "t_ro1",
    "crossings": {
        "signal": "v(out)",
        "level": 0.5,
        "edge": "rise",
        "value_from": "period.mean",
    },
}


# --------------------------------------------------------------------------- #
# Validation: closed vocabulary, dc refusal -- before any run
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "value_from", ["period", "count", "period.std", 5, True, ["period.mean"]]
)
def test_unknown_or_malformed_selector_is_refused_by_name(value_from):
    with pytest.raises(sim.SimError, match="crossings.value_from"):
        sim_crossings.validate_crossings_spec(
            "t_ro1", {**_DECL["crossings"], "value_from": value_from}
        )


def test_refusal_message_lists_the_allowed_selectors():
    with pytest.raises(sim.SimError) as excinfo:
        sim_crossings.validate_crossings_spec(
            "t_ro1", {**_DECL["crossings"], "value_from": "period"}
        )
    assert "'period.mean'" in str(excinfo.value)
    assert "'frequency'" in str(excinfo.value)


def test_value_from_is_refused_on_a_dc_sweep_before_any_run(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "dc", "args": "V1 0 4 0.5"},
            "measurements": [_DECL],
        },
    )
    with pytest.raises(sim.SimError, match="not supported on a dc sweep"):
        sim.run_sim(str(request))


def test_a_request_without_value_from_declares_no_batch_feature():
    assert sb._request_features({"measurements": [_DECL]}) == ["crossings_value_from"]
    quiet = {"signal": "v(out)", "level": 0.5, "edge": "rise"}
    assert sb._request_features({"measurements": [{"crossings": quiet}]}) == []


# --------------------------------------------------------------------------- #
# Grading: the reduction is the entry's value
# --------------------------------------------------------------------------- #


def _run_request(tmp_path, monkeypatch, points, crossings, **extra):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [{"name": "t_ro1", "crossings": crossings, **extra}],
        },
    )
    _stub_ngspice(monkeypatch, raw=_rawfile(points))
    return sim.run_sim(str(request))


def test_period_mean_is_the_graded_value_and_is_graded_by_limits(tmp_path, monkeypatch):
    """A synthetic ring oscillator with a known period: the entry's `value`
    is `reductions.period.mean` (seconds), and `limits` grade it on either
    side of the true period."""
    report = _run_request(
        tmp_path,
        monkeypatch,
        _TRIANGLE,
        _DECL["crossings"],
        limits={"max": 2.5e-9},
    )
    (measurement,) = report["corners"][0]["measurements"]
    assert measurement["value"] == pytest.approx(2e-9)
    assert measurement["status"] == "pass"
    assert measurement["crossings"]["reductions"]["period"]["mean"] == (
        pytest.approx(2e-9)
    )
    # The count is still reported (total 2 rise edges -> no truncation) on
    # the series, but nothing grades it.
    assert measurement["crossings"]["times"] == pytest.approx([0.5e-9, 2.5e-9])

    failing = _run_request(
        tmp_path,
        monkeypatch,
        _TRIANGLE,
        _DECL["crossings"],
        limits={"min": 3e-9},
    )
    (measurement,) = failing["corners"][0]["measurements"]
    assert (measurement["status"], measurement["margin"]) == (
        "fail",
        pytest.approx(-1e-9),
    )
    assert failing["status"] == "fail"


def test_frequency_selector_is_the_reciprocal_period(tmp_path, monkeypatch):
    report = _run_request(
        tmp_path,
        monkeypatch,
        _TRIANGLE,
        {**_DECL["crossings"], "value_from": "frequency"},
        unit="Hz",
    )
    (measurement,) = report["corners"][0]["measurements"]
    assert measurement["value"] == pytest.approx(5e8)
    # The caller's unit is echoed, not overwritten.
    assert measurement["unit"] == "Hz"
    assert measurement["crossings"]["reductions"]["period"]["mean"] == (
        pytest.approx(2e-9)
    )


def test_either_edge_grades_a_full_cycle_period(tmp_path, monkeypatch):
    """`edge: "either"` needs two edges per cycle (k = 2): the mean period is
    the full-cycle period, not the half."""
    report = _run_request(
        tmp_path,
        monkeypatch,
        _TRIANGLE,
        {**_DECL["crossings"], "edge": "either"},
    )
    (measurement,) = report["corners"][0]["measurements"]
    assert measurement["value"] == pytest.approx(2e-9)
    assert len(measurement["crossings"]["times"]) == 4  # 2 cycles = 4 edges


def test_value_from_implies_the_period_reduction_without_reduce(tmp_path, monkeypatch):
    """`reduce` is not listed, yet `crossings.reductions.period` is reported
    (the evidence for the graded value); a declared `reduce` list is still
    honoured alongside."""
    report = _run_request(tmp_path, monkeypatch, _TRIANGLE, _DECL["crossings"])
    (measurement,) = report["corners"][0]["measurements"]
    assert set(measurement["crossings"]["reductions"]) == {"period"}

    both = _run_request(
        tmp_path,
        monkeypatch,
        _TRIANGLE,
        {**_DECL["crossings"], "reduce": ["tie", "period"]},
    )
    (measurement,) = both["corners"][0]["measurements"]
    assert set(measurement["crossings"]["reductions"]) == {"period", "tie"}
    assert measurement["value"] == pytest.approx(2e-9)


def test_start_up_exclusion_via_from_changes_the_graded_period(tmp_path, monkeypatch):
    """The window is the existing `from`/`to`: excluding start-up edges from
    the graded period needs no second declaration."""
    # Rise crossings at 0.5, 2.5, 5.0, 7.0, 9.0 ns -> periods 2, 2.5, 2, 2
    # ns: the short first interval is the start-up outlier.
    points: list[tuple[float, float]] = []
    for t in (0.5e-9, 2.5e-9, 5.0e-9, 7.0e-9, 9.0e-9):
        points += [(t - 0.25e-9, 0.0), (t + 0.25e-9, 1.0), (t + 0.75e-9, 0.0)]

    with_window = _run_request(
        tmp_path,
        monkeypatch,
        points,
        {**_DECL["crossings"], "from": "4n"},
    )
    (measurement,) = with_window["corners"][0]["measurements"]
    assert measurement["crossings"]["times"] == pytest.approx([5.0e-9, 7.0e-9, 9.0e-9])
    assert measurement["value"] == pytest.approx(2e-9)

    without = _run_request(tmp_path, monkeypatch, points, _DECL["crossings"])
    (measurement,) = without["corners"][0]["measurements"]
    assert measurement["value"] == pytest.approx(2.125e-9)


def test_truncated_series_is_reduced_over_the_returned_edges(tmp_path, monkeypatch):
    report = _run_request(
        tmp_path,
        monkeypatch,
        _TRIANGLE,
        {**_DECL["crossings"], "edge": "either", "max": 3},
    )
    (measurement,) = report["corners"][0]["measurements"]
    series = measurement["crossings"]
    assert series["truncated"] is True  # 4 edges found, 3 returned
    assert series["times"] == pytest.approx([0.5e-9, 1.5e-9, 2.5e-9])
    assert measurement["value"] == pytest.approx(2e-9)  # t[2] - t[0]


def test_monte_carlo_statistics_pool_the_reduction(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "monte_carlo": {"n": 3, "seed": 7, "vary": "mismatch"},
            "measurements": [_DECL],
        },
    )
    _stub_ngspice(monkeypatch, raw=_rawfile(_TRIANGLE))

    report = sim.run_sim(str(request))

    # Every sample sees the same stubbed waveform, so every statistic is the
    # period -- what matters is that the rollup read the reduction, not the
    # count (2 edges would read 2.0).
    (entry,) = report["measurements"]
    assert entry["name"] == "t_ro1"
    assert entry["monte_carlo"]["n"] == 3
    assert entry["monte_carlo"]["errored"] == 0
    assert entry["monte_carlo"]["mean"] == pytest.approx(2e-9)
    assert entry["monte_carlo"]["min"] == pytest.approx(2e-9)
    assert entry["monte_carlo"]["max"] == pytest.approx(2e-9)


# --------------------------------------------------------------------------- #
# Too few edges: an error entry, never a small value
# --------------------------------------------------------------------------- #


def _single_edge_points():
    # Exactly one rise crossing of 0.5 (at 0.5 ns); the falling ramp is
    # excluded for edge "rise".
    return [(0.0, 0.0), (1e-9, 1.0), (2e-9, 0.0), (3e-9, 0.0)]


def test_too_few_edges_is_an_error_entry_with_the_reason_token(tmp_path, monkeypatch):
    report = _run_request(
        tmp_path, monkeypatch, _single_edge_points(), _DECL["crossings"]
    )
    (corner,) = report["corners"]
    (measurement,) = corner["measurements"]
    assert measurement["status"] == "error"
    assert measurement["value"] is None
    assert measurement["margin"] is None
    assert "crossings" not in measurement
    (diagnostic,) = corner["diagnostics"]
    assert diagnostic["code"] == "measurement"
    assert diagnostic["message"].startswith("[crossings_insufficient_edges]")
    assert "needs at least 2 crossings (got 1)" in diagnostic["message"]


def test_zero_edges_is_the_same_named_error(tmp_path, monkeypatch):
    # The level is never reached: 0 crossings, period omitted, error entry.
    flat = [(0.0, 0.0), (1e-9, 0.1), (2e-9, 0.0), (3e-9, 0.1)]
    report = _run_request(tmp_path, monkeypatch, flat, _DECL["crossings"])
    (corner,) = report["corners"]
    (measurement,) = corner["measurements"]
    assert measurement["status"] == "error"
    (diagnostic,) = corner["diagnostics"]
    assert diagnostic["message"].startswith("[crossings_insufficient_edges]")
    assert "needs at least 2 crossings (got 0)" in diagnostic["message"]


def test_graded_value_from_reports_the_omitted_reason_directly():
    """The selector evaluator (pure function) covers the defensive branches
    the corner path guards against."""
    value, reason = graded_value_from(
        "m", {"period": {"omitted": "needs at least 3 crossings (got 2)"}}, "frequency"
    )
    assert value is None
    assert reason is not None
    assert reason.startswith("[crossings_insufficient_edges]")
    assert "needs at least 3 crossings (got 2)" in reason
    value, reason = graded_value_from("m", None, "period.mean")
    assert value is None
    assert reason is not None
    assert reason.startswith("[crossings_insufficient_edges]")


# --------------------------------------------------------------------------- #
# Batch path: the declaration travels verbatim, the value comes back
# --------------------------------------------------------------------------- #


def _value_from_entry() -> dict:
    """What the job instance's own `klt sim` reports for `_DECL`: the period
    as `value`, the reduction it read beside it."""
    return {
        "name": "t_ro1",
        "value": 2e-09,
        "unit": None,
        "status": "pass",
        "margin": None,
        "crossings": {
            "times": [5e-10, 2.5e-09],
            "edges": ["rise", "rise"],
            "truncated": False,
            "reductions": {
                "period": {
                    "n": 1,
                    "mean": 2e-09,
                    "std": 0.0,
                    "min": 2e-09,
                    "max": 2e-09,
                }
            },
        },
    }


def test_batch_run_carries_the_value_from_request_and_report(tmp_path, monkeypatch):
    """Issue #6913 through the batch fleet: the declaration (including
    `value_from`) reaches the worker verbatim -- it regenerates the deck and
    must compute the reduction there -- and the collected report carries the
    graded period, not a count."""
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    document = json.loads(_sim_report(corner_ids=("tt_1.800_27",)))
    for corner in document["corners"]:
        corner["measurements"] = [_value_from_entry()]
    runner.outputs = {"report.json": json.dumps(document)}
    uploaded: dict[str, str] = {}

    def _capture(argv, timeout_s):
        captured = uploaded_request_of(argv)
        if captured is not None:
            uploaded["request"] = json.dumps(captured[0])
        return runner(argv, timeout_s)

    _install_fake_batch_transport(monkeypatch, runner=runner)
    monkeypatch.setattr(sb, "_run_subprocess", _capture)

    _write_batch_body(tmp_path)
    request = _batch_request(
        tmp_path, measurements=[_DECL], options={"keep_artifacts": False}
    )
    report = sim.run_sim(str(_write_batch_request(tmp_path, request)))

    # The declaration reaches the worker verbatim (it regenerates the deck).
    assert json.loads(uploaded["request"])["measurements"] == [_DECL]
    (corner,) = report["corners"]
    (measurement,) = corner["measurements"]
    assert measurement["value"] == pytest.approx(2e-9)
    assert measurement["crossings"]["reductions"]["period"]["mean"] == (
        pytest.approx(2e-9)
    )
