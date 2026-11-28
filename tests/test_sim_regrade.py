"""Tests for ``klt sim --regrade`` (issue #6778, ``sim_regrade.py``).

A re-run of only the check-failing units with the tightened settings the
request declares in ``options.tightened_rerun``: selection by corner id
against the first pass's committed report, dispatch through the ordinary
``run_sim`` machinery (a batch baseline dispatches a one-unit subset on
batch), and a companion record holding **both** attempts' observations with
the grade taken from the tightened run -- a surviving failure stays a
failure, a timeout or unusable re-run is unresolved, never a pass.

Unit tests stub ``subprocess.run`` (no real ngspice); the batch test uses
the recorded-argv fake-runner discipline of ``tests/test_sim_batch.py`` (no
aws CLI, no S3, no network).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from helpers.sim_regrade_bench import (
    TIGHTENED_ARGS,
    _failing_id,
    _first_pass,
    _request,
    _stub_engine,
)
from klayout_tools import sim, sim_regrade, sim_regrade_run
from klayout_tools.sim_errors import SimError

pytestmark = pytest.mark.usefixtures(
    "real_build_identity_git", "bare_name_ngspice_resolution"
)

# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Declaration and argument-splice validation
# --------------------------------------------------------------------------- #


def test_validate_tightened_rerun_does_not_load_sim_runner():
    code = """
import sys
from klayout_tools.sim_regrade import validate_tightened_rerun

