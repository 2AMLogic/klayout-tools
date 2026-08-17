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

from klayout_tools import pdk as pdk_module
from klayout_tools import post_route_sta
from klayout_tools.cli import main
from klayout_tools.post_route_sta import PostRouteStaError, load_request, run_sta


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


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
) -> Path:
    """Fabricate a minimal open_pdks-layout variant -- mirrors
    `tests/test_place_and_route.py`'s identical `_make_pdk_install`, trimmed
    to the lib/LEF views this module actually resolves (no GDS view --
    `klt sta` never merges a GDS)."""
    variant_dir = root / variant
    (variant_dir / "libs.tech").mkdir(parents=True, exist_ok=True)
    lib_dir = variant_dir / "libs.ref" / cell_library

    if with_lib:
        lib_views_dir = lib_dir / "lib"
        lib_views_dir.mkdir(parents=True, exist_ok=True)
        content = (
            f'    default_operating_conditions : "{corner}";\n'
            "    nom_process : 1.0;\n"
            "    nom_temperature : 25.0;\n"
            "    nom_voltage : 1.8;\n"
        )
        (lib_views_dir / f"{cell_library}__{corner}.lib").write_text(
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


@pytest.mark.parametrize("field", ["def", "pdk", "constraints"])
def test_load_request_missing_required_field(tmp_path, field):
    request = _base_request()
    del request[field]
    path = _write_request(tmp_path / "request.json", request)
    with pytest.raises(PostRouteStaError, match=f"missing required field: {field}"):
        load_request(path)


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


# --------------------------------------------------------------------------- #
# Stubbed-OpenROAD end-to-end: response envelope.
# --------------------------------------------------------------------------- #


_STA_METRICS = {
    "timing__setup__ws": -0.15,
    "timing__setup__tns": -1.2,
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
) -> None:
    metrics = metrics if metrics is not None else _STA_METRICS

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["openroad", "-version"]:
            return _FakeCompleted(stdout=f"{version} \n")
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
            stdout_lines = [
                post_route_sta._SPEF_NET_CHECK_BEGIN,
                f"{a} {b}",
                f"{c} {d}",
                post_route_sta._SPEF_NET_CHECK_END,
                *stdout_lines,
            ]
        return _FakeCompleted(returncode=0, stdout="\n".join(stdout_lines))

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
    assert report["fmax_mhz"] == 500.0
    assert report["setup_violation_count"] == 1
    assert report["hold_violation_count"] == 0
    assert report["clock_skew_ns"] == 0.021
    assert report["estimated_power_mw"] == 8.4
    assert report["def_path"].endswith("top.def")
    assert report["spef_path"] is None
    assert report["spef_annotation"] is None

    provenance = report["provenance"]
    assert provenance["pdk"]["name"] == "sky130A"
    assert provenance["deck"]["name"] == "sky130_fd_sc_hd__tt_025C_1v80"
    assert provenance["input"]["content_hash"] is not None


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


def test_run_sta_with_spef_incomplete_annotation_warns(tmp_path, monkeypatch):
    spef_path = tmp_path / "top.spef"
    spef_path.write_text("*D_NET clk 0.01\n*END\n", encoding="utf-8")
    request_path = _setup_success_env(tmp_path, monkeypatch, spef="top.spef")
    _stub_openroad_success(monkeypatch, spef_check=(1, 1, 3, 10))

    report = run_sta(request_path)

    annotation = report["spef_annotation"]
    assert annotation["annotation_complete"] is False
    assert "only 3 of 10 nets" in annotation["annotation_warning"]


def test_run_sta_engine_failure_raises(tmp_path, monkeypatch):
    request_path = _setup_success_env(tmp_path, monkeypatch)

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["openroad", "-version"]:
            return _FakeCompleted(stdout="26Q3-771-gdeadbeef\n")
        return _FakeCompleted(
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
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


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
