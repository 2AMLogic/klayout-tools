"""Tests for `klt place-and-route` and the `klayout_tools.place_and_route`
library.

Four tiers, mirroring `tests/test_synthesize.py`'s own structure:

- **Request/library unit tests** exercise `load_request`/`run_place_and_route`'s
  validation paths (bad request, floorplan-method conflicts, missing
  seed/constraints, unresolvable `pdk.cell_library`/corner/LEF) directly,
  against **fabricated** open_pdks-layout PDK installs created under
  `tmp_path` (never a real PDK).
- **Stubbed-OpenROAD tests** run `run_place_and_route` end to end with
  `place_and_route.subprocess.run` replaced by a fake that writes the same
  `-metrics <file>.json` shape a real per-stage OpenROAD run produces
  (verified live for issue #425's own worked example -- see
  `place_and_route.py`'s own module docstring) -- no `openroad` binary
  required, covering a full route-stage run, a `target_stage: "place"`
  partial run (success case), and an engine failure partway through a
  stage. The DEF->GDS merge (`_merge_def_to_gds`) is stubbed out for these
  tests (it needs a real DEF + real standard-cell GDS view to exercise
  meaningfully) and covered by its own focused unit tests below.
- **CLI tests** cover exit codes and `--format text/json`.
- **Integration tests** (`@pytest.mark.skipif` when either `openroad` is not
  on `$PATH` or no real PDK install resolving that test's own
  `<cell_library>` LEF/liberty/GDS set is found) run the real GCD worked
  example end to end -- this is the acceptance criterion's "verified end to
  end against a real install" check. There is one per supported
  standard-cell library: `sky130_fd_sc_hd` (issue #425) and
  `gf180mcu_fd_sc_mcu9t5v0` (issue #637), both gated the same way via
  `_find_real_pnr_variant()`. Neither is required for CI (CI installs
  neither `openroad` nor a real standard-cell PDK today, matching `klt
  synthesize`'s own noted CI gap) but each runs on any machine with both
  halves of its toolchain. **This repo's own worked example for issue #425
  instead verified the identical code path manually via a real
  `openroad/orfs` Docker image** (`openroad -no_init -exit -metrics ...`
  against a real volare-fetched `sky130A` install, floorplan through a full
  detailed route with 0 DRC violations, followed by a real DEF->GDS merge
  via this module's own `_merge_def_to_gds` producing a valid GDS) -- see
  the PR description for the full transcript; that manual run is not
  automated as a test here since it depends on Docker, which is not a
  project dependency. The gf180mcu sibling (issue #637) has **not** had an
  equivalent manual run: no `openroad`/Docker/volare and no gf180mcu
  install were available in the environment that added it, so its live
  proof is the gated test above, wherever that toolchain exists.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from klayout_tools import pdk as pdk_module
from klayout_tools import place_and_route
from klayout_tools.cli import main
from klayout_tools.place_and_route import (
    PlaceAndRouteError,
    load_request,
    run_place_and_route,
)

_GCD_RTL = """\
module gcd #(
    parameter WIDTH = 16
) (
    input  wire             clk,
    input  wire             rst_n,
    input  wire              start,
    input  wire [WIDTH-1:0] a_in,
    input  wire [WIDTH-1:0] b_in,
    output reg              done,
    output reg  [WIDTH-1:0] result
);

    reg [WIDTH-1:0] a, b;
    reg             busy;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            a      <= {WIDTH{1'b0}};
            b      <= {WIDTH{1'b0}};
            busy   <= 1'b0;
            done   <= 1'b0;
            result <= {WIDTH{1'b0}};
        end else if (start && !busy) begin
            a      <= a_in;
            b      <= b_in;
            busy   <= 1'b1;
            done   <= 1'b0;
        end else if (busy) begin
            if (b == {WIDTH{1'b0}}) begin
                busy   <= 1'b0;
                done   <= 1'b1;
                result <= a;
            end else if (a > b) begin
                a <= a - b;
            end else begin
                b <= b - a;
            end
        end else begin
            done <= 1'b0;
        end
    end

endmodule
"""


def _write(path: Path, text: str) -> str:
    path.write_text(text, encoding="utf-8")
    return str(path)


def _write_request(path: Path, request: dict) -> str:
    path.write_text(json.dumps(request), encoding="utf-8")
    return str(path)


def _base_request(**overrides) -> dict:
    request = {
        "engine": "openroad",
        "netlist": "gcd_synth.v",
        "hdl_toplevel": "gcd",
        "pdk": {"cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80"},
        "floorplan": {
            "method": "utilization",
            "utilization_pct": 38,
            "aspect_ratio": 1.0,
            "core_margin_um": 2.0,
            "site": "unithd",
        },
        "io": {"layer_h": "met3", "layer_v": "met2"},
        "constraints": {"clock_port": "clk", "clock_period_ns": 1.1},
        "seed": 1,
        "target_stage": "route",
    }
    request.update(overrides)
    return request


def _isolate_pdk(monkeypatch, tmp_path: Path) -> None:
    """Scrub PDK env vars and empty `pdk.py`'s search-space constants, so
    `find_pdk()` only ever resolves what a test explicitly points it at
    (mirrors `tests/test_synthesize.py`'s own fixture)."""
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
    with_gds: bool = True,
) -> Path:
    """Fabricate a minimal open_pdks-layout variant with a standard-cell
    library's `lib/`/`techlef/`/`lef/`/`gds/` views -- enough for
    `run_place_and_route`'s own resolution, never real, engine-parseable
    file contents (the stubbed-OpenROAD tests never invoke a real
    `openroad`; the GDS merge is separately stubbed in those tests)."""
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

    if with_gds:
        gds_dir = lib_dir / "gds"
        gds_dir.mkdir(parents=True, exist_ok=True)
        (gds_dir / f"{cell_library}.gds").write_text("# fake gds\n")

    lib_dir.mkdir(parents=True, exist_ok=True)
    return variant_dir


def _setup_success_env(tmp_path, monkeypatch, **request_overrides) -> str:
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    _write(tmp_path / "gcd_synth.v", "// fake mapped netlist\n")
    return _write_request(tmp_path / "request.json", _base_request(**request_overrides))


# --------------------------------------------------------------------------- #
# `load_request`
# --------------------------------------------------------------------------- #


def test_load_request_missing_file(tmp_path):
    with pytest.raises(PlaceAndRouteError, match="file not found"):
        load_request(str(tmp_path / "nope.json"))


def test_load_request_directory(tmp_path):
    with pytest.raises(PlaceAndRouteError, match="not a file"):
        load_request(str(tmp_path))


def test_load_request_invalid_json(tmp_path):
    path = _write(tmp_path / "request.json", "{not json")
    with pytest.raises(PlaceAndRouteError, match="not valid JSON"):
        load_request(path)


def test_load_request_not_an_object(tmp_path):
    path = _write(tmp_path / "request.json", "[1, 2, 3]")
    with pytest.raises(PlaceAndRouteError, match="must contain a JSON object"):
        load_request(path)


@pytest.mark.parametrize(
    "field", ["netlist", "hdl_toplevel", "pdk", "floorplan", "seed"]
)
def test_load_request_missing_required_field(tmp_path, field):
    request = _base_request()
    del request[field]
    path = _write_request(tmp_path / "request.json", request)
    with pytest.raises(PlaceAndRouteError, match=f"missing required field: {field}"):
        load_request(path)


# --------------------------------------------------------------------------- #
# `run_place_and_route` request validation (no PDK/OpenROAD involved)
# --------------------------------------------------------------------------- #


def test_run_unsupported_engine(tmp_path):
    _write(tmp_path / "gcd_synth.v", "// netlist\n")
    request_path = _write_request(
        tmp_path / "request.json", _base_request(engine="siemens-tool")
    )
    with pytest.raises(PlaceAndRouteError, match="unsupported engine 'siemens-tool'"):
        run_place_and_route(request_path)


def test_run_netlist_not_found(tmp_path):
    request_path = _write_request(
        tmp_path / "request.json", _base_request(netlist="nope.v")
    )
    with pytest.raises(PlaceAndRouteError, match="netlist not found: nope.v"):
        run_place_and_route(request_path)


def test_run_hdl_toplevel_must_be_nonempty_string(tmp_path):
    _write(tmp_path / "gcd_synth.v", "// netlist\n")
    request_path = _write_request(
        tmp_path / "request.json", _base_request(hdl_toplevel="")
    )
    with pytest.raises(PlaceAndRouteError, match="hdl_toplevel must be"):
        run_place_and_route(request_path)


def test_run_cell_library_required(tmp_path):
    _write(tmp_path / "gcd_synth.v", "// netlist\n")
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(pdk={"corner": "tt_025C_1v80"}),
    )
    with pytest.raises(PlaceAndRouteError, match="pdk.cell_library is required"):
        run_place_and_route(request_path)


def test_run_seed_must_be_integer(tmp_path):
    _write(tmp_path / "gcd_synth.v", "// netlist\n")
    request_path = _write_request(tmp_path / "request.json", _base_request(seed="1"))
    with pytest.raises(PlaceAndRouteError, match="seed must be an integer"):
        run_place_and_route(request_path)


def test_run_target_stage_must_be_valid(tmp_path):
    _write(tmp_path / "gcd_synth.v", "// netlist\n")
    request_path = _write_request(
        tmp_path / "request.json", _base_request(target_stage="cts_and_beyond")
    )
    with pytest.raises(PlaceAndRouteError, match="target_stage must be one of"):
        run_place_and_route(request_path)


# --------------------------------------------------------------------------- #
# Floorplan method validation -- the "reject more than one method" rule,
# mirroring OpenROAD-flow-scripts' own `methods_defined > 1` check.
# --------------------------------------------------------------------------- #


def test_run_floorplan_method_required(tmp_path):
    _write(tmp_path / "gcd_synth.v", "// netlist\n")
    request_path = _write_request(
        tmp_path / "request.json", _base_request(floorplan={"utilization_pct": 38})
    )
    with pytest.raises(PlaceAndRouteError, match="floorplan.method must be one of"):
        run_place_and_route(request_path)


def test_run_floorplan_more_than_one_method_rejected(tmp_path):
    _write(tmp_path / "gcd_synth.v", "// netlist\n")
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(
            floorplan={
                "method": "utilization",
                "utilization_pct": 38,
                "site": "unithd",
                # Stray field from the "def" method alongside "utilization"'s
                # own fields -- this is the rejection this test targets.
                "def_path": "existing.def",
            }
        ),
    )
    with pytest.raises(PlaceAndRouteError, match="more than one floorplan method"):
        run_place_and_route(request_path)


def test_run_floorplan_utilization_requires_utilization_pct(tmp_path):
    _write(tmp_path / "gcd_synth.v", "// netlist\n")
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(floorplan={"method": "utilization", "site": "unithd"}),
    )
    with pytest.raises(PlaceAndRouteError, match="requires: utilization_pct"):
        run_place_and_route(request_path)


def test_run_floorplan_utilization_requires_site(tmp_path):
    _write(tmp_path / "gcd_synth.v", "// netlist\n")
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(floorplan={"method": "utilization", "utilization_pct": 38}),
    )
    with pytest.raises(PlaceAndRouteError, match="floorplan.site is required"):
        run_place_and_route(request_path)


def test_run_floorplan_explicit_requires_die_and_core_area(tmp_path):
    _write(tmp_path / "gcd_synth.v", "// netlist\n")
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(floorplan={"method": "explicit", "site": "unithd"}),
    )
    with pytest.raises(PlaceAndRouteError, match="requires: die_area_um, core_area_um"):
        run_place_and_route(request_path)


def test_run_floorplan_explicit_area_must_be_4_numbers(tmp_path):
    _write(tmp_path / "gcd_synth.v", "// netlist\n")
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(
            floorplan={
                "method": "explicit",
                "site": "unithd",
                "die_area_um": [0, 0, 92.13],
                "core_area_um": [2.3, 2.72, 89.7, 89.76],
            }
        ),
    )
    with pytest.raises(PlaceAndRouteError, match="die_area_um must be an array"):
        run_place_and_route(request_path)


def test_run_floorplan_def_method_requires_def_path(tmp_path):
    _write(tmp_path / "gcd_synth.v", "// netlist\n")
    request_path = _write_request(
        tmp_path / "request.json", _base_request(floorplan={"method": "def"})
    )
    with pytest.raises(PlaceAndRouteError, match="requires: def_path"):
        run_place_and_route(request_path)


# --------------------------------------------------------------------------- #
# constraints / io / stage-dependent requirements
# --------------------------------------------------------------------------- #


def test_run_constraints_clock_port_and_period_required_together(tmp_path):
    _write(tmp_path / "gcd_synth.v", "// netlist\n")
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(constraints={"clock_port": "clk"}),
    )
    with pytest.raises(PlaceAndRouteError, match="clock_period_ns must be"):
        run_place_and_route(request_path)


def test_run_target_stage_place_requires_clock(tmp_path, monkeypatch):
    request_path = _setup_success_env(
        tmp_path,
        monkeypatch,
        target_stage="place",
        constraints=None,
    )
    with pytest.raises(
        PlaceAndRouteError, match="required to reach target_stage 'place'"
    ):
        run_place_and_route(request_path)


def test_run_target_stage_place_requires_io(tmp_path, monkeypatch):
    request_path = _setup_success_env(
        tmp_path,
        monkeypatch,
        target_stage="place",
        io=None,
    )
    with pytest.raises(PlaceAndRouteError, match="io.layer_h/layer_v are required"):
        run_place_and_route(request_path)


def test_run_target_stage_floorplan_does_not_require_clock(tmp_path, monkeypatch):
    """A `target_stage: "floorplan"` request needs no clock/io -- those are
    only meaningful from the "place" stage onward."""
    request_path = _setup_success_env(
        tmp_path,
        monkeypatch,
        target_stage="floorplan",
        constraints=None,
        io=None,
    )
    _stub_openroad_success(monkeypatch, stages=("floorplan",))

    report = run_place_and_route(request_path)

    assert report["stage_reached"] == "floorplan"


# --------------------------------------------------------------------------- #
# PDK / LEF / liberty resolution failures
# --------------------------------------------------------------------------- #


def test_run_no_pdk_installed(tmp_path, monkeypatch):
    _isolate_pdk(monkeypatch, tmp_path)
    _write(tmp_path / "gcd_synth.v", "// netlist\n")
    request_path = _write_request(tmp_path / "request.json", _base_request())
    with pytest.raises(PlaceAndRouteError, match="no supported-layout PDK install"):
        run_place_and_route(request_path)


def test_run_liberty_not_found(tmp_path, monkeypatch):
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A", with_lib=False)
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    _write(tmp_path / "gcd_synth.v", "// netlist\n")
    request_path = _write_request(tmp_path / "request.json", _base_request())
    with pytest.raises(PlaceAndRouteError, match="liberty not found for deck"):
        run_place_and_route(request_path)


def test_run_lef_not_found(tmp_path, monkeypatch):
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A", with_lef=False)
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    _write(tmp_path / "gcd_synth.v", "// netlist\n")
    request_path = _write_request(tmp_path / "request.json", _base_request())
    with pytest.raises(PlaceAndRouteError, match="LEF not found for deck"):
        run_place_and_route(request_path)


def test_run_unknown_cell_library_rejects_cts_stage(tmp_path, monkeypatch):
    """A `cell_library` with liberty/LEF assets but no
    `_CTS_BUFFER_CELLS` entry still fails with the existing clear error once
    a run reaches the `cts` stage -- never a silent guess (issue #629)."""
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "acmeA", cell_library="acme_fd_sc_hd")
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    _write(tmp_path / "gcd_synth.v", "// netlist\n")
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(
            pdk={"cell_library": "acme_fd_sc_hd", "corner": "tt_025C_1v80"},
            target_stage="cts",
        ),
    )
    with pytest.raises(
        PlaceAndRouteError,
        match=(
            "no clock-tree buffer cell known for standard-cell library 'acme_fd_sc_hd'"
        ),
    ):
        run_place_and_route(request_path)


