"""`klt characterize --wait-for-capacity` (issue #5348): forwarded to every
`run_sim` call, and refused up front when invalid."""

from __future__ import annotations

import argparse

import pytest

from klayout_tools import characterize, characterize_split, sim
from klayout_tools.cli import characterize_cmd
from test_characterize import _batch_request, _inverter_request, _write_request


def _record_run_sim(monkeypatch):
    """Record each `run_sim` call's kwargs; fail the sim so no simulator is needed."""
    seen: list[dict] = []

    def _fake_run_sim(*_args, **kwargs):
        seen.append(kwargs)
        raise sim.SimError("stop here")

    monkeypatch.setattr(characterize.sim, "run_sim", _fake_run_sim)
    return seen


@pytest.mark.parametrize("value", [600.0, 0.0])
def test_wait_for_capacity_reaches_run_sim(tmp_path, monkeypatch, value):
    seen = _record_run_sim(monkeypatch)
    path = _write_request(tmp_path, _inverter_request(tmp_path))
    with pytest.raises(characterize.CharacterizeError, match="simulation failed"):
        characterize.run_characterize(str(path), backend="batch", capacity_wait_s=value)
    assert [kw["capacity_wait_s"] for kw in seen] == [value]


def test_omitted_wait_for_capacity_is_not_passed(tmp_path, monkeypatch):
    seen = _record_run_sim(monkeypatch)
    path = _write_request(tmp_path, _inverter_request(tmp_path))
    with pytest.raises(characterize.CharacterizeError):
        characterize.run_characterize(str(path))
    assert len(seen) == 1 and "capacity_wait_s" not in seen[0]


def test_cli_forwards_wait_for_capacity(monkeypatch):
    captured = {}

    def _fake(request, **kwargs):
        captured.update(kwargs)
        raise characterize.CharacterizeError("stop")

    monkeypatch.setattr(characterize_cmd, "run_characterize", _fake)
    args = argparse.Namespace(
        request="r.json",
        outdir=None,
        lib=None,
        backend="batch",
        keep_artifacts=False,
        format="json",
        wait_for_capacity=600.0,
    )
    characterize_cmd.run(args)
    assert captured["capacity_wait_s"] == 600.0


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"capacity_wait_s": -1}, "capacity_wait_s"),
        ({"capacity_wait_s": True}, "capacity_wait_s"),
        ({"capacity_wait_s": 5, "backend": "local"}, "requires backend 'batch'"),
    ],
)
def test_bad_wait_for_capacity_refused_before_io(tmp_path, monkeypatch, kwargs, match):
    seen = _record_run_sim(monkeypatch)
    path = _write_request(tmp_path, _batch_request(tmp_path))
    with pytest.raises(characterize.CharacterizeError, match=match):
        characterize.run_characterize(str(path), outdir=str(tmp_path / "o"), **kwargs)
    assert not seen and not (tmp_path / "o").exists()


def _record_sim_requests(monkeypatch):
    """Record the generated sim request of every `run_sim_leg` call."""
    seen: list[dict] = []

    def _fake_leg(*, sim_request, **_kwargs):
        seen.append(sim_request)
        raise sim.SimError("stop here")

    monkeypatch.setattr(characterize_split, "run_sim_leg", _fake_leg)
    return seen


def test_request_batch_block_is_forwarded_to_sim_request(tmp_path, monkeypatch):
    seen = _record_sim_requests(monkeypatch)
    batch = {"capacity_wait_s": 600}
    path = _write_request(tmp_path, _inverter_request(tmp_path, batch=batch))
    with pytest.raises(characterize.CharacterizeError, match="simulation failed"):
        characterize.run_characterize(str(path))
    assert seen[0]["batch"] == batch


def test_request_batch_block_reaches_constraint_deck(tmp_path):
    request = _inverter_request(tmp_path, batch={"capacity_wait_s": 30})
    kwargs = dict(
        request=request,
        request_dir=str(tmp_path),
        testbench_path="tb.spice",
        corner={"supply_v": 1.8, "temperature_c": 25, "process": None},
        plan={"window": {"step_ns": 0.01, "stop_ns": 1.0}, "measurements": []},
        options={"timeout_s": 10, "keep_artifacts": False, "osdi_preload": []},
        keep_artifacts=None,
    )
    assert characterize._build_sim_request(**kwargs)["batch"] == {"capacity_wait_s": 30}
    request.pop("batch")
    assert "batch" not in characterize._build_sim_request(**kwargs)


def test_non_object_request_batch_refused(tmp_path, monkeypatch):
    _record_sim_requests(monkeypatch)
    path = _write_request(tmp_path, _inverter_request(tmp_path, batch=600))
    with pytest.raises(characterize.CharacterizeError, match=r"request\.batch"):
        characterize.run_characterize(str(path))
