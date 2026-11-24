"""`klt eval` grades a `klt place-and-route` verdict (issue #6905).

`klt place-and-route` used to report `"status": "ok"` unconditionally; it now
reports `"status": "failed"` (and exits 1) when the routed result carries a
confirmed open or an unrouted net. `klt eval` is the one in-repo consumer that
scores a P&R report, so the new verdict has to reach it -- otherwise a
candidate with a physical open is scored `valid: true`.

Kept apart from `tests/test_eval.py` / `tests/test_digital_fleet.py`, which sit
at their `file-size-baseline.json` ceilings.
"""

from __future__ import annotations

import json

import pytest

import klayout_tools.eval as eval_module
from klayout_tools import digital_fleet as df
from klayout_tools.eval import _derive_status, run_eval

_OK = {"status": "ok", "stage_reached": "route", "worst_slack_ns": 0.4}
# The shape `klt place-and-route` emits for a confirmed routed open: a
# complete route-stage report whose verdict is `failed`.
_OPEN = {
    "status": "failed",
    "stage_reached": "route",
    "worst_slack_ns": 0.4,
    "congestion": {
        "completeness": {
            "unrouted_net_count": 0,
            "partially_routed_net_count": 1,
            "pin_connectivity": {
                "status": "open",
                "open_nets": [
                    {"net": "net331", "affected_pins": [{"instance": "u1", "pin": "I"}]}
                ],
            },
        }
    },
}
_UNROUTED = dict(_OPEN, congestion={"completeness": {"unrouted_net_count": 2}})


def _gate(**extra):
    return {"check": "place-and-route", "name": "place-and-route", **extra}


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        (_OK, ("pass", 0, None)),
        (_OPEN, ("fail", 1, None)),
        (_UNROUTED, ("fail", 1, None)),
    ],
)
def test_default_derivation_mirrors_the_verbs_own_exit(report, expected):
    # No threshold: pass iff `status == "ok"`, citing the exit `klt
    # place-and-route` itself returns (0 / 1) -- as `_status_drc`/`_status_lvs`.
    assert _derive_status("place-and-route", report, _gate()) == expected


def test_explicit_threshold_still_overrides_the_default_derivation():
    # Documented: a `threshold` replaces the check's own derivation. A
    # `status` threshold is how a descriptor spells the same verdict.
    status_gate = _gate(threshold={"metric": "status", "equals": "ok"})
    assert _derive_status("place-and-route", _OPEN, status_gate)[0] == "fail"
    assert _derive_status("place-and-route", _OK, status_gate)[0] == "pass"
    slack_gate = _gate(threshold={"metric": "worst_slack_ns", "min": 0})
    assert _derive_status("place-and-route", _OPEN, slack_gate)[0] == "pass"


def _run(tmp_path, monkeypatch, report, gates):
    calls = []

    def _fake(request_path):
        calls.append(request_path)
        return dict(report)

    monkeypatch.setattr(eval_module, "run_place_and_route", _fake)
    (tmp_path / "pnr_request.json").write_text("{}")
    args = {"request": "pnr_request.json"}
    descriptor = {
        "gates": [dict(gate, args=args) for gate in gates],
        "objective": {
            "check": "place-and-route",
            "metric": "worst_slack_ns",
            "args": args,
        },
    }
    path = tmp_path / "descriptor.json"
    path.write_text(json.dumps(descriptor))
    return run_eval(str(path)), calls


def test_gate_without_threshold_no_longer_raises_and_grades_status(
    tmp_path, monkeypatch
):
    ok, _ = _run(tmp_path, monkeypatch, _OK, [_gate()])
    assert ok["valid"] is True
    bad, _ = _run(tmp_path, monkeypatch, _OPEN, [_gate()])
    assert bad["valid"] is False
    (gate,) = bad["gates"]
    assert (gate["status"], gate["exit_code"]) == ("fail", 1)


def test_status_gate_plus_timing_gate_runs_the_flow_once(tmp_path, monkeypatch):
    # The documented recipe for keeping the verdict *and* a timing ceiling:
    # two gates over identical `args` share one cached invocation.
    timing = _gate(name="timing", threshold={"metric": "worst_slack_ns", "min": 0})
    report, calls = _run(tmp_path, monkeypatch, _OPEN, [_gate(), timing])
    assert report["valid"] is False
    assert [g["status"] for g in report["gates"]] == ["fail", "pass"]
    assert len(calls) == 1


def _fleet_descriptor(tmp_path):
    """The eval descriptor `digital_fleet` actually ships for a candidate,
    written out the way the remote job would see it."""
    rtl = tmp_path / "top.v"
    rtl.write_text("module top; endmodule\n")
    candidate = df.DigitalCandidate(
        candidate_id="cand-0",
        rtl_sources=(str(rtl),),
        hdl_toplevel="top",
        pdk={"cell_library": "sky130_fd_sc_hd"},
        floorplan={"method": "utilization", "utilization_pct": 40, "site": "unithd"},
        seed=1,
    )
    job = df.build_digital_job_description(candidate)
    for item in job.inputs:
        if item.content is not None:
            (tmp_path / item.remote_name).write_text(item.content)
    return str(tmp_path / "eval_descriptor.json")


@pytest.mark.parametrize(("report", "valid"), [(_OK, True), (_OPEN, False)])
def test_digital_fleet_candidate_gate_rejects_a_routed_open(
    tmp_path, monkeypatch, report, valid
):
    # The descriptor `build_digital_job_description` composes used to threshold
    # on `stage_reached` -- which only echoes `target_stage` -- so a confirmed
    # open scored `valid: true` and a fleet sweep ranked it.
    synth = {"status": "ok", "instance_count": 3, "area_um2": 1.0}
    monkeypatch.setattr(eval_module, "run_synthesize", lambda request_path: dict(synth))
    monkeypatch.setattr(
        eval_module,
        "run_place_and_route",
        lambda request_path: dict(report, die_area_um2=100.0),
    )
    result = run_eval(_fleet_descriptor(tmp_path))
    assert result["valid"] is valid
    assert {g["name"]: g["status"] for g in result["gates"]}["place-and-route"] == (
        "pass" if valid else "fail"
    )
