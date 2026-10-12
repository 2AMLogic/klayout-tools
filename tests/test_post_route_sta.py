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
from klayout_tools._openroad_engine import _container_runtime_hint
from klayout_tools.cli import main
from klayout_tools.post_route_sta import PostRouteStaError, load_request, run_sta

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


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


# --------------------------------------------------------------------------- #
# Multi-corner characterization (`request.pdk.corners`, issue #1871).
# --------------------------------------------------------------------------- #

_MULTI_CORNER_METRICS = {
    "tt_025C_1v80": {
        "timing__setup__ws": -0.15,
        "timing__setup__tns": -1.2,
        "timing__hold__ws": 0.03812,
        "timing__hold__tns": 0.0,
        "timing__fmax": 500_000_000.0,
        "power__total": 0.0084,
        "clock__skew__setup": 0.021,
    },
    "ss_100C_1v60": {
        "timing__setup__ws": -0.42,
        "timing__setup__tns": -3.5,
        "timing__hold__ws": 0.01,
        "timing__hold__tns": 0.0,
        "timing__fmax": 300_000_000.0,
        "power__total": 0.006,
        "clock__skew__setup": 0.03,
    },
}


def _setup_multi_corner_env(
    tmp_path,
    monkeypatch,
    corners=("tt_025C_1v80", "ss_100C_1v60"),
    **request_overrides,
) -> str:
    """Mirrors `_setup_success_env`, but fabricates a `lib/` view for every
    name in `corners` (`_setup_success_env` only ever fabricates the one
    scalar `pdk.corner` needs) and requests `pdk.corners` instead of the
    scalar `pdk.corner`."""
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    for corner in corners:
        _make_pdk_install(install_root, "sky130A", corner=corner)
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    _write(tmp_path / "top.def", "# fake routed def\n")
    request = _base_request(
        pdk={"cell_library": "sky130_fd_sc_hd", "corners": list(corners)},
        **request_overrides,
    )
    return _write_request(tmp_path / "request.json", request)


def _stub_openroad_multi_corner(
    monkeypatch,
    metrics_by_corner: dict[str, dict] | None = None,
    *,
    version: str = "26Q3-771-gdeadbeef",
    mutate_def_path: str | None = None,
    mutate_after_corner: str | None = None,
) -> None:
    """Stand in for one `openroad` run per requested corner -- unlike
    `_stub_openroad_success` (always the same metrics regardless of which
    corner/liberty the generated script actually loads), this stub picks the
    metrics dict keyed by whichever corner name appears in the script's own
    `read_liberty` line (`<cell_library>__<corner>.lib`), so a multi-corner
    test can assert each `corners[]` entry carries *that* corner's own
    distinct values -- not the same stubbed number N times over.

    `mutate_def_path`/`mutate_after_corner` (both optional, and only used by
    the content-hash-invariant regression test below): when given, appends a
    byte to `mutate_def_path` immediately after the run for
    `mutate_after_corner` completes -- simulating the input geometry
    changing mid-characterization, which `_run_multi_corner`'s own
    before/after hash check must catch.
    """
    metrics_by_corner = metrics_by_corner or _MULTI_CORNER_METRICS

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["openroad", "-version"]:
            return fake_completed(stdout=f"{version} \n")
        assert cmd[0] == "openroad"
        metrics_path = cmd[4]
        script_path = cmd[5]
        script_text = Path(script_path).read_text(encoding="utf-8")
        corner_name = next(
            name for name in metrics_by_corner if f"__{name}.lib" in script_text
        )
        with open(metrics_path, "w", encoding="utf-8") as handle:
            json.dump(metrics_by_corner[corner_name], handle)

        stdout_lines = [
            post_route_sta._SETUP_VIOLATIONS_BEGIN,
            post_route_sta._SETUP_VIOLATIONS_END,
            post_route_sta._HOLD_VIOLATIONS_BEGIN,
            post_route_sta._HOLD_VIOLATIONS_END,
        ]
        if mutate_def_path is not None and corner_name == mutate_after_corner:
            with open(mutate_def_path, "a", encoding="utf-8") as handle:
                handle.write("# mutated mid-run\n")
        return fake_completed(returncode=0, stdout="\n".join(stdout_lines))

    monkeypatch.setattr(post_route_sta.subprocess, "run", fake_run)


def test_run_sta_corners_produces_per_corner_array(tmp_path, monkeypatch):
    request_path = _setup_multi_corner_env(tmp_path, monkeypatch)
    _stub_openroad_multi_corner(monkeypatch)

    report = run_sta(request_path)

    assert report["schema_version"] == 1
    assert report["status"] == "ok"
    assert report["engine"] == "openroad"
    assert report["engine_version"] == "26Q3-771-gdeadbeef"

    # The single-corner-only fields must not appear on a `corners` response
    # -- there is no one "the" slack value once N corners are characterized.
    assert "worst_slack_ns" not in report
    assert "spef_annotation" not in report

    corners = report["corners"]
    assert [entry["corner"] for entry in corners] == ["tt_025C_1v80", "ss_100C_1v60"]

    tt_entry, ss_entry = corners
    assert tt_entry["worst_slack_ns"] == -0.15
    assert tt_entry["fmax_mhz"] == 500.0
    assert tt_entry["spef_annotation"] is None
    assert tt_entry["deck"]["name"] == "sky130_fd_sc_hd__tt_025C_1v80"

    assert ss_entry["worst_slack_ns"] == -0.42
    assert ss_entry["total_negative_slack_ns"] == -3.5
    assert ss_entry["fmax_mhz"] == 300.0
    assert ss_entry["deck"]["name"] == "sky130_fd_sc_hd__ss_100C_1v60"

    # Structurally identical fields across every corner (issue #1871's own
    # "def_path/provenance.input hoisted once" requirement) are hoisted to
    # the top level -- never repeated inside a `corners[]` entry.
    assert report["def_path"].endswith("top.def")
    assert report["verilog_path"] is None
    assert report["geometry_source"] == "routed"
    assert report["spef_path"] is None
    for entry in corners:
        assert "def_path" not in entry
        assert "provenance" not in entry

    provenance = report["provenance"]
    assert provenance["pdk"]["name"] == "sky130A"
    assert provenance["input"]["content_hash"] is not None
    # `deck` is corner-specific -- never meaningfully hoistable to the
    # top level, so the shared `provenance` block carries none.
    assert provenance["deck"] is None


def test_run_sta_corners_and_corner_mutually_exclusive(tmp_path, monkeypatch):
    request_path = _setup_multi_corner_env(
        tmp_path, monkeypatch, corners=("tt_025C_1v80",)
    )
    # Splice a scalar `pdk.corner` back in alongside `pdk.corners`.
    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    request["pdk"]["corner"] = "tt_025C_1v80"
    Path(request_path).write_text(json.dumps(request), encoding="utf-8")

    with pytest.raises(
        PostRouteStaError,
        match="pdk.corner and request.pdk.corners are mutually exclusive",
    ):
        run_sta(request_path)