def test_run_unknown_cell_library_rejects_route_stage(tmp_path, monkeypatch):
    """Same as above, one stage further -- a `cell_library` with a known CTS
    buffer but no `_ROUTING_LAYER_RANGE` entry still fails clearly when a run
    reaches `route` (issue #629). The CTS table is patched with a fake entry
    so the earlier `cts`-stage check does not mask the routing-layer check
    this test targets."""
    _isolate_pdk(monkeypatch, tmp_path)
    monkeypatch.setitem(
        place_and_route._CTS_BUFFER_CELLS, "acme_fd_sc_hd", "acme_fd_sc_hd__buf_4"
    )
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "acmeA", cell_library="acme_fd_sc_hd")
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    _write(tmp_path / "gcd_synth.v", "// netlist\n")
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(pdk={"cell_library": "acme_fd_sc_hd", "corner": "tt_025C_1v80"}),
    )
    with pytest.raises(
        PlaceAndRouteError,
        match="no routing-layer range known for standard-cell library 'acme_fd_sc_hd'",
    ):
        run_place_and_route(request_path)


def test_run_unknown_cell_library_rejects_route_stage_missing_antenna_diode_cell(
    tmp_path, monkeypatch
):
    """Same as above, one check further -- a `cell_library` with a known CTS
    buffer and routing-layer range but no `_ANTENNA_DIODE_CELLS` entry still
    fails clearly when a run reaches `route` (issue #759), never a silent
    guess. Both earlier tables are patched with fake entries so their own
    checks do not mask the antenna-diode check this test targets."""
    _isolate_pdk(monkeypatch, tmp_path)
    monkeypatch.setitem(
        place_and_route._CTS_BUFFER_CELLS, "acme_fd_sc_hd", "acme_fd_sc_hd__buf_4"
    )
    monkeypatch.setitem(
        place_and_route._ROUTING_LAYER_RANGE, "acme_fd_sc_hd", "met1-met5"
    )
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "acmeA", cell_library="acme_fd_sc_hd")
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    _write(tmp_path / "gcd_synth.v", "// netlist\n")
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(pdk={"cell_library": "acme_fd_sc_hd", "corner": "tt_025C_1v80"}),
    )
    with pytest.raises(
        PlaceAndRouteError,
        match="no antenna-diode cell known for standard-cell library 'acme_fd_sc_hd'",
    ):
        run_place_and_route(request_path)


# --------------------------------------------------------------------------- #
# `request.macros` validation (issue #438, Epic #393 Phase 2 Capability A)
# --------------------------------------------------------------------------- #


def _write_macro_lef(path: Path, macro_name: str = "analog_block") -> None:
    path.write_text(
        "VERSION 5.7 ;\n"
        f"MACRO {macro_name}\n"
        "  CLASS BLOCK ;\n"
        "  SIZE 5.0 BY 5.0 ;\n"
        "  PIN VOUT\n"
        "    DIRECTION OUTPUT ;\n"
        "    USE ANALOG ;\n"
        "  END VOUT\n"
        f"END {macro_name}\n"
        "END LIBRARY\n",
        encoding="utf-8",
    )


def test_macros_field_absent_validates_to_empty_list(tmp_path):
    assert place_and_route._validate_macros(None, str(tmp_path)) == []


def test_macros_field_must_be_a_list(tmp_path):
    with pytest.raises(PlaceAndRouteError, match="request.macros must be a list"):
        place_and_route._validate_macros({"not": "a list"}, str(tmp_path))


def test_macro_entry_must_be_an_object(tmp_path):
    with pytest.raises(
        PlaceAndRouteError, match=r"request\.macros\[0\] must be an object"
    ):
        place_and_route._validate_macros(["not an object"], str(tmp_path))


def test_macro_instance_required(tmp_path):
    _write_macro_lef(tmp_path / "m.lef")
    with pytest.raises(PlaceAndRouteError, match="instance is required"):
        place_and_route._validate_macros(
            [{"lef": "m.lef", "x_um": 1, "y_um": 1}], str(tmp_path)
        )


def test_macro_lef_must_exist(tmp_path):
    with pytest.raises(PlaceAndRouteError, match="lef not found"):
        place_and_route._validate_macros(
            [{"instance": "u1", "lef": "missing.lef", "x_um": 1, "y_um": 1}],
            str(tmp_path),
        )


def test_macro_lef_must_declare_exactly_one_macro(tmp_path):
    (tmp_path / "empty.lef").write_text(
        "VERSION 5.7 ;\nEND LIBRARY\n", encoding="utf-8"
    )
    with pytest.raises(PlaceAndRouteError, match="must declare exactly one MACRO"):
        place_and_route._validate_macros(
            [{"instance": "u1", "lef": "empty.lef", "x_um": 1, "y_um": 1}],
            str(tmp_path),
        )


def test_macro_x_y_um_required_numbers(tmp_path):
    _write_macro_lef(tmp_path / "m.lef")
    with pytest.raises(PlaceAndRouteError, match=r"x_um is required"):
        place_and_route._validate_macros(
            [{"instance": "u1", "lef": "m.lef", "y_um": 1}], str(tmp_path)
        )


