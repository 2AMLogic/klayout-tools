"""Tests for `klt sta` and the `klayout_tools.post_route_sta` library
(issue #1099).

Named `test_post_route_sta.py` rather than `test_sta.py` because
`tests/test_sta.py` already exists -- it tests the unrelated, already-shipped
`klayout_tools.sta` module (the `klt_statime_native` Rust boundary backing
`klt synthesize`'s pre-layout `sta` field). See
`klayout_tools/post_route_sta.py`'s own module docstring "Naming note" for
the full rationale.

Four tiers, mirroring `tests/test_place_and_route.py`'s own structure:

- **Request/library unit tests** exercise `load_request`/`run_sta`'s
  validation paths directly, against fabricated open_pdks-layout PDK
  installs created under `tmp_path` (never a real PDK).
- **Script-assembly tests** assert `_sta_script_lines`'s exact emitted Tcl
  order (LEF x2 -> DEF -> liberty -> clock -> optional spef check/read_spef
  -> metrics/violations), with no `-floorplan_initialize` flag.
- **Stubbed-OpenROAD tests** run `run_sta` end to end with
  `post_route_sta.subprocess.run` replaced by a fake that writes the same
  `-metrics <file>.json` shape a real OpenROAD run produces -- no `openroad`
  binary required.
- **CLI tests** cover exit codes and `--format text/json` dispatch.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import pdk as pdk_module
from klayout_tools import post_route_sta
from klayout_tools.cli import main
from klayout_tools.post_route_sta import PostRouteStaError, load_request, run_sta


def _write(path: Path, text: str) -> str:
    path.write_text(text, encoding="utf-8")
    return str(path)


def _write_request(path: Path, request: dict) -> str:
    path.write_text(json.dumps(request), encoding="utf-8")
    return str(path)


def _base_request(**overrides) -> dict:
    request = {
        "def": "top.def",
        "pdk": {"cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80"},
        "constraints": {"clock_port": "clk", "clock_period_ns": 1.1},
    }
    request.update(overrides)
    return request


def _isolate_pdk(monkeypatch, tmp_path: Path) -> None:
    """Mirrors `tests/test_place_and_route.py`'s identical fixture."""
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(pdk_module, "STORE_DIRS", [])
    monkeypatch.setattr(pdk_module, "CONVENTIONAL_PREFIXES", [])


def _make_pdk_install(
    root: Path,
    variant: str,
    *,
    cell_library: str = "sky130_fd_sc_hd",
    corner: str = "tt_025C_1v80",
    with_lib: bool = True,
    with_lef: bool = True,
    single_underscore_naming: bool = False,
) -> Path:
    """Fabricate a minimal open_pdks-layout variant -- mirrors
    `tests/test_place_and_route.py`'s identical `_make_pdk_install`, trimmed
    to the lib/LEF views this module actually resolves (no GDS view --
    `klt sta` never merges a GDS).

    ``single_underscore_naming=True`` fabricates IHP-Open-PDK's
    `sg13g2_stdcell` `lib/` naming instead (issue #1790, verified live
    against a real fetched v0.3.0 install): both the on-disk filename
    (`f"{cell_library}_{corner}.lib"`, a single underscore) and the
    `default_operating_conditions` attribute (`f"{cell_library}_{corner}"`,
    the full stem) use a single underscore -- no double-underscore file is
    written at all, matching a real IHP install."""
    variant_dir = root / variant
    (variant_dir / "libs.tech").mkdir(parents=True, exist_ok=True)
    lib_dir = variant_dir / "libs.ref" / cell_library

    if with_lib:
        lib_views_dir = lib_dir / "lib"
        lib_views_dir.mkdir(parents=True, exist_ok=True)
        separator = "_" if single_underscore_naming else "__"
        operating_conditions = (
            f"{cell_library}_{corner}" if single_underscore_naming else corner
        )
        content = (
            f'    default_operating_conditions : "{operating_conditions}";\n'
            "    nom_process : 1.0;\n"
            "    nom_temperature : 25.0;\n"
            "    nom_voltage : 1.8;\n"
        )
        (lib_views_dir / f"{cell_library}{separator}{corner}.lib").write_text(
            content, encoding="utf-8"
        )

    if with_lef:
        techlef_dir = lib_dir / "techlef"
        techlef_dir.mkdir(parents=True, exist_ok=True)
        (techlef_dir / f"{cell_library}__nom.tlef").write_text("# tech lef\n")
        lef_dir = lib_dir / "lef"
        lef_dir.mkdir(parents=True, exist_ok=True)
        (lef_dir / f"{cell_library}.lef").write_text("# merged cell lef\n")

    lib_dir.mkdir(parents=True, exist_ok=True)
    return variant_dir


def _setup_success_env(tmp_path, monkeypatch, **request_overrides) -> str:
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    _write(tmp_path / "top.def", "# fake routed def\n")
    return _write_request(tmp_path / "request.json", _base_request(**request_overrides))


# --------------------------------------------------------------------------- #
# `load_request`
# --------------------------------------------------------------------------- #


def test_load_request_missing_file(tmp_path):
    with pytest.raises(PostRouteStaError, match="file not found"):
        load_request(str(tmp_path / "nope.json"))


def test_load_request_directory(tmp_path):
    with pytest.raises(PostRouteStaError, match="not a file"):
        load_request(str(tmp_path))


def test_load_request_invalid_json(tmp_path):
    path = _write(tmp_path / "request.json", "{not json")
    with pytest.raises(PostRouteStaError, match="not valid JSON"):
        load_request(path)


def test_load_request_not_an_object(tmp_path):
    path = _write(tmp_path / "request.json", "[1, 2, 3]")
    with pytest.raises(PostRouteStaError, match="must contain a JSON object"):
        load_request(path)


@pytest.mark.parametrize("field", ["pdk", "constraints"])
def test_load_request_missing_required_field(tmp_path, field):
    request = _base_request()
    del request[field]
    path = _write_request(tmp_path / "request.json", request)
    with pytest.raises(PostRouteStaError, match=f"missing required field: {field}"):
        load_request(path)


def test_load_request_missing_def_is_not_a_load_request_error(tmp_path):
    """Issue #1825: unlike `pdk`/`constraints`, `def` is no longer a flat
    `load_request`-level required field -- `def`/`verilog` is a
    require-exactly-one-of relationship `run_sta` validates itself (see the
    `run_*` tests below), not something `validate_request_shape`'s flat
    required-fields check can express. A request missing `def` (with no
    `verilog` either) loads fine at this layer."""
    request = _base_request()
    del request["def"]
    path = _write_request(tmp_path / "request.json", request)

    loaded = load_request(path)

    assert "def" not in loaded


# --------------------------------------------------------------------------- #
# `run_sta` request validation (no PDK/OpenROAD involved)
# --------------------------------------------------------------------------- #


def test_run_def_not_found(tmp_path):
    request_path = _write_request(
        tmp_path / "request.json", _base_request(**{"def": "nope.def"})
    )
    with pytest.raises(PostRouteStaError, match="def not found: nope.def"):
        run_sta(request_path)


def test_run_cell_library_required(tmp_path):
    _write(tmp_path / "top.def", "# def\n")
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(pdk={"corner": "tt_025C_1v80"}),
    )
    with pytest.raises(PostRouteStaError, match="pdk.cell_library is required"):
        run_sta(request_path)


def test_run_pdk_must_be_object(tmp_path):
    _write(tmp_path / "top.def", "# def\n")
    request_path = _write_request(tmp_path / "request.json", _base_request(pdk="oops"))
    with pytest.raises(PostRouteStaError, match="request.pdk must be a JSON object"):
        run_sta(request_path)


def test_run_constraints_must_be_object(tmp_path):
    _write(tmp_path / "top.def", "# def\n")
    request_path = _write_request(
        tmp_path / "request.json", _base_request(constraints="oops")
    )
    with pytest.raises(PostRouteStaError, match="constraints must be a JSON object"):
        run_sta(request_path)


def test_run_constraints_clock_port_required(tmp_path):
    _write(tmp_path / "top.def", "# def\n")
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(constraints={"clock_period_ns": 1.1}),
    )
    with pytest.raises(PostRouteStaError, match="constraints.clock_port is required"):
        run_sta(request_path)


def test_run_constraints_clock_period_must_be_positive(tmp_path):
    _write(tmp_path / "top.def", "# def\n")
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(constraints={"clock_port": "clk", "clock_period_ns": -1}),
    )
    with pytest.raises(
        PostRouteStaError, match="constraints.clock_period_ns must be a positive"
    ):
        run_sta(request_path)


def test_run_hdl_toplevel_must_be_nonempty_string_when_given(tmp_path):
    _write(tmp_path / "top.def", "# def\n")
    request_path = _write_request(
        tmp_path / "request.json", _base_request(hdl_toplevel="")
    )
    with pytest.raises(PostRouteStaError, match="hdl_toplevel must be"):
        run_sta(request_path)