assert validate_tightened_rerun(None) is None
assert "klayout_tools.sim" not in sys.modules
assert validate_tightened_rerun({"reltol": 1e-4}) == {
    "tmax": None, "ngspice_options": {"reltol": 1e-4}
}
assert "klayout_tools.sim" not in sys.modules
"""
    source = Path(__file__).resolve().parents[1] / "src"
    subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONPATH": str(source)},
        check=True,
    )


@pytest.mark.parametrize(
    "value, match",
    [
        ("yes", "non-empty object"),
        ({}, "non-empty object"),
        ({"bogus": 1}, "unknown key"),
        ({"tmax": 0}, "tmax"),
        ({"tmax": -1}, "tmax"),
        ({"tmax": True}, "tmax"),
        ({"tmax": "fast"}, "tmax"),
    ],
)
def test_declaration_validation(value, match):
    with pytest.raises(SimError, match=match):
        sim_regrade.validate_tightened_rerun(value)


def test_declaration_requires_tran_for_tmax():
    with pytest.raises(SimError, match="requires a scalar analysis of kind"):
        sim_regrade.validate_tightened_rerun(
            {"tmax": "25p"}, analysis={"kind": "ac", "args": "1 1e9"}
        )
    with pytest.raises(SimError, match="requires a scalar analysis of kind"):
        sim_regrade.validate_tightened_rerun(
            {"tmax": "25p"}, analysis={"kind": "tran"}, steps=True
        )
    assert sim_regrade.validate_tightened_rerun(
        {"tmax": "25p"}, analysis={"kind": "tran", "args": "1n 1u"}
    ) == {"tmax": "25p", "ngspice_options": {}}


def test_declaration_refuses_solver_options_for_xyce():
    with pytest.raises(SimError, match="xyce"):
        sim_regrade.validate_tightened_rerun(
            {"reltol": 1e-4}, analysis={"kind": "tran"}, engine="xyce"
        )
    assert sim_regrade.validate_tightened_rerun(
        {"tmax": 2.5e-11}, analysis={"kind": "tran"}, engine="xyce"
    ) == {"tmax": "2.5e-11", "ngspice_options": {}}


def test_declaration_normalises_a_number_to_a_deck_token():
    assert sim_regrade.validate_tightened_rerun(
        {"tmax": 2.5e-11}, analysis={"kind": "tran"}
    ) == {"tmax": "2.5e-11", "ngspice_options": {}}


def test_tighten_tran_args_preserves_tstart_and_flags():
    assert sim_regrade.tighten_tran_args("1n 1u", "25p") == "1n 1u 0 25p"
    assert sim_regrade.tighten_tran_args("1n 1u 0", "25p") == "1n 1u 0 25p"
    assert sim_regrade.tighten_tran_args("1n 1u 5n", "25p") == "1n 1u 5n 25p"
    # An existing tmax is replaced, never stacked.
    assert sim_regrade.tighten_tran_args("1n 1u 0 50p", "25p") == "1n 1u 0 25p"
    # Flags (uic) survive at the end.
    assert sim_regrade.tighten_tran_args("1n 1u uic", "25p") == "1n 1u 0 25p uic"
    with pytest.raises(SimError, match="cannot apply the tightened tmax"):
        sim_regrade.tighten_tran_args("1u", "25p")


def test_first_pass_validates_the_declaration_before_dispatch(tmp_path, monkeypatch):
    """A malformed declaration is refused on the *first* pass, before any
    engine runs -- not discovered after a campaign."""
    launched = []
    _stub_engine(monkeypatch, decks=launched)
    request = _request(tmp_path, options={"tightened_rerun": {"tmaz": "25p"}})
    with pytest.raises(SimError, match="unknown key"):
        sim.run_sim(str(request), backend="local")
    assert launched == []


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #


def test_regrade_requires_a_declared_policy(tmp_path, monkeypatch):
    _stub_engine(monkeypatch)
    plain = _request(tmp_path)
    document = json.loads(plain.read_text())
    document["options"].pop("tightened_rerun")
    plain.write_text(json.dumps(document))
    report = _first_pass(tmp_path, plain)
    with pytest.raises(SimError, match="declares no options.tightened_rerun"):
        sim_regrade_run.run_regrade(
            str(plain), str(report), "check", [_failing_id(report)]
        )


def test_unknown_selection_id_is_refused_before_dispatch(tmp_path, monkeypatch):
    launched = []
    _stub_engine(monkeypatch, decks=launched)
    request = _request(tmp_path)
    report = _first_pass(tmp_path, request)
    launched.clear()  # only the re-run attempts from here on
    with pytest.raises(SimError, match="not in the source report: nope"):
        sim_regrade_run.run_regrade(str(request), str(report), "check", ["nope"])
    # An id the *report* has but this request no longer expands to (the
    # request's matrix drifted since the first pass) is refused just as
    # firmly -- before any dispatch.
    document = json.loads(report.read_text())
    document["corners"].append(
        {"corner_id": "default/1.500V/27C", "status": "fail", "measurements": []}
    )
    report.write_text(json.dumps(document))
    with pytest.raises(SimError, match="absent from this request's expansion"):
        sim_regrade_run.run_regrade(
            str(request), str(report), "check", ["default/1.500V/27C"]
        )
    assert launched == []


@pytest.mark.parametrize("indices", [[0], [0, 1, 1], [1, 0]])
def test_worker_refuses_inconsistent_explicit_selection(tmp_path, indices):
    path = _request(tmp_path)
    request = json.loads(path.read_text())
    points = sim.expand_corners(request.pop("corners"), [])
    request["_regrade"] = {"select_corner_ids": [p.corner_id for p in points]}
    request["_explicit_points"] = sim._corner_points_to_wire(
        [points[i] for i in indices]
    )
    path.write_text(json.dumps(request))
    with pytest.raises(SimError, match="corner IDs must match select_corner_ids"):
        sim.run_sim(str(path), backend="local", dry_run=True)


def test_split_jobs_keep_their_share_of_the_selected_ids(tmp_path):
    from klayout_tools import sim_remote

    path = _request(tmp_path, corners={"temperature_c": [0, 27, 80]})
    request = json.loads(path.read_text())
    points = sim.expand_corners(request["corners"], [])
    selected = [points[2], points[0]]
    ids = [p.corner_id for p in selected]
    request["_regrade"] = {"select_corner_ids": ids, "tmax": "25p"}
    for shard in sim_remote._shard_corner_points(selected, 2):
        worker = sim_remote._build_remote_request(
            request,
            explicit_points=shard,
            timeout_s=60,
            keep_artifacts=False,
            want_waveforms=False,
        )
        (tmp_path / worker["netlist"]).write_bytes(
            (tmp_path / "body.spice").read_bytes()
        )
        path.write_text(json.dumps(worker))
        report = sim.run_sim(str(path), dry_run=True)
        assert report["units"] == [
            {"corner_id": shard[0].corner_id, "analysis_args": TIGHTENED_ARGS}
        ]
    assert request["_regrade"]["select_corner_ids"] == ids


def test_duplicate_selection_is_refused(tmp_path, monkeypatch):
    _stub_engine(monkeypatch)
    request = _request(tmp_path)
    report = _first_pass(tmp_path, request)
    cid = _failing_id(report)
    with pytest.raises(SimError, match="repeats corner id"):
        sim_regrade_run.run_regrade(str(request), str(report), "check", [cid, cid])


def test_empty_selection_submits_zero_jobs(tmp_path, monkeypatch):
    launched = []
    _stub_engine(monkeypatch, decks=launched)
    request = _request(tmp_path)
    report = _first_pass(tmp_path, request)
    launched.clear()  # only the re-run attempts from here on
    record = sim_regrade_run.run_regrade(str(request), str(report), "check", [])
    assert record["status"] == "not_checked"
    assert record["units"] == []
    assert launched == []
    written = json.loads(
        (tmp_path / ".klt" / "sim" / "regrade" / "check" / "regrade.json").read_text()
    )
    assert written["status"] == "not_checked"


# --------------------------------------------------------------------------- #
# The companion record: recovery, a surviving failure, unresolved re-runs
# --------------------------------------------------------------------------- #


def test_recovered_dip_grades_from_the_tightened_run(tmp_path, monkeypatch):
    decks: list[str] = []
    runs: list[tuple[float, bool]] = []
    _stub_engine(monkeypatch, decks=decks, runs=runs)
    request = _request(tmp_path)
    report = _first_pass(tmp_path, request)
    cid = _failing_id(report)
    runs.clear()  # only the re-run attempts from here on

    record = sim_regrade_run.run_regrade(
        str(request), str(report), "vco_curve_dip", [cid]
    )

    assert record["status"] == "pass"
    assert (record["passed"], record["failed"], record["unresolved"]) == (1, 0, 0)
    (unit,) = record["units"]
    assert unit["corner_id"] == cid
    assert unit["check"] == "vco_curve_dip"
    assert unit["grade"] == "pass"
    # Both attempts' observations, first never silently replaced.
    assert unit["first"]["status"] == "fail"
    assert unit["first"]["measurements"][0]["value"] == pytest.approx(0.9)
    assert unit["tightened"]["status"] == "pass"
    assert unit["tightened"]["measurements"][0]["value"] == pytest.approx(1.5)
    assert unit["analysis_args"] == TIGHTENED_ARGS
    # Exactly one tightened dispatch of exactly the selected unit.
    assert runs == [(1.62, True)]
    assert any(TIGHTENED_ARGS in d for d in decks)
    assert any(".options reltol=0.0001" in d for d in decks)
    # The record is committed beside the re-run's artifacts, and the first
    # pass's own artifacts survive untouched.
    record_path = (
        tmp_path / ".klt" / "sim" / "regrade" / "vco_curve_dip" / "regrade.json"
    )
    assert json.loads(record_path.read_text())["status"] == "pass"
    # Both attempts' artifacts survive side by side: the first pass's corner
    # dir was not overwritten by the re-run's (keep_artifacts is on).
    first_deck = tmp_path / ".klt" / "sim" / "default_1p620V_27C" / "corner.cir"
    rerun_deck = (
        tmp_path
        / ".klt"
        / "sim"
        / "regrade"
        / "vco_curve_dip"
        / "default_1p620V_27C"
        / "corner.cir"
    )
    assert first_deck.is_file() and "tran 1n 1u\n" in first_deck.read_text()
    assert rerun_deck.is_file() and TIGHTENED_ARGS in rerun_deck.read_text()
    assert unit["first"]["artifacts"]["deck"] == str(first_deck)
    assert unit["tightened"]["artifacts"]["deck"] == str(rerun_deck)
    # The re-run's environment states the selection it applied.
    assert record["environment"]["regrade"] == {
        "select_corner_ids": [cid],
        "tmax": "25p",
        "ngspice_options": {"reltol": 0.0001},
    }
    assert record["environment"]["ngspice_options"] == {"reltol": 0.0001}


def test_surviving_failure_stays_a_failure(tmp_path, monkeypatch):
    _stub_engine(monkeypatch, tightened_value=0.9)
    request = _request(tmp_path)
    report = _first_pass(tmp_path, request)
    record = sim_regrade_run.run_regrade(
        str(request), str(report), "vco_curve_dip", [_failing_id(report)]
    )
    assert record["status"] == "fail"
    (unit,) = record["units"]
    assert unit["grade"] == "fail"
    assert unit["first"]["measurements"][0]["value"] == pytest.approx(0.9)
    assert unit["tightened"]["measurements"][0]["value"] == pytest.approx(0.9)


def test_two_selected_units_dispatch_each_once(tmp_path, monkeypatch):
    runs: list[tuple[float, bool]] = []
    _stub_engine(monkeypatch, runs=runs, first_pass_dip=False)
    corners = {"supply_v": {"vdd": [1.8, 1.62, 1.98]}}
    request = _request(tmp_path, corners=corners)
    report = sim.run_sim(str(request), backend="local")
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report))
    ids = [c["corner_id"] for c in report["corners"][:2]]
    runs.clear()  # only the re-run attempts from here on

    record = sim_regrade_run.run_regrade(str(request), str(report_path), "c", ids)

    assert record["selected"] == 2
    assert [u["corner_id"] for u in record["units"]] == ids
    assert sorted(runs) == [(1.62, True), (1.8, True)]


def test_timed_out_tightened_run_is_unresolved(tmp_path, monkeypatch):
    _stub_engine(monkeypatch, tightened_error="timeout")
    request = _request(tmp_path)
    report = _first_pass(tmp_path, request)
    record = sim_regrade_run.run_regrade(
        str(request), str(report), "vco_curve_dip", [_failing_id(report)]
    )
    assert record["status"] == "unresolved"
    (unit,) = record["units"]
    assert unit["grade"] == "unresolved"
    assert "timeout" in unit["grade_reason"]
    # The first observation is still there.
    assert unit["first"]["measurements"][0]["value"] == pytest.approx(0.9)
    assert unit["tightened"]["status"] == "error"


def test_missing_tightened_measurement_is_unresolved(tmp_path, monkeypatch):
    _stub_engine(monkeypatch, tightened_error="Error: no convergence")
    request = _request(tmp_path)
    report = _first_pass(tmp_path, request)
    record = sim_regrade_run.run_regrade(
        str(request), str(report), "vco_curve_dip", [_failing_id(report)]
    )
    (unit,) = record["units"]
    assert unit["grade"] == "unresolved"
    assert "measurements without a usable value: vout" in unit["grade_reason"]


def test_netlist_drift_since_the_first_pass_is_refused(tmp_path, monkeypatch):
    _stub_engine(monkeypatch)
    request = _request(tmp_path)
    report = _first_pass(tmp_path, request)
    (tmp_path / "body.spice").write_text(
        "Vdd vdd 0 DC 1.8\nR1 vdd out 2k\nC1 out 0 1n\n"
    )
    with pytest.raises(SimError, match="netlist identity mismatch"):
        sim_regrade_run.run_regrade(
            str(request), str(report), "check", [_failing_id(report)]
        )


def test_dry_run_previews_the_selection_without_dispatch(tmp_path, monkeypatch):
    launched = []
    _stub_engine(monkeypatch, decks=launched)
    request = _request(tmp_path)
    report = _first_pass(tmp_path, request)
    launched.clear()  # only the re-run attempts from here on
    preview = sim_regrade_run.run_regrade(
        str(request), str(report), "check", [_failing_id(report)], dry_run=True
    )
    assert preview["dry_run"] is True
    assert [u["corner_id"] for u in preview["units"]] == [_failing_id(report)]
    assert launched == []


# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# A request without the policy keeps today's behaviour
# --------------------------------------------------------------------------- #


def test_request_without_the_policy_is_unchanged(tmp_path, monkeypatch):
    decks: list[str] = []
    _stub_engine(monkeypatch, decks=decks)
    request = _request(tmp_path)
    document = json.loads(request.read_text())
    document["options"].pop("tightened_rerun")
    request.write_text(json.dumps(document))
    report = sim.run_sim(str(request), backend="local")
    assert report["status"] == "fail"  # the dipped first pass, unchanged
    assert "tightened_rerun" not in report["environment"]
    assert "regrade" not in report["environment"]
    assert not any("25p" in d for d in decks)
    assert not any(".options reltol=0.0001" in d for d in decks)