def test_macro_orientation_must_be_valid(tmp_path):
    _write_macro_lef(tmp_path / "m.lef")
    with pytest.raises(PlaceAndRouteError, match="orientation must be one of"):
        place_and_route._validate_macros(
            [
                {
                    "instance": "u1",
                    "lef": "m.lef",
                    "x_um": 1,
                    "y_um": 1,
                    "orientation": "SIDEWAYS",
                }
            ],
            str(tmp_path),
        )


def test_macro_orientation_defaults_to_r0(tmp_path):
    _write_macro_lef(tmp_path / "m.lef")
    macros = place_and_route._validate_macros(
        [{"instance": "u1", "lef": "m.lef", "x_um": 1, "y_um": 2}], str(tmp_path)
    )
    assert macros[0]["orientation"] == "R0"
    assert macros[0]["cell_name"] == "analog_block"


def test_macro_gds_must_exist_when_given(tmp_path):
    _write_macro_lef(tmp_path / "m.lef")
    with pytest.raises(PlaceAndRouteError, match="gds not found"):
        place_and_route._validate_macros(
            [
                {
                    "instance": "u1",
                    "lef": "m.lef",
                    "x_um": 1,
                    "y_um": 1,
                    "gds": "missing.gds",
                }
            ],
            str(tmp_path),
        )


def test_duplicate_macro_instance_names_rejected(tmp_path):
    _write_macro_lef(tmp_path / "m.lef")
    with pytest.raises(PlaceAndRouteError, match="instance values must be unique"):
        place_and_route._validate_macros(
            [
                {"instance": "u1", "lef": "m.lef", "x_um": 1, "y_um": 1},
                {"instance": "u1", "lef": "m.lef", "x_um": 2, "y_um": 2},
            ],
            str(tmp_path),
        )


# --------------------------------------------------------------------------- #
# Macro-pin routability cross-check (issue #464): a LEF pin with no `PORT`
# geometry at all (e.g. a device gate pin on bare poly `klt lef-abstract`
# emitted with `geometry_source: "none"`) that is actually wired into the
# netlist must be rejected before OpenROAD is invoked, rather than surfacing
# only as an opaque `GRT-0029` mid-route.
# --------------------------------------------------------------------------- #


def _write_macro_lef_with_port_less_pin(
    path: Path, macro_name: str = "diff_pair_0"
) -> None:
    """A macro LEF with one routable pin (`S`, real `PORT` on `li1`) and one
    PORT-less pin (`G`, no `PORT` at all) -- mirrors what `klt lef-abstract`
    emits for a device gate pin on bare poly (issue #464's own repro)."""
    path.write_text(
        "VERSION 5.7 ;\n"
        f"MACRO {macro_name}\n"
        "  CLASS BLOCK ;\n"
        "  SIZE 5.0 BY 5.0 ;\n"
        "  PIN S\n"
        "    DIRECTION INOUT ;\n"
        "    USE ANALOG ;\n"
        "    PORT\n"
        "      LAYER li1 ;\n"
        "        RECT 0.0 0.0 0.2 0.2 ;\n"
        "    END\n"
        "  END S\n"
        "  PIN G\n"
        "    DIRECTION INOUT ;\n"
        "    USE ANALOG ;\n"
        "  END G\n"
        f"END {macro_name}\n"
        "END LIBRARY\n",
        encoding="utf-8",
    )


def _write_structural_netlist(
    path: Path,
    *,
    cell_name: str = "diff_pair_0",
    instance_name: str = "u_analog",
    connections: dict[str, str],
) -> None:
    ports = ",\n    ".join(f".{port}({net})" for port, net in connections.items())
    path.write_text(
        "module top (clk);\n"
        "  input clk;\n"
        "  wire net_g, net_s;\n"
        f"  {cell_name} {instance_name} (\n"
        f"    {ports}\n"
        "  );\n"
        "endmodule\n",
        encoding="utf-8",
    )


def test_macro_pin_with_no_port_wired_into_netlist_is_rejected(tmp_path):
    _write_macro_lef_with_port_less_pin(tmp_path / "m.lef")
    netlist_path = tmp_path / "top.v"
    _write_structural_netlist(netlist_path, connections={"S": "net_s", "G": "net_g"})

    with pytest.raises(PlaceAndRouteError, match="pin 'G' has no PORT geometry"):
        place_and_route._validate_macros(
            [{"instance": "u_analog", "lef": "m.lef", "x_um": 1, "y_um": 1}],
            str(tmp_path),
            str(netlist_path),
        )


def test_macro_pin_with_no_port_left_unconnected_is_not_rejected(tmp_path):
    _write_macro_lef_with_port_less_pin(tmp_path / "m.lef")
    netlist_path = tmp_path / "top.v"
    _write_structural_netlist(netlist_path, connections={"S": "net_s", "G": ""})

    macros = place_and_route._validate_macros(
        [{"instance": "u_analog", "lef": "m.lef", "x_um": 1, "y_um": 1}],
        str(tmp_path),
        str(netlist_path),
    )
    assert macros[0]["instance"] == "u_analog"


def test_macro_pin_with_no_port_absent_from_port_list_is_not_rejected(tmp_path):
    """The netlist's own instantiation simply never names the PORT-less pin
    at all (e.g. an internally-terminated node the RTL author never
    exposed) -- not an error; only an *actually wired* PORT-less pin is."""
    _write_macro_lef_with_port_less_pin(tmp_path / "m.lef")
    netlist_path = tmp_path / "top.v"
    _write_structural_netlist(netlist_path, connections={"S": "net_s"})

    macros = place_and_route._validate_macros(
        [{"instance": "u_analog", "lef": "m.lef", "x_um": 1, "y_um": 1}],
        str(tmp_path),
        str(netlist_path),
    )
    assert macros[0]["instance"] == "u_analog"


def test_macro_pin_with_a_port_is_never_flagged_even_when_wired(tmp_path):
    """Edge case from the issue's own test plan: a pin with a real `PORT`
    (regardless of which routing layer it lands on) must never be flagged,
    even though it is wired -- only a `PORT`-less pin is a routability
    problem."""
    _write_macro_lef_with_port_less_pin(tmp_path / "m.lef")
    netlist_path = tmp_path / "top.v"
    _write_structural_netlist(netlist_path, connections={"S": "net_s", "G": ""})

    # No exception -- `S` (which has a real PORT) is wired, `G` (PORT-less)
    # is left unconnected.
    place_and_route._validate_macros(
        [{"instance": "u_analog", "lef": "m.lef", "x_um": 1, "y_um": 1}],
        str(tmp_path),
        str(netlist_path),
    )


def test_macro_cross_check_skipped_without_netlist_path(tmp_path):
    """`netlist_path=None` (this module's own direct unit tests above all
    rely on this default) skips the cross-check entirely -- even a pin that
    *would* be flagged if checked raises nothing."""
    _write_macro_lef_with_port_less_pin(tmp_path / "m.lef")
    macros = place_and_route._validate_macros(
        [{"instance": "u_analog", "lef": "m.lef", "x_um": 1, "y_um": 1}],
        str(tmp_path),
    )
    assert macros[0]["instance"] == "u_analog"


def test_macro_cross_check_skipped_when_instance_not_found_in_netlist(tmp_path):
    """A placeholder/non-Verilog netlist (or one that simply never
    instantiates this macro under this exact name) cannot be confidently
    parsed -- the cross-check is skipped rather than risk a false result,
    mirroring every pre-existing stubbed-OpenROAD macro test's own
    `// fake mapped netlist` fixture."""
    _write_macro_lef_with_port_less_pin(tmp_path / "m.lef")
    netlist_path = tmp_path / "top.v"
    netlist_path.write_text("// fake mapped netlist\n", encoding="utf-8")

    macros = place_and_route._validate_macros(
        [{"instance": "u_analog", "lef": "m.lef", "x_um": 1, "y_um": 1}],
        str(tmp_path),
        str(netlist_path),
    )
    assert macros[0]["instance"] == "u_analog"


def test_macro_instance_port_connections_parses_named_ports(tmp_path):
    netlist_path = tmp_path / "top.v"
    _write_structural_netlist(
        netlist_path, connections={"S": "net_s", "D": "net_d", "G": ""}
    )
    connections = place_and_route._macro_instance_port_connections(
        str(netlist_path), "diff_pair_0", "u_analog"
    )
    assert connections == {"S": "net_s", "D": "net_d", "G": ""}


def test_macro_instance_port_connections_none_when_instance_absent(tmp_path):
    netlist_path = tmp_path / "top.v"
    netlist_path.write_text("module top; endmodule\n", encoding="utf-8")
    assert (
        place_and_route._macro_instance_port_connections(
            str(netlist_path), "diff_pair_0", "u_analog"
        )
        is None
    )


def test_full_route_rejects_wired_port_less_macro_pin_before_openroad(
    tmp_path, monkeypatch
):
    """End-to-end: a `request.macros` entry whose LEF has a PORT-less pin
    that the real netlist wires up is rejected at request-validation time --
    `openroad` is never invoked (the `subprocess.run` stub below asserts
    this by raising if called at all)."""
    macro_lef = tmp_path / "diff_pair_0.lef"
    _write_macro_lef_with_port_less_pin(macro_lef, "diff_pair_0")

    def fail_if_called(cmd, **kwargs):
        raise AssertionError(f"OpenROAD must not be invoked: {cmd}")

    monkeypatch.setattr(place_and_route.subprocess, "run", fail_if_called)

    request_path = _setup_success_env(
        tmp_path,
        monkeypatch,
        macros=[
            {
                "instance": "u_analog",
                "lef": "diff_pair_0.lef",
                "x_um": 12.5,
                "y_um": 3.0,
            }
        ],
    )
    _write_structural_netlist(
        tmp_path / "gcd_synth.v",
        cell_name="diff_pair_0",
        instance_name="u_analog",
        connections={"S": "net_s", "G": "net_g"},
    )

    with pytest.raises(PlaceAndRouteError, match="pin 'G' has no PORT geometry"):
        run_place_and_route(request_path)


# --------------------------------------------------------------------------- #
# Stubbed OpenROAD: full `run_place_and_route` without a real `openroad`
# binary. The stub writes the same `-metrics <file>.json` shape a real
# per-stage run produces (verified live -- see `place_and_route.py`'s own
# module docstring), parsed from the generated `.tcl` script's own
# `write_db`/`write_def` lines, so the stub never hardcodes this module's
# private path-naming scheme.
# --------------------------------------------------------------------------- #

_WRITE_DB_RE = re.compile(r"^write_db (\S+)$")
_WRITE_DEF_RE = re.compile(r"^write_def (\S+)$")


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _script_lines(script_path: str) -> list[str]:
    with open(script_path, encoding="utf-8") as handle:
        return [line.rstrip("\n") for line in handle]


