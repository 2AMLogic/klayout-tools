"""A per-corner timeout follows the sim legs without editing the input bench."""

# ruff: noqa: F401, F811

import json

import pytest

from klayout_tools import pex
from klayout_tools.cli import main
from klayout_tools.pex_dry_run import run_pex_dry_run
from test_pex import (
    _async_inputs,
    _fake_async_sim,
    _two_corner_schematic_report,
    resistor_layout,
)
from test_pex_wait_for_capacity import _prepare


@pytest.mark.parametrize("timeout", [None, 423.5])
def test_pex_timeout_reaches_all_three_legs(
    tmp_path, resistor_layout, monkeypatch, timeout
):
    request = _prepare(tmp_path)
    before = request.read_bytes(), request.stat().st_mtime_ns
    calls = []

    def simulate(path, **kwargs):
        calls.append(kwargs)
        return _two_corner_schematic_report()

    monkeypatch.setattr(pex, "run_sim", simulate)
    report = pex.run_pex(
        resistor_layout,
        [str(request)],
        "sky130",
        device_only_leg=True,
        timeout_s=timeout,
    )
    assert len(calls) == 3
    if timeout is None:
        assert "simulation_timeout_s" not in report
        assert all("timeout_s" not in call for call in calls)
    else:
        assert report["simulation_timeout_s"] == timeout
        assert all(call["timeout_s"] == timeout for call in calls)
    assert (request.read_bytes(), request.stat().st_mtime_ns) == before


@pytest.mark.parametrize("runner", [pex.run_pex, run_pex_dry_run])
@pytest.mark.parametrize(
    "flags",
    [
        {"timeout_s": 0},
        {"timeout_s": -1},
        {"timeout_s": True},
        {"timeout_s": 10, "extract_only": True},
        {"timeout_s": 10, "measure_command": ["measure"]},
    ],
)
def test_rejects_inapplicable_timeout_before_layout_io(runner, flags):
    with pytest.raises(pex.PexError, match="--timeout-s"):
        runner("nonexistent.gds", [], "sky130", **flags)


def test_submit_and_bare_attach_preserve_timeout(
    tmp_path, resistor_layout, monkeypatch, capsys
):
    calls = []
    _fake_async_sim(monkeypatch, calls, sharded=True)
    request = _async_inputs(tmp_path)
    outdir = tmp_path / "batch"
    assert (
        main(
            [
                "pex",
                resistor_layout,
                str(request),
                "--deck",
                "sky130",
                "--backend",
                "batch",
                "--submit-only",
                "--hosts",
                "2",
                "--timeout-s",
                "387.5",
                "--outdir",
                str(outdir),
                "--format",
                "json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    manifest = outdir / "pex-submit.json"
    assert json.loads(manifest.read_text())["request"]["timeout_s"] == 387.5
    assert len(calls) == 2 and all(call["timeout_s"] == 387.5 for call in calls)
    calls.clear()
    assert main(["pex", "--attach", str(manifest), "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["simulation_timeout_s"] == 387.5
    assert len(calls) == 2 and all(call["timeout_s"] == 387.5 for call in calls)


def test_dry_run_forwards_timeout_to_nested_plan(
    tmp_path, resistor_layout, monkeypatch
):
    from klayout_tools import pex_dry_run

    request = _prepare(tmp_path)
    calls = []

    def plan(path, **kwargs):
        calls.append(kwargs)
        return {"dry_run": True}

    monkeypatch.setattr(pex_dry_run, "run_sim", plan)
    result = run_pex_dry_run(resistor_layout, [str(request)], "sky130", timeout_s=17.5)
    assert result["simulation_timeout_s"] == 17.5
    assert len(calls) == 1
    assert calls[0]["dry_run"] is True
    assert calls[0]["timeout_s"] == 17.5


@pytest.mark.parametrize("kind", ["commands", "decks"])
@pytest.mark.parametrize("runner", [pex.run_pex, run_pex_dry_run])
def test_timeout_identifies_incompatible_request_shape(
    tmp_path, resistor_layout, kind, runner
):
    path = tmp_path / "noncorner.json"
    path.write_text(json.dumps({kind: [{"id": "unit"}]}))
    with pytest.raises(
        pex.PexError, match="--timeout-s only applies to corner requests"
    ):
        runner(resistor_layout, [str(path)], "sky130", timeout_s=10)