def test_run_spef_not_found(tmp_path):
    _write(tmp_path / "top.def", "# def\n")
    request_path = _write_request(
        tmp_path / "request.json", _base_request(spef="nope.spef")
    )
    with pytest.raises(PostRouteStaError, match="spef not found: nope.spef"):
        run_sta(request_path)


# --------------------------------------------------------------------------- #
# Netlist-input (`verilog`) mode -- issue #1825.
# --------------------------------------------------------------------------- #


def _verilog_base_request(**overrides) -> dict:
    request = {
        "verilog": "top.v",
        "hdl_toplevel": "top",
        "pdk": {"cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80"},
        "constraints": {"clock_port": "clk", "clock_period_ns": 1.1},
    }
    request.update(overrides)
    return request


def _setup_verilog_success_env(tmp_path, monkeypatch, **request_overrides) -> str:
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    _write(tmp_path / "top.v", "module top(input clk); endmodule\n")
    return _write_request(
        tmp_path / "request.json", _verilog_base_request(**request_overrides)
    )


def test_run_def_and_verilog_mutually_exclusive(tmp_path):
    request = _base_request(verilog="top.v", hdl_toplevel="top")
    request_path = _write_request(tmp_path / "request.json", request)

    with pytest.raises(PostRouteStaError, match="not include both 'def' and 'verilog'"):
        run_sta(request_path)


def test_run_requires_def_or_verilog(tmp_path):
    request = _base_request()
    del request["def"]
    request_path = _write_request(tmp_path / "request.json", request)

    with pytest.raises(
        PostRouteStaError, match="must include one of 'def' or 'verilog'"
    ):
        run_sta(request_path)


def test_run_verilog_not_found(tmp_path):
    request_path = _write_request(
        tmp_path / "request.json", _verilog_base_request(**{"verilog": "nope.v"})
    )
    with pytest.raises(PostRouteStaError, match="verilog not found: nope.v"):
        run_sta(request_path)


def test_run_verilog_requires_hdl_toplevel(tmp_path):
    _write(tmp_path / "top.v", "module top(input clk); endmodule\n")
    request = _verilog_base_request()
    del request["hdl_toplevel"]
    request_path = _write_request(tmp_path / "request.json", request)

    with pytest.raises(
        PostRouteStaError, match="hdl_toplevel is required when request.verilog"
    ):
        run_sta(request_path)


def test_run_verilog_spef_rejected(tmp_path):
    _write(tmp_path / "top.v", "module top(input clk); endmodule\n")
    request_path = _write_request(
        tmp_path / "request.json", _verilog_base_request(spef="top.spef")
    )

    with pytest.raises(
        PostRouteStaError,
        match="request.spef is not supported together with request.verilog",
    ):
        run_sta(request_path)


def test_run_verilog_geometry_source_rejects_other_values(tmp_path):
    _write(tmp_path / "top.v", "module top(input clk); endmodule\n")
    request_path = _write_request(
        tmp_path / "request.json",
        _verilog_base_request(geometry_source="routed"),
    )

    with pytest.raises(
        PostRouteStaError, match="geometry_source must be 'netlist_estimate'"
    ):
        run_sta(request_path)


def test_run_wire_load_model_rejected_in_def_mode(tmp_path):
    _write(tmp_path / "top.def", "# fake routed def\n")
    request = _base_request(
        constraints={
            "clock_port": "clk",
            "clock_period_ns": 1.1,
            "wire_load_model": "Small",
        }
    )
    request_path = _write_request(tmp_path / "request.json", request)

    with pytest.raises(
        PostRouteStaError, match="wire_load_model/wire_load_mode are only valid"
    ):
        run_sta(request_path)


def test_run_wire_load_mode_requires_wire_load_model(tmp_path):
    _write(tmp_path / "top.v", "module top(input clk); endmodule\n")
    request = _verilog_base_request(
        constraints={
            "clock_port": "clk",
            "clock_period_ns": 1.1,
            "wire_load_mode": "top",
        }
    )
    request_path = _write_request(tmp_path / "request.json", request)

    with pytest.raises(
        PostRouteStaError,
        match="wire_load_mode requires constraints.wire_load_model",
    ):
        run_sta(request_path)


def test_run_wire_load_mode_invalid_value_rejected(tmp_path):
    _write(tmp_path / "top.v", "module top(input clk); endmodule\n")
    request = _verilog_base_request(
        constraints={
            "clock_port": "clk",
            "clock_period_ns": 1.1,
            "wire_load_model": "Small",
            "wire_load_mode": "bogus",
        }
    )
    request_path = _write_request(tmp_path / "request.json", request)

    with pytest.raises(PostRouteStaError, match="wire_load_mode must be one of"):
        run_sta(request_path)


def test_run_wire_load_model_must_be_nonempty_string(tmp_path):
    _write(tmp_path / "top.v", "module top(input clk); endmodule\n")
    request = _verilog_base_request(
        constraints={
            "clock_port": "clk",
            "clock_period_ns": 1.1,
            "wire_load_model": "",
        }
    )
    request_path = _write_request(tmp_path / "request.json", request)

    with pytest.raises(
        PostRouteStaError, match="wire_load_model must be a non-empty string"
    ):
        run_sta(request_path)


# --------------------------------------------------------------------------- #
# `_resolve_liberty`/`_resolve_lef` -- mirrors
# `tests/test_place_and_route.py`'s identical resolution tests.
# --------------------------------------------------------------------------- #


def test_resolve_liberty_unresolvable_cell_library(tmp_path, monkeypatch):
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")
    monkeypatch.setenv("PDK_ROOT", str(install_root))

    with pytest.raises(PostRouteStaError, match="standard-cell library"):
        post_route_sta._resolve_liberty("nonexistent_lib", "tt_025C_1v80")


def test_resolve_liberty_missing_corner(tmp_path, monkeypatch):
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")
    monkeypatch.setenv("PDK_ROOT", str(install_root))

    with pytest.raises(PostRouteStaError, match="no 'ss_100C_1v60' corner"):
        post_route_sta._resolve_liberty("sky130_fd_sc_hd", "ss_100C_1v60")


def test_resolve_liberty_defaults_to_nominal_corner(tmp_path, monkeypatch):
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")
    monkeypatch.setenv("PDK_ROOT", str(install_root))

    liberty_path, corner, info = post_route_sta._resolve_liberty(
        "sky130_fd_sc_hd", None
    )

    assert corner == "tt_025C_1v80"
    assert liberty_path.endswith("sky130_fd_sc_hd__tt_025C_1v80.lib")
    assert info["variant"] == "sky130A"


def test_resolve_liberty_ihp_single_underscore_fallback(tmp_path, monkeypatch):
    """IHP-Open-PDK's `sg13g2_stdcell` names its liberty views with a single
    underscore before the corner tag (`sg13g2_stdcell_typ_1p20V_25C.lib`),
    not open_pdks' double-underscore convention -- only the single-underscore
    file exists on disk here, no double-underscore file at all. Must still
    resolve via the single-underscore fallback rather than raising "liberty
    not found" (issue #1790, missed in `post_route_sta.py`'s own copy of
    `_resolve_liberty` until issue #1652 folded it into the shared
    `klayout_tools.pdk.resolve_liberty_for_cell_library` implementation
    `synthesize.py`/`place_and_route.py` already carried this fallback in)."""
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(
        install_root,
        "ihp-sg13g2",
        cell_library="sg13g2_stdcell",
        corner="typ_1p20V_25C",
        single_underscore_naming=True,
    )
    monkeypatch.setenv("PDK_ROOT", str(install_root))

    liberty_path, corner, info = post_route_sta._resolve_liberty("sg13g2_stdcell", None)

    assert corner == "typ_1p20V_25C"
    assert liberty_path.endswith("sg13g2_stdcell_typ_1p20V_25C.lib")
    assert "__" not in Path(liberty_path).name
    assert info["variant"] == "ihp-sg13g2"


def test_resolve_lef_missing(tmp_path, monkeypatch):
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A", with_lef=False)
    monkeypatch.setenv("PDK_ROOT", str(install_root))

    _, _, info = post_route_sta._resolve_liberty("sky130_fd_sc_hd", "tt_025C_1v80")
    with pytest.raises(PostRouteStaError, match="LEF not found for deck"):
        post_route_sta._resolve_lef("sky130_fd_sc_hd", info)


# --------------------------------------------------------------------------- #
# `_sta_script_lines` -- exact Tcl assembly + ordering.
# --------------------------------------------------------------------------- #