def _script_write_db_path(script_path: str) -> str:
    for line in _script_lines(script_path):
        match = _WRITE_DB_RE.match(line)
        if match:
            return match.group(1)
    raise AssertionError(f"no write_db line in {script_path}")


def _script_write_def_path(script_path: str) -> str | None:
    for line in _script_lines(script_path):
        match = _WRITE_DEF_RE.match(line)
        if match:
            return match.group(1)
    return None


def _stage_from_script_path(script_path: str) -> str:
    stem = os.path.basename(script_path)[: -len(".tcl")]
    for stage in place_and_route.STAGE_ORDER:
        if stem.endswith(f"_{stage}"):
            return stage
    raise AssertionError(f"could not determine stage from {script_path}")


#: Real per-stage `-metrics` shapes, trimmed from issue #425's own live
#: worked example (a real `openroad/orfs` Docker run against a real
#: volare-fetched sky130A install, full floorplan->place->cts->route).
_STAGE_METRICS = {
    "floorplan": {
        "timing__setup__ws": -3.71641,
        "timing__setup__tns": -143.072,
        "design__die__area": 8487.94,
        "design__core__area": 7607.3,
        "design__instance__utilization": 0.387993,
    },
    "place": {
        "route__wirelength__estimated": 7852.26,
        "timing__setup__ws": -1.9939,
        "timing__setup__tns": -75.8268,
        "timing__fmax": 3.23217e08,
        "design__die__area": 8487.94,
        "design__core__area": 7607.3,
        "design__instance__utilization": 0.439638,
        "power__total": 0.0115449,
    },
    "cts": {
        "route__wirelength__estimated": 8095.6,
        "timing__setup__ws": -2.05,
        "timing__setup__tns": -78.1,
        "timing__fmax": 3.1e08,
        "design__die__area": 8487.94,
        "design__core__area": 7607.3,
        "design__instance__utilization": 0.446546,
        "power__total": 0.0116,
        # Issue #783: `report_clock_skew_metric -setup` only runs on the
        # `"cts"`/`"route"` stages -- trimmed from a real
        # `openroad/orfs:latest` A/B worked example.
        "clock__skew__setup": 0.0421,
    },
    "route": {
        "route__wirelength": 9616,
        "route__drc_errors": 0,
        "timing__setup__ws": -2.18828,
        "timing__setup__tns": -82.8171,
        "timing__fmax": 3.0411e08,
        "design__die__area": 8487.94,
        "design__core__area": 7607.3,
        "design__instance__utilization": 0.446546,
        "power__total": 0.0116,
        "clock__skew__setup": 0.0389,
    },
}


def _stub_openroad_success(
    monkeypatch,
    *,
    stages: tuple[str, ...] = place_and_route.STAGE_ORDER,
    setup_violations: dict[str, int] | None = None,
    hold_violations: dict[str, int] | None = None,
    antenna_violations: dict[str, int] | None = None,
    version: str = "26Q3-771-gdeadbeef",
) -> None:
    setup_violations = setup_violations or {}
    hold_violations = hold_violations or {}
    antenna_violations = antenna_violations or {}

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["openroad", "-version"]:
            # `openroad -version` prints a *bare* version token on its own
            # stdout line -- confirmed live for issue #425's own worked
            # example -- distinct from the `OpenROAD <version>` banner a
            # script invocation prints; see `_openroad_version`'s own
            # docstring.
            return _FakeCompleted(stdout=f"{version} \n")
        assert cmd[0] == "openroad"
        metrics_path = cmd[4]
        script_path = cmd[5]
        stage = _stage_from_script_path(script_path)
        assert stage in stages

        checkpoint_out = _script_write_db_path(script_path)
        Path(checkpoint_out).write_text("fake odb checkpoint\n")

        with open(metrics_path, "w", encoding="utf-8") as handle:
            json.dump(_STAGE_METRICS[stage], handle)

        stdout_lines = []
        if stage != "floorplan":
            stdout_lines.append(place_and_route._SETUP_VIOLATIONS_BEGIN)
            stdout_lines += [
                f"pin_{i} (VIOLATED)" for i in range(setup_violations.get(stage, 0))
            ]
            stdout_lines.append(place_and_route._SETUP_VIOLATIONS_END)
            stdout_lines.append(place_and_route._HOLD_VIOLATIONS_BEGIN)
            stdout_lines += [
                f"pin_{i} (VIOLATED)" for i in range(hold_violations.get(stage, 0))
            ]
            stdout_lines.append(place_and_route._HOLD_VIOLATIONS_END)
        if stage == "route":
            stdout_lines.append(place_and_route._ANTENNA_VIOLATIONS_BEGIN)
            stdout_lines.append(
                f"[INFO ANT-0002] Found {antenna_violations.get(stage, 0)} "
                "net violations."
            )
            stdout_lines.append(place_and_route._ANTENNA_VIOLATIONS_END)

        def_path = _script_write_def_path(script_path)
        if def_path is not None:
            Path(def_path).write_text("fake def\n")

        return _FakeCompleted(returncode=0, stdout="\n".join(stdout_lines))

    monkeypatch.setattr(place_and_route.subprocess, "run", fake_run)


def _stub_merge_def_to_gds(monkeypatch) -> list[dict]:
    """Replace the DEF->GDS merge with a fake that just writes a placeholder
    file -- covered by its own focused unit tests below, since it needs a
    real DEF + real standard-cell GDS view to exercise meaningfully."""
    calls: list[dict] = []

    def fake_merge(**kwargs):
        calls.append(kwargs)
        Path(kwargs["out_path"]).write_text("fake gds\n")

    monkeypatch.setattr(place_and_route, "_merge_def_to_gds", fake_merge)
    return calls


def test_stubbed_full_route_success(tmp_path, monkeypatch):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(
        monkeypatch,
        setup_violations={"route": 3},
        hold_violations={"route": 1},
        antenna_violations={"route": 0},
    )
    merge_calls = _stub_merge_def_to_gds(monkeypatch)

    report = run_place_and_route(request_path)

    assert report["schema_version"] == 1
    assert report["engine"] == "openroad"
    assert report["engine_version"] == "26Q3-771-gdeadbeef"
    assert report["hdl_toplevel"] == "gcd"
    assert report["status"] == "ok"
    assert report["stage_reached"] == "route"
    assert report["seed"] == 1

    assert report["die_area_um2"] == 8487.94
    assert report["core_area_um2"] == 7607.3
    assert report["utilization_pct"] == pytest.approx(44.6546)
    assert report["wirelength_um"] == 9616
    assert report["worst_slack_ns"] == pytest.approx(-2.18828)
    assert report["total_negative_slack_ns"] == pytest.approx(-82.8171)
    assert report["fmax_mhz"] == pytest.approx(304.11)
    assert report["setup_violation_count"] == 3
    assert report["hold_violation_count"] == 1
    assert report["antenna_violation_count"] == 0
    assert report["estimated_power_mw"] == pytest.approx(11.6)
    # Issue #783: `clock_skew_ns` is the `stage_reached` ("route") entry's
    # own value, restated at top level -- same convention as every other
    # top-level metric field.
    assert report["clock_skew_ns"] == pytest.approx(0.0389)

    assert [stage["name"] for stage in report["stages"]] == list(
        place_and_route.STAGE_ORDER
    )
    # floorplan stage has no wirelength/fmax/power/violation-count fields.
    assert "wirelength_um" not in report["stages"][0]
    assert "setup_violation_count" not in report["stages"][0]
    assert "antenna_violation_count" not in report["stages"][0]
    # place/cts stages report setup/hold but not antenna (route-only check).
    assert "antenna_violation_count" not in report["stages"][1]
    assert "antenna_violation_count" not in report["stages"][2]
    # place stage onward does.
    assert "wirelength_um" in report["stages"][1]
    assert report["stages"][3]["setup_violation_count"] == 3

    # `clock_skew_ns` only exists from `"cts"` onward -- no clock tree yet
    # at floorplan/place (issue #783).
    assert "clock_skew_ns" not in report["stages"][0]  # floorplan
    assert "clock_skew_ns" not in report["stages"][1]  # place
    assert report["stages"][2]["clock_skew_ns"] == pytest.approx(0.0421)  # cts
    assert report["stages"][3]["clock_skew_ns"] == pytest.approx(0.0389)  # route

    assert report["def_path"] is not None
    assert os.path.isfile(report["def_path"])
    assert report["gds_path"] is not None
    assert os.path.isfile(report["gds_path"])
    assert len(merge_calls) == 1
    assert merge_calls[0]["hdl_toplevel"] == "gcd"

    provenance = report["provenance"]
    assert provenance["klt_version"]
    assert provenance["pdk"]["name"] == "sky130A"
    assert provenance["deck"]["name"] == "sky130_fd_sc_hd__tt_025C_1v80"