@pytest.mark.parametrize(
    "corners_value",
    [[], "tt_025C_1v80", [1, 2], [""], ["tt_025C_1v80", ""]],
)
def test_run_sta_corners_must_be_nonempty_list_of_strings(
    tmp_path, monkeypatch, corners_value
):
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    _write(tmp_path / "top.def", "# fake routed def\n")
    request = _base_request(
        pdk={"cell_library": "sky130_fd_sc_hd", "corners": corners_value}
    )
    request_path = _write_request(tmp_path / "request.json", request)

    with pytest.raises(
        PostRouteStaError,
        match="pdk.corners must be a non-empty list of non-empty strings",
    ):
        run_sta(request_path)


def test_run_sta_corners_rejects_duplicate_names(tmp_path, monkeypatch):
    request_path = _setup_multi_corner_env(
        tmp_path,
        monkeypatch,
        corners=("tt_025C_1v80", "tt_025C_1v80"),
    )

    with pytest.raises(PostRouteStaError, match="must not repeat a corner name"):
        run_sta(request_path)


def test_run_sta_corners_single_entry_still_produces_corners_array(
    tmp_path, monkeypatch
):
    """A one-element `pdk.corners` list is still the list shape (`corners:
    [...]`), never collapsed back to the scalar response -- the caller asked
    for the list contract, even if it only named one corner."""
    request_path = _setup_multi_corner_env(
        tmp_path, monkeypatch, corners=("tt_025C_1v80",)
    )
    _stub_openroad_multi_corner(monkeypatch)

    report = run_sta(request_path)

    assert "worst_slack_ns" not in report
    assert len(report["corners"]) == 1
    assert report["corners"][0]["corner"] == "tt_025C_1v80"
    assert report["corners"][0]["worst_slack_ns"] == -0.15


def test_run_sta_corners_content_hash_invariant_violation_raises(tmp_path, monkeypatch):
    """Issue #1871's own structural guarantee: every corner in a `corners`
    response must characterize the identical geometry. If the input file
    changes partway through the corner loop, this must be a loud,
    attributable error -- not a `corners` array that silently mixes two
    different geometries under one shared `def_path`/`provenance.input`."""
    request_path = _setup_multi_corner_env(tmp_path, monkeypatch)
    def_path = str(tmp_path / "top.def")
    _stub_openroad_multi_corner(
        monkeypatch,
        mutate_def_path=def_path,
        mutate_after_corner="tt_025C_1v80",
    )

    with pytest.raises(PostRouteStaError, match="geometry backing this multi-corner"):
        run_sta(request_path)


# --------------------------------------------------------------------------- #
# Violation-count check families (issue #2741).
#
# `report_check_types -max_delay`/`-min_delay` on their own filter to *data*
# checks, while `report_worst_slack_metric`/`report_tns_metric` measure every
# setup-side/hold-side check -- including asynchronous recovery/removal and
# inferred clock-gating checks. The fixture below is real OpenROAD
# `26Q3-1510-g6cb3f2b704` stdout (one session per scenario on a
# dfrtp/dfxtp/and2 netlist, sky130_fd_sc_hd tt_025C_1v80), captured with both
# the new combined report commands (`stdout`) and the legacy data-only ones
# (`legacy_stdout`), alongside each session's own WNS/TNS metrics.
# --------------------------------------------------------------------------- #

_CHECK_FAMILY_CAPTURES = json.loads(
    (
        Path(__file__).parent
        / "fixtures"
        / "sta_check_families"
        / "openroad_26Q3-1510_captures.json"
    ).read_text(encoding="utf-8")
)

_LEGACY_SETUP_BEGIN = "===LEGACY_SETUP_BEGIN==="
_LEGACY_SETUP_END = "===LEGACY_SETUP_END==="
_LEGACY_HOLD_BEGIN = "===LEGACY_HOLD_BEGIN==="
_LEGACY_HOLD_END = "===LEGACY_HOLD_END==="

#: scenario -> (setup_violation_count, hold_violation_count) the real engine
#: reported for the combined check-family commands.
_EXPECTED_CHECK_FAMILY_COUNTS = {
    "clean": (0, 0),
    "removal_only": (0, 2),
    "recovery_only": (2, 0),
    "data_hold_only": (0, 3),
    "data_setup_only": (3, 0),
    # data + async families failing together: 3 data endpoints (ff/D,
    # ff2/D, ff3/D) + 2 async endpoints (ff/RESET_B, ff2/RESET_B) -- each
    # endpoint listed once, no double counting across families.
    "mixed_hold": (0, 5),
    "mixed_setup": (5, 0),
    "clock_gating_setup_only": (1, 0),
    "clock_gating_hold_only": (0, 1),
    "unconstrained": (0, 0),
}


def _captured_counts(
    stdout: str, markers: tuple[str, str, str, str]
) -> tuple[int, int]:
    from klayout_tools._openroad_engine import _count_violations

    setup_begin, setup_end, hold_begin, hold_end = markers
    return (
        _count_violations(stdout, setup_begin, setup_end),
        _count_violations(stdout, hold_begin, hold_end),
    )


_STA_MARKERS = (
    post_route_sta._SETUP_VIOLATIONS_BEGIN,
    post_route_sta._SETUP_VIOLATIONS_END,
    post_route_sta._HOLD_VIOLATIONS_BEGIN,
    post_route_sta._HOLD_VIOLATIONS_END,
)


def test_violation_count_lines_request_every_check_family():
    """Both sides ask for the same check families their WNS/TNS metric
    measures: data + recovery + clock-gating setup, and data + removal +
    clock-gating hold -- one `report_check_types` call per side so each stays
    one marker-delimited block for `_count_violations`."""
    assert post_route_sta._violation_count_lines() == [
        f'puts "{post_route_sta._SETUP_VIOLATIONS_BEGIN}"',
        "report_check_types -max_delay -recovery -clock_gating_setup"
        " -violators -format end",
        f'puts "{post_route_sta._SETUP_VIOLATIONS_END}"',
        f'puts "{post_route_sta._HOLD_VIOLATIONS_BEGIN}"',
        "report_check_types -min_delay -removal -clock_gating_hold"
        " -violators -format end",
        f'puts "{post_route_sta._HOLD_VIOLATIONS_END}"',
    ]


def test_check_family_capture_used_the_shipped_report_commands():
    """Guards the fixture against drift: it must have been captured with
    exactly the report Tcl this module now emits, otherwise the parse tests
    below would be asserting against a different command's output."""
    assert (
        _CHECK_FAMILY_CAPTURES["report_tcl"] == post_route_sta._violation_count_lines()
    )
    assert _CHECK_FAMILY_CAPTURES["engine_version"] == "26Q3-1510-g6cb3f2b704"