def test_sta_script_lines_order_no_spef():
    lines = post_route_sta._sta_script_lines(
        tech_lef="/pdk/tech.lef",
        cell_lef="/pdk/cells.lef",
        def_path="/design/top.def",
        liberty_path="/pdk/lib.lib",
        clock_port="clk",
        clock_period_ns=2.5,
    )

    assert lines[0] == "read_lef /pdk/tech.lef"
    assert lines[1] == "read_lef /pdk/cells.lef"
    assert lines[2] == "read_def /design/top.def"
    assert lines[3] == "read_liberty /pdk/lib.lib"
    assert lines[4] == "create_clock -name clk -period 2.5 [get_ports clk]"
    assert "report_worst_slack_metric -setup" in lines
    assert "report_worst_slack_metric -hold" in lines
    assert "report_tns_metric -setup" in lines
    assert "report_tns_metric -hold" in lines
    assert "report_fmax_metric" in lines
    assert "report_power_metric" in lines
    assert "report_clock_skew_metric -setup" in lines
    # No floorplan-stage `-floorplan_initialize` flag anywhere -- this is
    # what distinguishes a standalone-analysis `read_def` from
    # `place_and_route.py`'s floorplan-stage load of a caller-supplied DEF.
    assert not any("floorplan_initialize" in line for line in lines)
    assert not any("read_spef" in line for line in lines)


def test_sta_script_lines_with_spef_reads_after_liberty_and_clock():
    lines = post_route_sta._sta_script_lines(
        tech_lef="/pdk/tech.lef",
        cell_lef="/pdk/cells.lef",
        def_path="/design/top.def",
        liberty_path="/pdk/lib.lib",
        clock_port="clk",
        clock_period_ns=2.5,
        spef_path="/design/top.spef",
        spef_net_names=["net_a", "net_b[3]"],
    )

    read_spef_idx = lines.index("read_spef /design/top.spef")
    read_liberty_idx = lines.index("read_liberty /pdk/lib.lib")
    clock_idx = next(i for i, ln in enumerate(lines) if ln.startswith("create_clock"))

    assert read_liberty_idx < clock_idx < read_spef_idx
    # The net-name-correlation check runs before `read_spef` (see
    # `_spef_net_check_lines`'s own docstring).
    check_begin_idx = next(
        i for i, ln in enumerate(lines) if post_route_sta._SPEF_NET_CHECK_BEGIN in ln
    )
    assert check_begin_idx < read_spef_idx
    assert "{net_a} {net_b[3]}" in "\n".join(lines)
    # The missing-nets diagnostic block also runs before `read_spef`, right
    # after the aggregate-count block (see `_spef_net_check_lines`).
    missing_begin_idx = next(
        i for i, ln in enumerate(lines) if post_route_sta._SPEF_MISSING_NETS_BEGIN in ln
    )
    assert check_begin_idx < missing_begin_idx < read_spef_idx


def test_sta_script_lines_wrap_read_spef_in_annotation_evidence():
    """Issue #1624: `read_spef` must be surrounded by the evidence
    scaffolding that lets the response say whether the annotation actually
    landed -- a `report_checks` fingerprint on either side, markers
    bracketing the call itself (so the SPEF reader's own warnings land in
    one delimited stdout region), and a closing
    `report_parasitic_annotation`."""
    lines = post_route_sta._sta_script_lines(
        tech_lef="/pdk/tech.lef",
        cell_lef="/pdk/cells.lef",
        def_path="/design/top.def",
        liberty_path="/pdk/lib.lib",
        clock_port="clk",
        clock_period_ns=2.5,
        spef_path="/design/top.spef",
        spef_net_names=["net_a"],
    )

    def idx(needle: str) -> int:
        return next(i for i, ln in enumerate(lines) if needle in ln)

    read_spef_idx = lines.index("read_spef /design/top.spef")
    assert (
        idx(post_route_sta._DELAY_PRE_BEGIN)
        < idx(post_route_sta._DELAY_PRE_END)
        < idx(post_route_sta._SPEF_READ_BEGIN)
        < read_spef_idx
        < idx(post_route_sta._SPEF_READ_END)
        < idx(post_route_sta._DELAY_POST_BEGIN)
        < idx(post_route_sta._DELAY_POST_END)
        < idx(post_route_sta._PARASITIC_ANNOTATION_BEGIN)
        < idx(post_route_sta._PARASITIC_ANNOTATION_END)
    )
    # Both fingerprints must be the *identical* invocation -- a differing
    # flag would make the two blocks differ for reasons unrelated to
    # annotation. `-digits 6` (not the default 2) and `min_max` (not
    # setup-only) are what make the comparison sensitive.
    fingerprints = [ln for ln in lines if ln.startswith("report_checks")]
    assert fingerprints == [
        "report_checks -path_delay min_max -digits 6 -unconstrained",
        "report_checks -path_delay min_max -digits 6 -unconstrained",
    ]
    # `report_parasitic_annotation` is wrapped in `catch` so an OpenSTA
    # build without the command degrades the derived fields to null rather
    # than aborting the run.
    assert any(ln.startswith("catch {report_parasitic_annotation}") for ln in lines)


def test_sta_script_lines_no_spef_has_no_annotation_evidence():
    """None of the issue-#1624 scaffolding is emitted for a run with no
    `spef` -- there is no annotation to attest to, and the extra
    `report_checks` would be pure cost."""
    lines = post_route_sta._sta_script_lines(
        tech_lef="/pdk/tech.lef",
        cell_lef="/pdk/cells.lef",
        def_path="/design/top.def",
        liberty_path="/pdk/lib.lib",
        clock_port="clk",
        clock_period_ns=2.5,
    )

    script = "\n".join(lines)
    assert post_route_sta._DELAY_PRE_BEGIN not in script
    assert post_route_sta._SPEF_READ_BEGIN not in script
    assert post_route_sta._PARASITIC_ANNOTATION_BEGIN not in script
    assert "report_checks" not in script


def test_sta_netlist_script_lines_order():
    """Issue #1825: the netlist-mode session links a structural netlist
    directly (`read_verilog` + `link_design`), never `read_def` -- and the
    liberty-before-LEF-before-verilog ordering mirrors
    `place_and_route.py`'s own `"floorplan"`-stage load."""
    lines = post_route_sta._sta_netlist_script_lines(
        tech_lef="/pdk/tech.lef",
        cell_lef="/pdk/cells.lef",
        verilog_path="/design/top.v",
        liberty_path="/pdk/lib.lib",
        hdl_toplevel="top",
        clock_port="clk",
        clock_period_ns=2.5,
    )

    assert lines[0] == "read_liberty /pdk/lib.lib"
    assert lines[1] == "read_lef /pdk/tech.lef"
    assert lines[2] == "read_lef /pdk/cells.lef"
    assert lines[3] == "read_verilog /design/top.v"
    assert lines[4] == "link_design top"
    assert lines[5] == "create_clock -name clk -period 2.5 [get_ports clk]"
    assert "report_worst_slack_metric -setup" in lines
    assert "report_worst_slack_metric -hold" in lines
    assert "report_tns_metric -setup" in lines
    assert "report_tns_metric -hold" in lines
    assert "report_fmax_metric" in lines
    assert "report_power_metric" in lines
    assert "report_clock_skew_metric -setup" in lines
    assert not any("read_def" in line for line in lines)
    assert not any(line.startswith("set_wire_load") for line in lines)


def test_sta_netlist_script_lines_with_wire_load_model():
    lines = post_route_sta._sta_netlist_script_lines(
        tech_lef="/pdk/tech.lef",
        cell_lef="/pdk/cells.lef",
        verilog_path="/design/top.v",
        liberty_path="/pdk/lib.lib",
        hdl_toplevel="top",
        clock_port="clk",
        clock_period_ns=2.5,
        wire_load_model="Medium",
        wire_load_mode="top",
    )

    clock_idx = next(i for i, ln in enumerate(lines) if ln.startswith("create_clock"))
    mode_idx = lines.index("set_wire_load_mode top")
    model_idx = lines.index("set_wire_load_model -name {Medium}")
    assert clock_idx < mode_idx < model_idx


def test_sta_netlist_script_lines_wire_load_model_without_mode():
    """`wire_load_mode` is optional even when `wire_load_model` is given --
    only `set_wire_load_model` is emitted, no `set_wire_load_mode`."""
    lines = post_route_sta._sta_netlist_script_lines(
        tech_lef="/pdk/tech.lef",
        cell_lef="/pdk/cells.lef",
        verilog_path="/design/top.v",
        liberty_path="/pdk/lib.lib",
        hdl_toplevel="top",
        clock_port="clk",
        clock_period_ns=2.5,
        wire_load_model="Small",
    )

    assert "set_wire_load_model -name {Small}" in lines
    # `startswith("set_wire_load_mode")` would also match
    # `"set_wire_load_model ..."` -- `"mode"` is a literal prefix of
    # `"model"` -- so check for the space-terminated `set_wire_load_mode `
    # command instead, which only the (absent here) mode-setting line has.
    assert not any(line.startswith("set_wire_load_mode ") for line in lines)