def test_stubbed_full_route_reports_nonzero_antenna_violation_count(
    tmp_path, monkeypatch
):
    """`antenna_violation_count` parses a multi-digit `check_antennas` count
    (issue #759), not just the zero-violations happy path above."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch, antenna_violations={"route": 12})
    _stub_merge_def_to_gds(monkeypatch)

    report = run_place_and_route(request_path)

    assert report["antenna_violation_count"] == 12
    assert report["stages"][3]["antenna_violation_count"] == 12


def test_count_antenna_violations_defensive_when_markers_missing():
    """`_count_antenna_violations` returns `None` (never `0`) when the
    markers are absent from stdout -- keeps a genuinely missing signal
    distinguishable from a confirmed-zero violation count, mirroring
    `_count_violations`'s own defensive-`0` fallback shape but for a `None`
    sentinel (this field is nullable, unlike the setup/hold counts)."""
    assert place_and_route._count_antenna_violations("no markers here") is None


def test_stubbed_full_route_with_macro_emits_read_lef_and_place_macro(
    tmp_path, monkeypatch
):
    """Issue #438: a declared `request.macros` entry must (a) `read_lef` the
    macro before `read_verilog`/`link_design`, and (b) `place_macro` it at
    the declared location -- both inside the floorplan stage script, per
    `place_macro`'s own real-OpenROAD-verified usage (this module's
    docstring "Hard-macro placement" section)."""
    macro_lef = tmp_path / "analog_block.lef"
    _write_macro_lef(macro_lef, "analog_block")

    request_path = _setup_success_env(
        tmp_path,
        monkeypatch,
        macros=[
            {
                "instance": "u_analog",
                "lef": "analog_block.lef",
                "x_um": 12.5,
                "y_um": 3.0,
                "orientation": "MX",
            }
        ],
    )
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)

    report = run_place_and_route(request_path)

    assert report["macros"] == [
        {
            "instance": "u_analog",
            "lef": str(macro_lef),
            "x_um": 12.5,
            "y_um": 3.0,
            "orientation": "MX",
        }
    ]

    floorplan_script = os.path.join(
        os.path.dirname(request_path),
        ".klt",
        "place-and-route",
        "pnr_gcd_floorplan.tcl",
    )
    lines = _script_lines(floorplan_script)
    assert f"read_lef {macro_lef}" in lines
    read_lef_idx = lines.index(f"read_lef {macro_lef}")
    read_verilog_idx = next(
        i for i, ln in enumerate(lines) if ln.startswith("read_verilog")
    )
    assert read_lef_idx < read_verilog_idx

    place_macro_line = (
        "place_macro -macro_name u_analog -location {12.5 3.0} -orientation MX -exact"
    )
    assert place_macro_line in lines
    link_design_idx = next(
        i for i, ln in enumerate(lines) if ln.startswith("link_design")
    )
    make_tracks_idx = lines.index("make_tracks")
    place_macro_idx = lines.index(place_macro_line)
    assert link_design_idx < place_macro_idx < make_tracks_idx


def test_stubbed_no_macros_field_emits_no_place_macro_lines(tmp_path, monkeypatch):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)

    report = run_place_and_route(request_path)

    assert report["macros"] == []
    floorplan_script = os.path.join(
        os.path.dirname(request_path),
        ".klt",
        "place-and-route",
        "pnr_gcd_floorplan.tcl",
    )
    lines = _script_lines(floorplan_script)
    assert not any("place_macro" in line for line in lines)


def test_stubbed_target_stage_place_partial_success(tmp_path, monkeypatch):
    """A `target_stage: "place"` request that completes placement is a
    successful (exit 0) response with `def_path`/`gds_path` both `null` by
    design -- contract spike section 5's "Partial-completion design"."""
    request_path = _setup_success_env(tmp_path, monkeypatch, target_stage="place")
    _stub_openroad_success(monkeypatch, stages=("floorplan", "place"))
    merge_calls = _stub_merge_def_to_gds(monkeypatch)

    report = run_place_and_route(request_path)

    assert report["status"] == "ok"
    assert report["stage_reached"] == "place"
    assert report["def_path"] is None
    assert report["gds_path"] is None
    assert len(merge_calls) == 0
    assert [stage["name"] for stage in report["stages"]] == ["floorplan", "place"]
    # place-stage metrics are present at top level (the last completed
    # stage), even though routing never ran.
    assert report["wirelength_um"] == 7852.26
    assert report["worst_slack_ns"] == pytest.approx(-1.9939)
    # `antenna_violation_count` is `null` before the `route` stage runs --
    # the route stage is where `repair_antennas`/`check_antennas` live.
    assert report["antenna_violation_count"] is None
    # `clock_skew_ns` is `null` before the `cts` stage runs -- no clock tree
    # exists yet at `"place"` (issue #783).
    assert report["clock_skew_ns"] is None


def test_stubbed_target_stage_floorplan_only(tmp_path, monkeypatch):
    request_path = _setup_success_env(
        tmp_path, monkeypatch, target_stage="floorplan", constraints=None, io=None
    )
    _stub_openroad_success(monkeypatch, stages=("floorplan",))

    report = run_place_and_route(request_path)

    assert report["stage_reached"] == "floorplan"
    assert len(report["stages"]) == 1
    assert report["wirelength_um"] is None
    assert report["fmax_mhz"] is None
    assert report["setup_violation_count"] is None
    assert report["def_path"] is None
    assert report["gds_path"] is None


def test_stubbed_engine_failure_mid_stage(tmp_path, monkeypatch):
    request_path = _setup_success_env(tmp_path, monkeypatch)

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["openroad", "-version"]:
            return _FakeCompleted(stdout="26Q3-771-gdeadbeef \n")
        script_path = cmd[5]
        stage = _stage_from_script_path(script_path)
        if stage == "floorplan":
            checkpoint_out = _script_write_db_path(script_path)
            Path(checkpoint_out).write_text("fake odb\n")
            with open(cmd[4], "w", encoding="utf-8") as handle:
                json.dump(_STAGE_METRICS["floorplan"], handle)
            return _FakeCompleted(returncode=0)
        return _FakeCompleted(
            returncode=1, stderr="[ERROR PPL-0001] no valid pin placement found\n"
        )

    monkeypatch.setattr(place_and_route.subprocess, "run", fake_run)

    with pytest.raises(
        PlaceAndRouteError, match=r"openroad 'place' stage failed:.*PPL-0001"
    ):
        run_place_and_route(request_path)


def test_stubbed_missing_metrics_output(tmp_path, monkeypatch):
    request_path = _setup_success_env(
        tmp_path, monkeypatch, target_stage="floorplan", constraints=None, io=None
    )

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["openroad", "-version"]:
            return _FakeCompleted(stdout="26Q3-771-gdeadbeef \n")
        script_path = cmd[5]
        checkpoint_out = _script_write_db_path(script_path)
        Path(checkpoint_out).write_text("fake odb\n")
        return _FakeCompleted(returncode=0)  # no metrics file written

    monkeypatch.setattr(place_and_route.subprocess, "run", fake_run)

    with pytest.raises(PlaceAndRouteError, match="did not produce the expected"):
        run_place_and_route(request_path)


def test_stubbed_missing_openroad_binary(tmp_path, monkeypatch):
    request_path = _setup_success_env(
        tmp_path, monkeypatch, target_stage="floorplan", constraints=None, io=None
    )

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("no such file: openroad")

    monkeypatch.setattr(place_and_route.subprocess, "run", fake_run)

    with pytest.raises(PlaceAndRouteError, match="could not launch openroad"):
        run_place_and_route(request_path)


def test_stubbed_engine_version_unresolvable(tmp_path, monkeypatch):
    request_path = _setup_success_env(
        tmp_path, monkeypatch, target_stage="floorplan", constraints=None, io=None
    )

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["openroad", "-version"]:
            raise FileNotFoundError("openroad vanished")
        script_path = cmd[5]
        checkpoint_out = _script_write_db_path(script_path)
        Path(checkpoint_out).write_text("fake odb\n")
        with open(cmd[4], "w", encoding="utf-8") as handle:
            json.dump(_STAGE_METRICS["floorplan"], handle)
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(place_and_route.subprocess, "run", fake_run)

    report = run_place_and_route(request_path)
    assert report["engine_version"] is None


# --------------------------------------------------------------------------- #
# Non-sky130 cell library + `--pdk`/`--pdk-root` (issue #629)
# --------------------------------------------------------------------------- #


#: The nominal liberty corner, placement site and IO layers ORFS's own
#: `platforms/gf180/config.mk` names for this library (`TC_LIB_FILES` ->
#: `..__tt_025C_5v00.lib.gz`, `PLACE_SITE ?= GF018hv5v_green_sc9` for the
#: `9t` track option, `IO_PLACER_H/V ?= Metal3/Metal4`) -- used so the
#: fabricated install and request below mirror a real gf180mcu run rather
#: than carrying sky130's values under a gf180mcu name (issue #637).
_GF180MCU_CELL_LIBRARY = "gf180mcu_fd_sc_mcu9t5v0"
_GF180MCU_CORNER = "tt_025C_5v00"


def _setup_gf180mcu_success_env(tmp_path, monkeypatch, **request_overrides) -> str:
    """`_setup_success_env`'s gf180mcu twin: a fabricated `gf180mcuC` install
    shipping a `gf180mcu_fd_sc_mcu9t5v0` liberty/LEF/GDS set, plus a request
    whose floorplan site and IO layers match ORFS's own gf180 platform."""
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(
        install_root,
        "gf180mcuC",
        cell_library=_GF180MCU_CELL_LIBRARY,
        corner=_GF180MCU_CORNER,
    )
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    _write(tmp_path / "gcd_synth.v", "// fake mapped netlist\n")
    request = _base_request(
        pdk={"cell_library": _GF180MCU_CELL_LIBRARY, "corner": _GF180MCU_CORNER},
        io={"layer_h": "Metal3", "layer_v": "Metal4"},
        **request_overrides,
    )
    request["floorplan"]["site"] = "GF018hv5v_green_sc9"
    return _write_request(tmp_path / "request.json", request)


def test_stubbed_full_route_success_gf180mcu(tmp_path, monkeypatch):
    """A second, non-sky130 cell library reaches the `route` stage the same
    way sky130 does -- the liberty/LEF resolver was never sky130-specific,
    and `_CTS_BUFFER_CELLS`/`_ROUTING_LAYER_RANGE` now carry a real, verified
    `gf180mcu_fd_sc_mcu9t5v0` entry alongside sky130's (issue #629)."""
    request_path = _setup_gf180mcu_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)
    merge_calls = _stub_merge_def_to_gds(monkeypatch)

    report = run_place_and_route(request_path)

    assert report["status"] == "ok"
    assert report["stage_reached"] == "route"
    assert report["def_path"] is not None
    assert os.path.isfile(report["def_path"])
    assert report["gds_path"] is not None
    assert os.path.isfile(report["gds_path"])
    assert len(merge_calls) == 1

    provenance = report["provenance"]
    assert provenance["pdk"]["name"] == "gf180mcuC"
    assert provenance["deck"]["name"] == f"gf180mcu_fd_sc_mcu9t5v0__{_GF180MCU_CORNER}"


def _stage_script(request_path: str, stage: str, hdl_toplevel: str = "gcd") -> str:
    return os.path.join(
        os.path.dirname(request_path),
        ".klt",
        "place-and-route",
        f"pnr_{hdl_toplevel}_{stage}.tcl",
    )