@pytest.mark.parametrize("scenario", sorted(_EXPECTED_CHECK_FAMILY_COUNTS))
def test_count_violations_on_real_check_family_reports(scenario):
    captured = _CHECK_FAMILY_CAPTURES["scenarios"][scenario]
    assert (
        _captured_counts(captured["stdout"], _STA_MARKERS)
        == _EXPECTED_CHECK_FAMILY_COUNTS[scenario]
    )


@pytest.mark.parametrize(
    "scenario",
    [
        "removal_only",
        "recovery_only",
        "clock_gating_setup_only",
        "clock_gating_hold_only",
        "mixed_hold",
        "mixed_setup",
        "data_hold_only",
        "data_setup_only",
        "clean",
    ],
)
def test_constrained_failing_side_has_nonzero_count(scenario):
    """On these constrained fixtures every side whose captured WNS is
    clearly negative (well beyond the report's 2-digit rounding) also has a
    nonzero count, and every side with non-negative WNS counts zero. This is
    a property of these specific captures, not a claim that WNS and the
    count are interchangeable in general (rounded-to-zero slack and
    unconstrained designs are deliberately excluded)."""
    captured = _CHECK_FAMILY_CAPTURES["scenarios"][scenario]
    metrics = captured["metrics"]
    setup_count, hold_count = _captured_counts(captured["stdout"], _STA_MARKERS)
    assert (metrics["timing__setup__ws"] < -0.01) == (setup_count > 0)
    assert (metrics["timing__hold__ws"] < -0.01) == (hold_count > 0)
    assert (metrics["timing__setup__tns"] < 0) == (setup_count > 0)
    assert (metrics["timing__hold__tns"] < 0) == (hold_count > 0)


@pytest.mark.parametrize(
    ("scenario", "legacy_counts"),
    [
        ("removal_only", (0, 0)),
        ("recovery_only", (0, 0)),
        ("clock_gating_setup_only", (0, 0)),
        ("clock_gating_hold_only", (0, 0)),
        ("mixed_hold", (0, 3)),
        ("mixed_setup", (3, 0)),
        ("data_hold_only", (0, 3)),
        ("data_setup_only", (3, 0)),
    ],
)
def test_legacy_data_only_commands_undercount_on_real_engine(scenario, legacy_counts):
    """Documents the engine behavior behind issue #2741: the pre-fix
    `-max_delay`/`-min_delay`-only reports miss every recovery/removal and
    clock-gating violator the WNS/TNS metrics include, while data-only
    scenarios count identically under the old and new commands."""
    captured = _CHECK_FAMILY_CAPTURES["scenarios"][scenario]
    legacy = _captured_counts(
        captured["legacy_stdout"],
        (_LEGACY_SETUP_BEGIN, _LEGACY_SETUP_END, _LEGACY_HOLD_BEGIN, _LEGACY_HOLD_END),
    )
    assert legacy == legacy_counts
    if scenario.startswith("data_"):
        assert legacy == _EXPECTED_CHECK_FAMILY_COUNTS[scenario]


def test_unconstrained_capture_counts_zero_with_sentinel_slack():
    """Unconstrained: the engine reports its 1e39 sentinel WNS and "No paths
    found." in both blocks -- zero counts, and `timing_status` (not the
    count) is what flags the design as untimed."""
    captured = _CHECK_FAMILY_CAPTURES["scenarios"]["unconstrained"]
    assert captured["metrics"]["timing__setup__ws"] >= 1e29
    assert captured["stdout"].count("No paths found.") == 2
    assert _captured_counts(captured["stdout"], _STA_MARKERS) == (0, 0)


def _stub_openroad_check_family_engine(
    monkeypatch, scenario_by_corner: dict[str | None, str]
) -> list[str]:
    """Replay the real captured engine output for `scenario_by_corner`
    (keyed by corner name, or `None` for a single-corner run) -- choosing
    the combined-command capture only when the generated script actually
    requests the combined commands, and the legacy data-only capture
    otherwise, so the replay reproduces what the pinned engine would print
    for whichever Tcl `run_sta` really generated."""
    scripts: list[str] = []
    combined = _CHECK_FAMILY_CAPTURES["report_tcl"]

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["openroad", "-version"]:
            return fake_completed(stdout="26Q3-1510-g6cb3f2b704 \n")
        metrics_path, script_path = cmd[4], cmd[5]
        script_text = Path(script_path).read_text(encoding="utf-8")
        scripts.append(script_text)
        corner = next(
            (c for c in scenario_by_corner if c and f"__{c}.lib" in script_text),
            None,
        )
        captured = _CHECK_FAMILY_CAPTURES["scenarios"][scenario_by_corner[corner]]
        with open(metrics_path, "w", encoding="utf-8") as handle:
            json.dump(captured["metrics"], handle)
        if all(line in script_text.splitlines() for line in combined):
            stdout = captured["stdout"]
        else:
            stdout = (
                captured["legacy_stdout"]
                .replace(_LEGACY_SETUP_BEGIN, post_route_sta._SETUP_VIOLATIONS_BEGIN)
                .replace(_LEGACY_SETUP_END, post_route_sta._SETUP_VIOLATIONS_END)
                .replace(_LEGACY_HOLD_BEGIN, post_route_sta._HOLD_VIOLATIONS_BEGIN)
                .replace(_LEGACY_HOLD_END, post_route_sta._HOLD_VIOLATIONS_END)
            )
        return fake_completed(returncode=0, stdout=stdout)

    monkeypatch.setattr(post_route_sta.subprocess, "run", fake_run)
    return scripts


def test_run_sta_single_corner_counts_removal_violations(tmp_path, monkeypatch):
    """The issue's own symptom: negative hold WNS/TNS from a removal-only
    failure must come with a nonzero `hold_violation_count`."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_check_family_engine(monkeypatch, {None: "removal_only"})

    report = run_sta(request_path)

    assert report["worst_hold_slack_ns"] < 0
    assert report["total_negative_hold_slack_ns"] < 0
    assert report["hold_violation_count"] == 2
    assert report["setup_violation_count"] == 0


def test_run_sta_single_corner_counts_recovery_violations(tmp_path, monkeypatch):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_check_family_engine(monkeypatch, {None: "recovery_only"})

    report = run_sta(request_path)

    assert report["worst_slack_ns"] < 0
    assert report["setup_violation_count"] == 2
    assert report["hold_violation_count"] == 0


def test_run_sta_multi_corner_counts_check_families_per_corner(tmp_path, monkeypatch):
    """Each `corners[]` entry gets the same check-family policy, scoped to
    that corner's own session: one corner fails removal only, the other is
    a mixed data + recovery setup failure, and neither leaks into the
    other's counts."""
    request_path = _setup_multi_corner_env(tmp_path, monkeypatch)
    scripts = _stub_openroad_check_family_engine(
        monkeypatch,
        {"tt_025C_1v80": "removal_only", "ss_100C_1v60": "mixed_setup"},
    )

    report = run_sta(request_path)

    tt_entry, ss_entry = report["corners"]
    assert (tt_entry["setup_violation_count"], tt_entry["hold_violation_count"]) == (
        0,
        2,
    )
    assert tt_entry["worst_hold_slack_ns"] < 0
    assert (ss_entry["setup_violation_count"], ss_entry["hold_violation_count"]) == (
        5,
        0,
    )
    assert ss_entry["worst_slack_ns"] < 0
    assert len(scripts) == 2


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
# Container-runtime-unreachable diagnosis (issue #2632)
# --------------------------------------------------------------------------- #