def test_spef_net_check_lines_uses_unescaped_names_verbatim():
    """The Tcl array that backs the design-side correlation check
    (`klt_spef_have`) must be keyed by the *unescaped* net name --
    `get_full_name` (design side) never returns SPEF's backslash-escaped
    spelling, so a still-escaped key would never match (issue #1422)."""
    lines = post_route_sta._spef_net_check_lines(["a[10]", "u_sub/net"])
    script = "\n".join(lines)

    assert "{a[10]} {u_sub/net}" in script
    # No literal backslash anywhere in the generated Tcl -- confirms this
    # helper never re-introduces SPEF's own escaping.
    assert "\\" not in script


def test_spef_net_names_parses_d_net_lines(tmp_path):
    spef_path = tmp_path / "top.spef"
    spef_path.write_text(
        '*SPEF "IEEE 1481-1998"\n'
        "*D_NET net_a 0.012345\n"
        "*CONN\n"
        "*P net_a B\n"
        "*END\n"
        "*D_NET net_b[3] 0.5\n"
        "*CONN\n"
        "*END\n",
        encoding="utf-8",
    )

    names = post_route_sta._spef_net_names(str(spef_path))

    assert names == ["net_a", "net_b[3]"]


def test_unescape_spef_name_reverses_extract_spef_escaping():
    """`_unescape_spef_name` must be the exact inverse of
    `extract_spef.py`'s `_spef_name()` -- imported directly (not
    hand-copied) so the two can never silently drift apart."""
    from klayout_tools.extract_spef import _spef_name

    for raw in ("a[10]", "u_sub/net", "data[7:0]", "net$1", "plain_name"):
        assert post_route_sta._unescape_spef_name(_spef_name(raw)) == raw


def test_unescape_spef_name_examples():
    assert post_route_sta._unescape_spef_name(r"a\[10\]") == "a[10]"
    assert post_route_sta._unescape_spef_name(r"u_sub\/net") == "u_sub/net"
    assert post_route_sta._unescape_spef_name("plain_name") == "plain_name"


def test_spef_net_names_unescapes_bracket_and_slash_escaped_names(tmp_path):
    """Issue #1422: a real SPEF writer (`extract_spef.py::_spef_name`)
    backslash-escapes every SPEF-reserved character it writes into a
    `*D_NET` name -- `_spef_net_names` must undo that, not return the raw,
    still-escaped text, or every bus-indexed/hierarchical net name silently
    fails to correlate against the design's real (unescaped) net names."""
    spef_path = tmp_path / "top.spef"
    spef_path.write_text(
        '*SPEF "IEEE 1481-1999"\n'
        "*D_NET a\\[10\\] 0.012345\n"
        "*CONN\n"
        "*END\n"
        "*D_NET u_sub\\/net 0.5\n"
        "*CONN\n"
        "*END\n"
        "*D_NET plain_net 0.1\n"
        "*CONN\n"
        "*END\n",
        encoding="utf-8",
    )

    names = post_route_sta._spef_net_names(str(spef_path))

    assert names == ["a[10]", "plain_net", "u_sub/net"]
    # None of the escaping backslashes should survive.
    assert not any("\\" in name for name in names)


def test_parse_spef_missing_nets_extracts_block():
    stdout = "\n".join(
        [
            post_route_sta._SPEF_NET_CHECK_BEGIN,
            "1 3",
            "1 3",
            post_route_sta._SPEF_NET_CHECK_END,
            post_route_sta._SPEF_MISSING_NETS_BEGIN,
            "a[10]",
            "u_sub/net",
            post_route_sta._SPEF_MISSING_NETS_END,
        ]
    )

    assert post_route_sta._parse_spef_missing_nets(stdout) == ["a[10]", "u_sub/net"]


def test_parse_spef_missing_nets_empty_block():
    stdout = "\n".join(
        [
            post_route_sta._SPEF_MISSING_NETS_BEGIN,
            post_route_sta._SPEF_MISSING_NETS_END,
        ]
    )

    assert post_route_sta._parse_spef_missing_nets(stdout) == []


def test_parse_spef_missing_nets_no_markers_returns_empty():
    assert post_route_sta._parse_spef_missing_nets("no markers here") == []


# --------------------------------------------------------------------------- #
# Post-`read_spef` annotation evidence (issue #1624).
# --------------------------------------------------------------------------- #


#: Two verbatim `read_spef` reader warnings, copied from a real OpenROAD
#: 26Q3 run against a SPEF whose flattened names the reader could not
#: resolve -- the exact failure issue #1624 reports.
_READER_WARNINGS = [
    "[WARNING STA-1650] /tmp/top.spef line 16, net "
    "g_slice\\[0\\].u_slice\\/_02_ not found.",
    "[WARNING STA-1648] /tmp/top.spef line 18, instance "
    "g_slice\\[0\\].u_slice\\/_16_:B not found.",
]


def _spef_read_block(*lines: str) -> str:
    return "\n".join(
        [
            post_route_sta._SPEF_READ_BEGIN,
            *lines,
            post_route_sta._SPEF_READ_END,
        ]
    )


def test_spef_reader_warnings_counts_delimited_stdout_region():
    stdout = "\n".join(
        [
            "[WARNING STA-1234] a liberty warning from before read_spef",
            _spef_read_block(*_READER_WARNINGS),
            "[WARNING STA-5678] a warning from after read_spef",
        ]
    )

    count, sample = post_route_sta._spef_reader_warnings(stdout, "")

    # Only the two inside the region count -- the reader is the only thing
    # running there, but a liberty/clock warning outside it is not evidence
    # of a failed annotation.
    assert count == 2
    assert sample == [line.strip() for line in _READER_WARNINGS]


def test_spef_reader_warnings_counts_unknown_codes_inside_region():
    """Inside the `read_spef` region the SPEF reader is the only thing
    running, so a warning code this module has never seen still counts --
    the region check is deliberately broader than the STA-1648/1650 pair."""
    stdout = _spef_read_block("[WARNING STA-9999] some future reader warning.")

    count, sample = post_route_sta._spef_reader_warnings(stdout, "")

    assert count == 1
    assert sample == ["[WARNING STA-9999] some future reader warning."]


def test_spef_reader_warnings_matches_stderr_narrowly():
    """OpenROAD builds differ in which stream the logger writes to, and
    stderr cannot be interleaved with the Tcl `puts` markers -- so stderr is
    matched against the known SPEF-reader codes only."""
    stdout = _spef_read_block()
    stderr = "\n".join(
        [
            *_READER_WARNINGS,
            "[WARNING STA-1234] an unrelated warning on stderr",
        ]
    )

    count, sample = post_route_sta._spef_reader_warnings(stdout, stderr)

    assert count == 2
    assert all("STA-16" in line for line in sample)


def test_spef_reader_warnings_without_markers_falls_back_to_known_codes():
    stdout = "\n".join([*_READER_WARNINGS, "[WARNING STA-1234] unrelated"])

    count, _sample = post_route_sta._spef_reader_warnings(stdout, "")

    assert count == 2


def test_spef_reader_warnings_sample_is_capped():
    warnings = [f"[WARNING STA-1650] net n{i} not found." for i in range(50)]
    stdout = _spef_read_block(*warnings)

    count, sample = post_route_sta._spef_reader_warnings(stdout, "")

    assert count == 50
    assert len(sample) == post_route_sta._SPEF_READER_WARNING_SAMPLE_LIMIT


def test_spef_reader_warnings_clean_run_reports_zero():
    count, sample = post_route_sta._spef_reader_warnings(_spef_read_block(), "")

    assert count == 0
    assert sample == []


def _delay_blocks(pre: str, post: str) -> str:
    return "\n".join(
        [
            post_route_sta._DELAY_PRE_BEGIN,
            pre,
            post_route_sta._DELAY_PRE_END,
            post_route_sta._DELAY_POST_BEGIN,
            post,
            post_route_sta._DELAY_POST_END,
        ]
    )


_UNANNOTATED_PATH = "   0.028072    0.028072 ^ u_a/i1/Y (sky130_fd_sc_hd__inv_2)"
_ANNOTATED_PATH = "   0.163705    0.163705 ^ u_a/i1/Y (sky130_fd_sc_hd__inv_2)"


def test_parse_delay_changed_identical_reports_is_false():
    """The reported failure mode: `read_spef` ran, but every path is
    digit-identical to the pre-annotation report."""
    stdout = _delay_blocks(_UNANNOTATED_PATH, _UNANNOTATED_PATH)

    assert post_route_sta._parse_delay_changed(stdout) is False


def test_parse_delay_changed_differing_reports_is_true():
    stdout = _delay_blocks(_UNANNOTATED_PATH, _ANNOTATED_PATH)

    assert post_route_sta._parse_delay_changed(stdout) is True