def test_gf180mcu_cts_and_route_scripts_carry_verified_reference_data(
    tmp_path, monkeypatch
):
    """Issue #637: both per-cell-library reference-data tables must reach the
    generated Tcl verbatim, with the values ORFS's own `platforms/gf180/
    config.mk` pins -- in particular `Metal2-Metal5`, **not**
    `Metal1-Metal5`.

    `MIN_ROUTING_LAYER ?= Metal2` there is deliberate: this library's
    standard cells pin out on `Metal1` itself (`buf_4`'s `I`/`Z` are both
    `LAYER Metal1` in the platform's own `..._9t_sc.lef`), so `Metal1` is
    reserved for pin access and intra-cell/power-rail geometry rather than
    opened up to free signal routing. sky130hd's cells pin out on `li1`
    instead, which is why its own entry legitimately does start at `met1` --
    see the sky130 counterpart test below."""
    request_path = _setup_gf180mcu_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)

    run_place_and_route(request_path)

    cts_lines = _script_lines(_stage_script(request_path, "cts"))
    assert (
        "clock_tree_synthesis -root_buf gf180mcu_fd_sc_mcu9t5v0__buf_4 "
        "-buf_list gf180mcu_fd_sc_mcu9t5v0__buf_4 "
        "-sink_clustering_enable -obstruction_aware"
    ) in cts_lines
    # The platform's own `MIN_BUF_CELL_AND_PORTS` names a `dlya` delay cell
    # (a hold-fixing buffer) -- the wrong shape for a clock-tree root buffer,
    # and deliberately not what this table carries.
    assert not any("dlya" in line for line in cts_lines)

    route_lines = _script_lines(_stage_script(request_path, "route"))
    assert "set_routing_layers -signal Metal2-Metal5" in route_lines
    assert not any("Metal1-Metal5" in line for line in route_lines)
    # sky130's lowercase `met*` convention must never leak into a gf180mcu
    # run -- the layer name is passed through to OpenROAD verbatim.
    assert not any("met1-met5" in line for line in route_lines)
    # Issue #759: the gf180mcu antenna-diode cell (`_ANTENNA_DIODE_CELLS`),
    # verified against `platforms/gf180/lef/gf180mcu_5LM_1TM_9K_9t_sc.lef`'s
    # only `CLASS core ANTENNACELL`-marked macro.
    assert "repair_antennas gf180mcu_fd_sc_mcu9t5v0__antenna" in route_lines
    assert not any("sky130_fd_sc_hd__diode_2" in line for line in route_lines)


def test_sky130hd_cts_and_route_scripts_carry_verified_reference_data(
    tmp_path, monkeypatch
):
    """The sky130hd counterpart of the gf180mcu guard above (issue #637):
    `met1-met5` per ORFS's `platforms/sky130hd/config.mk`
    (`MIN_ROUTING_LAYER ?= met1`, `MAX_ROUTING_LAYER ?= met5`), and the
    `sky130_fd_sc_hd__buf_4` that platform's own `MIN_BUF_CELL_AND_PORTS`
    names. Pins the two entries against an accidental cross-PDK edit."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)

    run_place_and_route(request_path)

    cts_lines = _script_lines(_stage_script(request_path, "cts"))
    assert (
        "clock_tree_synthesis -root_buf sky130_fd_sc_hd__buf_4 "
        "-buf_list sky130_fd_sc_hd__buf_4 "
        "-sink_clustering_enable -obstruction_aware"
    ) in cts_lines

    route_lines = _script_lines(_stage_script(request_path, "route"))
    assert "set_routing_layers -signal met1-met5" in route_lines
    # Issue #759: the sky130hd antenna-diode cell (`_ANTENNA_DIODE_CELLS`),
    # verified against `platforms/sky130hd/lef/sky130_fd_sc_hd_merged.lef`'s
    # only `CLASS CORE ANTENNACELL`-marked macro -- the same cell the survey
    # itself flagged as an unverified [LIT]-tier recollection, now confirmed.
    assert "repair_antennas sky130_fd_sc_hd__diode_2" in route_lines
    assert not any("gf180mcu_fd_sc_mcu9t5v0__antenna" in line for line in route_lines)


def test_route_stage_runs_post_route_antenna_repair(tmp_path, monkeypatch):
    """Issue #759 (P&R survey section 2.7/3.3, priority 3): `repair_antennas`
    must run after `detailed_route` and before `write_def`, followed by a
    reroute pass (mirroring ORFS's own `flow/scripts/detail_route.tcl`, which
    re-runs `detailed_route` immediately after `repair_antennas` to route/
    legalize each newly inserted diode instance) and `check_antennas` to
    report the post-repair violation count."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)

    run_place_and_route(request_path)

    route_lines = _script_lines(_stage_script(request_path, "route"))
    detailed_route_indices = [
        i for i, line in enumerate(route_lines) if line.startswith("detailed_route")
    ]
    repair_index = next(
        i for i, line in enumerate(route_lines) if line.startswith("repair_antennas")
    )
    check_index = route_lines.index("check_antennas")
    write_def_index = next(
        i for i, line in enumerate(route_lines) if line.startswith("write_def")
    )

    # detailed_route -> repair_antennas -> detailed_route (reroute) ->
    # check_antennas -> ... -> write_def, in that order.
    assert len(detailed_route_indices) == 2
    assert detailed_route_indices[0] < repair_index < detailed_route_indices[1]
    assert detailed_route_indices[1] < check_index < write_def_index


def test_place_stage_enables_routability_and_timing_driven_global_placement(
    tmp_path, monkeypatch
):
    """Issue #745 (P&R survey Priority 1): OpenROAD's `gpl` module already
    implements routability- and timing-driven global placement -- enabling
    both on the `place` stage's `global_placement` call is a pure flag
    addition, no new dependency. `-timing_driven` requires `create_clock` to
    already be in effect, which `_clock_lines` guarantees earlier in the same
    stage script."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)

    run_place_and_route(request_path)

    place_lines = _script_lines(_stage_script(request_path, "place"))
    assert any(
        "global_placement" in line
        and "-routability_driven" in line
        and "-timing_driven" in line
        for line in place_lines
    )


def test_cts_stage_runs_post_cts_hold_repair(tmp_path, monkeypatch):
    """Issue #746 (P&R survey #735 section 3.2, priority 2): post-CTS hold-
    timing repair -- `repair_timing -hold` must run after
    `clock_tree_synthesis` (with fresh post-CTS parasitics, needed for hold
    slack to be meaningful) and before the stage's closing
    `detailed_placement`, so hold-fixing buffers get legalized in the same
    pass as CTS's own buffers."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)

    run_place_and_route(request_path)

    cts_lines = _script_lines(_stage_script(request_path, "cts"))
    assert "repair_timing -hold" in cts_lines

    cts_index = next(
        i for i, line in enumerate(cts_lines) if line.startswith("clock_tree_synthesis")
    )
    hold_index = cts_lines.index("repair_timing -hold")
    placement_index = max(
        i for i, line in enumerate(cts_lines) if line == "detailed_placement"
    )
    assert cts_index < hold_index < placement_index


def test_cts_stage_enables_sink_clustering_and_obstruction_awareness(
    tmp_path, monkeypatch
):
    """Issue #783 (P&R survey #735 section 3.4, priority 4): TritonCTS's
    `-sink_clustering_enable`/`-obstruction_aware` flags must be present on
    the `clock_tree_synthesis` call, alongside the existing `-root_buf`/
    `-buf_list` pair -- and `-balance_levels` must NOT be added (confirmed
    live, via `info body clock_tree_synthesis` against a real
    `openroad/orfs:latest` container, that this OpenROAD version treats
    `-balance_levels` as obsolete: it only emits a warning, never a real
    effect)."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)

    run_place_and_route(request_path)

    cts_lines = _script_lines(_stage_script(request_path, "cts"))
    cts_call = next(
        line for line in cts_lines if line.startswith("clock_tree_synthesis")
    )
    assert "-root_buf sky130_fd_sc_hd__buf_4" in cts_call
    assert "-buf_list sky130_fd_sc_hd__buf_4" in cts_call
    assert "-sink_clustering_enable" in cts_call
    assert "-obstruction_aware" in cts_call
    assert "-balance_levels" not in cts_call
    assert not any("-balance_levels" in line for line in cts_lines)


def test_cts_and_route_stages_report_clock_skew_metric(tmp_path, monkeypatch):
    """Issue #783: `report_clock_skew_metric -setup` (the response's new
    `clock_skew_ns` field) must run on the `"cts"` and `"route"` stages --
    where a real clock tree exists -- but never on `"place"`/`"floorplan"`,
    which run before `clock_tree_synthesis`."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)

    run_place_and_route(request_path)

    cts_lines = _script_lines(_stage_script(request_path, "cts"))
    assert "report_clock_skew_metric -setup" in cts_lines

    route_lines = _script_lines(_stage_script(request_path, "route"))
    assert "report_clock_skew_metric -setup" in route_lines

    place_lines = _script_lines(_stage_script(request_path, "place"))
    assert not any("report_clock_skew_metric" in line for line in place_lines)

    floorplan_lines = _script_lines(_stage_script(request_path, "floorplan"))
    assert not any("report_clock_skew_metric" in line for line in floorplan_lines)


def test_cli_pdk_flag_pins_variant(tmp_path, monkeypatch, capsys):
    """`--pdk` (issue #629) selects a specific installed variant, beating
    `$PDK` -- mirroring `klt extract`'s identical flag."""
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")
    _make_pdk_install(install_root, "sky130B")
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    monkeypatch.setenv("PDK", "sky130A")
    _write(tmp_path / "gcd_synth.v", "// fake mapped netlist\n")
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(target_stage="floorplan", constraints=None, io=None),
    )
    _stub_openroad_success(monkeypatch, stages=("floorplan",))

    exit_code = main(
        ["place-and-route", request_path, "--pdk", "sky130B", "--format", "json"]
    )

    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["provenance"]["pdk"]["name"] == "sky130B"


# --------------------------------------------------------------------------- #
# CLI: exit codes, --format text/json
# --------------------------------------------------------------------------- #


def test_cli_success_exits_zero_json(tmp_path, monkeypatch, capsys):
    request_path = _setup_success_env(
        tmp_path, monkeypatch, target_stage="floorplan", constraints=None, io=None
    )
    _stub_openroad_success(monkeypatch, stages=("floorplan",))

    exit_code = main(["place-and-route", request_path, "--format", "json"])

    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok"
    assert out["stage_reached"] == "floorplan"


def test_cli_text_default_format(tmp_path, monkeypatch, capsys):
    request_path = _setup_success_env(
        tmp_path, monkeypatch, target_stage="floorplan", constraints=None, io=None
    )
    _stub_openroad_success(monkeypatch, stages=("floorplan",))

    exit_code = main(["place-and-route", request_path])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "status: ok" in out
    assert "stage_reached: floorplan" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_cli_error_exits_one_with_json_error(tmp_path, monkeypatch, capsys):
    _isolate_pdk(monkeypatch, tmp_path)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(netlist="nope.v")
    )

    exit_code = main(["place-and-route", request_path, "--format", "json"])

    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["schema_version"] == 1
    assert err["error"]["command"] == "place-and-route"
    assert "netlist not found" in err["error"]["message"]


def test_cli_error_exits_one_text_format(tmp_path, monkeypatch, capsys):
    _isolate_pdk(monkeypatch, tmp_path)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(netlist="nope.v")
    )

    exit_code = main(["place-and-route", request_path])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert err.startswith("klt place-and-route:")


