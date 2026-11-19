"""LEF-less ``request.macros`` entries: untimed black boxes (issue #5707).

The ``klt sta`` end-to-end tests for the black-box declaration live here,
plus the ``missing_lef_master_hint`` black-box-form test that used to sit
in ``tests/test_macros.py``. The tests import the shared request/stub
helpers from ``tests.test_post_route_sta`` unchanged, and live in their
own module so neither source test file grows past its per-file size
ceiling (the ratchet gating every PR). Validation-side unit tests for the
shared macros surface stay in ``tests/test_macros.py``.
"""

from __future__ import annotations

import pytest

from klayout_tools import macros as macro_spec
from klayout_tools.post_route_sta import PostRouteStaError, run_sta
from test_post_route_sta import (
    _isolate_pdk,
    _make_pdk_install,
    _setup_success_env,
    _setup_verilog_success_env,
    _sta_script_text,
    _stub_openroad_multi_corner,
    _stub_openroad_success,
    _verilog_base_request,
    _write,
    _write_macro_files,
    _write_request,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def test_missing_lef_master_hint_names_blackbox_form_only_when_asked():
    text = "[ERROR ORD-2013] instance u_m LEF master mystery not found."
    assert '{"cell": "mystery"}' not in macro_spec.missing_lef_master_hint(text)
    hint = macro_spec.missing_lef_master_hint(text, blackbox_form=True)
    assert '{"cell": "mystery"}' in hint
    assert "excluded" in hint


def test_lefless_macro_links_as_blackbox_in_verilog_mode(tmp_path, monkeypatch):
    """`{"cell": ...}` alone gets a generated stub LEF read before
    `link_design`, and is listed in `blackboxed_cells`."""
    request_path = _setup_verilog_success_env(
        tmp_path, monkeypatch, macros=[{"cell": "mystery"}]
    )
    _stub_openroad_success(monkeypatch)

    report = run_sta(request_path)

    text = _sta_script_text(tmp_path)
    lines = text.splitlines()
    stub_index = next(
        i
        for i, line in enumerate(lines)
        if line.endswith(".lef") and "blackbox_" in line
    )
    link_index = next(
        i for i, line in enumerate(lines) if line.startswith("link_design ")
    )
    assert stub_index < link_index
    stub_path = lines[stub_index].split(" ", 1)[1]
    with open(stub_path, encoding="utf-8") as handle:
        stub = handle.read()
    assert "MACRO mystery" in stub and "END mystery" in stub
    assert report["blackboxed_cells"] == [{"cell": "mystery"}]
    assert report["macros"][0]["lef"] is None
    assert report["macros"][0]["lib"] is None


def test_blackboxed_cells_always_present_and_empty_without_macros(
    tmp_path, monkeypatch
):
    request_path = _setup_verilog_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)

    report = run_sta(request_path)

    assert report["blackboxed_cells"] == []
    lines = _sta_script_text(tmp_path).splitlines()
    assert len([line for line in lines if line.startswith("read_lef ")]) == 2


def test_lefless_macro_mixes_with_lef_entries(tmp_path, monkeypatch):
    _write_macro_files(tmp_path)
    request_path = _setup_verilog_success_env(
        tmp_path,
        monkeypatch,
        macros=[{"lef": "sram_8x8.lef"}, {"cell": "mystery"}],
    )
    _stub_openroad_success(monkeypatch)

    report = run_sta(request_path)

    assert report["blackboxed_cells"] == [{"cell": "mystery"}]
    assert [m["cell"] for m in report["macros"]] == ["sram_8x8", "mystery"]


def test_lefless_macro_rejected_in_def_mode(tmp_path, monkeypatch):
    request_path = _setup_success_env(
        tmp_path, monkeypatch, macros=[{"cell": "mystery"}]
    )
    with pytest.raises(
        PostRouteStaError, match=r"macros\[0\]\.lef is required in def mode"
    ):
        run_sta(request_path)


def test_lefless_macro_with_lib_rejected(tmp_path, monkeypatch):
    _write_macro_files(tmp_path)
    request_path = _setup_verilog_success_env(
        tmp_path,
        monkeypatch,
        macros=[{"cell": "mystery", "lib": {"default": "sram_8x8_tt.lib"}}],
    )
    with pytest.raises(
        PostRouteStaError, match=r"macros\[0\]\.lib is not allowed without"
    ):
        run_sta(request_path)


def test_macro_without_lef_or_cell_rejected_in_verilog_mode(tmp_path, monkeypatch):
    request_path = _setup_verilog_success_env(tmp_path, monkeypatch, macros=[{}])
    with pytest.raises(PostRouteStaError, match=r"macros\[0\]\.lef is required"):
        run_sta(request_path)


def test_blackboxed_cells_hoisted_on_multi_corner_response(tmp_path, monkeypatch):
    corners = ("tt_025C_1v80", "ss_100C_1v60")
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    for corner in corners:
        _make_pdk_install(install_root, "sky130A", corner=corner)
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    _write(tmp_path / "top.v", "module top(input clk); endmodule\n")
    request = _verilog_base_request(macros=[{"cell": "mystery"}])
    request["pdk"] = {"cell_library": "sky130_fd_sc_hd", "corners": list(corners)}
    request_path = _write_request(tmp_path / "request.json", request)
    _stub_openroad_multi_corner(monkeypatch)

    report = run_sta(request_path)

    assert report["blackboxed_cells"] == [{"cell": "mystery"}]
    assert all("blackboxed_cells" not in entry for entry in report["corners"])
