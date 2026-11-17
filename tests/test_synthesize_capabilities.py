"""Library capability regressions for #2088 and the three #2085 failures."""

import re
import shutil
from pathlib import Path

import pytest

from klayout_tools import synthesize, synthesize_library_hint
from klayout_tools.place_and_route import _reject_unsupported_netlist_constructs
from test_synthesize import (
    _abs_path,
    _base_request,
    _isolate_pdk,
    _make_pdk_install,
    _stub_yosys_success,
    _write_request,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

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
    constr = Path(_abs_path(report["script_path"], tmp_path)).with_name(
        "regression_abc.constr"
    )
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


def test_near_miss_cell_library_hint_names_closest_verified_key(tmp_path, monkeypatch):
    """Issue #5074's confirmed cause: `_TIE_CELLS` is exact-match, so
    `asap7sc7p5t_28_RVT` misses the verified `asap7sc7p5t_28_R` and silently
    loses `setundef -zero`/`hilomap`. The warning must name the near miss."""
    library = "asap7sc7p5t_28_RVT"
    _stub_install(tmp_path, monkeypatch, library)
    assert library not in synthesize._TIE_CELLS

    report = synthesize.run_synthesize(_request(tmp_path, library))

    text = next(
        rep["text"]
        for rep in report["warnings"]["representatives"]
        if rep["category"] == "unsupported_cell_library"
    )
    assert "closest cell_library with a verified tie-cell mapping is" in text
    assert "'asap7sc7p5t_28_R'" in text
    # Every verified key is disclosed, not just the closest one.
    for key in synthesize._TIE_CELLS:
        assert key in text


def test_unknown_cell_library_hint_lists_keys_without_claiming_a_closest(
    tmp_path, monkeypatch
):
    """A genuinely unrelated name has no near miss: the hint must say so
    rather than suggesting an unrelated library."""
    library = "totally_unrelated_xyz"
    _stub_install(tmp_path, monkeypatch, library)

    report = synthesize.run_synthesize(_request(tmp_path, library))

    text = next(
        rep["text"]
        for rep in report["warnings"]["representatives"]
        if rep["category"] == "unsupported_cell_library"
    )
    assert "No verified tie-cell mapping has a name close to this one" in text
    assert "the keys that do have one are:" in text
    for key in synthesize._TIE_CELLS:
        assert key in text


def test_cell_library_hint_is_best_effort(tmp_path, monkeypatch):
    """Mirrors `pdk_cells._other_variants_hint`: a discovery failure must
    never mask the original `unsupported_cell_library` text."""
    library = "asap7sc7p5t_28_RVT"
    _stub_install(tmp_path, monkeypatch, library)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("hint discovery exploded")

    monkeypatch.setattr(synthesize_library_hint.difflib, "get_close_matches", _boom)

    report = synthesize.run_synthesize(_request(tmp_path, library))

    text = next(
        rep["text"]
        for rep in report["warnings"]["representatives"]
        if rep["category"] == "unsupported_cell_library"
    )
    assert f"cell_library '{library}' has no verified mapping for:" in text
    assert "setundef -zero/hilomap" in text
    assert "These capabilities were skipped." in text
    assert "tie-cell mapping is" not in text


def test_supported_cell_library_emits_no_tie_cell_hint(tmp_path, monkeypatch):
    """A key that is in `_TIE_CELLS` gets no hint at all -- and, with its
    `-constr` entry present too, no `unsupported_cell_library` warning."""
    _stub_install(tmp_path, monkeypatch, _LIBRARY)

    report = synthesize.run_synthesize(_request(tmp_path, _LIBRARY))

    assert "unsupported_cell_library" not in report["warnings"]["by_category"]
    assert "verified tie-cell mapping" not in str(report["warnings"])


def test_library_capability_warnings_is_a_leaf_of_its_tables():
    """`synthesize_library_hint` takes both tables as arguments rather than
    importing `synthesize`, so the disclosure is testable without a run and
    the module stays import-leaf (issue #5074)."""
    empty = synthesize_library_hint.library_capability_warnings(
        "anything", tie_cells={}, constr_inputs={"anything"}
    )
    assert "closest cell_library" not in empty["unsupported_cell_library"]
    assert "setundef" in empty["unsupported_cell_library"]
    # No tie-cell keys to disclose at all -> the hint stays empty rather than
    # claiming a closest match.
    assert "the keys that do have one are" not in empty["unsupported_cell_library"]

    supported = synthesize_library_hint.library_capability_warnings(
        "acme", tie_cells={"acme"}, constr_inputs={"acme"}
    )
    assert supported == {}


def test_tie_cell_keys_hint_is_pure_and_bounded():
    hint = synthesize_library_hint.tie_cell_keys_hint(
        "asap7sc7p5t_28_RVT", ["asap7sc7p5t_28_R", "sky130_fd_sc_hd"]
    )
    assert "is 'asap7sc7p5t_28_R'" in hint
    # Keys are disclosed in sorted order, independent of the argument's own.
    assert "are: asap7sc7p5t_28_R, sky130_fd_sc_hd --" in hint
    assert synthesize_library_hint.tie_cell_keys_hint("whatever", []) == ""
