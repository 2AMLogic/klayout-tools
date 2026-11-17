"""End-to-end `klt synthesize` run for a LEF-less macro (issue #5612).

Split out of `test_synthesize.py` to keep that governed file under its
per-file size ceiling. Reuses that module's stubbed-Yosys harness (no real
`yosys` binary required): the request declares a macro with only a
`verilog_blackbox`, and the run must key the instance count on the
blackbox's module and disclose the missing abstract as
`lef.scope == "absent"`."""

from __future__ import annotations

import pytest

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