def test_parse_delay_changed_ignores_blank_line_and_trailing_space_churn():
    stdout = _delay_blocks(f"\n{_UNANNOTATED_PATH}   \n\n", f"{_UNANNOTATED_PATH}\n")

    assert post_route_sta._parse_delay_changed(stdout) is False


def test_parse_delay_changed_missing_markers_is_unknown():
    assert post_route_sta._parse_delay_changed("no markers here") is None


def test_parse_delay_changed_no_paths_is_unknown_not_false():
    """A design `report_checks` finds no path in produces two identical
    blocks that say nothing about annotation -- that must degrade to null
    (unknown), never to a `false` that would call an honest run
    incomplete."""
    assert post_route_sta._parse_delay_changed(_delay_blocks("", "")) is None
    assert (
        post_route_sta._parse_delay_changed(
            _delay_blocks("No paths found.", "No paths found.")
        )
        is None
    )


def _parasitic_block(text: str) -> str:
    return "\n".join(
        [
            post_route_sta._PARASITIC_ANNOTATION_BEGIN,
            text,
            post_route_sta._PARASITIC_ANNOTATION_END,
        ]
    )


def test_parse_parasitic_annotation_reads_both_counts():
    stdout = _parasitic_block(
        "Found 3 unannotated drivers.\nFound 1 partially unannotated drivers."
    )

    assert post_route_sta._parse_parasitic_annotation(stdout) == (3, 1)


def test_parse_parasitic_annotation_partial_line_is_not_the_total():
    """`Found 3 partially unannotated drivers.` must not be misread as the
    (absent) unannotated-driver total."""
    stdout = _parasitic_block("Found 3 partially unannotated drivers.")

    assert post_route_sta._parse_parasitic_annotation(stdout) == (None, 3)


def test_parse_parasitic_annotation_missing_block_is_unknown():
    assert post_route_sta._parse_parasitic_annotation("nothing here") == (None, None)


def test_parse_parasitic_annotation_unsupported_command_is_unknown():
    """An OpenSTA build without `report_parasitic_annotation` leaves the
    `catch`-guarded block empty -- both fields degrade to null."""
    assert post_route_sta._parse_parasitic_annotation(_parasitic_block("")) == (
        None,
        None,
    )


# --------------------------------------------------------------------------- #
# Stubbed-OpenROAD end-to-end: response envelope.
# --------------------------------------------------------------------------- #


_STA_METRICS = {
    "timing__setup__ws": -0.15,
    "timing__setup__tns": -1.2,
    "timing__hold__ws": 0.03812,
    "timing__hold__tns": 0.0,
    "timing__fmax": 500_000_000.0,
    "power__total": 0.0084,
    "clock__skew__setup": 0.021,
}


def _stub_openroad_success(
    monkeypatch,
    *,
    metrics: dict | None = None,
    version: str = "26Q3-771-gdeadbeef",
    setup_violations: int = 1,
    hold_violations: int = 0,
    spef_check: tuple[int, int, int, int] | None = None,
    spef_missing_nets: list[str] | None = None,
    spef_reader_warnings: list[str] | None = None,
    spef_delay_changed: bool = True,
    spef_parasitic_annotation: tuple[int, int] | None = (0, 0),
    spef_stderr: str = "",
) -> None:
    """Stand in for one `openroad` run.

    When `spef_check` is given, the stub also emits the issue-#1624
    annotation-evidence blocks a real run emits around `read_spef` -- by
    default the shape of a *successful* annotation (no reader warnings, a
    delay report that moved, zero unannotated drivers), so a test that only
    varies the name-correlation counts still exercises the full gate.
    """
    metrics = metrics if metrics is not None else _STA_METRICS

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["openroad", "-version"]:
            return fake_completed(stdout=f"{version} \n")
        assert cmd[0] == "openroad"
        metrics_path = cmd[4]
        with open(metrics_path, "w", encoding="utf-8") as handle:
            json.dump(metrics, handle)

        stdout_lines = [
            post_route_sta._SETUP_VIOLATIONS_BEGIN,
            *[f"pin_{i} (VIOLATED)" for i in range(setup_violations)],
            post_route_sta._SETUP_VIOLATIONS_END,
            post_route_sta._HOLD_VIOLATIONS_BEGIN,
            *[f"pin_{i} (VIOLATED)" for i in range(hold_violations)],
            post_route_sta._HOLD_VIOLATIONS_END,
        ]
        if spef_check is not None:
            a, b, c, d = spef_check
            post_delay = _ANNOTATED_PATH if spef_delay_changed else _UNANNOTATED_PATH
            annotation_lines = []
            if spef_parasitic_annotation is not None:
                unannotated, partial = spef_parasitic_annotation
                annotation_lines = [
                    f"Found {unannotated} unannotated drivers.",
                    f"Found {partial} partially unannotated drivers.",
                ]
            stdout_lines = [
                post_route_sta._SPEF_NET_CHECK_BEGIN,
                f"{a} {b}",
                f"{c} {d}",
                post_route_sta._SPEF_NET_CHECK_END,
                post_route_sta._SPEF_MISSING_NETS_BEGIN,
                *(spef_missing_nets or []),
                post_route_sta._SPEF_MISSING_NETS_END,
                post_route_sta._DELAY_PRE_BEGIN,
                _UNANNOTATED_PATH,
                post_route_sta._DELAY_PRE_END,
                post_route_sta._SPEF_READ_BEGIN,
                *(spef_reader_warnings or []),
                post_route_sta._SPEF_READ_END,
                post_route_sta._DELAY_POST_BEGIN,
                post_delay,
                post_route_sta._DELAY_POST_END,
                post_route_sta._PARASITIC_ANNOTATION_BEGIN,
                *annotation_lines,
                post_route_sta._PARASITIC_ANNOTATION_END,
                *stdout_lines,
            ]
        return fake_completed(
            returncode=0, stdout="\n".join(stdout_lines), stderr=spef_stderr
        )

    monkeypatch.setattr(post_route_sta.subprocess, "run", fake_run)


def test_run_sta_response_envelope(tmp_path, monkeypatch):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)

    report = run_sta(request_path)

    assert report["schema_version"] == 1
    assert report["engine"] == "openroad"
    assert report["engine_version"] == "26Q3-771-gdeadbeef"
    assert report["status"] == "ok"
    assert report["worst_slack_ns"] == -0.15
    assert report["total_negative_slack_ns"] == -1.2
    assert report["worst_hold_slack_ns"] == 0.03812
    assert report["total_negative_hold_slack_ns"] == 0.0
    assert report["fmax_mhz"] == 500.0
    assert report["setup_violation_count"] == 1
    assert report["hold_violation_count"] == 0
    assert report["clock_skew_ns"] == 0.021
    assert report["estimated_power_mw"] == 8.4
    assert report["def_path"].endswith("top.def")
    # Issue #1826: `geometry_source` defaults to `"routed"` when the request
    # omits it -- byte-for-byte the same behaviour this command had before
    # the field existed.
    assert report["geometry_source"] == "routed"
    assert report["spef_path"] is None
    assert report["spef_annotation"] is None

    provenance = report["provenance"]
    assert provenance["pdk"]["name"] == "sky130A"
    assert provenance["deck"]["name"] == "sky130_fd_sc_hd__tt_025C_1v80"
    assert provenance["input"]["content_hash"] is not None


def test_run_sta_geometry_source_placement_estimate_echoed(tmp_path, monkeypatch):
    """Issue #1826 (gap 1): a caller analysing a pre-route DEF (e.g. `klt
    place-and-route`'s own `unrouted_def_path`) declares that explicitly via
    `request.geometry_source`, echoed back verbatim -- so the response is
    never silently shaped the same as a routed signoff result."""
    request_path = _setup_success_env(
        tmp_path, monkeypatch, geometry_source="placement_estimate"
    )
    _stub_openroad_success(monkeypatch)

    report = run_sta(request_path)

    assert report["geometry_source"] == "placement_estimate"
    # Nothing about the OpenSTA session construction actually changes --
    # this is a caller-supplied label, not a different Tcl script.
    assert report["worst_slack_ns"] == -0.15


def test_run_sta_geometry_source_invalid_value_rejected(tmp_path, monkeypatch):
    request_path = _setup_success_env(tmp_path, monkeypatch, geometry_source="routing")
    _stub_openroad_success(monkeypatch)

    with pytest.raises(
        PostRouteStaError, match="request.geometry_source must be one of"
    ):
        run_sta(request_path)