#: What Docker Engine Community 29.8.1 prints when its daemon socket does not
#: exist -- reproduced live on a dev host (2026-09-30) with the
#: non-destructive `DOCKER_HOST=unix:///nonexistent-test.sock docker run --rm
#: hello-world`, which never touches the real daemon. Exit code 1.
_DOCKER_29_UNREACHABLE = (
    "failed to connect to the docker API at unix:///nonexistent-test.sock; "
    "check if the path is correct and if the daemon is running: dial unix "
    "/nonexistent-test.sock: connect: no such file or directory\n"
)

#: The long-documented wording from older Docker CLI releases. Shares no
#: usable literal substring with the 29.x message above -- which is exactly
#: why the detection keys on the `daemon ... running` shape instead.
_DOCKER_CLASSIC_UNREACHABLE = (
    "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
    "Is the docker daemon running?\n"
)

#: Docker's documented output when the invoking user cannot open the daemon
#: socket. Note it does NOT contain the word "running", so it is a genuinely
#: separate signature rather than a second spelling of the first one.
_DOCKER_PERMISSION_DENIED = (
    "Got permission denied while trying to connect to the Docker daemon "
    "socket at unix:///var/run/docker.sock: Get "
    '"http://%2Fvar%2Frun%2Fdocker.sock/v1.47/containers/json": dial unix '
    "/var/run/docker.sock: connect: permission denied\n"
)

#: A genuine OpenROAD/Tcl analysis failure -- the engine really ran, the
#: design/inputs are the problem. Must never pick up a container hint.
_GENUINE_STA_FAILURE_STDOUT = "[ERROR STA-1234] something went wrong\n"
_GENUINE_STA_FAILURE_STDERR = "Error: sta_modexp.tcl, 12 STA-1234\n"


@pytest.mark.parametrize(
    "stderr",
    [
        pytest.param(_DOCKER_29_UNREACHABLE, id="docker-29"),
        pytest.param(_DOCKER_CLASSIC_UNREACHABLE, id="docker-classic"),
    ],
)
def test_container_runtime_hint_recognizes_both_daemon_wordings(stderr):
    """Both known "daemon unreachable" wordings are recognized, and the hint
    says the request was never analyzed -- the whole point of the
    distinction, since retrying the same request after starting the runtime
    is the correct response."""
    hint = _container_runtime_hint(fake_completed(returncode=1, stderr=stderr))

    assert hint is not None
    assert "container runtime" in hint
    assert "scripts/install-openroad-docker.sh" in hint
    assert "docker info" in hint
    assert "never analyzed" in hint


def test_container_runtime_hint_recognizes_permission_denied():
    """The socket-permission case gets its own remedy -- starting the daemon
    would not help; the invoking user needs access to it."""
    hint = _container_runtime_hint(
        fake_completed(returncode=1, stderr=_DOCKER_PERMISSION_DENIED)
    )

    assert hint is not None
    assert "not permitted" in hint
    assert "docker" in hint
    assert "docker info" not in hint


def test_container_runtime_hint_reads_stdout_too():
    """Detection is stream-agnostic: a wrapper that merges docker's message
    onto stdout is diagnosed the same way."""
    assert (
        _container_runtime_hint(
            fake_completed(returncode=1, stdout=_DOCKER_CLASSIC_UNREACHABLE)
        )
        is not None
    )


def test_container_runtime_hint_none_for_genuine_analysis_failure():
    """A real OpenROAD/Tcl failure gets no hint. A false-positive here would
    be actively misleading -- it would tell a caller to go restart Docker
    when the actual problem is their design."""
    assert (
        _container_runtime_hint(
            fake_completed(
                returncode=1,
                stdout=_GENUINE_STA_FAILURE_STDOUT,
                stderr=_GENUINE_STA_FAILURE_STDERR,
            )
        )
        is None
    )


def test_container_runtime_hint_none_for_empty_output():
    assert _container_runtime_hint(fake_completed(returncode=1)) is None


def test_engine_error_message_appends_container_runtime_hint():
    """The hint is appended to -- never a replacement for -- the engine's own
    captured output, following `place_and_route.py`'s `_mount_namespace_hint`
    pattern (issue #1868)."""
    completed = fake_completed(returncode=1, stderr=_DOCKER_29_UNREACHABLE)

    message = post_route_sta._engine_error_message(completed)

    assert message.startswith("openroad sta run exited with code 1: ")
    assert "failed to connect to the docker API" in message
    assert " -- the openroad wrapper could not reach its container runtime" in message


def test_engine_error_message_appends_permission_hint_to_bracket_error():
    """Appending composes with the bracketed-diagnostic branch too, not only
    with the exit-code fallback."""
    completed = fake_completed(
        returncode=1,
        stdout="[ERROR STA-9999] could not start\n",
        stderr=_DOCKER_PERMISSION_DENIED,
    )

    message = post_route_sta._engine_error_message(completed)

    assert message.startswith(
        "openroad sta run failed: [ERROR STA-9999] could not start -- "
    )
    assert "not permitted to reach the container runtime's socket" in message


@pytest.mark.parametrize(
    ("stdout", "stderr", "expected"),
    [
        pytest.param(
            _GENUINE_STA_FAILURE_STDOUT,
            _GENUINE_STA_FAILURE_STDERR,
            "openroad sta run failed: [ERROR STA-1234] something went wrong",
            id="bracket-diagnostic",
        ),
        pytest.param(
            "",
            _GENUINE_STA_FAILURE_STDERR,
            "openroad sta run failed: Error: sta_modexp.tcl, 12 STA-1234",
            id="bare-error-trailer",
        ),
        pytest.param(
            "",
            "no timing paths found\n",
            "openroad sta run exited with code 1: no timing paths found",
            id="exit-code-fallback",
        ),
        pytest.param(
            "",
            "",
            "openroad sta run exited with code 1: no output captured",
            id="no-output",
        ),
    ],
)
def test_engine_error_message_unchanged_without_container_signature(
    stdout, stderr, expected
):
    """Regression guard: every message shape is byte-identical to its pre-#2632
    output when neither container signature is present."""
    completed = fake_completed(returncode=1, stdout=stdout, stderr=stderr)

    assert post_route_sta._engine_error_message(completed) == expected