def test_cli_missing_request_arg_is_usage_error():
    with pytest.raises(SystemExit) as exc_info:
        main(["place-and-route"])
    assert exc_info.value.code == 2


# --------------------------------------------------------------------------- #
# DEF -> GDS merge (`_merge_def_to_gds`) -- real `klayout.db`, no OpenROAD
# needed. Builds a tiny synthetic DEF + matching standard-cell GDS view by
# hand (real KLayout LEF/DEF/GDS readers, fabricated content) to exercise
# the merge logic's own success and failure paths.
# --------------------------------------------------------------------------- #

kdb = pytest.importorskip("klayout.db")


def _write_tiny_tech_lef(path: Path) -> None:
    path.write_text(
        "VERSION 5.7 ;\n"
        'BUSBITCHARS "[]" ;\n'
        'DIVIDERCHAR "/" ;\n'
        "UNITS\n"
        "  DATABASE MICRONS 1000 ;\n"
        "END UNITS\n"
        "MANUFACTURINGGRID 0.005 ;\n"
        "SITE unithd\n"
        "  SYMMETRY Y ;\n"
        "  CLASS CORE ;\n"
        "  SIZE 0.46 BY 2.72 ;\n"
        "END unithd\n"
        "LAYER met1\n"
        "  TYPE ROUTING ;\n"
        "  DIRECTION HORIZONTAL ;\n"
        "  WIDTH 0.14 ;\n"
        "  PITCH 0.34 ;\n"
        "END met1\n",
        encoding="utf-8",
    )


def _write_tiny_cell_lef(path: Path, cell_name: str) -> None:
    path.write_text(
        "VERSION 5.7 ;\n"
        'BUSBITCHARS "[]" ;\n'
        'DIVIDERCHAR "/" ;\n'
        "UNITS\n"
        "  DATABASE MICRONS 1000 ;\n"
        "END UNITS\n"
        f"MACRO {cell_name}\n"
        "  CLASS CORE ;\n"
        "  SITE unithd ;\n"
        "  SIZE 0.46 BY 2.72 ;\n"
        "  PIN A\n"
        "    DIRECTION INPUT ;\n"
        "    PORT\n"
        "      LAYER met1 ; RECT 0 0 0.1 0.1 ;\n"
        "    END\n"
        "  END A\n"
        "END " + cell_name + "\n",
        encoding="utf-8",
    )


def _write_matching_cell_gds(path: Path, cell_name: str) -> None:
    layout = kdb.Layout()
    layout.dbu = 0.001
    cell = layout.create_cell(cell_name)
    layer = layout.layer(kdb.LayerInfo(68, 20))  # met1 drawing, sky130-ish
    cell.shapes(layer).insert(kdb.Box(0, 0, 100, 100))
    layout.write(str(path))


def _write_tiny_def(path: Path, *, design_name: str, cell_name: str) -> None:
    path.write_text(
        "VERSION 5.8 ;\n"
        'DIVIDERCHAR "/" ;\n'
        'BUSBITCHARS "[]" ;\n'
        f"DESIGN {design_name} ;\n"
        "UNITS DISTANCE MICRONS 1000 ;\n"
        "DIEAREA ( 0 0 ) ( 4600 2720 ) ;\n"
        "ROW ROW_0 unithd 0 0 N DO 10 BY 1 STEP 460 0 ;\n"
        "COMPONENTS 1 ;\n"
        f"- inst1 {cell_name} + PLACED ( 0 0 ) N ;\n"
        "END COMPONENTS\n"
        "END DESIGN\n",
        encoding="utf-8",
    )


def _fake_pdk_info(root: Path, variant: str = "sky130A") -> dict:
    return {
        "schema_version": 1,
        "root": str(root),
        "variant": variant,
        "version": None,
        "resolved_via": "test",
        "assets": {
            "libs_ref": str(root / variant / "libs.ref"),
            "klayout": None,
        },
    }


def test_merge_def_to_gds_success(tmp_path):
    root = tmp_path / "install"
    cell_name = "testcell"
    tech_lef = tmp_path / "tech.tlef"
    cell_lef = tmp_path / "cells.lef"
    _write_tiny_tech_lef(tech_lef)
    _write_tiny_cell_lef(cell_lef, cell_name)

    gds_dir = root / "sky130A" / "libs.ref" / "sky130_fd_sc_hd" / "gds"
    gds_dir.mkdir(parents=True)
    _write_matching_cell_gds(gds_dir / "sky130_fd_sc_hd.gds", cell_name)

    def_path = tmp_path / "design.def"
    _write_tiny_def(def_path, design_name="top", cell_name=cell_name)

    out_path = tmp_path / "out.gds"

    place_and_route._merge_def_to_gds(
        def_path=str(def_path),
        tech_lef=str(tech_lef),
        cell_lef=str(cell_lef),
        pdk_info=_fake_pdk_info(root),
        cell_library="sky130_fd_sc_hd",
        hdl_toplevel="top",
        macros=[],
        out_path=str(out_path),
    )

    assert out_path.is_file()
    result = kdb.Layout()
    result.read(str(out_path))
    assert result.top_cell().name == "top"


def test_merge_def_to_gds_missing_top_cell(tmp_path):
    root = tmp_path / "install"
    cell_name = "testcell"
    tech_lef = tmp_path / "tech.tlef"
    cell_lef = tmp_path / "cells.lef"
    _write_tiny_tech_lef(tech_lef)
    _write_tiny_cell_lef(cell_lef, cell_name)

    gds_dir = root / "sky130A" / "libs.ref" / "sky130_fd_sc_hd" / "gds"
    gds_dir.mkdir(parents=True)
    _write_matching_cell_gds(gds_dir / "sky130_fd_sc_hd.gds", cell_name)

    def_path = tmp_path / "design.def"
    _write_tiny_def(def_path, design_name="top", cell_name=cell_name)

    with pytest.raises(PlaceAndRouteError, match="does not define top cell"):
        place_and_route._merge_def_to_gds(
            def_path=str(def_path),
            tech_lef=str(tech_lef),
            cell_lef=str(cell_lef),
            pdk_info=_fake_pdk_info(root),
            cell_library="sky130_fd_sc_hd",
            hdl_toplevel="wrong_top_name",
            macros=[],
            out_path=str(tmp_path / "out.gds"),
        )


def test_merge_def_to_gds_missing_gds_view(tmp_path):
    root = tmp_path / "install"
    (root / "sky130A" / "libs.ref" / "sky130_fd_sc_hd").mkdir(parents=True)
    cell_name = "testcell"
    tech_lef = tmp_path / "tech.tlef"
    cell_lef = tmp_path / "cells.lef"
    _write_tiny_tech_lef(tech_lef)
    _write_tiny_cell_lef(cell_lef, cell_name)

    def_path = tmp_path / "design.def"
    _write_tiny_def(def_path, design_name="top", cell_name=cell_name)

    with pytest.raises(PlaceAndRouteError, match="standard-cell GDS view not found"):
        place_and_route._merge_def_to_gds(
            def_path=str(def_path),
            tech_lef=str(tech_lef),
            cell_lef=str(cell_lef),
            pdk_info=_fake_pdk_info(root),
            cell_library="sky130_fd_sc_hd",
            hdl_toplevel="top",
            macros=[],
            out_path=str(tmp_path / "out.gds"),
        )


def test_merge_def_to_gds_empty_cell_is_an_error(tmp_path):
    """A LEF macro with no matching GDS view produces an empty cell in the
    merged layout -- def2stream.py's own "LEF Cell has no matching GDS/OAS
    cell" check, ported here as a raised error rather than a silent GDS."""
    root = tmp_path / "install"
    cell_name = "testcell"
    tech_lef = tmp_path / "tech.tlef"
    cell_lef = tmp_path / "cells.lef"
    _write_tiny_tech_lef(tech_lef)
    _write_tiny_cell_lef(cell_lef, cell_name)

    gds_dir = root / "sky130A" / "libs.ref" / "sky130_fd_sc_hd" / "gds"
    gds_dir.mkdir(parents=True)
    # A GDS view for a *different* cell -- the DEF's own `testcell`
    # instance has nothing to merge against.
    _write_matching_cell_gds(gds_dir / "sky130_fd_sc_hd.gds", "some_other_cell")

    def_path = tmp_path / "design.def"
    _write_tiny_def(def_path, design_name="top", cell_name=cell_name)

    with pytest.raises(PlaceAndRouteError, match="empty \\(unmatched\\) cells"):
        place_and_route._merge_def_to_gds(
            def_path=str(def_path),
            tech_lef=str(tech_lef),
            cell_lef=str(cell_lef),
            pdk_info=_fake_pdk_info(root),
            cell_library="sky130_fd_sc_hd",
            hdl_toplevel="top",
            macros=[],
            out_path=str(tmp_path / "out.gds"),
        )


def _write_tiny_macro_lef(
    path: Path, macro_name: str, size_um: tuple[float, float]
) -> None:
    path.write_text(
        "VERSION 5.7 ;\n"
        f"MACRO {macro_name}\n"
        "  CLASS BLOCK ;\n"
        f"  SIZE {size_um[0]} BY {size_um[1]} ;\n"
        "END " + macro_name + "\n"
        "END LIBRARY\n",
        encoding="utf-8",
    )


def _write_tiny_def_with_macro(
    path: Path, *, design_name: str, cell_name: str, macro_cell_name: str
) -> None:
    path.write_text(
        "VERSION 5.8 ;\n"
        'DIVIDERCHAR "/" ;\n'
        'BUSBITCHARS "[]" ;\n'
        f"DESIGN {design_name} ;\n"
        "UNITS DISTANCE MICRONS 1000 ;\n"
        "DIEAREA ( 0 0 ) ( 46000 27200 ) ;\n"
        "ROW ROW_0 unithd 0 0 N DO 10 BY 1 STEP 460 0 ;\n"
        "COMPONENTS 2 ;\n"
        f"- inst1 {cell_name} + PLACED ( 0 0 ) N ;\n"
        f"- u_macro {macro_cell_name} + PLACED ( 10000 10000 ) N ;\n"
        "END COMPONENTS\n"
        "END DESIGN\n",
        encoding="utf-8",
    )


