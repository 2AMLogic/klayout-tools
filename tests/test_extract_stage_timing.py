"""Exclusive stage timing and the parasitics stages of `klt extract` (issue #5716).

A `--parasitics` run used to leave its injection phase in no stage at all
(62% of one measured run was `unattributed_seconds`, with the `--progress`
heartbeat silent throughout) and folded the parasitics compute into
`device_extraction`. `StageTimer.stage` now credits a nested stage's seconds to
the nested stage alone, and the two phases carry their own stage.

Split out of `test_extract.py` (the per-file size ratchet forbids growing it).
"""

from __future__ import annotations

import io
import time

import pytest

from klayout_tools.extract import run_extract
from klayout_tools.extract_progress import StageTimer, active_timer, stage
from test_extract import _make_inverter_layout, _write_gds


def test_nested_stage_time_is_exclusive_and_reconciles():
    timer = StageTimer()
    with active_timer(timer):
        with stage("outer"):
            time.sleep(0.05)
            with stage("inner"):
                time.sleep(0.10)
            time.sleep(0.05)
        time.sleep(0.02)
    timing = timer.report()
    stages = timing["stages"]
    assert stages["inner"] == pytest.approx(0.10, abs=0.04)
    # The parent excludes the child: ~0.10s of its own, not ~0.20s.
    assert stages["outer"] == pytest.approx(0.10, abs=0.04)
    assert sum(stages.values()) + timing["unattributed_seconds"] == pytest.approx(
        timing["total_seconds"], abs=0.01
    )
    assert timing["unattributed_seconds"] > 0


def test_flat_stages_are_unchanged_by_exclusive_accounting():
    timer = StageTimer()
    with active_timer(timer):
        for name in ("a", "b"):
            with stage(name):
                time.sleep(0.03)
    stages = timer.report()["stages"]
    assert set(stages) == {"a", "b"}
    assert all(seconds == pytest.approx(0.03, abs=0.02) for seconds in stages.values())


def test_repeated_nested_stage_accumulates_and_parent_stays_exclusive():
    timer = StageTimer()
    with active_timer(timer):
        with stage("outer"):
            for _ in range(2):
                with stage("inner"):
                    time.sleep(0.04)
    timing = timer.report()
    assert timing["stages"]["inner"] == pytest.approx(0.08, abs=0.04)
    assert timing["stages"]["outer"] < 0.03
    assert sum(timing["stages"].values()) <= timing["total_seconds"] + 0.01


def test_heartbeat_names_the_open_nested_stage_not_the_parent():
    stream = io.StringIO()
    timer = StageTimer(progress="heartbeat", stream=stream, heartbeat_s=0.02)
    with active_timer(timer):
        with stage("device_extraction"):
            with stage("parasitics_compute"):
                time.sleep(0.2)
    lines = stream.getvalue().splitlines()
    assert any("parasitics_compute still running" in line for line in lines)
    assert not any("device_extraction still running" in line for line in lines)


def test_parasitics_stages_present_only_with_parasitics(tmp_path):
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    plain = run_extract(path, "sky130")["timing"]
    assert "parasitics_compute" not in plain["stages"]
    assert "parasitics_inject" not in plain["stages"]

    with_parasitics = run_extract(path, "sky130", parasitics=True)["timing"]
    stages = with_parasitics["stages"]
    assert {"parasitics_compute", "parasitics_inject", "device_extraction"} <= set(
        stages
    )
    assert sum(stages.values()) + with_parasitics[
        "unattributed_seconds"
    ] == pytest.approx(with_parasitics["total_seconds"], abs=0.01)


def test_parasitics_stages_survive_no_netlist(tmp_path):
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    timing = run_extract(path, "sky130", parasitics=True, no_netlist=True)["timing"]
    assert "parasitics_compute" in timing["stages"]
    assert "parasitics_inject" in timing["stages"]


def test_parasitics_stages_recorded_when_no_geometry_is_ground_eligible(tmp_path):
    # `build_parasitics_report`'s empty-report branch must still be a stage.
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.create_cell("TOP")
    path = _write_gds(layout, tmp_path / "empty.gds")
    timing = run_extract(path, "sky130", parasitics=True)["timing"]
    assert "parasitics_inject" in timing["stages"]
