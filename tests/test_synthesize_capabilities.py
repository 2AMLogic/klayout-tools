"""Library capability regressions for #2088 and the three #2085 failures."""

import re
import shutil
from pathlib import Path

import pytest

from klayout_tools import synthesize
from klayout_tools.place_and_route import _reject_unsupported_netlist_constructs
from test_synthesize import (
    _abs_path,
    _base_request,
    _isolate_pdk,
    _make_pdk_install,
    _stub_yosys_success,
    _write_request,
)

_LIBRARY = "gf180mcu_fd_sc_mcu7t5v0"
_CORNER = "tt_025C_5v00"
_RTL = """\
module regression(input clk, input signed [3:0] a, b,
                  output reg signed [7:0] q);
    function [1:0] pick;
        input [1:0] sel;
        begin
            case (sel)
                2'b00: pick = 2'b01;
                default: pick = 2'bxx;
            endcase
        end
    endfunction
    wire signed [3:0] sum = a + b;
    wire [1:0] extra = pick(a[1:0]);
    always @(posedge clk) q <= {extra, sum, 2'b01};
endmodule
"""


def _request(tmp_path, library, clock_period_ns=81.38):
    (tmp_path / ".git").mkdir()
    (tmp_path / "regression.v").write_text(_RTL)
    return _write_request(
        tmp_path / "request.json",
        _base_request(
            sources=["regression.v"],
            hdl_toplevel="regression",
            pdk={"cell_library": library, "corner": _CORNER},
            constraints={"clock_period_ns": clock_period_ns},
        ),
    )


def _stub_install(tmp_path, monkeypatch, library):
    _isolate_pdk(monkeypatch, tmp_path)
    root = tmp_path / "install"
    _make_pdk_install(
        root,
        "gf180mcuD",
        cell_library=library,
        corners=((_CORNER, 1.0, 25.0, 5.0),),
    )
    monkeypatch.setenv("PDK_ROOT", str(root))
    _stub_yosys_success(monkeypatch, hdl_toplevel="regression")


def test_gf180_7t_activates_constraints_and_constant_mapping(tmp_path, monkeypatch):
    _stub_install(tmp_path, monkeypatch, _LIBRARY)
    report = synthesize.run_synthesize(_request(tmp_path, _LIBRARY))
    script = Path(_abs_path(report["script_path"], tmp_path)).read_text()
    assert " -constr " in script
    assert " -D 81380" in script
    assert "regression_abc.log abc -liberty" in script
    assert " -dont_use " not in script  # gf180's omission is intentional.
    assert (
        "clean\nsetundef -zero\n"
        f"hilomap -hicell {_LIBRARY}__tieh Z -locell {_LIBRARY}__tiel ZN\n"
        "tee -q -o "
    ) in script
    constr = tmp_path / ".klt/synthesize/regression_abc.constr"
    assert constr.read_text() == (
        f"set_driving_cell {_LIBRARY}__buf_4\nset_load 13.43\n"
    )
    assert report["timing"]["critical_path_ps"] > 0
    assert report["timing"]["delay_target_ps"] == 81380
    assert report["warnings"]["total"] == 0


@pytest.mark.parametrize("clock_period_ns", [None, 81.38])
@pytest.mark.parametrize("missing", ["both", "constraints", "ties"])
def test_missing_capabilities_are_explicit_and_preserve_engine_warnings(
    tmp_path, monkeypatch, clock_period_ns, missing
):
    library = "acme_sc_hd"
    _stub_install(tmp_path, monkeypatch, library)
    if missing == "constraints":
        monkeypatch.setitem(synthesize._TIE_CELLS, library, (("hi", "H"), ("lo", "L")))
    if missing == "ties":
        monkeypatch.setitem(synthesize._ABC_CONSTR_INPUTS, library, ("buf", 1.0))
    _stub_yosys_success(
        monkeypatch,
        hdl_toplevel="regression",
        yosys_log="Warning: engine diagnostic\nWarning: engine diagnostic\n",
    )
    report = synthesize.run_synthesize(_request(tmp_path, library, clock_period_ns))
    summary = report["warnings"]
    assert summary["total"] == 3
    assert summary["by_category"] == {"other": 2, "unsupported_cell_library": 1}
    assert summary["representatives"][0] == {
        "category": "other",
        "count": 2,
        "text": "engine diagnostic",
    }
    capability = summary["representatives"][1]
    assert capability["category"] == "unsupported_cell_library"
    assert library in capability["text"]
    assert ("-constr" in capability["text"]) == (missing != "ties")
    assert ("ABC timing" in capability["text"]) == (missing != "ties")
    assert ("setundef" in capability["text"]) == (missing != "constraints")
    assert ("hilomap" in capability["text"]) == (missing != "constraints")
    script = Path(_abs_path(report["script_path"], tmp_path)).read_text()
    assert (" -constr " in script) == (missing == "ties")
    assert ("setundef -zero" in script) == (missing == "constraints")
    assert ("hilomap " in script) == (missing == "constraints")
    assert (report["timing"] is None) == (missing != "ties")


def test_real_yosys_gf180_7t_all_three_netlist_regressions(tmp_path):
    """Real tools/library only: synthetic RTL reproduces all three defects.

    Uses a locally installed public 7t liberty; never downloads in pytest.
    The PR records the pinned upstream artifact used for its live run.
    """
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not installed")
    try:
        _, _, pdk_info = synthesize._resolve_liberty(_LIBRARY, _CORNER)
    except synthesize.SynthesizeError as exc:
        pytest.skip(f"no real gf180 7t liberty: {exc}")
    report = synthesize.run_synthesize(
        _request(tmp_path, _LIBRARY),
        pdk_root=pdk_info["root"],
        pdk_variant=pdk_info["variant"],
    )
    netlist_path = _abs_path(report["netlist_path"], tmp_path)
    netlist = Path(netlist_path).read_text()
    body = re.sub(r"/\*.*?\*/|//[^\n]*", "", netlist, flags=re.DOTALL)
    assert re.search(r"\b(input|output|wire|reg)\s+signed\b", body) is None
    assert re.search(r"\d+'[sS]?[bBoOdDhH][0-9a-fA-FxXzZ_]+", body) is None
    assert f"{_LIBRARY}__tieh" in body
    assert f"{_LIBRARY}__tiel" in body
    assert report["timing"]["critical_path_ps"] > 0
    assert report["timing"]["delay_target_ps"] == 81380
    assert "unsupported_cell_library" not in report["warnings"]["by_category"]
    _reject_unsupported_netlist_constructs(netlist_path)
