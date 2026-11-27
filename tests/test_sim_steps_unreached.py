"""Tests for `on_unreached` on step-scoped SPICE measurements (issue #7072).

The step-scope twin of `tests/test_sim.py`'s issue #3108 block: a
step-local `.meas` card whose target the step's *completed* analysis never
reaches reports `not_reached` when the request opts in, on failure evidence
scoped to that step's own log region (after its `klt_step_end` marker,
before the next step's `klt_step_begin`). Split into its own file for
`tests/test_sim_steps.py`'s file-size ratchet, following its conventions:
unit tests stub `subprocess.run`. The integration tests that run the real
`ngspice -b` (and skip without it) live in
`tests/test_sim_steps_unreached_ngspice.py`, which imports this module's
request/bench helpers.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import sim, sim_measurement_results, sim_steps
from klayout_tools.cli.sim_cmd import _exit_code_for

pytestmark = pytest.mark.usefixtures(
    "real_build_identity_git", "bare_name_ngspice_resolution"
)

#: The issue #7072 reproduction bench: a 0..1 V pulse on RC -- the 2 V
#: target of `missing` is unreachable by construction, the 0.8 V target of
#: `hit` crosses during the first rise.
_PULSE_BODY = "Vx x 0 PULSE(0 1 1n 100p 100p 2n 5n)\nRx x 0 1k\n"

_MISSING_CARD = ".meas tran missing when v(x)=2 rise=1 td=4n"
_HIT_CARD = ".meas tran hit when v(x)=0.8 rise=1"

#: The control command `klt` emits for `_MISSING_CARD` (same text, no dot).
_MISSING_CMD = "meas tran missing when v(x)=2 rise=1 td=4n"


def _write_body(tmp_path: Path, text: str = _PULSE_BODY) -> Path:
    path = tmp_path / "body.spice"
    path.write_text(text)
    return path


def _write_request(tmp_path: Path, request: dict) -> Path:
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    return path


def _request(steps: list[dict], derived: list[dict] | None = None) -> dict:
    request: dict = {"netlist": "body.spice", "analysis_steps": steps}
    if derived is not None:
        request["measurements"] = derived
    return request


def _edge_step(measurements: list[dict]) -> dict:
    return {
        "name": "edge",
        "analysis": {"kind": "tran", "args": "20p 20n"},
        "measurements": measurements,
    }


def _grade(request: dict, log_text: str):
    analysis, specs = sim_steps.resolve_analysis_steps(copy.deepcopy(request))
    return sim_measurement_results._corner_measurement_results(
        sim._parse_measurements(log_text),
        specs,
        analysis=analysis,
        log_text=log_text,
        is_xyce=False,
        save_mode="all",
    )


def _region_log(
    *,
    steps: int,
    failed: tuple[int, ...] = (),
    evidence: dict[int, str] | None = None,
    values: dict[str, float] | None = None,
) -> str:
    """A synthetic ngspice log whose per-step measurement output lands where
    the real control block prints it: after the step's own end marker,
    before the next step's begin marker."""
    lines = ["Circuit: * klt"]
    for index in range(steps):
        lines.append(f"klt_step_begin {index} unknown{index + 1}")
        end_plot = f"unknown{index + 1}" if index in failed else f"tran{index + 1}"
        lines.append(f"klt_step_end {index} {end_plot}")
        lines.append((evidence or {}).get(index, ""))
    for key, value in (values or {}).items():
        lines.append(f"{key} = {value:e}")
    return "\n".join(line for line in lines if line) + "\n"


def _confirmed_miss(local: str, cmd: str) -> str:
    """Both ngspice lines a step-scoped unreached card prints (ngspice 47)."""
    return f"Error: measure  {local}  when(WHEN) : out of interval\n {cmd} failed!\n"