def test_run_sta_netlist_mode_response_envelope(tmp_path, monkeypatch):
    """Issue #1825: a `verilog`-mode run reports the same setup/hold
    WNS/TNS shape a `def`-mode run does, but with `def_path: null`,
    `verilog_path` populated, `geometry_source: "netlist_estimate"`, and the
    wire-load estimate knob actually used echoed for provenance."""
    request_path = _setup_verilog_success_env(
        tmp_path,
        monkeypatch,
        constraints={
            "clock_port": "clk",
            "clock_period_ns": 1.1,
            "wire_load_model": "Medium",
            "wire_load_mode": "top",
        },
    )
    _stub_openroad_success(monkeypatch)

    report = run_sta(request_path)

    assert report["status"] == "ok"
    assert report["def_path"] is None
    assert report["verilog_path"].endswith("top.v")
    assert report["geometry_source"] == "netlist_estimate"
    assert report["wire_load_model"] == "Medium"
    assert report["wire_load_mode"] == "top"
    assert report["worst_slack_ns"] == -0.15
    assert report["total_negative_slack_ns"] == -1.2
    assert report["worst_hold_slack_ns"] == 0.03812
    assert report["total_negative_hold_slack_ns"] == 0.0
    assert report["spef_path"] is None
    assert report["spef_annotation"] is None

    provenance = report["provenance"]
    assert provenance["input"]["content_hash"] is not None


def test_run_sta_netlist_mode_no_wire_load_knob_echoes_null(tmp_path, monkeypatch):
    """Omitting `constraints.wire_load_model`/`.wire_load_mode` entirely is
    legal -- OpenSTA falls back to the resolved liberty's own default wire
    load (if any); the response echoes exactly what was requested (nothing),
    never a guess at OpenSTA's own silent default."""
    request_path = _setup_verilog_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)

    report = run_sta(request_path)

    assert report["geometry_source"] == "netlist_estimate"
    assert report["wire_load_model"] is None
    assert report["wire_load_mode"] is None


def test_run_sta_def_mode_wire_load_fields_are_null(tmp_path, monkeypatch):
    """A `def`-mode response always carries `wire_load_model`/`.wire_load_mode`
    as `null`/`null` and `verilog_path` as `null` -- same field shape as a
    `verilog`-mode response, just the mutually-exclusive fields flipped."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)

    report = run_sta(request_path)

    assert report["verilog_path"] is None
    assert report["wire_load_model"] is None
    assert report["wire_load_mode"] is None


def test_run_sta_hold_metrics_absent_degrade_to_null(tmp_path, monkeypatch):
    """When the ``-metrics`` JSON carries no ``timing__hold__ws``/
    ``timing__hold__tns`` keys at all (e.g. an older OpenROAD build, or a
    design OpenSTA finds no hold path to measure in), the two new fields
    degrade to ``null`` -- the same null-handling the existing setup-side
    fields already have, never a ``KeyError``."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    metrics_without_hold = {
        "timing__setup__ws": -0.15,
        "timing__setup__tns": -1.2,
        "timing__fmax": 500_000_000.0,
        "power__total": 0.0084,
        "clock__skew__setup": 0.021,
    }
    _stub_openroad_success(monkeypatch, metrics=metrics_without_hold)

    report = run_sta(request_path)

    assert report["worst_slack_ns"] == -0.15
    assert report["worst_hold_slack_ns"] is None
    assert report["total_negative_hold_slack_ns"] is None


def test_run_sta_with_spef_reports_annotation(tmp_path, monkeypatch):
    spef_path = tmp_path / "top.spef"
    spef_path.write_text("*D_NET clk 0.01\n*END\n", encoding="utf-8")
    request_path = _setup_success_env(tmp_path, monkeypatch, spef="top.spef")
    assert spef_path.exists()
    _stub_openroad_success(monkeypatch, spef_check=(1, 1, 1, 1))

    report = run_sta(request_path)

    assert report["spef_path"].endswith("top.spef")
    annotation = report["spef_annotation"]
    assert annotation["nets_annotated"] == 1
    assert annotation["nets_total"] == 1
    assert annotation["design_nets_annotated"] == 1
    assert annotation["design_nets_total"] == 1
    assert annotation["annotation_complete"] is True
    assert annotation["annotation_warning"] is None
    # No missing-net sample when correlation is already complete.
    assert annotation["design_nets_missing_sample"] == []
    # ... and the post-`read_spef` evidence agrees (issue #1624): the reader
    # kept every record, the delays moved, and OpenSTA holds parasitics for
    # every driver.
    assert annotation["reader_warning_count"] == 0
    assert annotation["reader_warning_sample"] == []
    assert annotation["delay_changed"] is True
    assert annotation["unannotated_driver_count"] == 0
    assert annotation["partially_unannotated_driver_count"] == 0


def test_run_sta_reader_warnings_force_annotation_incomplete(tmp_path, monkeypatch):
    """Regression for issue #1624: name correlation is *perfect* (every
    design net is named by the SPEF) but `read_spef` discarded the records
    it could not resolve, leaving the delays bit-identical to the
    unannotated run. `annotation_complete` must be `false`, not `true`."""
    spef_path = tmp_path / "top.spef"
    spef_path.write_text("*D_NET clk 0.01\n*END\n", encoding="utf-8")
    request_path = _setup_success_env(tmp_path, monkeypatch, spef="top.spef")
    _stub_openroad_success(
        monkeypatch,
        spef_check=(139, 152, 139, 139),
        spef_reader_warnings=_READER_WARNINGS,
        spef_delay_changed=False,
        spef_parasitic_annotation=(139, 0),
    )

    report = run_sta(request_path)

    annotation = report["spef_annotation"]
    # The pre-`read_spef` name correlation still reports a clean sheet --
    # that is exactly why it could not catch this on its own.
    assert annotation["design_nets_annotated"] == annotation["design_nets_total"]
    assert annotation["design_nets_missing_sample"] == []

    assert annotation["annotation_complete"] is False
    assert annotation["reader_warning_count"] == 2
    assert annotation["reader_warning_sample"] == [
        line.strip() for line in _READER_WARNINGS
    ]
    assert annotation["delay_changed"] is False
    assert annotation["unannotated_driver_count"] == 139
    warning = annotation["annotation_warning"]
    assert "read_spef discarded annotation for 2" in warning
    assert "byte-identical" in warning
    assert "139 driver(s) with no parasitics" in warning
    assert "NOT a real-parasitics measurement" in warning


def test_run_sta_reader_warnings_on_stderr_force_incomplete(tmp_path, monkeypatch):
    """Some OpenROAD builds log the reader's warnings to stderr, where they
    cannot be interleaved with the script's own stdout markers -- they must
    still gate `annotation_complete` (issue #1624)."""
    spef_path = tmp_path / "top.spef"
    spef_path.write_text("*D_NET clk 0.01\n*END\n", encoding="utf-8")
    request_path = _setup_success_env(tmp_path, monkeypatch, spef="top.spef")
    _stub_openroad_success(
        monkeypatch,
        spef_check=(4, 4, 4, 4),
        spef_stderr="\n".join(_READER_WARNINGS),
    )

    annotation = run_sta(request_path)["spef_annotation"]

    assert annotation["reader_warning_count"] == 2
    assert annotation["annotation_complete"] is False


def test_run_sta_unchanged_delays_alone_force_annotation_incomplete(
    tmp_path, monkeypatch
):
    """The delay fingerprint is decisive on its own: even with no reader
    warning and no unannotated driver, a post-`read_spef` timing report
    byte-identical to the pre-`read_spef` one means these are the
    unannotated numbers (issue #1624, suggested fix 3)."""
    spef_path = tmp_path / "top.spef"
    spef_path.write_text("*D_NET clk 0.01\n*END\n", encoding="utf-8")
    request_path = _setup_success_env(tmp_path, monkeypatch, spef="top.spef")
    _stub_openroad_success(
        monkeypatch, spef_check=(9, 9, 9, 9), spef_delay_changed=False
    )

    annotation = run_sta(request_path)["spef_annotation"]

    assert annotation["delay_changed"] is False
    assert annotation["reader_warning_count"] == 0
    assert annotation["annotation_complete"] is False
    assert "byte-identical" in annotation["annotation_warning"]


def test_run_sta_unannotated_drivers_force_annotation_incomplete(tmp_path, monkeypatch):
    """`report_parasitic_annotation`'s post-`read_spef` accounting gates too
    (issue #1624, suggested fix 2) -- OpenSTA saying it holds no parasitics
    for a driver outranks a pre-`read_spef` name match."""
    spef_path = tmp_path / "top.spef"
    spef_path.write_text("*D_NET clk 0.01\n*END\n", encoding="utf-8")
    request_path = _setup_success_env(tmp_path, monkeypatch, spef="top.spef")
    _stub_openroad_success(
        monkeypatch, spef_check=(9, 9, 9, 9), spef_parasitic_annotation=(7, 0)
    )

    annotation = run_sta(request_path)["spef_annotation"]

    assert annotation["unannotated_driver_count"] == 7
    assert annotation["annotation_complete"] is False
    assert "7 driver(s) with no parasitics" in annotation["annotation_warning"]