def test_merge_def_to_gds_tolerates_abstract_only_macro_instance(tmp_path):
    """A macro instance with no declared `gds` (issue #438) stays empty in
    the merged layout without raising -- the abstract-only case this
    issue's own DEF-level placement/obstruction verification needs."""
    root = tmp_path / "install"
    cell_name = "testcell"
    macro_cell_name = "analog_block"
    tech_lef = tmp_path / "tech.tlef"
    cell_lef = tmp_path / "cells.lef"
    macro_lef = tmp_path / "analog_block.lef"
    _write_tiny_tech_lef(tech_lef)
    _write_tiny_cell_lef(cell_lef, cell_name)
    _write_tiny_macro_lef(macro_lef, macro_cell_name, (5.0, 5.0))

    gds_dir = root / "sky130A" / "libs.ref" / "sky130_fd_sc_hd" / "gds"
    gds_dir.mkdir(parents=True)
    _write_matching_cell_gds(gds_dir / "sky130_fd_sc_hd.gds", cell_name)

    def_path = tmp_path / "design.def"
    _write_tiny_def_with_macro(
        def_path,
        design_name="top",
        cell_name=cell_name,
        macro_cell_name=macro_cell_name,
    )

    out_path = tmp_path / "out.gds"
    place_and_route._merge_def_to_gds(
        def_path=str(def_path),
        tech_lef=str(tech_lef),
        cell_lef=str(cell_lef),
        pdk_info=_fake_pdk_info(root),
        cell_library="sky130_fd_sc_hd",
        hdl_toplevel="top",
        macros=[
            {
                "instance": "u_macro",
                "lef": str(macro_lef),
                "cell_name": macro_cell_name,
                "x_um": 10.0,
                "y_um": 10.0,
                "orientation": "R0",
                "gds": None,
            }
        ],
        out_path=str(out_path),
    )

    assert out_path.is_file()
    result = kdb.Layout()
    result.read(str(out_path))
    assert result.cell(macro_cell_name) is not None


def test_merge_def_to_gds_merges_macro_gds_view_when_declared(tmp_path):
    """When a macro entry declares its own `gds`, that view is merged in
    (non-empty) exactly like the standard-cell GDS view."""
    root = tmp_path / "install"
    cell_name = "testcell"
    macro_cell_name = "analog_block"
    tech_lef = tmp_path / "tech.tlef"
    cell_lef = tmp_path / "cells.lef"
    macro_lef = tmp_path / "analog_block.lef"
    macro_gds = tmp_path / "analog_block.gds"
    _write_tiny_tech_lef(tech_lef)
    _write_tiny_cell_lef(cell_lef, cell_name)
    _write_tiny_macro_lef(macro_lef, macro_cell_name, (5.0, 5.0))
    _write_matching_cell_gds(macro_gds, macro_cell_name)

    gds_dir = root / "sky130A" / "libs.ref" / "sky130_fd_sc_hd" / "gds"
    gds_dir.mkdir(parents=True)
    _write_matching_cell_gds(gds_dir / "sky130_fd_sc_hd.gds", cell_name)

    def_path = tmp_path / "design.def"
    _write_tiny_def_with_macro(
        def_path,
        design_name="top",
        cell_name=cell_name,
        macro_cell_name=macro_cell_name,
    )

    out_path = tmp_path / "out.gds"
    place_and_route._merge_def_to_gds(
        def_path=str(def_path),
        tech_lef=str(tech_lef),
        cell_lef=str(cell_lef),
        pdk_info=_fake_pdk_info(root),
        cell_library="sky130_fd_sc_hd",
        hdl_toplevel="top",
        macros=[
            {
                "instance": "u_macro",
                "lef": str(macro_lef),
                "cell_name": macro_cell_name,
                "x_um": 10.0,
                "y_um": 10.0,
                "orientation": "R0",
                "gds": str(macro_gds),
            }
        ],
        out_path=str(out_path),
    )

    result = kdb.Layout()
    result.read(str(out_path))
    macro_cell = result.cell(macro_cell_name)
    assert macro_cell is not None
    assert not macro_cell.is_empty()


# --------------------------------------------------------------------------- #
# Integration: real OpenROAD + a real, host-resolved sky130 PDK
# (skipped when either is unavailable -- never required for CI, see this
# module's own docstring)
# --------------------------------------------------------------------------- #

HAVE_OPENROAD = shutil.which("openroad") is not None


def _find_real_pnr_variant(cell_library: str) -> tuple[str, str] | None:
    """Search every install/variant `list_pdks()` discovers for one shipping
    a real ``cell_library`` liberty + tech/cell LEF + GDS view. Returns
    ``(root, variant)`` or ``None``.

    Parameterized by cell library (issue #637) so the same gate serves both
    the sky130hd and gf180mcu live worked examples -- the four asset paths
    it probes are exactly what `lef_files()`/`_resolve_liberty`/
    `_resolve_gds_view` resolve, and none of them is PDK-family-specific."""
    try:
        result = pdk_module.list_pdks()
    except Exception:
        return None
    for install in result["installs"]:
        for variant in install["variants"]:
            lib_dir = os.path.join(
                install["root"], variant["name"], "libs.ref", cell_library
            )
            if all(
                os.path.exists(os.path.join(lib_dir, sub))
                for sub in (
                    "lib",
                    os.path.join("techlef", f"{cell_library}__nom.tlef"),
                    os.path.join("lef", f"{cell_library}.lef"),
                    os.path.join("gds", f"{cell_library}.gds"),
                )
            ):
                return install["root"], variant["name"]
    return None


_REAL_SKY130_PNR_VARIANT = _find_real_pnr_variant("sky130_fd_sc_hd")
_REAL_GF180MCU_PNR_VARIANT = _find_real_pnr_variant(_GF180MCU_CELL_LIBRARY)


@pytest.mark.skipif(
    not HAVE_OPENROAD, reason="openroad is not installed on this machine"
)
@pytest.mark.skipif(
    _REAL_SKY130_PNR_VARIANT is None,
    reason="no real sky130_fd_sc_hd LEF/liberty/GDS set resolves via list_pdks()",
)
def test_integration_real_openroad_gcd_worked_example(tmp_path, monkeypatch):
    """The GCD worked example, run against a real `openroad` binary and a
    real, host-resolved sky130 PDK install."""
    root, variant = _REAL_SKY130_PNR_VARIANT
    monkeypatch.setenv("PDK_ROOT", root)
    monkeypatch.setenv("PDK", variant)

    # A real synthesized netlist, produced the same way Phase 2's own
    # integration test does (via `klt synthesize`), kept minimal here.
    from klayout_tools.synthesize import run_synthesize

    rtl_path = tmp_path / "gcd.v"
    rtl_path.write_text(_GCD_RTL, encoding="utf-8")
    synth_request = _write_request(
        tmp_path / "synth_request.json",
        {
            "engine": "yosys",
            "sources": ["gcd.v"],
            "hdl_toplevel": "gcd",
            "pdk": {"cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80"},
        },
    )
    synth_report = run_synthesize(synth_request)

    request_path = _write_request(
        tmp_path / "pnr_request.json",
        _base_request(netlist=synth_report["netlist_path"]),
    )

    report = run_place_and_route(request_path)

    assert report["status"] == "ok"
    assert report["stage_reached"] == "route"
    assert report["def_path"] is not None
    assert os.path.isfile(report["def_path"])
    assert report["gds_path"] is not None
    assert os.path.isfile(report["gds_path"])
    assert report["die_area_um2"] is not None
    assert report["core_area_um2"] is not None


@pytest.mark.skipif(
    not HAVE_OPENROAD, reason="openroad is not installed on this machine"
)
@pytest.mark.skipif(
    _REAL_GF180MCU_PNR_VARIANT is None,
    reason=(
        "no real gf180mcu_fd_sc_mcu9t5v0 LEF/liberty/GDS set resolves via list_pdks()"
    ),
)
def test_integration_real_openroad_gcd_worked_example_gf180mcu(tmp_path, monkeypatch):
    """The same GCD worked example as above, on gf180mcu instead of sky130hd
    (issue #637) -- `klt synthesize` -> `klt place-and-route`, floorplan
    through a full detailed route, against a real `openroad` binary and a
    real host-resolved gf180mcu install.

    This is the automated form of this issue's "one real synthesized
    gf180mcu netlist reaches a routed GDS end-to-end" acceptance criterion.
    Gated exactly like its sky130hd sibling, and skipped (never failed) on
    a machine without both halves of the toolchain -- CI installs neither
    `openroad` nor a real gf180mcu standard-cell PDK today.

    `request.pdk.corner` is deliberately omitted so `_resolve_liberty`
    resolves the install's own nominal corner, rather than this test
    hard-coding one gf180mcu liberty corner name. Floorplan site and IO
    layers are ORFS's own `platforms/gf180/config.mk` values
    (`PLACE_SITE ?= GF018hv5v_green_sc9` for `TRACK_OPTION ?= 9t`,
    `IO_PLACER_H/V ?= Metal3/Metal4`); the 10 ns clock is a realistic
    180 nm/5 V target rather than sky130hd's 1.1 ns.
    """
    root, variant = _REAL_GF180MCU_PNR_VARIANT
    monkeypatch.setenv("PDK_ROOT", root)
    monkeypatch.setenv("PDK", variant)

    from klayout_tools.synthesize import run_synthesize

    rtl_path = tmp_path / "gcd.v"
    rtl_path.write_text(_GCD_RTL, encoding="utf-8")
    synth_request = _write_request(
        tmp_path / "synth_request.json",
        {
            "engine": "yosys",
            "sources": ["gcd.v"],
            "hdl_toplevel": "gcd",
            "pdk": {"cell_library": _GF180MCU_CELL_LIBRARY},
        },
    )
    synth_report = run_synthesize(synth_request)

    request_path = _write_request(
        tmp_path / "pnr_request.json",
        _base_request(
            netlist=synth_report["netlist_path"],
            pdk={"cell_library": _GF180MCU_CELL_LIBRARY},
            floorplan={
                "method": "utilization",
                "utilization_pct": 38,
                "aspect_ratio": 1.0,
                "core_margin_um": 2.0,
                "site": "GF018hv5v_green_sc9",
            },
            io={"layer_h": "Metal3", "layer_v": "Metal4"},
            constraints={"clock_port": "clk", "clock_period_ns": 10.0},
        ),
    )

    report = run_place_and_route(request_path)

    assert report["status"] == "ok"
    assert report["stage_reached"] == "route"
    assert report["def_path"] is not None
    assert os.path.isfile(report["def_path"])
    assert report["gds_path"] is not None
    assert os.path.isfile(report["gds_path"])
    assert report["die_area_um2"] is not None
    assert report["core_area_um2"] is not None


# Sanity: `subprocess` really is the module this file's stubs patch (guards
# against a future refactor silently making the stubs a no-op).
def test_place_and_route_uses_stdlib_subprocess():
    assert place_and_route.subprocess is subprocess
