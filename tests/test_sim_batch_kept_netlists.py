"""Replay batch-generated corner netlists after their worker inputs are gone."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import sim
from klayout_tools import sim_batch as sb
from test_sim_batch import _FakeRunner, _run_failed_job, _sim_report, _status

pytestmark = [
    pytest.mark.parametrize("attach", [False, True], ids=["submit", "attach"]),
    pytest.mark.parametrize("scaled", [False, True], ids=["expanded", "scaled"]),
]


@pytest.fixture
def kept_deck(tmp_path, monkeypatch, attach, scaled):
    # Two DUTs flatten to the same name. One also needs a staging rewrite of
    # its own, so the bytes uploaded for it have no upload-side local_path.
    for folder in ("high side", "low"):
        directory = tmp_path / folder
        directory.mkdir()
        (directory / "dut.spice").write_text(
            '.include "resistor.spice"\n' if folder == "high side" else "Rb out 0 1k\n"
        )
    (tmp_path / "high side/resistor.spice").write_text("Ra in out 1k\n")
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "dut.spice").write_text("* shared model\n")
    monkeypatch.setenv("KLT_TEST_LIB", str(shared))
    source = tmp_path / "bench.cir"
    source.write_text(
        '* divider\n.include "$KLT_TEST_LIB/dut.spice"\n'
        '.include "high side/dut.spice"\n.inc "low/dut.spice"\n'
        "Vdd in 0 PWL(0 1.8 1u 1.8)\n"
    )
    request = {"netlist": source.name, "analysis": {"kind": "tran", "args": "10n 1u"}}
    measurements = [
        {"name": "output", "spice": ".meas tran output FIND v(out) AT=0.5u"}
    ]
    request["measurements"] = measurements
    if scaled:
        request["corners"] = {"supply_v": {"vdd": [1.2]}, "supply_waveforms": ["vdd"]}
    inputs = tmp_path / "worker/inputs"
    spec = sb._build_batch_job_spec(request, str(source), corner_count=1, timeout_s=30)
    for item in spec.inputs:
        item.materialize(str(inputs))
    corner_dir = tmp_path / "worker/outputs/artifacts/tt_27C"
    corner_dir.mkdir(parents=True)
    staged = str(inputs / sb.BATCH_NETLIST_FILENAME)
    generated, diagnostics = sim._expand_netlist_includes(staged, str(corner_dir))
    assert diagnostics == []
    point = sim.CornerPoint(
        None,
        {"vdd": 1.2} if scaled else {},
        27,
        supply_waveforms=("vdd",) if scaled else (),
    )
    generated, _ = sim._scale_supply_waveforms(
        generated, staged, str(corner_dir), point
    )
    deck = corner_dir / "corner.cir"
    sim._write_corner_deck(
        deck_path=str(deck),
        netlist_path=generated,
        models_lib=None,
        point=point,
        analysis=request["analysis"],
        measurements_spec=measurements,
        raw_path=None,
    )
    report = json.loads(_sim_report())
    report["corners"][0]["artifacts"] = {"deck": str(deck)}
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {
        "report.json": json.dumps(report),
        **{f"artifacts/tt_27C/{p.name}": p.read_text() for p in corner_dir.iterdir()},
    }
    (tmp_path / "worker").rename(tmp_path / "worker-gone")
    corners, _, _ = _run_failed_job(
        tmp_path,
        monkeypatch,
        runner,
        points=1,
        attach=attach,
        netlist_path=str(source),
        request=request,
        keep_artifacts=True,
        artifacts_dir=str(tmp_path / "kept"),
    )
    return Path(corners[0]["artifacts"]["deck"])


def test_generated_netlist_points_to_original_staged_inputs(
    kept_deck, tmp_path, scaled
):
    expanded = kept_deck.with_name("netlist.expanded.cir")
    selected = (
        kept_deck.with_name("netlist.supply_waveforms.cir") if scaled else expanded
    )
    assert f".include {selected}\n" in kept_deck.read_text()
    for sibling in {selected, expanded}:
        text = sibling.read_text()
        assert f'.include "{tmp_path}/shared/dut.spice"' in text
        assert f'.include "{tmp_path}/high side/dut.spice"' in text
        assert f'.inc "{tmp_path}/low/dut.spice"' in text
        assert str(tmp_path / "worker") not in text
    if scaled:
        assert "PWL(0 1.2 1u 1.2)" in selected.read_text()


@pytest.mark.skipif(shutil.which("ngspice") is None, reason="ngspice is unavailable")
def test_ngspice_replays_generated_netlist(kept_deck, scaled):
    replay = subprocess.run(
        ["ngspice", "-b", str(kept_deck)],
        cwd=kept_deck.parent,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert replay.returncode == 0, replay.stdout + replay.stderr
    measurement = re.search(r"^output\s+=\s+(\S+)", replay.stdout, re.MULTILINE)
    assert measurement, replay.stdout
    assert float(measurement[1]) == pytest.approx(0.6 if scaled else 0.9)