def test_run_sta_partially_unannotated_drivers_do_not_gate(tmp_path, monkeypatch):
    """`partially_unannotated_driver_count` is reported but deliberately
    does not gate: a complete, correctly-read SPEF routinely reports a
    non-zero partial count (load pins with no distinct RC node of their
    own), verified against a real OpenROAD 26Q3 run -- gating on it would
    fail every honest annotation."""
    spef_path = tmp_path / "top.spef"
    spef_path.write_text("*D_NET clk 0.01\n*END\n", encoding="utf-8")
    request_path = _setup_success_env(tmp_path, monkeypatch, spef="top.spef")
    _stub_openroad_success(
        monkeypatch, spef_check=(9, 9, 9, 9), spef_parasitic_annotation=(0, 3)
    )

    annotation = run_sta(request_path)["spef_annotation"]

    assert annotation["partially_unannotated_driver_count"] == 3
    assert annotation["annotation_complete"] is True
    assert annotation["annotation_warning"] is None


def test_run_sta_missing_evidence_blocks_degrade_to_null_not_false(
    tmp_path, monkeypatch
):
    """An OpenROAD build that emits none of the new blocks (no
    `report_parasitic_annotation`, no fingerprint markers) reports the
    derived fields as `null` -- unknown, not a fabricated `false` -- and
    falls back to the name-correlation verdict alone."""
    spef_path = tmp_path / "top.spef"
    spef_path.write_text("*D_NET clk 0.01\n*END\n", encoding="utf-8")
    request_path = _setup_success_env(tmp_path, monkeypatch, spef="top.spef")

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["openroad", "-version"]:
            return fake_completed(stdout="26Q3-771-gdeadbeef\n")
        with open(cmd[4], "w", encoding="utf-8") as handle:
            json.dump(_STA_METRICS, handle)
        return fake_completed(
            returncode=0,
            stdout="\n".join(
                [
                    post_route_sta._SPEF_NET_CHECK_BEGIN,
                    "5 5",
                    "5 5",
                    post_route_sta._SPEF_NET_CHECK_END,
                ]
            ),
        )

    monkeypatch.setattr(post_route_sta.subprocess, "run", fake_run)

    annotation = run_sta(request_path)["spef_annotation"]

    assert annotation["delay_changed"] is None
    assert annotation["unannotated_driver_count"] is None
    assert annotation["partially_unannotated_driver_count"] is None
    assert annotation["reader_warning_count"] == 0
    assert annotation["annotation_complete"] is True


def test_run_sta_with_spef_incomplete_annotation_warns(tmp_path, monkeypatch):
    spef_path = tmp_path / "top.spef"
    spef_path.write_text("*D_NET clk 0.01\n*END\n", encoding="utf-8")
    request_path = _setup_success_env(tmp_path, monkeypatch, spef="top.spef")
    _stub_openroad_success(
        monkeypatch,
        spef_check=(1, 1, 3, 10),
        spef_missing_nets=["a[10]", "u_sub/net"],
    )

    report = run_sta(request_path)

    annotation = report["spef_annotation"]
    assert annotation["annotation_complete"] is False
    assert "only 3 of 10 nets" in annotation["annotation_warning"]
    assert annotation["design_nets_missing_sample"] == ["a[10]", "u_sub/net"]


def test_run_sta_with_escaped_spef_net_names_no_longer_depressed(tmp_path, monkeypatch):
    """Regression for issue #1422: a caller-supplied SPEF whose `*D_NET`
    names carry real SPEF escaping (bus-index brackets, hierarchical
    slashes) must feed the *unescaped* name set into the correlation check's
    Tcl -- the stub simulates a fully-correlated OpenSTA run, and this test
    asserts the request pipeline gets there (i.e. `_spef_net_names` produced
    the real, unescaped names `_sta_script_lines` embedded, not the raw
    escaped SPEF text)."""
    spef_path = tmp_path / "top.spef"
    spef_path.write_text(
        '*SPEF "IEEE 1481-1999"\n'
        "*D_NET a\\[10\\] 0.01\n*CONN\n*END\n"
        "*D_NET u_sub\\/net 0.02\n*CONN\n*END\n",
        encoding="utf-8",
    )
    request_path = _setup_success_env(tmp_path, monkeypatch, spef="top.spef")
    # A real OpenSTA session, given the now-unescaped names, correlates all
    # of them -- this is what the stub simulates.
    _stub_openroad_success(monkeypatch, spef_check=(2, 2, 2, 2))

    report = run_sta(request_path)

    annotation = report["spef_annotation"]
    assert annotation["annotation_complete"] is True
    assert annotation["design_nets_annotated"] == 2
    assert annotation["design_nets_total"] == 2

    # The generated Tcl script itself embeds the *unescaped* names -- this
    # is the actual mechanism the fix changes.
    script_path = tmp_path / ".klt" / "sta" / "sta_top.tcl"
    script_text = script_path.read_text(encoding="utf-8")
    assert "{a[10]} {u_sub/net}" in script_text
    assert "\\[" not in script_text
    assert "\\/" not in script_text


# --------------------------------------------------------------------------- #
# `constraints.input_delay_ns`/`.output_delay_ns` + `timing_status`
# (issue #1865)
# --------------------------------------------------------------------------- #


#: The exact lines `_io_delay_lines` emits for `input_delay_ns: 0.2` /
#: `output_delay_ns: 0.3` with `clock_port: "clk"`.
_IO_INPUT_DELAY_LINES = [
    "set klt_clock_port [get_ports clk]",
    "set klt_non_clock_inputs "
    "[lsearch -inline -all -not -exact [all_inputs] $klt_clock_port]",
    "set_input_delay 0.2 -clock clk $klt_non_clock_inputs",
]
_IO_OUTPUT_DELAY_LINE = "set_output_delay 0.3 -clock clk [all_outputs]"

_IO_DELAY_CONSTRAINTS = {
    "clock_port": "clk",
    "clock_period_ns": 1.1,
    "input_delay_ns": 0.2,
    "output_delay_ns": 0.3,
}

#: OpenSTA's own unconstrained-design sentinel, as it reaches this repo via
#: OpenROAD's `-metrics` dump.
_UNCONSTRAINED = 1e39


def _sta_script_text(tmp_path, name: str = "sta_top.tcl") -> str:
    return (tmp_path / ".klt" / "sta" / name).read_text(encoding="utf-8")


def test_io_delay_lines_match_place_and_route_byte_for_byte():
    """`klt sta` keeps its own copy of `_io_delay_lines` (it validates and
    emits `constraints` independently of `klt place-and-route`, which it can
    run with no upstream request at all) -- this asserts the two copies
    cannot drift, so a caller correlating the two commands' generated Tcl
    never sees two different spellings of the same constraint (#1865)."""
    from klayout_tools import place_and_route

    for args in (
        (None, None),
        (0.2, None),
        (None, 0.3),
        (0.2, 0.3),
        (0, 0),
    ):
        assert post_route_sta._io_delay_lines("clk", *args) == (
            place_and_route._io_delay_lines("clk", *args)
        )


def test_io_delay_lines_helper_emits_only_given_fields():
    assert post_route_sta._io_delay_lines("clk", None, None) == []
    assert post_route_sta._io_delay_lines("clk", 0.2, None) == _IO_INPUT_DELAY_LINES
    assert post_route_sta._io_delay_lines("clk", None, 0.3) == [_IO_OUTPUT_DELAY_LINE]
    assert post_route_sta._io_delay_lines("clk", 0.2, 0.3) == [
        *_IO_INPUT_DELAY_LINES,
        _IO_OUTPUT_DELAY_LINE,
    ]


@pytest.mark.parametrize("field", ["input_delay_ns", "output_delay_ns"])
@pytest.mark.parametrize("bad_value", [-1.0, "fast", True, [0.2]])
def test_run_sta_io_delay_rejects_non_numbers(tmp_path, monkeypatch, field, bad_value):
    request_path = _setup_success_env(
        tmp_path,
        monkeypatch,
        constraints={"clock_port": "clk", "clock_period_ns": 1.1, field: bad_value},
    )
    _stub_openroad_success(monkeypatch)

    with pytest.raises(
        PostRouteStaError, match=f"{field} must be a non-negative number"
    ):
        run_sta(request_path)


