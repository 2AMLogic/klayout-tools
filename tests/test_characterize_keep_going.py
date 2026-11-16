"""`klt characterize --keep-going` / `--timeout-s` (issue #5380).

Cell characterization is faked (no simulator); the batch/pool/emit plumbing
around it is real.
"""

from __future__ import annotations

import argparse
import json

import pytest

from klayout_tools import characterize, characterize_pool, sim
from klayout_tools.cli import characterize_cmd
from klayout_tools.cli.parser import create_parser
from test_characterize import (
    _batch_request,
    _fake_result,
    _inverter_request,
    _write_request,
)

_SIM_REPORT = {
    "corners": [{"corner_id": "c0", "status": "pass", "runtime_s": 1.0}],
    "environment": {"engine": "ngspice", "engine_version": "46"},
}


def _fake_cells(monkeypatch, tmp_path, request, failing=()):
    """Replace `_characterize_cell` with canned results; cells named in
    `failing` raise `CharacterizeError`."""
    results = {}
    for spec in request["cells"]:
        single = {**request, "cell": spec}
        single.pop("cells")
        result = _fake_result(tmp_path, single, measured={})
        result.update(
            sim_report=_SIM_REPORT,
            testbench_path="tb",
            sim_request_path="req",
            sim_report_path="rep",
        )
        results[spec["name"]] = result

    def fake(*, cell, **_kwargs):
        if cell["name"] in failing:
            raise characterize.CharacterizeError("simulation failed: timed out")
        return results[cell["name"]]

    monkeypatch.setattr(characterize, "_characterize_cell", fake)


def test_default_stays_fail_fast(tmp_path, monkeypatch):
    request = _batch_request(tmp_path)
    _fake_cells(monkeypatch, tmp_path, request, failing={"inv_demo"})
    path = _write_request(tmp_path, request)
    with pytest.raises(characterize.CharacterizeError, match="cell 'inv_demo'"):
        characterize.run_characterize(str(path))
    assert not list(tmp_path.rglob("*.lib"))


def test_keep_going_skips_failed_cell(tmp_path, monkeypatch):
    request = _batch_request(tmp_path)
    _fake_cells(monkeypatch, tmp_path, request, failing={"inv_demo"})
    path = _write_request(tmp_path, request)
    report = characterize.run_characterize(str(path), keep_going=True)
    assert [c["cell"]["name"] for c in report["cells"]] == ["nand2_demo"]
    assert [f["cell"] for f in report["failed_cells"]] == ["inv_demo"]
    assert "timed out" in report["failed_cells"][0]["error"]
    lib = open(report["liberty"]["path"]).read()
    assert "nand2_demo" in lib and "inv_demo" not in lib


def test_failed_cells_empty_when_none_fail(tmp_path, monkeypatch):
    request = _batch_request(tmp_path)
    _fake_cells(monkeypatch, tmp_path, request)
    path = _write_request(tmp_path, request)
    for keep_going in (False, True):
        report = characterize.run_characterize(str(path), keep_going=keep_going)
        assert report["failed_cells"] == []


def test_keep_going_all_fail_errors_without_lib(tmp_path, monkeypatch):
    request = _batch_request(tmp_path)
    _fake_cells(monkeypatch, tmp_path, request, failing={"inv_demo", "nand2_demo"})
    path = _write_request(tmp_path, request)
    with pytest.raises(characterize.CharacterizeError) as info:
        characterize.run_characterize(str(path), keep_going=True)
    assert "inv_demo" in str(info.value) and "nand2_demo" in str(info.value)
    assert not list(tmp_path.rglob("*.lib"))


def test_keep_going_with_cell_subset(tmp_path, monkeypatch):
    request = _batch_request(tmp_path)
    _fake_cells(monkeypatch, tmp_path, request, failing={"inv_demo"})
    path = _write_request(tmp_path, request)
    with pytest.raises(characterize.CharacterizeError, match="every cell failed"):
        characterize.run_characterize(str(path), cells=["inv_demo"], keep_going=True)
    report = characterize.run_characterize(
        str(path), cells=["nand2_demo"], keep_going=True
    )
    assert report["failed_cells"] == []


def test_keep_going_refused_on_single_cell_request(tmp_path):
    path = _write_request(tmp_path, _inverter_request(tmp_path))
    with pytest.raises(characterize.CharacterizeError, match="--keep-going"):
        characterize.run_characterize(str(path), keep_going=True)


def test_keep_going_pooled_path(tmp_path, monkeypatch):
    monkeypatch.delenv("KLT_SIM_BACKEND", raising=False)
    request = _batch_request(tmp_path)
    _fake_cells(monkeypatch, tmp_path, request, failing={"inv_demo"})
    path = _write_request(tmp_path, request)
    report = characterize.run_characterize(
        str(path), backend="batch", capacity_wait_s=600, keep_going=True
    )
    assert [f["cell"] for f in report["failed_cells"]] == ["inv_demo"]
    assert [c["cell"]["name"] for c in report["cells"]] == ["nand2_demo"]


def test_partitioned_propagates_unexpected_errors():
    def boom(_i):
        raise RuntimeError("bug")

    with pytest.raises(RuntimeError):
        characterize_pool.run_cells_partitioned(
            boom, ["a"], "local", {}, None, characterize.CharacterizeError, True
        )


def test_timeout_override_reaches_sim_request(tmp_path, monkeypatch):
    seen = []

    def fake_run_sim(path, **_kwargs):
        seen.append(json.load(open(path)))
        raise sim.SimError("stop here")

    monkeypatch.setattr(characterize.sim, "run_sim", fake_run_sim)
    request = _inverter_request(tmp_path)
    request.setdefault("options", {})["timeout_s"] = 900
    path = _write_request(tmp_path, request)
    for override, expected in ((None, 900), (1234, 1234)):
        with pytest.raises(characterize.CharacterizeError):
            characterize.run_characterize(str(path), timeout_s=override)
        assert seen[-1]["options"]["timeout_s"] == expected


@pytest.mark.parametrize("value", [0, -5])
def test_non_positive_timeout_is_an_error(tmp_path, value):
    path = _write_request(tmp_path, _inverter_request(tmp_path))
    with pytest.raises(characterize.CharacterizeError, match="--timeout-s"):
        characterize.run_characterize(str(path), timeout_s=value)


def test_cli_parses_and_forwards_flags(monkeypatch):
    args = create_parser().parse_args(
        ["characterize", "r.json", "--timeout-s", "1800", "--keep-going"]
    )
    assert args.timeout_s == 1800.0 and args.keep_going is True
    captured = {}

    def fake(request, **kwargs):
        captured.update(kwargs)
        raise characterize.CharacterizeError("stop")

    monkeypatch.setattr(characterize_cmd, "run_characterize", fake)
    characterize_cmd.run(args)
    assert captured["timeout_s"] == 1800.0 and captured["keep_going"] is True


def test_text_output_lists_failed_cells(tmp_path, monkeypatch, capsys):
    request = _batch_request(tmp_path)
    _fake_cells(monkeypatch, tmp_path, request, failing={"inv_demo"})
    path = _write_request(tmp_path, request)
    args = argparse.Namespace(
        request=str(path),
        outdir=None,
        lib=None,
        backend=None,
        keep_artifacts=False,
        format="text",
        keep_going=True,
    )
    assert characterize_cmd.run(args) == 0
    out = capsys.readouterr().out
    assert "FAILED" in out and "failed: inv_demo" in out