def test_run_sta_unreachable_container_runtime_surfaces_hint(tmp_path, monkeypatch):
    """End to end through `run_sta`: the raised `PostRouteStaError` -- which is
    what becomes `error.message` -- carries the hint alongside docker's own
    captured text and the retained-log trailer."""
    request_path = _setup_success_env(tmp_path, monkeypatch)

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["openroad", "-version"]:
            return fake_completed(stdout="26Q3-771-gdeadbeef\n")
        return fake_completed(returncode=1, stderr=_DOCKER_29_UNREACHABLE)

    monkeypatch.setattr(post_route_sta.subprocess, "run", fake_run)

    with pytest.raises(PostRouteStaError) as excinfo:
        run_sta(request_path)

    message = str(excinfo.value)
    assert "failed to connect to the docker API" in message
    assert "could not reach its container runtime" in message
    assert "scripts/install-openroad-docker.sh" in message
    assert "openroad invocation:" in message


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


def test_cli_success_exits_zero_json_multi_corner(tmp_path, monkeypatch, capsys):
    request_path = _setup_multi_corner_env(tmp_path, monkeypatch)
    _stub_openroad_multi_corner(monkeypatch)

    exit_code = main(["sta", request_path, "--format", "json"])

    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok"
    assert out["schema_version"] == 1
    assert [entry["corner"] for entry in out["corners"]] == [
        "tt_025C_1v80",
        "ss_100C_1v60",
    ]


def test_cli_text_format_multi_corner(tmp_path, monkeypatch, capsys):
    request_path = _setup_multi_corner_env(tmp_path, monkeypatch)
    _stub_openroad_multi_corner(monkeypatch)

    exit_code = main(["sta", request_path])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "status: ok" in out
    assert "corner: tt_025C_1v80" in out
    assert "corner: ss_100C_1v60" in out
    assert "worst_slack_ns: -0.15" in out
    assert "worst_slack_ns: -0.42" in out
    assert "def_path:" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


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


# --------------------------------------------------------------------------- #
# Per-port min/max I/O delays + worst-path identity (issue #2740).
#
# `tests/fixtures/sta_worst_paths/boundary_captures.json` is real engine
# output (openroad/orfs:latest image sha256:0f1f4f03..., sky130_fd_sc_hd)
# from `run_sta` sessions on a small boundary netlist: two boundary data
# ports (`a_in`, `b_in`) with distinct min/max delays, a bus whose bit
# `bus[1]` gets a min-only negative delay, an async reset `rst_n` left on
# the scalar default, and an output `q_a` with a max-only output delay. It
# also records direct `find_timing_paths -from [get_ports X]` queries made
# against the same constraints written as plain SDC.
# --------------------------------------------------------------------------- #

_WORST_PATH_CAPTURES = json.loads(
    (
        Path(__file__).parent
        / "fixtures"
        / "sta_worst_paths"
        / "boundary_captures.json"
    ).read_text(encoding="utf-8")
)

_BOUNDARY_INPUT_DELAYS = {
    "a_in": {"min_ns": 0.3, "max_ns": 0.6},
    "b_in": {"min_ns": 0.05, "max_ns": 1.2},
    "bus[1]": {"min_ns": -0.2},
}
_BOUNDARY_OUTPUT_DELAYS = {"q_a": {"max_ns": 0.5}}


def _boundary_constraints(**overrides) -> dict:
    constraints = {
        "clock_port": "clk",
        "clock_period_ns": 2.0,
        "input_delay_ns": 0.0,
        "output_delay_ns": 0.1,
        "input_delays": json.loads(json.dumps(_BOUNDARY_INPUT_DELAYS)),
        "output_delays": json.loads(json.dumps(_BOUNDARY_OUTPUT_DELAYS)),
    }
    constraints.update(overrides)
    return constraints


def _stub_openroad_capture(monkeypatch, scenario_by_corner: dict) -> list[str]:
    """Replay a captured boundary scenario per corner (`None` key for a
    single-corner run); returns the generated scripts."""
    scripts: list[str] = []

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["openroad", "-version"]:
            return fake_completed(stdout="unknown \n")
        metrics_path, script_path = cmd[4], cmd[5]
        script_text = Path(script_path).read_text(encoding="utf-8")
        scripts.append(script_text)
        corner = next(
            (c for c in scenario_by_corner if c and f"__{c}.lib" in script_text),
            None,
        )
        captured = _WORST_PATH_CAPTURES["scenarios"][scenario_by_corner[corner]]
        if captured["metrics"] is not None:
            with open(metrics_path, "w", encoding="utf-8") as handle:
                json.dump(captured["metrics"], handle)
            return fake_completed(returncode=0, stdout=captured["stdout"])
        return fake_completed(returncode=3, stdout=captured["stdout"])

    monkeypatch.setattr(post_route_sta.subprocess, "run", fake_run)
    return scripts


def test_worst_path_capture_used_the_shipped_tcl():
    """Fixture drift guard: the capture must come from the Tcl this module
    emits today."""
    assert _WORST_PATH_CAPTURES["worst_path_tcl"] == post_route_sta._worst_path_lines()
    assert _WORST_PATH_CAPTURES["constraints"]["input_delays"] == _BOUNDARY_INPUT_DELAYS
    assert (
        _WORST_PATH_CAPTURES["constraints"]["output_delays"] == _BOUNDARY_OUTPUT_DELAYS
    )


# -- validation ------------------------------------------------------------- #


def test_validate_port_delay_maps_omitted_and_empty_are_none():
    assert post_route_sta._validate_port_delay_maps({}, "clk") == (None, None)
    assert post_route_sta._validate_port_delay_maps(
        {"input_delays": {}, "output_delays": {}}, "clk"
    ) == (None, None)


def test_validate_port_delay_maps_partial_bounds_not_copied():
    inputs, outputs = post_route_sta._validate_port_delay_maps(
        {
            "input_delays": {"a": {"min_ns": 0.1}, "b": {"max_ns": 2}},
            "output_delays": {"q": {"min_ns": -0.5, "max_ns": 0}},
        },
        "clk",
    )
    assert inputs == {"a": {"min_ns": 0.1}, "b": {"max_ns": 2.0}}
    assert outputs == {"q": {"min_ns": -0.5, "max_ns": 0.0}}


@pytest.mark.parametrize(
    ("constraints", "message"),
    [
        ({"input_delays": [1]}, "input_delays must be a JSON object"),
        ({"output_delays": "q"}, "output_delays must be a JSON object"),
        ({"input_delays": {"a": 0.1}}, r"input_delays\['a'\] must be a JSON object"),
        ({"input_delays": {"a": {}}}, r"input_delays\['a'\] is empty"),
        ({"input_delays": {"a": {"min": 0.1}}}, "unknown key"),
        ({"input_delays": {"a": {"min_ns": True}}}, "min_ns must be a finite number"),
        ({"input_delays": {"a": {"max_ns": None}}}, "max_ns must be a finite number"),
        ({"input_delays": {"a": {"max_ns": "1"}}}, "max_ns must be a finite number"),
        (
            {"output_delays": {"q": {"max_ns": float("nan")}}},
            "max_ns must be a finite number",
        ),
        (
            {"output_delays": {"q": {"min_ns": float("inf")}}},
            "min_ns must be a finite number",
        ),
        (
            {"input_delays": {"a": {"min_ns": 0.5, "max_ns": 0.1}}},
            "must not exceed max_ns",
        ),
        ({"input_delays": {"clk": {"min_ns": 0.1}}}, "must not name the clock port"),
        ({"input_delays": {"": {"min_ns": 0.1}}}, "non-empty port names"),
        ({"input_delays": {" a": {"min_ns": 0.1}}}, "leading/trailing whitespace"),
        ({"input_delays": {"a\nb": {"min_ns": 0.1}}}, "control character"),
    ],
)
def test_validate_port_delay_maps_rejects(constraints, message):
    with pytest.raises(PostRouteStaError, match=message):
        post_route_sta._validate_port_delay_maps(constraints, "clk")