def test_run_sta_io_delay_emitted_after_create_clock(tmp_path, monkeypatch):
    """`klt sta` accepts a `constraints` block of its own -- it can run
    standalone against an externally-produced DEF, with no `klt
    place-and-route` request anywhere upstream -- so the I/O constraints are
    validated and emitted here too, never inherited (issue #1865)."""
    request_path = _setup_success_env(
        tmp_path, monkeypatch, constraints=dict(_IO_DELAY_CONSTRAINTS)
    )
    _stub_openroad_success(monkeypatch)

    report = run_sta(request_path)
    assert report["status"] == "ok"

    lines = _sta_script_text(tmp_path).splitlines()
    for line in (*_IO_INPUT_DELAY_LINES, _IO_OUTPUT_DELAY_LINE):
        assert line in lines
    create_clock_idx = next(
        i for i, ln in enumerate(lines) if ln.startswith("create_clock")
    )
    assert create_clock_idx < lines.index(_IO_INPUT_DELAY_LINES[0])
    assert create_clock_idx < lines.index(_IO_OUTPUT_DELAY_LINE)


def test_run_sta_io_delay_emitted_in_verilog_mode(tmp_path, monkeypatch):
    """The from-scratch netlist mode (issue #1825) is exactly the mode a
    boundary block is most likely to be analysed in -- it gets the same
    treatment as `def` mode (issue #1865)."""
    request_path = _setup_verilog_success_env(
        tmp_path, monkeypatch, constraints=dict(_IO_DELAY_CONSTRAINTS)
    )
    _stub_openroad_success(monkeypatch)

    report = run_sta(request_path)
    assert report["status"] == "ok"

    lines = _sta_script_text(tmp_path, "sta_top.tcl").splitlines()
    for line in (*_IO_INPUT_DELAY_LINES, _IO_OUTPUT_DELAY_LINE):
        assert line in lines


def test_run_sta_io_delay_single_field_emits_only_that_side(tmp_path, monkeypatch):
    request_path = _setup_success_env(
        tmp_path,
        monkeypatch,
        constraints={
            "clock_port": "clk",
            "clock_period_ns": 1.1,
            "output_delay_ns": 0.3,
        },
    )
    _stub_openroad_success(monkeypatch)

    run_sta(request_path)

    lines = _sta_script_text(tmp_path).splitlines()
    assert _IO_OUTPUT_DELAY_LINE in lines
    assert not any(line.startswith("set_input_delay") for line in lines)
    assert not any("klt_clock_port" in line for line in lines)


def test_run_sta_no_io_delay_emits_no_io_delay_lines(tmp_path, monkeypatch):
    """Omitting both fields reproduces this command's generated Tcl
    byte-for-byte as it was before issue #1865."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)

    run_sta(request_path)

    script = _sta_script_text(tmp_path)
    assert "set_input_delay" not in script
    assert "set_output_delay" not in script
    assert "klt_clock_port" not in script


def test_run_sta_timing_status_constrained(tmp_path, monkeypatch):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)

    report = run_sta(request_path)

    assert report["timing_status"] == "constrained"


def test_run_sta_timing_status_unconstrained_on_the_sentinel(tmp_path, monkeypatch):
    """Issue #1865: with no constrained startpoint or endpoint, OpenSTA
    reports `1e+39` -- a *positive* number a naive `worst_slack_ns >= 0`
    gate reads as "timing closed". The raw values stay exactly as reported
    (the contract stays additive), and `timing_status` says what they
    are."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(
        monkeypatch,
        metrics={
            "timing__setup__ws": _UNCONSTRAINED,
            "timing__setup__tns": 0.0,
            "timing__hold__ws": _UNCONSTRAINED,
            "timing__hold__tns": 0.0,
        },
        setup_violations=0,
    )

    report = run_sta(request_path)

    assert report["worst_slack_ns"] == _UNCONSTRAINED
    assert report["total_negative_slack_ns"] == 0.0
    assert report["worst_hold_slack_ns"] == _UNCONSTRAINED
    assert report["setup_violation_count"] == 0
    assert report["timing_status"] == "unconstrained"


def test_run_sta_timing_status_null_when_no_slack_reported(tmp_path, monkeypatch):
    """`null` is "nothing to classify", not a claim either way -- a run
    whose `-metrics` dump carries no slack key at all reports it (issue
    #1865)."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch, metrics={"power__total": 0.0084})

    report = run_sta(request_path)

    assert report["worst_slack_ns"] is None
    assert report["worst_hold_slack_ns"] is None
    assert report["timing_status"] is None


def test_run_sta_engine_failure_raises(tmp_path, monkeypatch):
    request_path = _setup_success_env(tmp_path, monkeypatch)

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["openroad", "-version"]:
            return fake_completed(stdout="26Q3-771-gdeadbeef\n")
        return fake_completed(
            returncode=1,
            stderr="[ERROR STA-1234] something went wrong",
        )

    monkeypatch.setattr(post_route_sta.subprocess, "run", fake_run)

    with pytest.raises(PostRouteStaError, match=r"\[ERROR STA-1234\]"):
        run_sta(request_path)


def test_run_sta_missing_openroad_binary_raises(tmp_path, monkeypatch):
    request_path = _setup_success_env(tmp_path, monkeypatch)

    def fake_run(cmd, **kwargs):
        raise OSError("no such file or directory: 'openroad'")

    monkeypatch.setattr(post_route_sta.subprocess, "run", fake_run)

    with pytest.raises(PostRouteStaError, match="could not launch openroad"):
        run_sta(request_path)


# --------------------------------------------------------------------------- #
# CLI dispatch
# --------------------------------------------------------------------------- #


def test_cli_success_exits_zero_json(tmp_path, monkeypatch, capsys):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)

    exit_code = main(["sta", request_path, "--format", "json"])

    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok"
    assert out["schema_version"] == 1


def test_cli_text_default_format(tmp_path, monkeypatch, capsys):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)

    exit_code = main(["sta", request_path])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "status: ok" in out
    assert "worst_slack_ns: -0.15" in out
    assert "worst_hold_slack_ns: 0.03812" in out
    assert "total_negative_hold_slack_ns: 0.0" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_cli_text_format_prints_missing_nets_sample(tmp_path, monkeypatch, capsys):
    spef_path = tmp_path / "top.spef"
    spef_path.write_text("*D_NET clk 0.01\n*END\n", encoding="utf-8")
    request_path = _setup_success_env(tmp_path, monkeypatch, spef="top.spef")
    _stub_openroad_success(
        monkeypatch,
        spef_check=(1, 1, 3, 10),
        spef_missing_nets=["a[10]", "u_sub/net"],
    )

    exit_code = main(["sta", request_path])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "spef_annotation:" in out
    assert "warning:" in out
    assert "missing nets (sample): a[10], u_sub/net" in out


def test_cli_text_format_prints_annotation_evidence(tmp_path, monkeypatch, capsys):
    """Issue #1624: the text summary must show the evidence behind
    `complete=False`, not just the verdict -- a caller reading the human
    output should see *why* the annotation is not trustworthy."""
    spef_path = tmp_path / "top.spef"
    spef_path.write_text("*D_NET clk 0.01\n*END\n", encoding="utf-8")
    request_path = _setup_success_env(tmp_path, monkeypatch, spef="top.spef")
    _stub_openroad_success(
        monkeypatch,
        spef_check=(139, 152, 139, 139),
        spef_reader_warnings=_READER_WARNINGS,
        spef_delay_changed=False,
        spef_parasitic_annotation=(139, 0),
    )

    exit_code = main(["sta", request_path])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "complete=False" in out
    assert "delay_changed: False" in out
    assert "reader_warning_count: 2" in out
    assert "unannotated_driver_count: 139 (partial: 0)" in out
    assert "reader warnings (sample):" in out
    assert "[WARNING STA-1650]" in out


def test_cli_text_format_prints_netlist_mode_wire_load(tmp_path, monkeypatch, capsys):
    request_path = _setup_verilog_success_env(
        tmp_path,
        monkeypatch,
        constraints={
            "clock_port": "clk",
            "clock_period_ns": 1.1,
            "wire_load_model": "Small",
        },
    )
    _stub_openroad_success(monkeypatch)

    exit_code = main(["sta", request_path])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "geometry_source: netlist_estimate" in out
    assert "wire_load_model: Small" in out
    assert "wire_load_mode: None" in out
    assert "verilog_path:" in out
    assert out.count("def_path: None") == 1


def test_cli_error_exits_one_with_json_error(tmp_path, monkeypatch, capsys):
    _isolate_pdk(monkeypatch, tmp_path)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(**{"def": "nope.def"})
    )

    exit_code = main(["sta", request_path, "--format", "json"])

    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["schema_version"] == 1
    assert err["error"]["command"] == "sta"
    assert "def not found" in err["error"]["message"]


def test_cli_error_exits_one_text_format(tmp_path, monkeypatch, capsys):
    _isolate_pdk(monkeypatch, tmp_path)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(**{"def": "nope.def"})
    )

    exit_code = main(["sta", request_path])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert err.startswith("klt sta:")


def test_cli_missing_request_arg_is_usage_error():
    with pytest.raises(SystemExit) as exc_info:
        main(["sta"])
    assert exc_info.value.code == 2
