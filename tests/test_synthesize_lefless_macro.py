"""End-to-end `klt synthesize` run for a LEF-less macro (issue #5612).

Split out of `test_synthesize.py` to keep that governed file under its
per-file size ceiling. Reuses that module's stubbed-Yosys harness (no real
`yosys` binary required): the request declares a macro with only a
`verilog_blackbox`, and the run must key the instance count on the
blackbox's module and disclose the missing abstract as
`lef.scope == "absent"`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools import macros, post_route_sta, synthesize
from klayout_tools.synthesize import run_synthesize
from test_synthesize import (
    _MACRO_MODULE_STATS,
    _macro_success_env,
    _stub_yosys_success,
    _write,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def test_lefless_macro_with_a_blackbox_runs_and_echoes_absent_lef(
    tmp_path, monkeypatch
):
    """Issue #5612: a macro with a `verilog_blackbox` and no `lef` validates,
    keys its instance count on the blackbox module, and discloses the missing
    abstract as `lef.scope == "absent"`."""
    request_path = _macro_success_env(
        tmp_path,
        monkeypatch,
        macros=[{"verilog_blackbox": "sram_bb.v"}],
        liberty_cells={"sky130_fd_sc_hd__xor2_1": 7.5},
    )
    _write(
        tmp_path / "sram_bb.v",
        "(* blackbox *)\nmodule sram_8x8 (D, Q);\ninput D;\noutput Q;\nendmodule\n",
    )
    _stub_yosys_success(monkeypatch, module_stats=_MACRO_MODULE_STATS)

    report = run_synthesize(request_path)

    assert report["macros"][0]["lef"] == {"path": None, "scope": "absent"}
    assert report["macros"][0]["blackbox_source"] == "verilog_blackbox"
    assert report["macro_instance_counts_by_type"] == {"sram_8x8": 1}
    assert report["macro_instance_count"] == 1
    assert "sram_8x8" in report["cells_without_area"]


def _multicycle_request(tmp_path, monkeypatch, macro):
    path = Path(_macro_success_env(tmp_path, monkeypatch, macros=[macro]))
    _write(tmp_path / "sram_bb.v", "module sram_8x8(input D, output Q); endmodule\n")
    request = json.loads(path.read_text())
    request["constraints"] = {
        "clocks": [{"port": "clk", "period_ns": 2}],
        "multicycle_paths": [{"from": "ff", "to": "ff", "setup": 4, "hold": 3}],
    }
    path.write_text(json.dumps(request))
    return path


def test_multicycle_sta_rejects_a_liberty_without_lef_before_pdk_resolution(
    tmp_path, monkeypatch
):
    path = _multicycle_request(
        tmp_path,
        monkeypatch,
        {
            "verilog_blackbox": "sram_bb.v",
            "lib": {"default": "sram_8x8_tt.lib"},
        },
    )
    monkeypatch.setattr(
        synthesize,
        "_resolve_liberty",
        lambda *a, **kw: pytest.fail("late validation"),
    )
    with pytest.raises(
        synthesize.SynthesizeError,
        match=r"request.macros\[0\].*lef.*multicycle_paths",
    ):
        run_synthesize(str(path))


def test_multicycle_sta_receives_a_cell_only_for_an_untimed_macro(
    tmp_path, monkeypatch
):
    path = _multicycle_request(tmp_path, monkeypatch, {"verilog_blackbox": "sram_bb.v"})

    def run_sta(sta_path, **kwargs):
        generated = json.loads(Path(sta_path).read_text())["macros"]
        validated = macros.validate_macros(
            generated,
            str(Path(sta_path).parent),
            error_cls=post_route_sta.PostRouteStaError,
            allow_blackbox=True,
        )
        assert generated == [{"cell": "sram_8x8"}]
        assert validated[0]["blackbox"] is True
        return {
            "geometry_source": "netlist_estimate",
            "worst_slack_ns": 0.5,
            "worst_hold_slack_ns": 0.1,
            "timing_status": "constrained",
            "multicycle_paths": [],
        }

    _stub_yosys_success(monkeypatch, module_stats=_MACRO_MODULE_STATS)
    monkeypatch.setattr(post_route_sta, "run_sta", run_sta)
    result = run_synthesize(str(path))
    assert result["constrained_timing"]["timing_status"] == "constrained"
    assert result["macros"][0]["blackbox_source"] == "verilog_blackbox"
