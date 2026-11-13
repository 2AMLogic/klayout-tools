"""`klt characterize --split-grid` and the timeout-free resume key (#5410).

The simulator is stubbed: `sim.run_sim` answers each leg's measurement names
with deterministic values, so split and unsplit runs can be compared exactly.
"""

from __future__ import annotations

import json
import os
import zlib

import pytest

from klayout_tools import characterize, characterize_split, sim
from test_characterize import _inverter_request, _plan_for

GRID = {
    "input_transition_ns": [0.02, 0.05, 0.08, 0.12, 0.2],
    "output_load_pf": [0.005, 0.03],
}


def _stub_run_sim(calls, fail_on=None):
    def fake(path, **kwargs):
        calls.append((path, kwargs))
        if fail_on and f"{os.sep}{fail_on}{os.sep}" in path:
            raise sim.SimError("boom")
        with open(path, encoding="utf-8") as handle:
            request = json.load(handle)
        measurements = [
            {
                "name": m["name"],
                "value": 1e-12 * (1 + zlib.crc32(m["name"].encode()) % 97),
            }
            for m in request["measurements"]
        ]
        return {
            "corners": [
                {
                    "corner_id": "c0",
                    "status": "pass",
                    "runtime_s": 2.0,
                    "measurements": measurements,
                }
            ],
            "environment": {},
        }

    return fake


def _run(tmp_path, monkeypatch, split_grid, fail_on=None, resume=False, **overrides):
    calls: list = []
    monkeypatch.setattr(sim, "run_sim", _stub_run_sim(calls, fail_on))
    request = _inverter_request(tmp_path, grid=GRID, **overrides)
    plan, r = _plan_for(tmp_path, request)
    options = {**r["options"], "split_grid": split_grid}
    work = tmp_path / f"work-{split_grid}"
    result = characterize._characterize_cell(
        cell=r["cell"],
        arcs=r["arcs"],
        corner=r["corner"],
        grid=r["grid"],
        thresholds=r["thresholds"],
        options=options,
        request=request,
        request_dir=str(tmp_path),
        work_dir=str(work),
        backend="local",
        keep_artifacts=None,
        resume=resume,
    )
    return result, calls, work


def _outputs(result):
    return {
        k: result[k] for k in ("tables", "power_tables", "leakage", "pin_capacitance")
    }


def test_split_rows_chunking():
    assert characterize_split.split_rows(5, 2) == [(0, 1, 2), (3, 4)]
    assert characterize_split.split_rows(5, 1) == [(0, 1, 2, 3, 4)]
    assert len(characterize_split.split_rows(2, 9)) == 2
    assert characterize_split.split_rows(7, 3) == [(0, 1, 2), (3, 4), (5, 6)]


def test_split_run_equals_unsplit_run(tmp_path, monkeypatch):
    whole, whole_calls, _ = _run(tmp_path, monkeypatch, 1)
    for n in (2, 3, 99):
        split, calls, work = _run(tmp_path, monkeypatch, n)
        assert _outputs(split) == _outputs(whole)
        assert len(calls) == min(n, 5)
        assert (work / "grid-1" / "testbench.spice").exists()
        assert split["sim_report"]["corners"][0]["runtime_s"] == 2.0 * len(calls)
    assert len(whole_calls) == 1


def test_default_decks_are_byte_identical(tmp_path, monkeypatch):
    result, _, work = _run(tmp_path, monkeypatch, 1)
    plan, _r = _plan_for(tmp_path, _inverter_request(tmp_path, grid=GRID))
    assert (work / "testbench.spice").read_text() == plan["netlist_text"]
    assert not (work / "grid-1").exists()


@pytest.mark.parametrize("bad", [0, -1, 1.5, "2", True])
def test_bad_values_are_refused(bad):
    with pytest.raises(characterize.CharacterizeError, match="split_grid"):
        characterize_split.resolve_split_grid(
            {"split_grid": bad}, None, characterize.CharacterizeError
        )
    with pytest.raises(characterize.CharacterizeError, match="--split-grid"):
        characterize_split.resolve_split_grid({}, bad, characterize.CharacterizeError)


def test_cli_wins_over_request_option():
    resolve = characterize_split.resolve_split_grid
    err = characterize.CharacterizeError
    assert resolve({"split_grid": 4}, 2, err) == 2
    assert resolve({"split_grid": 4}, None, err) == 4
    assert resolve({}, None, err) == 1


def test_chunk_failure_names_the_chunk(tmp_path, monkeypatch):
    with pytest.raises(characterize.CharacterizeError, match=r"grid chunk 2/2"):
        _run(tmp_path, monkeypatch, 2, fail_on="grid-2")


def test_resume_reruns_only_the_failed_chunk(tmp_path, monkeypatch):
    with pytest.raises(characterize.CharacterizeError):
        _run(tmp_path, monkeypatch, 2, fail_on="grid-2", resume=True)
    _, calls, _ = _run(tmp_path, monkeypatch, 2, resume=True)
    assert len(calls) == 1 and f"{os.sep}grid-2{os.sep}" in calls[0][0]


def test_timeout_change_does_not_resubmit_collected_legs(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, 1, resume=True)
    request = _inverter_request(tmp_path, grid=GRID)
    plan, r = _plan_for(tmp_path, request)
    options = {**r["options"], "split_grid": 1, "timeout_s": 3000.0}
    calls: list = []
    monkeypatch.setattr(sim, "run_sim", _stub_run_sim(calls))
    characterize._characterize_cell(
        cell=r["cell"],
        arcs=r["arcs"],
        corner=r["corner"],
        grid=r["grid"],
        thresholds=r["thresholds"],
        options=options,
        request=request,
        request_dir=str(tmp_path),
        work_dir=str(tmp_path / "work-1"),
        backend="local",
        keep_artifacts=None,
        resume=True,
    )
    assert calls == []


def test_checkpoint_fingerprint_ignores_timeout(tmp_path):
    netlist = tmp_path / "n.spice"
    netlist.write_text("* n\n")
    kwargs = dict(
        netlist_path=str(netlist),
        models_lib=None,
        analysis={"kind": "tran"},
        measurements_spec=[],
        corner_points=[],
        engine="ngspice",
    )
    assert sim._checkpoint_fingerprint(
        timeout_s=900.0, **kwargs
    ) == sim._checkpoint_fingerprint(timeout_s=3000.0, **kwargs)
