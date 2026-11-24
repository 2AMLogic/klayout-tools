"""Arithmetic auto trials use their own ABC statistics after a missing stime log."""

import pytest

from klayout_tools import synthesize
from klayout_tools.synthesize_abc_timing import ABC_STATS_DUMP_FILENAME
from klayout_tools.synthesize_arithmetic import _EngineOptions, _measure_candidate
from test_synthesize import _ABC_STIME_LINE
from test_synthesize_abc_stime_capture import (
    _MULTI_REGION_DUMP,
    _SINGLE_REGION_BANNER,
    _SINGLE_REGION_DUMP,
    _capture_stub_yosys,
)


def _trial(tmp_path, monkeypatch, label, dump, log=_SINGLE_REGION_BANNER):
    _capture_stub_yosys(monkeypatch, abc_log_text=log, stats_dump=dump)
    # An unavailable whole-netlist STA path selects the actual ABC reader.
    monkeypatch.setattr(synthesize, "_read_sta_timing", lambda *args: (None, "absent"))
    measured = _measure_candidate(
        label=label,
        trial_dir=str(tmp_path / label),
        resolved_sources=["/abs/design.v"],
        hdl_toplevel="gcd",
        engine_options=_EngineOptions(
            liberty_path="/abs/lib.lib",
            cell_library="sky130_fd_sc_hd",
            delay_target_ps=2000,
            dont_use_globs=(),
            tie_cells=None,
            constr_inputs=("driver_cell", 1.0),
        ),
        adder_sources=(),
        adder_techmap_path=None,
    )
    assert measured is not None
    return measured["delay_ns"]


def test_distinct_trials_and_replay_without_a_fresh_dump(tmp_path, monkeypatch):
    assert _trial(tmp_path, monkeypatch, "default", _SINGLE_REGION_DUMP) == 0.771
    alternate = _SINGLE_REGION_DUMP.replace("771", "925")
    assert _trial(tmp_path, monkeypatch, "ripple", alternate) == 0.925
    assert _trial(tmp_path, monkeypatch, "ripple", None) is None
    assert not (tmp_path / "ripple" / ABC_STATS_DUMP_FILENAME).exists()
    assert (
        tmp_path / "default" / ABC_STATS_DUMP_FILENAME
    ).read_text() == _SINGLE_REGION_DUMP


@pytest.mark.parametrize("dump", ["unreadable", _MULTI_REGION_DUMP])
def test_ambiguous_or_malformed_trial_dump_has_no_delay(tmp_path, monkeypatch, dump):
    assert _trial(tmp_path, monkeypatch, "trial", dump) is None


def test_trial_keeps_precise_log_timing(tmp_path, monkeypatch):
    log = _SINGLE_REGION_BANNER + _ABC_STIME_LINE + "\n"
    assert _trial(
        tmp_path, monkeypatch, "trial", _SINGLE_REGION_DUMP, log
    ) == pytest.approx(2.48593)