def test_run_sta_port_delay_validation_runs_before_engine(tmp_path, monkeypatch):
    request_path = _setup_success_env(
        tmp_path,
        monkeypatch,
        constraints=_boundary_constraints(input_delays={"a_in": {}}),
    )
    calls: list = []
    monkeypatch.setattr(
        post_route_sta.subprocess, "run", lambda *a, **k: calls.append(a)
    )
    with pytest.raises(PostRouteStaError, match="is empty"):
        run_sta(request_path)
    assert calls == []


def test_signed_per_port_delays_allowed_but_legacy_scalar_stays_non_negative(
    tmp_path, monkeypatch
):
    inputs, _ = post_route_sta._validate_port_delay_maps(
        {"input_delays": {"d": {"min_ns": -0.4, "max_ns": 0}}}, "clk"
    )
    assert inputs == {"d": {"min_ns": -0.4, "max_ns": 0.0}}
    with pytest.raises(PostRouteStaError, match="must be a non-negative number"):
        post_route_sta._validate_constraints(
            {"clock_port": "clk", "clock_period_ns": 1, "input_delay_ns": -0.4}
        )


# -- Tcl generation --------------------------------------------------------- #


def test_request_io_delay_lines_without_maps_is_legacy_scalar_tcl():
    for args in ((None, None), (0.2, None), (None, 0.3), (0.2, 0.3)):
        assert post_route_sta._request_io_delay_lines(
            "clk", *args, None, None
        ) == post_route_sta._io_delay_lines("clk", *args)


def test_port_io_delay_lines_target_only_mapped_ports():
    """Two boundary ports with different min/max plus a min-only bus bit:
    each gets exactly its own bounds, and the scalar default goes only to
    the ports absent from the map (clock excluded)."""
    lines = post_route_sta._port_io_delay_lines(
        "clk", 0.0, 0.1, _BOUNDARY_INPUT_DELAYS, _BOUNDARY_OUTPUT_DELAYS
    )
    delay_lines = [ln for ln in lines if ln.lstrip().startswith("set_")]
    assert [ln for ln in delay_lines if "[get_ports" in ln and "-m" in ln] == [
        "set_input_delay -min 0.3 -clock clk [get_ports a_in]",
        "set_input_delay -max 0.6 -clock clk [get_ports a_in]",
        "set_input_delay -min 0.05 -clock clk [get_ports b_in]",
        "set_input_delay -max 1.2 -clock clk [get_ports b_in]",
        "set_input_delay -min -0.2 -clock clk [get_ports bus\\[1\\]]",
        "set_output_delay -max 0.5 -clock clk [get_ports q_a]",
    ]
    # Partial bounds are never mirrored onto the other side.
    assert not any("-max" in ln and "bus\\[1\\]" in ln for ln in lines)
    assert not any("-min" in ln and "q_a" in ln for ln in lines)
    # The scalar default is applied to the filtered "unmapped" set only.
    assert "set klt_input_mapped [list a_in b_in bus\\[1\\]]" in lines
    assert "set klt_output_mapped [list q_a]" in lines
    assert "    set_input_delay 0.0 -clock clk $klt_input_default_ports" in lines
    assert "    set_output_delay 0.1 -clock clk $klt_output_default_ports" in lines
    assert (
        "set klt_input_candidates "
        "[lsearch -inline -all -not -exact [all_inputs] $klt_clock_port]"
    ) in lines
    # No blanket `$klt_non_clock_inputs`/`[all_outputs]` write that a later
    # per-port line would have to partially override.
    assert not any("$klt_non_clock_inputs" in ln for ln in lines)
    assert not any(ln.startswith("set_output_delay 0.1") for ln in lines)


def test_port_io_delay_lines_without_scalar_emits_no_default():
    lines = post_route_sta._port_io_delay_lines(
        "clk", None, None, {"a": {"max_ns": 1.0}}, None
    )
    assert lines == ["set_input_delay -max 1.0 -clock clk [get_ports a]"]


@pytest.mark.parametrize(
    ("port", "tcl"),
    [
        ("data[3]", "data\\[3\\]"),
        ("a$b", "a\\$b"),
        ("x{y}", "x\\{y\\}"),
        ('q"r', 'q\\"r'),
        ("s;t#u", "s\\;t\\#u"),
        ("v\\w", "v\\\\w"),
        ("sp ace", "sp\\ ace"),
        ("u_sub/p", "u_sub/p"),
    ],
)
def test_tcl_literal_port_names_never_substituted(port, tcl):
    assert post_route_sta._tcl_literal(port) == tcl
    lines = post_route_sta._port_io_delay_lines(
        "clk", None, None, {port: {"min_ns": 0.1}}, None
    )
    assert lines == [f"set_input_delay -min 0.1 -clock clk [get_ports {tcl}]"]


def test_port_check_lines_list_every_key_with_allowed_directions():
    lines = post_route_sta._port_check_lines(
        _BOUNDARY_INPUT_DELAYS, _BOUNDARY_OUTPUT_DELAYS
    )
    script = "\n".join(lines)
    assert (
        "[list input a_in {input inout bidirect} input b_in {input inout bidirect} "
        "input bus\\[1\\] {input inout bidirect} output q_a {output inout bidirect}]"
    ) in script
    assert f'puts "{post_route_sta._PORT_CHECK_BEGIN}"' in lines
    assert f"    exit {post_route_sta._PORT_CHECK_EXIT_CODE}" in lines
    # Literal match only: exactly one port, whose full name equals the key.
    assert "[get_full_name $klt_port] ne $klt_name" in script


@pytest.mark.parametrize("mode", ["def", "verilog"])
def test_run_sta_both_modes_apply_same_port_semantics(tmp_path, monkeypatch, mode):
    setup = _setup_success_env if mode == "def" else _setup_verilog_success_env
    request_path = setup(tmp_path, monkeypatch, constraints=_boundary_constraints())
    scripts = _stub_openroad_capture(monkeypatch, {None: "boundary_tt_025C_1v80"})

    run_sta(request_path)

    lines = scripts[0].splitlines()
    expected = post_route_sta._request_io_delay_lines(
        "clk", 0.0, 0.1, _BOUNDARY_INPUT_DELAYS, _BOUNDARY_OUTPUT_DELAYS
    )
    start = lines.index(expected[0])
    assert lines[start : start + len(expected)] == expected
    clock_idx = next(i for i, ln in enumerate(lines) if ln.startswith("create_clock"))
    assert clock_idx < start
    # The port check precedes every per-port delay write.
    assert lines.index(f'puts "{post_route_sta._PORT_CHECK_END}"') < lines.index(
        "set_input_delay -min 0.3 -clock clk [get_ports a_in]"
    )