def _stub_ngspice(monkeypatch, log_text: str) -> None:
    def fake_run(cmd, capture_output, text, timeout, cwd=None, env=None):
        Path(cmd[cmd.index("-o") + 1]).write_text(log_text)
        return fake_completed("** ngspice-46\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)


def _run(tmp_path: Path, monkeypatch, request: dict, log_text: str):
    _write_body(tmp_path)
    _stub_ngspice(monkeypatch, log_text)
    return sim.run_sim(str(_write_request(tmp_path, request)))


# --------------------------------------------------------------------------- #
# Grading (unit, stubbed log)
# --------------------------------------------------------------------------- #


def test_confirmed_miss_with_opt_in_grades_not_reached_with_step_provenance():
    request = _request(
        [
            _edge_step(
                [
                    {"name": "hit", "spice": _HIT_CARD, "unit": "s"},
                    {
                        "name": "missing",
                        "spice": _MISSING_CARD,
                        "on_unreached": "null",
                        "unit": "s",
                    },
                ]
            )
        ]
    )
    log = _region_log(
        steps=1,
        evidence={0: _confirmed_miss("missing", _MISSING_CMD)},
        values={"edge__hit": 1.08e-09},
    )
    results, diagnostics, extra = _grade(request, log)
    by_name = {r["name"]: r for r in results}
    assert by_name["edge.missing"] == {
        "name": "edge.missing",
        "value": None,
        "unit": "s",
        "status": "not_reached",
        "margin": None,
        "step": "edge",
    }
    assert by_name["edge.hit"]["status"] == "pass"
    assert by_name["edge.hit"]["value"] == pytest.approx(1.08e-09)
    assert ("info", "measurement_not_reached") in {
        (d["severity"], d["code"]) for d in diagnostics
    }
    assert not any(d["code"] == "measurement" for d in diagnostics)
    assert extra["steps"] == [{"name": "edge", "kind": "tran", "status": "ok"}]


def test_report_grades_pass_partial_with_skipped_bound_and_count(tmp_path, monkeypatch):
    request = _request(
        [
            _edge_step(
                [
                    {
                        "name": "hit",
                        "spice": _HIT_CARD,
                        "unit": "s",
                        "limits": {"max": 1e-08},
                    },
                    {
                        "name": "missing",
                        "spice": _MISSING_CARD,
                        "on_unreached": "null",
                        "unit": "s",
                        "limits": {"max": 1e-08},
                    },
                ]
            )
        ]
    )
    report = _run(
        tmp_path,
        monkeypatch,
        request,
        _region_log(
            steps=1,
            evidence={0: _confirmed_miss("missing", _MISSING_CMD)},
            values={"edge__hit": 1.08e-09},
        ),
    )
    corner = report["corners"][0]
    assert corner["status"] == "pass"
    assert report["not_reached"] == 1
    assert report["errored"] == 0
    assert [item["reason"] for item in report["coverage"]["skipped"]] == ["not_reached"]
    assert report["status"] == "pass_partial"
    assert _exit_code_for(report, deck_mode=False) == 0


def test_without_the_opt_in_the_miss_keeps_today_error(tmp_path, monkeypatch):
    request = _request(
        [
            _edge_step(
                [
                    {
                        "name": "missing",
                        "spice": _MISSING_CARD,
                        "unit": "s",
                        "limits": {"max": 1e-08},
                    }
                ]
            )
        ]
    )
    report = _run(
        tmp_path,
        monkeypatch,
        request,
        _region_log(steps=1, evidence={0: _confirmed_miss("missing", _MISSING_CMD)}),
    )
    corner = report["corners"][0]
    assert corner["measurements"][0]["status"] == "error"
    assert corner["status"] == "error"
    assert report["status"] == "error"
    assert "not_reached" not in report
    assert _exit_code_for(report, deck_mode=False) == 4
    assert any(
        d["message"].startswith("measurement 'edge.missing' produced no value")
        for d in corner["diagnostics"]
    )


def test_evidence_is_scoped_to_the_step_that_printed_it():
    card = ".meas tran missing when v(x)=2 rise=1 td=4n"
    cmd = "meas tran missing when v(x)=2 rise=1 td=4n"
    measurements = [{"name": "missing", "spice": card, "on_unreached": "null"}]
    request = _request(
        [
            {
                "name": "a",
                "analysis": {"kind": "tran", "args": "20p 20n"},
                "measurements": measurements,
            },
            {
                "name": "b",
                "analysis": {"kind": "tran", "args": "20p 20n"},
                "measurements": copy.deepcopy(measurements),
            },
        ]
    )
    log = _region_log(
        steps=2,
        evidence={
            0: _confirmed_miss("missing", cmd),
            1: (
                "Error: measure  missing  when(WHEN) : Expressions like "
                f"0.5*1.62 are not supported.\n {cmd} failed!\n"
            ),
        },
    )
    results, diagnostics, _ = _grade(request, log)
    by_name = {r["name"]: r for r in results}
    assert by_name["a.missing"]["status"] == "not_reached"
    assert by_name["b.missing"]["status"] == "error"
    assert [d["code"] for d in diagnostics].count("measurement_not_reached") == 1


def test_evidence_from_a_later_step_never_confirms_an_earlier_one():
    cmd = "meas tran missing when v(x)=2 rise=1 td=4n"
    measurements = [{"name": "missing", "spice": _MISSING_CARD, "on_unreached": "null"}]
    request = _request(
        [
            {
                "name": "a",
                "analysis": {"kind": "tran", "args": "20p 20n"},
                "measurements": measurements,
            },
            {
                "name": "b",
                "analysis": {"kind": "tran", "args": "20p 20n"},
                "measurements": copy.deepcopy(measurements),
            },
        ]
    )
    # Only step b's region carries the evidence: a whole-log search by the
    # shared local name would confirm both; the regions must not.
    log = _region_log(steps=2, evidence={1: _confirmed_miss("missing", cmd)})
    results, diagnostics, _ = _grade(request, log)
    by_name = {r["name"]: r for r in results}
    assert by_name["a.missing"]["status"] == "error"
    assert by_name["b.missing"]["status"] == "not_reached"


@pytest.mark.parametrize(
    "evidence",
    [
        pytest.param(
            "Error: measure  missing  when(WHEN) : out of interval\n",
            id="reason-without-failed-echo",
        ),
        pytest.param(
            f" {_MISSING_CMD} failed!\n",
            id="failed-echo-without-reason",
        ),
    ],
)
def test_incomplete_evidence_pair_stays_an_error(evidence):
    request = _request(
        [
            _edge_step(
                [{"name": "missing", "spice": _MISSING_CARD, "on_unreached": "null"}]
            )
        ]
    )
    results, diagnostics, _ = _grade(
        request, _region_log(steps=1, evidence={0: evidence})
    )
    assert results[0]["status"] == "error"
    assert [d["code"] for d in diagnostics] == ["measurement"]


def test_failed_step_stays_step_failed_even_with_evidence():
    request = _request(
        [
            _edge_step(
                [{"name": "missing", "spice": _MISSING_CARD, "on_unreached": "null"}]
            )
        ]
    )
    log = _region_log(
        steps=1, failed=(0,), evidence={0: _confirmed_miss("missing", _MISSING_CMD)}
    )
    results, diagnostics, extra = _grade(request, log)
    assert results[0]["status"] == "error"
    assert [d["code"] for d in diagnostics] == ["step_failed"]
    assert extra["steps"] == [{"name": "edge", "kind": "tran", "status": "error"}]


def test_solver_error_diagnostic_blocks_the_classification(tmp_path, monkeypatch):
    log = (
        "Error: Timestep too small; run aborted\n"
        "doAnalyses: iteration limit reached\n"
        + _region_log(steps=1, evidence={0: _confirmed_miss("missing", _MISSING_CMD)})
    )
    request = _request(
        [
            _edge_step(
                [{"name": "missing", "spice": _MISSING_CARD, "on_unreached": "null"}]
            )
        ]
    )
    report = _run(tmp_path, monkeypatch, request, log)
    corner = report["corners"][0]
    assert any(d["severity"] == "error" for d in corner["diagnostics"])
    assert corner["measurements"][0]["status"] == "error"
    assert corner["status"] == "error"


def test_successful_crossing_with_the_opt_in_is_graded_normally(tmp_path, monkeypatch):
    request = _request(
        [
            _edge_step(
                [
                    {
                        "name": "hit",
                        "spice": _HIT_CARD,
                        "on_unreached": "null",
                        "unit": "s",
                        "limits": {"max": 1e-08},
                    }
                ]
            )
        ]
    )
    report = _run(
        tmp_path,
        monkeypatch,
        request,
        _region_log(steps=1, values={"edge__hit": 1.08e-09}),
    )
    corner = report["corners"][0]
    assert corner["measurements"][0]["status"] == "pass"
    assert corner["status"] == "pass"
    assert "not_reached" in report and report["not_reached"] == 0


def test_derived_over_unreached_is_derived_input_unavailable(tmp_path, monkeypatch):
    request = _request(
        [
            _edge_step(
                [{"name": "missing", "spice": _MISSING_CARD, "on_unreached": "null"}]
            )
        ],
        derived=[{"name": "drop", "expr": "abs(edge.missing)", "unit": "V"}],
    )
    report = _run(
        tmp_path,
        monkeypatch,
        request,
        _region_log(steps=1, evidence={0: _confirmed_miss("missing", _MISSING_CMD)}),
    )
    corner = report["corners"][0]
    by_name = {m["name"]: m for m in corner["measurements"]}
    assert by_name["edge.missing"]["status"] == "not_reached"
    assert by_name["drop"]["status"] == "error"
    assert corner["status"] == "error"
    assert any(d["code"] == "derived_input_unavailable" for d in corner["diagnostics"])


def test_all_requested_bounds_unreached_reports_not_checked(tmp_path, monkeypatch):
    request = _request(
        [
            _edge_step(
                [
                    {
                        "name": "missing",
                        "spice": _MISSING_CARD,
                        "on_unreached": "null",
                        "unit": "s",
                        "limits": {"max": 1e-08},
                    }
                ]
            )
        ]
    )
    report = _run(
        tmp_path,
        monkeypatch,
        request,
        _region_log(steps=1, evidence={0: _confirmed_miss("missing", _MISSING_CMD)}),
    )
    assert report["corners"][0]["status"] == "pass"
    assert report["status"] == "not_checked"
    assert _exit_code_for(report, deck_mode=False) == 4


def test_mixed_corners_roll_up_like_the_scalar_path(tmp_path, monkeypatch):
    monkeypatch.setenv(sim.MAX_WORKERS_ENV, "1")
    request = _request(
        [
            _edge_step(
                [
                    {
                        "name": "missing",
                        "spice": _MISSING_CARD,
                        "on_unreached": "null",
                        "unit": "s",
                        "limits": {"max": 1e-08},
                    }
                ]
            )
        ]
    )
    request["corners"] = {"temperature_c": [-40, 27]}
    reached = _region_log(steps=1, values={"edge__missing": 5e-09})
    unreached = _region_log(
        steps=1, evidence={0: _confirmed_miss("missing", _MISSING_CMD)}
    )
    logs = [reached, unreached]
    _write_body(tmp_path)

    def fake_run(cmd, capture_output, text, timeout, cwd=None, env=None):
        Path(cmd[cmd.index("-o") + 1]).write_text(logs.pop(0))
        return fake_completed("** ngspice-46\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)
    report = sim.run_sim(str(_write_request(tmp_path, request)))
    by_corner = {
        c["corner_id"]: c["measurements"][0]["status"] for c in report["corners"]
    }
    assert set(by_corner.values()) == {"pass", "not_reached"}
    assert report["not_reached"] == 1
    rollup = {m["name"]: m["status"] for m in report["measurements"]}
    assert rollup["edge.missing"] == "pass"
    assert report["status"] == "pass_partial"