def test_run_sta_corner_sweep_applies_same_port_tcl_per_corner(tmp_path, monkeypatch):
    request_path = _setup_multi_corner_env(
        tmp_path, monkeypatch, constraints=_boundary_constraints()
    )
    scripts = _stub_openroad_capture(
        monkeypatch,
        {
            "tt_025C_1v80": "boundary_tt_025C_1v80",
            "ss_100C_1v60": "boundary_ss_100C_1v60",
        },
    )

    report = run_sta(request_path)

    expected = "\n".join(
        post_route_sta._request_io_delay_lines(
            "clk", 0.0, 0.1, _BOUNDARY_INPUT_DELAYS, _BOUNDARY_OUTPUT_DELAYS
        )
    )
    assert len(scripts) == 2
    assert all(expected in script for script in scripts)
    tt_entry, ss_entry = report["corners"]
    # Distinct per-corner identities/slacks from each corner's own session.
    assert tt_entry["worst_hold_path"]["slack_ns"] == -0.29559
    assert ss_entry["worst_hold_path"]["slack_ns"] == -0.6148
    assert tt_entry["worst_setup_path"]["slack_ns"] == 0.69875
    assert ss_entry["worst_setup_path"]["slack_ns"] == 0.54129


def test_run_sta_scalar_only_request_tcl_unchanged(tmp_path, monkeypatch):
    """No maps: the I/O-delay Tcl is exactly the pre-#2740 scalar block and
    no port check is emitted."""
    request_path = _setup_success_env(
        tmp_path, monkeypatch, constraints=dict(_IO_DELAY_CONSTRAINTS)
    )
    _stub_openroad_success(monkeypatch)

    run_sta(request_path)

    script = _sta_script_text(tmp_path)
    assert "\n".join([*_IO_INPUT_DELAY_LINES, _IO_OUTPUT_DELAY_LINE]) in script
    assert post_route_sta._PORT_CHECK_BEGIN not in script
    assert "klt_input_mapped" not in script


# -- engine port check ------------------------------------------------------ #


def test_run_sta_rejected_ports_raise_actionable_error(tmp_path, monkeypatch):
    """Real engine output for a missing port, a whole-bus name, a wildcard
    and wrong-direction entries on both maps."""
    captured = _WORST_PATH_CAPTURES["scenarios"]["bad_ports_tt_025C_1v80"]
    request_path = _setup_success_env(
        tmp_path,
        monkeypatch,
        constraints=_boundary_constraints(
            input_delays=captured["input_delays"],
            output_delays=captured["output_delays"],
        ),
    )
    _stub_openroad_capture(monkeypatch, {None: "bad_ports_tt_025C_1v80"})

    with pytest.raises(PostRouteStaError) as exc_info:
        run_sta(request_path)

    message = str(exc_info.value)
    assert message.startswith("per-port I/O delay constraint(s) rejected")
    assert "input_delays['nope']: no port with exactly this name" in message
    assert "input_delays['bus']: matches 2 ports" in message
    assert "input_delays['b*']: matches 3 ports" in message
    assert "input_delays['q_a']: port direction is 'output'" in message
    assert "output_delays['a_in']: port direction is 'input'" in message
    assert "'a_in']: " not in message.split("output_delays")[0]


def test_parse_port_check_errors_empty_block_and_absent_markers():
    clean = _WORST_PATH_CAPTURES["scenarios"]["boundary_tt_025C_1v80"]["stdout"]
    assert post_route_sta._parse_port_check_errors(clean) == []
    assert post_route_sta._parse_port_check_errors("") == []


# -- worst-path identity ---------------------------------------------------- #


def test_worst_path_lines_query_each_group_and_catch_errors():
    lines = post_route_sta._worst_path_lines()
    script = "\n".join(lines)
    assert (
        "find_timing_paths -path_delay $klt_path_side "
        "-group_path_count 1 -endpoint_path_count 1"
    ) in script
    assert "[$klt_pe check_role]" in script
    assert lines[0] == f'puts "{post_route_sta._WORST_PATHS_BEGIN}"'
    assert lines[-1] == f'puts "{post_route_sta._WORST_PATHS_END}"'
    assert lines[1] == "if {[catch {"


def test_run_sta_reports_removal_not_data_hold_as_worst_hold(tmp_path, monkeypatch):
    """The issue's own symptom, on real engine output: the worst hold check
    at the corner is the async-reset *removal* check, reported as such --
    not as data hold -- while the boundary data ports carry their own
    (better) slacks."""
    request_path = _setup_verilog_success_env(
        tmp_path, monkeypatch, constraints=_boundary_constraints()
    )
    _stub_openroad_capture(monkeypatch, {None: "boundary_tt_025C_1v80"})

    report = run_sta(request_path)

    assert report["worst_hold_path"] == {
        "status": "ok",
        "check_type": "removal",
        "engine_check_role": "removal",
        "startpoint": "rst_n",
        "endpoint": "ff_a/RESET_B",
        "slack_ns": -0.29559,
        "consistent_with_aggregate": True,
        "reason": None,
    }
    assert report["worst_setup_path"] == {
        "status": "ok",
        "check_type": "setup",
        "engine_check_role": "setup",
        "startpoint": "b_in",
        "endpoint": "ff_b/D",
        "slack_ns": 0.69875,
        "consistent_with_aggregate": True,
        "reason": None,
    }
    # Aggregate contracts are untouched.
    assert report["worst_hold_slack_ns"] == -0.29559
    assert report["worst_slack_ns"] == 0.69875
    assert report["hold_violation_count"] == 2
    assert report["setup_violation_count"] == 0
    assert report["timing_status"] == "constrained"


def test_captured_worst_paths_match_direct_engine_queries():
    """The selected records agree with the engine's direct per-port and
    global queries made against the same constraints written as plain SDC
    (fixture `direct_queries`)."""
    direct = _WORST_PATH_CAPTURES["direct_queries"]["tt_025C_1v80"]
    stdout = _WORST_PATH_CAPTURES["scenarios"]["boundary_tt_025C_1v80"]["stdout"]
    setup, hold = post_route_sta._worst_paths(
        stdout, direct["worst_slack_max_s"] * 1e9, direct["worst_slack_min_s"] * 1e9
    )
    b_in_max = direct["from"]["b_in"]["max"]
    rst_min = direct["from"]["rst_n"]["min"]
    assert (setup["engine_check_role"], setup["endpoint"]) == (b_in_max[0], b_in_max[2])
    assert setup["slack_ns"] == round(b_in_max[1] * 1e9, 5)
    assert (hold["engine_check_role"], hold["endpoint"]) == (rst_min[0], rst_min[2])
    assert hold["slack_ns"] == round(rst_min[1] * 1e9, 5)
    # Per-port semantics as the engine saw them: the min-only bus bit has
    # no setup path at all; the defaulted sibling bit has both sides.
    assert direct["from"]["bus[1]"]["max"] is None
    assert direct["from"]["bus[0]"]["max"] is not None


def test_run_sta_unconstrained_reports_no_paths(tmp_path, monkeypatch):
    request_path = _setup_verilog_success_env(tmp_path, monkeypatch)
    _stub_openroad_capture(monkeypatch, {None: "unconstrained_tt_025C_1v80"})

    report = run_sta(request_path)

    assert report["timing_status"] == "unconstrained"
    for key in ("worst_setup_path", "worst_hold_path"):
        assert report[key]["status"] == "no_paths"
        assert report[key]["startpoint"] is None
        assert report[key]["check_type"] is None
        assert report[key]["slack_ns"] is None


@pytest.mark.parametrize(
    ("block", "reason"),
    [
        (None, "absent from engine output"),
        (['ERROR\tinvalid command name "find_timing_paths"'], "introspection failed"),
        (["PATH\tmax\tsetup\t1e-10\tonly_start"], "malformed worst-path record"),
        (["PATH\tmax\tsetup\tfast\ta\tb"], "malformed worst-path slack"),
        (["PATH\tsideways\tsetup\t1e-10\ta\tb"], "malformed worst-path record"),
        (["PATH\tmax\tsetup\tinf\ta\tb"], "non-finite"),
    ],
)
def test_worst_paths_unavailable_never_fabricated(block, reason):
    if block is None:
        stdout = "no markers here"
    else:
        stdout = "\n".join(
            [post_route_sta._WORST_PATHS_BEGIN, *block, post_route_sta._WORST_PATHS_END]
        )
    setup, hold = post_route_sta._worst_paths(stdout, -0.1, 0.2)
    for record in (setup, hold):
        assert record["status"] == "unavailable"
        assert reason in record["reason"]
        assert record["startpoint"] is None
        assert record["endpoint"] is None
        assert record["check_type"] is None
        assert record["slack_ns"] is None


def _path_block(*rows: str) -> str:
    return "\n".join(
        [post_route_sta._WORST_PATHS_BEGIN, *rows, post_route_sta._WORST_PATHS_END]
    )


@pytest.mark.parametrize(
    ("role", "check_type"),
    [
        ("setup", "setup"),
        ("recovery", "recovery"),
        ("clock gating setup", "clock_gating_setup"),
        ("output setup", "output_setup"),
        ("data check setup", "unknown"),
    ],
)
def test_worst_path_check_type_mapping(role, check_type):
    setup, _ = post_route_sta._worst_paths(
        _path_block(f"PATH\tmax\t{role}\t-1e-10\ta\tb"), -0.1, None
    )
    assert setup["check_type"] == check_type
    assert setup["engine_check_role"] == role


def test_worst_path_selects_minimum_regardless_of_engine_order():
    stdout = _path_block(
        "PATH\tmin\thold\t2e-10\td\tff/D",
        "PATH\tmin\tremoval\t-3e-10\trst\tff/RESET_B",
        "PATH\tmin\tclock gating hold\t1e-10\ten\tcg/B",
    )
    _, hold = post_route_sta._worst_paths(stdout, None, -0.3)
    assert (hold["check_type"], hold["startpoint"], hold["slack_ns"]) == (
        "removal",
        "rst",
        -0.3,
    )
    assert hold["consistent_with_aggregate"] is True


def test_worst_path_inconsistent_with_aggregate_is_flagged():
    stdout = _path_block("PATH\tmax\tsetup\t-1e-10\ta\tff/D")
    setup, hold = post_route_sta._worst_paths(stdout, -0.5, -0.2)
    assert setup["status"] == "inconsistent"
    assert setup["consistent_with_aggregate"] is False
    assert setup["startpoint"] == "a"
    assert "differs from the aggregate WNS -0.5" in setup["reason"]
    # No hold path at all, yet a real hold WNS: inconsistent, no identity.
    assert hold["status"] == "inconsistent"
    assert hold["startpoint"] is None
    assert hold["consistent_with_aggregate"] is False


def test_worst_path_without_aggregate_is_ok_but_unchecked():
    setup, hold = post_route_sta._worst_paths(
        _path_block("PATH\tmax\tsetup\t1e-10\ta\tff/D"), None, None
    )
    assert setup["status"] == "ok"
    assert setup["consistent_with_aggregate"] is None
    assert hold["status"] == "no_paths"


def test_run_sta_without_worst_path_block_preserves_aggregates(tmp_path, monkeypatch):
    """An engine output with no worst-path block (older capture/stub) keeps
    every aggregate field and reports the records as unavailable."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)

    report = run_sta(request_path)

    assert report["worst_slack_ns"] == -0.15
    assert report["setup_violation_count"] == 1
    assert report["worst_setup_path"]["status"] == "unavailable"
    assert report["worst_hold_path"]["status"] == "unavailable"


# -- live engine ------------------------------------------------------------ #


def _live_sky130_env() -> str | None:
    import os
    import shutil

    if shutil.which("openroad") is None:
        return None
    if os.environ.get("KLT_SKIP_OPENROAD_TESTS") == "1":
        return None
    # The documented Docker wrapper only mounts the PDK when PDK_ROOT is set.
    if not os.environ.get("PDK_ROOT"):
        return None
    try:
        post_route_sta._resolve_liberty(
            "sky130_fd_sc_hd", "tt_025C_1v80", variant="sky130A"
        )
    except PostRouteStaError:
        return None
    return "sky130A"


@pytest.mark.skipif(
    _live_sky130_env() is None,
    reason="needs openroad on PATH, PDK_ROOT with a sky130A install, and "
    "KLT_SKIP_OPENROAD_TESTS unset",
)
def test_live_engine_boundary_paths(tmp_path):
    """Engine integration: the same boundary request against a real
    OpenROAD session must reproduce the captured identities."""
    _write(tmp_path / "boundary.v", _WORST_PATH_CAPTURES["netlist"])
    request = {
        "verilog": "boundary.v",
        "hdl_toplevel": "top",
        "pdk": {"cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80"},
        "constraints": _boundary_constraints(),
    }
    request_path = _write_request(tmp_path / "request.json", request)

    report = run_sta(request_path, pdk_variant="sky130A")

    hold = report["worst_hold_path"]
    setup = report["worst_setup_path"]
    assert (hold["check_type"], hold["startpoint"], hold["endpoint"]) == (
        "removal",
        "rst_n",
        "ff_a/RESET_B",
    )
    assert (setup["check_type"], setup["startpoint"], setup["endpoint"]) == (
        "setup",
        "b_in",
        "ff_b/D",
    )
    assert hold["consistent_with_aggregate"] is True
    assert setup["consistent_with_aggregate"] is True
