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
  `_find_real_pnr_variant()`. Neither is required for CI (`ci.yml`'s
  PR-gating `test` job installs neither `openroad` nor a real
  standard-cell PDK; the `workflow_dispatch`-only
  `place-and-route-smoke.yml` job added by issue #1328 does provision
  both, but for sky130A only and never on a PR) but each runs on any
  machine with both halves of its toolchain. **This repo's own worked
  example for issue #425 instead verified the identical code path
  manually via a real `openroad/orfs` Docker
  image** (`openroad -no_init -exit -metrics ...`
  against a real volare-fetched `sky130A` install, floorplan through a full
  detailed route with 0 DRC violations, followed by a real DEF->GDS merge
  via this module's own `_merge_def_to_gds` producing a valid GDS) -- see
  the PR description for the full transcript; that manual run is not
  automated as a test here since it depends on Docker, which is not a
  project dependency. **The gf180mcu sibling (issue #637) has now had its
  own equivalent live run too** (issue #1331, 2026-08-22): `openroad
  26Q3-1080-gab6fd26351` from the `openroad/orfs:latest` Docker image
  against a real volare `gf180mcuC` install (`open_pdks
  c6d73a35f524070e85faff4a6a9eef49553ebc2b`), floorplan through a full
  detailed route with `route__drc_errors: 0` and 0 antenna-violating
  nets/pins, followed by a real DEF->GDS merge producing a valid GDS. See
  `docs/cli/place-and-route.md`'s "Live verification" section for the full
  result, including the two findings that run surfaced (the 5LM variant
  requirement encoded in `_find_real_pnr_variant` below, and the
  `request.power`-dependent DRC outcome of the merged GDS).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import extract as extract_module
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
    prefixed_operating_conditions: bool = False,
    tech_lef_corners: tuple[str, ...] = ("nom",),
) -> Path:
    """Fabricate a minimal open_pdks-layout variant with a standard-cell
    library's `lib/`/`techlef/`/`lef/`/`gds/` views -- enough for
    `run_place_and_route`'s own resolution, never real, engine-parseable
    file contents (the stubbed-OpenROAD tests never invoke a real
    `openroad`; the GDS merge is separately stubbed in those tests).

    ``prefixed_operating_conditions=True`` writes the `.lib` file's
    `default_operating_conditions` attribute as `f"{cell_library}__{corner}"`
    instead of the bare ``corner`` -- gf180mcu_fd_sc_mcu9t5v0's real shape
    (issue #820); the on-disk filename is unaffected.

    ``tech_lef_corners`` (issue #1100) -- which ``__<corner>.tlef`` files to
    stage under `techlef/`; the default ``("nom",)`` matches every existing
    caller's prior fixture shape (a `"nom"`-only install) byte-for-byte."""
    variant_dir = root / variant
    (variant_dir / "libs.tech").mkdir(parents=True, exist_ok=True)
    lib_dir = variant_dir / "libs.ref" / cell_library

    if with_lib:
        lib_views_dir = lib_dir / "lib"
        lib_views_dir.mkdir(parents=True, exist_ok=True)
        operating_conditions = (
            f"{cell_library}__{corner}" if prefixed_operating_conditions else corner
        )
        content = (
            f'    default_operating_conditions : "{operating_conditions}";\n'
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
        for tech_lef_corner in tech_lef_corners:
            (techlef_dir / f"{cell_library}__{tech_lef_corner}.tlef").write_text(
                "# tech lef\n"
            )
        lef_dir = lib_dir / "lef"
        lef_dir.mkdir(parents=True, exist_ok=True)
        (lef_dir / f"{cell_library}.lef").write_text("# merged cell lef\n")

    if with_gds:
        gds_dir = lib_dir / "gds"
        gds_dir.mkdir(parents=True, exist_ok=True)
        (gds_dir / f"{cell_library}.gds").write_text("# fake gds\n")

    lib_dir.mkdir(parents=True, exist_ok=True)
    return variant_dir


def _setup_success_env(
    tmp_path, monkeypatch, *, install_kwargs: dict | None = None, **request_overrides
) -> str:
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A", **(install_kwargs or {}))
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


def test_run_interconnect_corner_invalid_rejected(tmp_path):
    """Issue #1100: an out-of-enum `request.pdk.interconnect_corner` raises
    `PlaceAndRouteError` -- never a silent fallback or a traceback -- caught
    by the plain enum-check alongside the existing `pdk.corner` validation,
    before any PDK resolution is attempted."""
    _write(tmp_path / "gcd_synth.v", "// netlist\n")
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(
            pdk={
                "cell_library": "sky130_fd_sc_hd",
                "corner": "tt_025C_1v80",
                "interconnect_corner": "typ",
            }
        ),
    )
    with pytest.raises(
        PlaceAndRouteError, match="pdk.interconnect_corner must be one of"
    ):
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


def test_resolve_liberty_nominal_corner_default_gf180mcu_shape(tmp_path, monkeypatch):
    """gf180mcu_fd_sc_mcu9t5v0's `.lib` files write their own
    `<cell_library>__` prefix into `default_operating_conditions`, unlike
    sky130's bare attribute. Omitting `pdk.corner` (``requested_corner=None``)
    must still resolve to the correctly-named on-disk liberty file -- not a
    doubled-prefix path that does not exist (issue #820)."""
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(
        install_root,
        "gf180mcuD",
        cell_library="gf180mcu_fd_sc_mcu9t5v0",
        corner="tt_025C_1v80",
        prefixed_operating_conditions=True,
    )
    monkeypatch.setenv("PDK_ROOT", str(install_root))

    liberty_path, corner, info = place_and_route._resolve_liberty(
        "gf180mcu_fd_sc_mcu9t5v0", None
    )
    assert corner == "tt_025C_1v80"
    assert liberty_path.endswith("gf180mcu_fd_sc_mcu9t5v0__tt_025C_1v80.lib")
    assert info["variant"] == "gf180mcuD"


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


def test_run_power_requested_for_unknown_cell_library_rejects_at_floorplan(
    tmp_path, monkeypatch
):
    """`request.power` on a `cell_library` with no `_POWER_PIN_PATTERNS`
    entry fails clearly -- and, unlike the CTS/routing-layer/antenna-diode
    checks above, unconditionally (issue #1091's own PDN Tcl always runs at
    the end of the `"floorplan"` stage, the first stage every run reaches),
    never a silent guess."""
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "acmeA", cell_library="acme_fd_sc_hd")
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    _write(tmp_path / "gcd_synth.v", "// netlist\n")
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(
            pdk={"cell_library": "acme_fd_sc_hd", "corner": "tt_025C_1v80"},
            target_stage="floorplan",
            constraints=None,
            io=None,
            power={"straps": _BASE_STRAPS},
        ),
    )
    with pytest.raises(
        PlaceAndRouteError,
        match="no power pin-pattern table known for standard-cell library "
        "'acme_fd_sc_hd'",
    ):
        run_place_and_route(request_path)


def test_run_power_requested_at_route_stage_for_unknown_filler_cell_library(
    tmp_path, monkeypatch
):
    """A `cell_library` with power pin-pattern and tapcell entries but no
    `_FILLER_CELLS` entry still fails clearly once a `request.power` run
    reaches the `"route"` stage (never a silent guess) -- `target_stage:
    "place"` on the same fake library is unaffected, since
    `filler_placement` is a `"route"`-stage-only call."""
    _isolate_pdk(monkeypatch, tmp_path)
    monkeypatch.setitem(
        place_and_route._CTS_BUFFER_CELLS, "acme_fd_sc_hd", "acme_fd_sc_hd__buf_4"
    )
    monkeypatch.setitem(
        place_and_route._ROUTING_LAYER_RANGE, "acme_fd_sc_hd", "met1-met5"
    )
    monkeypatch.setitem(
        place_and_route._ANTENNA_DIODE_CELLS,
        "acme_fd_sc_hd",
        ("acme_fd_sc_hd__diode_1", "DIODE"),
    )
    monkeypatch.setitem(
        place_and_route._POWER_PIN_PATTERNS,
        "acme_fd_sc_hd",
        place_and_route._POWER_PIN_PATTERNS["sky130_fd_sc_hd"],
    )
    monkeypatch.setitem(
        place_and_route._TAPCELL_CELLS,
        "acme_fd_sc_hd",
        ("acme_fd_sc_hd__tap_1", None, 14),
    )
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "acmeA", cell_library="acme_fd_sc_hd")
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    _write(tmp_path / "gcd_synth.v", "// netlist\n")
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(
            pdk={"cell_library": "acme_fd_sc_hd", "corner": "tt_025C_1v80"},
            power={"straps": _BASE_STRAPS},
        ),
    )
    with pytest.raises(
        PlaceAndRouteError,
        match="no filler-cell masters known for standard-cell library 'acme_fd_sc_hd'",
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
# `request.power` validation (issue #1091) -- see `_validate_power`.
# --------------------------------------------------------------------------- #

_BASE_STRAPS = [
    {"layer": "met1", "width_um": 0.48, "pitch_um": 5.44, "followpins": True},
    {"layer": "met4", "width_um": 1.6, "pitch_um": 27.14, "offset_um": 13.57},
    {"layer": "met5", "width_um": 1.6, "pitch_um": 27.2, "offset_um": 13.6},
]


def test_power_field_absent_validates_to_none():
    assert place_and_route._validate_power(None) is None


def test_power_must_be_an_object():
    with pytest.raises(PlaceAndRouteError, match="request.power must be a JSON object"):
        place_and_route._validate_power("not an object")


def test_power_straps_required():
    with pytest.raises(PlaceAndRouteError, match="request.power.straps is required"):
        place_and_route._validate_power({})


def test_power_straps_must_be_a_non_empty_list():
    with pytest.raises(PlaceAndRouteError, match="request.power.straps is required"):
        place_and_route._validate_power({"straps": []})


def test_power_strap_entry_must_be_an_object():
    with pytest.raises(
        PlaceAndRouteError, match=r"request\.power\.straps\[0\] must be an object"
    ):
        place_and_route._validate_power({"straps": ["not an object"]})


def test_power_strap_layer_required():
    with pytest.raises(PlaceAndRouteError, match="layer is required"):
        place_and_route._validate_power(
            {"straps": [{"width_um": 1.0, "pitch_um": 5.0}]}
        )


@pytest.mark.parametrize("value", [0, -1.0, "1.0", None, True])
def test_power_strap_width_um_must_be_a_positive_number(value):
    with pytest.raises(PlaceAndRouteError, match="width_um is required"):
        place_and_route._validate_power(
            {"straps": [{"layer": "met1", "width_um": value, "pitch_um": 5.0}]}
        )


@pytest.mark.parametrize("value", [0, -1.0, "5.0", None, True])
def test_power_strap_pitch_um_must_be_a_positive_number(value):
    with pytest.raises(PlaceAndRouteError, match="pitch_um is required"):
        place_and_route._validate_power(
            {"straps": [{"layer": "met1", "width_um": 1.0, "pitch_um": value}]}
        )


def test_power_strap_offset_um_must_be_a_number_when_given():
    with pytest.raises(PlaceAndRouteError, match="offset_um must be a number"):
        place_and_route._validate_power(
            {
                "straps": [
                    {
                        "layer": "met1",
                        "width_um": 1.0,
                        "pitch_um": 5.0,
                        "offset_um": "0",
                    }
                ]
            }
        )


def test_power_strap_followpins_must_be_a_boolean_when_given():
    with pytest.raises(PlaceAndRouteError, match="followpins must be a boolean"):
        place_and_route._validate_power(
            {
                "straps": [
                    {
                        "layer": "met1",
                        "width_um": 1.0,
                        "pitch_um": 5.0,
                        "followpins": "yes",
                    }
                ]
            }
        )


def test_power_net_and_ground_net_must_differ():
    with pytest.raises(PlaceAndRouteError, match="must differ"):
        place_and_route._validate_power(
            {
                "power_net": "VDD",
                "ground_net": "VDD",
                "straps": _BASE_STRAPS,
            }
        )


@pytest.mark.parametrize("field", ["power_net", "ground_net"])
def test_power_net_fields_must_be_non_empty_strings_when_given(field):
    with pytest.raises(PlaceAndRouteError, match=f"{field} must be a non-empty string"):
        place_and_route._validate_power({field: "", "straps": _BASE_STRAPS})


def test_power_net_fields_default_to_vdd_vss():
    validated = place_and_route._validate_power({"straps": _BASE_STRAPS})
    assert validated["power_net"] == "VDD"
    assert validated["ground_net"] == "VSS"


def test_power_normalizes_strap_numbers_and_defaults():
    validated = place_and_route._validate_power(
        {"straps": [{"layer": "met1", "width_um": 1, "pitch_um": 5}]}
    )
    strap = validated["straps"][0]
    assert strap == {
        "layer": "met1",
        "width_um": 1.0,
        "pitch_um": 5.0,
        "offset_um": 0.0,
        "spacing_um": None,
        "followpins": False,
    }
    assert validated["connects"] == []


# --------------------------------------------------------------------------- #
# `request.power.straps[].spacing_um` validation (issue #1133).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", [0, -1.0, "0.56", True])
def test_power_strap_spacing_um_must_be_a_positive_number_when_given(value):
    with pytest.raises(
        PlaceAndRouteError, match="spacing_um must be a positive number"
    ):
        place_and_route._validate_power(
            {
                "straps": [
                    {
                        "layer": "met1",
                        "width_um": 1.0,
                        "pitch_um": 5.0,
                        "spacing_um": value,
                    }
                ]
            }
        )


def test_power_strap_spacing_um_normalizes_when_given():
    validated = place_and_route._validate_power(
        {"straps": [{"layer": "met1", "width_um": 1, "pitch_um": 5, "spacing_um": 1}]}
    )
    assert validated["straps"][0]["spacing_um"] == 1.0


def test_power_strap_spacing_um_omitted_defaults_to_none():
    validated = place_and_route._validate_power(
        {"straps": [{"layer": "met1", "width_um": 1, "pitch_um": 5}]}
    )
    assert validated["straps"][0]["spacing_um"] is None


def test_power_strap_spacing_um_valid_with_only_one_strap():
    """issue #1133: `spacing_um` describes a stripe's own paired power/ground
    spacing on its own layer -- it does not require an adjacent strap, so a
    single-strap request with `spacing_um` set validates fine."""
    validated = place_and_route._validate_power(
        {"straps": [{"layer": "met1", "width_um": 1, "pitch_um": 5, "spacing_um": 0.5}]}
    )
    assert validated["straps"][0]["spacing_um"] == 0.5


# --------------------------------------------------------------------------- #
# `request.power.connects[]` validation (issue #1133).
# --------------------------------------------------------------------------- #


def test_power_connects_omitted_defaults_to_empty_list():
    validated = place_and_route._validate_power({"straps": _BASE_STRAPS})
    assert validated["connects"] == []


def test_power_connects_must_be_a_list():
    with pytest.raises(
        PlaceAndRouteError, match="request.power.connects must be a list"
    ):
        place_and_route._validate_power(
            {"straps": _BASE_STRAPS, "connects": "not a list"}
        )


def test_power_connects_entry_must_be_an_object():
    with pytest.raises(
        PlaceAndRouteError, match=r"request\.power\.connects\[0\] must be an object"
    ):
        place_and_route._validate_power(
            {"straps": _BASE_STRAPS, "connects": ["not an object"]}
        )


@pytest.mark.parametrize(
    "layers",
    [None, "met1", ["met1"], ["met1", "met4", "met5"], ["met1", ""], [1, "met4"]],
)
def test_power_connects_layers_must_be_a_2element_list_of_strings(layers):
    with pytest.raises(PlaceAndRouteError, match=r"connects\[0\]\.layers is required"):
        place_and_route._validate_power(
            {"straps": _BASE_STRAPS, "connects": [{"layers": layers}]}
        )


def test_power_connects_layers_must_match_a_consecutive_strap_pair():
    with pytest.raises(
        PlaceAndRouteError,
        match=r"does not match any consecutive pair in request\.power\.straps",
    ):
        place_and_route._validate_power(
            {"straps": _BASE_STRAPS, "connects": [{"layers": ["met1", "met5"]}]}
        )


def test_power_connects_rejects_duplicate_pair():
    with pytest.raises(PlaceAndRouteError, match="duplicates an earlier"):
        place_and_route._validate_power(
            {
                "straps": _BASE_STRAPS,
                "connects": [
                    {"layers": ["met1", "met4"]},
                    {"layers": ["met1", "met4"], "max_columns": 5},
                ],
            }
        )


@pytest.mark.parametrize("value", [0, -1, 1.5, "5", True])
def test_power_connects_max_columns_must_be_a_positive_integer_when_given(value):
    with pytest.raises(
        PlaceAndRouteError, match="max_columns must be a positive integer"
    ):
        place_and_route._validate_power(
            {
                "straps": _BASE_STRAPS,
                "connects": [{"layers": ["met1", "met4"], "max_columns": value}],
            }
        )


@pytest.mark.parametrize("value", [[], "met2", [1, "met3"], [""]])
def test_power_connects_ongrid_must_be_a_non_empty_list_of_strings_when_given(value):
    with pytest.raises(PlaceAndRouteError, match="ongrid must be a non-empty list"):
        place_and_route._validate_power(
            {
                "straps": _BASE_STRAPS,
                "connects": [{"layers": ["met1", "met4"], "ongrid": value}],
            }
        )


def test_power_connects_split_cuts_must_be_an_object_when_given():
    with pytest.raises(PlaceAndRouteError, match="split_cuts must be an object"):
        place_and_route._validate_power(
            {
                "straps": _BASE_STRAPS,
                "connects": [
                    {"layers": ["met1", "met4"], "split_cuts": "not an object"}
                ],
            }
        )


def test_power_connects_split_cuts_layer_required():
    with pytest.raises(PlaceAndRouteError, match="split_cuts.layer is required"):
        place_and_route._validate_power(
            {
                "straps": _BASE_STRAPS,
                "connects": [
                    {
                        "layers": ["met1", "met4"],
                        "split_cuts": {"width_um": 0.128},
                    }
                ],
            }
        )


@pytest.mark.parametrize("value", [0, -1.0, "0.128", None, True])
def test_power_connects_split_cuts_width_um_must_be_a_positive_number(value):
    with pytest.raises(PlaceAndRouteError, match="split_cuts.width_um is required"):
        place_and_route._validate_power(
            {
                "straps": _BASE_STRAPS,
                "connects": [
                    {
                        "layers": ["met1", "met4"],
                        "split_cuts": {"layer": "met3", "width_um": value},
                    }
                ],
            }
        )


def test_power_connects_normalizes_full_entry():
    validated = place_and_route._validate_power(
        {
            "straps": _BASE_STRAPS,
            "connects": [
                {
                    "layers": ["met1", "met4"],
                    "max_columns": 5,
                    "ongrid": ["met2", "met3", "met4"],
                    "split_cuts": {"layer": "met3", "width_um": 0.128},
                }
            ],
        }
    )
    assert validated["connects"] == [
        {
            "layers": ["met1", "met4"],
            "max_columns": 5,
            "ongrid": ["met2", "met3", "met4"],
            "split_cuts": {"layer": "met3", "width_um": 0.128},
        }
    ]


def test_power_connects_entry_omitting_all_tuning_normalizes_to_none_fields():
    validated = place_and_route._validate_power(
        {"straps": _BASE_STRAPS, "connects": [{"layers": ["met1", "met4"]}]}
    )
    assert validated["connects"] == [
        {
            "layers": ["met1", "met4"],
            "max_columns": None,
            "ongrid": None,
            "split_cuts": None,
        }
    ]


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
#: Issue #996 -- OpenROAD's own `write_verilog` (the `dbSta` command, not
#: Yosys's identically-named one `klt synthesize` drives), mirroring
#: `tests/test_synthesize.py`'s `_WRITE_VERILOG_RE` for that other engine.
#: Deliberately matches only the first (path) argument, not `$` at end of
#: line: since issue #1091 (and, once `request.power` is omitted, issue
#: #1442's own row-rail fallback), this line may carry a trailing
#: `-remove_cells {...}` clause naming the physical-only instances this
#: stage inserted -- the path is always the first token either way.
_WRITE_VERILOG_RE = re.compile(r"^write_verilog (\S+)")
_OUTPUT_DRC_RE = re.compile(r"^detailed_route -output_drc (\S+) -output_maze")


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


def _script_write_verilog_path(script_path: str) -> str | None:
    """The `write_verilog <path>` path a `"route"`-stage script names (issue
    #996), or `None` for a script that writes no netlist."""
    for line in _script_lines(script_path):
        match = _WRITE_VERILOG_RE.match(line)
        if match:
            return match.group(1)
    return None


def _script_output_drc_path(script_path: str) -> str | None:
    """The `detailed_route -output_drc <rpt>` path a `"route"`-stage script
    names -- both `detailed_route` calls in the route branch (pre/post
    `repair_antennas`) write to the same deterministic path, so the first
    match is sufficient (issue #938)."""
    for line in _script_lines(script_path):
        match = _OUTPUT_DRC_RE.match(line)
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


#: The post-route corner-sweep invocation's own `-metrics` dump shape
#: (issue #949) -- deliberately distinct from `_STAGE_METRICS["route"]`'s
#: own `timing__setup__ws`/`timing__hold__ws` values, so a test asserting
#: `worst_setup_slack_ns`/`worst_hold_slack_ns` catches an accidental alias
#: onto the existing nominal-corner-only `worst_slack_ns` field.
_CORNER_SWEEP_METRICS = {
    "timing__setup__ws": -4.02163,
    "timing__hold__ws": 0.08421,
}


def _stub_openroad_success(
    monkeypatch,
    *,
    stages: tuple[str, ...] = place_and_route.STAGE_ORDER,
    setup_violations: dict[str, int] | None = None,
    hold_violations: dict[str, int] | None = None,
    antenna_violations: dict[str, int] | None = None,
    route_drc_violations: int = 0,
    corner_sweep_metrics: dict[str, float] | None = None,
    per_corner_sweep_metrics: dict[str, dict[str, float]] | None = None,
    version: str = "26Q3-771-gdeadbeef",
) -> None:
    setup_violations = setup_violations or {}
    hold_violations = hold_violations or {}
    antenna_violations = antenna_violations or {}
    corner_sweep_metrics = (
        _CORNER_SWEEP_METRICS if corner_sweep_metrics is None else corner_sweep_metrics
    )
    # Issue #1092: per-corner breakdown -- one single-corner OpenROAD
    # invocation per swept corner (`_run_corner_sweep`'s own `len(corners) >
    # 1` branch), each with its own `-metrics` dump. Defaults to reporting
    # `corner_sweep_metrics` for every corner (the same value the combined
    # session's own aggregate uses) unless a test overrides a specific
    # corner's own numbers.
    per_corner_sweep_metrics = per_corner_sweep_metrics or {}

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["openroad", "-version"]:
            # `openroad -version` prints a *bare* version token on its own
            # stdout line -- confirmed live for issue #425's own worked
            # example -- distinct from the `OpenROAD <version>` banner a
            # script invocation prints; see `_openroad_version`'s own
            # docstring.
            return fake_completed(stdout=f"{version} \n")
        assert cmd[0] == "openroad"
        metrics_path = cmd[4]
        script_path = cmd[5]
        if script_path.endswith("_route_corners.tcl"):
            # The post-route corner-sweep script (issue #949,
            # `_corner_sweep_script_lines`) -- a second, standalone OpenROAD
            # invocation, never one of `STAGE_ORDER`'s own four stages, so it
            # never reaches `_stage_from_script_path` below. It writes only
            # its own `-metrics` dump -- no `write_db`/`write_def` line to
            # fake, no violation-marker stdout to emit.
            with open(metrics_path, "w", encoding="utf-8") as handle:
                json.dump(corner_sweep_metrics, handle)
            return fake_completed(returncode=0, stdout="")
        corner_script_match = re.match(
            r".*_route_corner_(?P<name>.+)\.tcl$", os.path.basename(script_path)
        )
        if corner_script_match is not None:
            # Issue #1092's own per-corner single-corner sweep invocation
            # (`_run_corner_sweep`'s `len(corners) > 1` branch) -- same
            # shape as the combined-sweep branch above, one per swept
            # corner, defaulting to `corner_sweep_metrics` unless overridden
            # for that specific corner name.
            corner_name = corner_script_match.group("name")
            with open(metrics_path, "w", encoding="utf-8") as handle:
                json.dump(
                    per_corner_sweep_metrics.get(corner_name, corner_sweep_metrics),
                    handle,
                )
            return fake_completed(returncode=0, stdout="")
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

            # `detailed_route -output_drc <rpt>` writes its own report file
            # (never stdout) -- fake one violation-type-tagged entry per
            # `route_drc_violations`, mirroring the real per-violation
            # `"violation type: <Type>"` header line format confirmed live
            # against a real `openroad/orfs:latest` build (issue #938). A
            # real DRC-clean run writes a 0-byte report, so
            # `route_drc_violations=0` (the default) intentionally writes an
            # empty file rather than skipping the write.
            drc_report_path = _script_output_drc_path(script_path)
            assert drc_report_path is not None
            Path(drc_report_path).write_text(
                "".join(
                    f"violation type: Metal Short\n"
                    f"\tsrcs:\n net: net{i}\n"
                    f"\tbbox = ( 0.0, 0.0 ) - ( 0.1, 0.1 )\nLayer: met1\n"
                    for i in range(route_drc_violations)
                )
            )

        def_path = _script_write_def_path(script_path)
        if def_path is not None:
            Path(def_path).write_text("fake def\n")

        # Issue #996: the `"route"` stage's own `write_verilog` artifact --
        # parsed out of the generated script exactly like the `write_def`
        # path above, so this stub never hardcodes the naming scheme.
        verilog_path = _script_write_verilog_path(script_path)
        if verilog_path is not None:
            Path(verilog_path).write_text("// fake as-built netlist\n")

        return fake_completed(returncode=0, stdout="\n".join(stdout_lines))

    monkeypatch.setattr(place_and_route.subprocess, "run", fake_run)


def _stub_merge_def_to_gds(monkeypatch) -> list[dict]:
    """Replace the DEF->GDS merge with a fake that just writes a placeholder
    file -- covered by its own focused unit tests below, since it needs a
    real DEF + real standard-cell GDS view to exercise meaningfully."""
    calls: list[dict] = []

    def fake_merge(**kwargs):
        calls.append(kwargs)
        Path(kwargs["out_path"]).write_text("fake gds\n")
        return {"path": None, "resolution": "none"}

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
    # Issue #1100: `request.pdk.interconnect_corner` omitted (the default
    # here) resolves to `"nom"` -- byte-identical to this module's original
    # behaviour, independently visible from the liberty corner baked into
    # `provenance.deck.name` below.
    assert report["interconnect_corner"] == "nom"

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
    # Issue #938: a DRC-clean `detailed_route` run (the default,
    # `route_drc_violations=0`) reports `0`, not `null` -- a 0-byte real
    # `-output_drc` report means zero violations, a confirmed value.
    assert report["route_drc_violation_count"] == 0
    assert report["estimated_power_mw"] == pytest.approx(11.6)
    # Issue #783: `clock_skew_ns` is the `stage_reached` ("route") entry's
    # own value, restated at top level -- same convention as every other
    # top-level metric field.
    assert report["clock_skew_ns"] == pytest.approx(0.0389)
    # Issue #949: the corner-swept worst-case setup/hold slack -- from the
    # post-route corner sweep's own `-metrics` dump (`_CORNER_SWEEP_METRICS`
    # above), deliberately distinct from `worst_slack_ns`'s nominal-
    # corner-only value above (-2.18828) -- proves the new fields are not an
    # accidental alias of the existing one.
    assert report["worst_setup_slack_ns"] == pytest.approx(-4.02163)
    assert report["worst_hold_slack_ns"] == pytest.approx(0.08421)

    assert [stage["name"] for stage in report["stages"]] == list(
        place_and_route.STAGE_ORDER
    )
    # floorplan stage has no wirelength/fmax/power/violation-count fields.
    assert "wirelength_um" not in report["stages"][0]
    assert "setup_violation_count" not in report["stages"][0]
    assert "antenna_violation_count" not in report["stages"][0]
    assert "route_drc_violation_count" not in report["stages"][0]
    assert "worst_setup_slack_ns" not in report["stages"][0]
    assert "worst_hold_slack_ns" not in report["stages"][0]
    # place/cts stages report setup/hold but not antenna (route-only check).
    assert "antenna_violation_count" not in report["stages"][1]
    assert "antenna_violation_count" not in report["stages"][2]
    # ... nor the route-only `detailed_route` DRC-violation count (#938)
    # ... nor the route-only corner sweep (#949).
    assert "route_drc_violation_count" not in report["stages"][1]
    assert "route_drc_violation_count" not in report["stages"][2]
    assert "worst_setup_slack_ns" not in report["stages"][1]
    assert "worst_setup_slack_ns" not in report["stages"][2]
    assert "worst_hold_slack_ns" not in report["stages"][1]
    assert "worst_hold_slack_ns" not in report["stages"][2]
    # place stage onward does.
    assert "wirelength_um" in report["stages"][1]
    assert report["stages"][3]["setup_violation_count"] == 3
    assert report["stages"][3]["route_drc_violation_count"] == 0
    assert report["stages"][3]["worst_setup_slack_ns"] == pytest.approx(-4.02163)
    assert report["stages"][3]["worst_hold_slack_ns"] == pytest.approx(0.08421)

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
    # Issue #996: the as-built netlist `write_verilog` produced, alongside
    # (never instead of) the DEF/GDS pair above.
    assert report["verilog_path"] is not None
    assert os.path.isfile(report["verilog_path"])
    assert len(merge_calls) == 1
    assert merge_calls[0]["hdl_toplevel"] == "gcd"

    provenance = report["provenance"]
    assert provenance["klt_version"]
    assert provenance["pdk"]["name"] == "sky130A"
    assert provenance["deck"]["name"] == "sky130_fd_sc_hd__tt_025C_1v80"

    # Issue #1091: `request.power` omitted (the default here) preserves
    # today's exact prior behavior for the *full, caller-configured* PDN --
    # no tapcell/full-PDN `add_global_connection`/`pdngen -- straps` Tcl
    # ever emitted, and the additive `power` field's own
    # `pdn`/`global_connect`/`power_net`/`ground_net`/`tapcell_master`/
    # `endcap_master`/`straps`/`connects` all still say so plainly.
    # `power.row_rail` is the one deliberate exception (issue #1442): a
    # real `sky130_fd_sc_hd` row-rail obstruction is now always emitted at
    # the "route" stage, `request.power` or not -- and, once that
    # obstruction exists, this stage's own `filler_placement`/
    # `global_connect` pair (issue #1091's existing call, previously gated
    # on `request.power` alone) also runs, closing every row gap safely --
    # see `_ROW_RAIL_STRAP`'s own docstring for the live-verified evidence.
    assert report["power"] == {
        "pdn": False,
        "global_connect": False,
        "power_net": None,
        "ground_net": None,
        "tapcell_master": None,
        "endcap_master": None,
        "filler_masters": [],
        "straps": [],
        "connects": [],
        "row_rail": {
            "emitted": True,
            "layer": "met1",
            "power_net": "VPWR",
            "ground_net": "VGND",
            "filler_masters": [
                "sky130_fd_sc_hd__fill_1",
                "sky130_fd_sc_hd__fill_2",
                "sky130_fd_sc_hd__fill_4",
                "sky130_fd_sc_hd__fill_8",
            ],
        },
    }
    floorplan_script = os.path.join(
        os.path.dirname(request_path),
        ".klt",
        "place-and-route",
        "pnr_gcd_floorplan.tcl",
    )
    floorplan_lines = _script_lines(floorplan_script)
    assert not any(
        line.startswith(("tapcell", "add_global_connection", "pdngen"))
        for line in floorplan_lines
    )
    # Issue #1100: the nominal tech LEF (the default `interconnect_corner`)
    # is the one actually loaded, matching this module's original behaviour.
    assert any(
        line.startswith("read_lef") and line.endswith("sky130_fd_sc_hd__nom.tlef")
        for line in floorplan_lines
    )
    route_script = os.path.join(
        os.path.dirname(request_path), ".klt", "place-and-route", "pnr_gcd_route.tcl"
    )
    route_lines = _script_lines(route_script)
    # Issue #1442: the row-rail fallback is present, right before
    # `global_route` -- a real pre-existing obstruction on the same layer
    # (`met1`) `set_routing_layers -signal met1-met5` opens up to signal
    # routing -- and `filler_placement` *does* now run on this
    # `request.power`-less run too, safe only because that obstruction
    # already exists (see `_row_rail_lines`'s own docstring for the
    # live-verified "naive unconditional filler_placement without a row
    # rail shorts VPWR/VGND to signal nets" negative control). `pdngen`
    # carries `-dont_add_pins` -- without it, `pdngen` promotes VPWR/VGND
    # into this design's own top-level Verilog port list even though
    # `define_pdn_grid` above never requested `-pins` (a second, independent
    # port-promotion mechanism, live-verified against a real openroad build
    # to break `tests/test_equiv.py`'s golden/gate port-list comparison --
    # see `_row_rail_lines`'s own docstring).
    assert "pdngen -dont_add_pins" in route_lines
    assert route_lines.index("pdngen -dont_add_pins") < route_lines.index(
        "global_route"
    )
    assert (
        "filler_placement {sky130_fd_sc_hd__fill_1 sky130_fd_sc_hd__fill_2 "
        "sky130_fd_sc_hd__fill_4 sky130_fd_sc_hd__fill_8}" in route_lines
    )
    assert route_lines.index("global_route") < route_lines.index(
        "filler_placement {sky130_fd_sc_hd__fill_1 sky130_fd_sc_hd__fill_2 "
        "sky130_fd_sc_hd__fill_4 sky130_fd_sc_hd__fill_8}"
    )
    assert any(
        line.startswith("add_pdn_stripe") and "-followpins" in line and "met1" in line
        for line in route_lines
    )


# --------------------------------------------------------------------------- #
# `request.pdk.interconnect_corner` (issue #1100) -- tech-LEF parasitic
# corner selection, independent of the liberty (device) corner
# `request.pdk.corner` already selects.
# --------------------------------------------------------------------------- #


def test_stubbed_full_route_resolves_selected_interconnect_corner(
    tmp_path, monkeypatch
):
    """`interconnect_corner: "max"` on an install that stages a `max` tech
    LEF resolves and loads that file, not the nominal one -- and the
    response records it distinctly from the liberty `corner` (encoded only
    in `provenance.deck.name`)."""
    request_path = _setup_success_env(
        tmp_path,
        monkeypatch,
        install_kwargs={"tech_lef_corners": ("min", "nom", "max")},
        pdk={
            "cell_library": "sky130_fd_sc_hd",
            "corner": "tt_025C_1v80",
            "interconnect_corner": "max",
        },
    )
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)

    report = run_place_and_route(request_path)

    assert report["status"] == "ok"
    assert report["interconnect_corner"] == "max"
    # The liberty (device) corner is still visible, independently, via
    # `provenance.deck.name` -- distinct from `interconnect_corner` above,
    # never conflated even though both name "a corner".
    assert report["provenance"]["deck"]["name"] == "sky130_fd_sc_hd__tt_025C_1v80"

    floorplan_script = os.path.join(
        os.path.dirname(request_path),
        ".klt",
        "place-and-route",
        "pnr_gcd_floorplan.tcl",
    )
    floorplan_lines = _script_lines(floorplan_script)
    assert any(
        line.startswith("read_lef") and line.endswith("sky130_fd_sc_hd__max.tlef")
        for line in floorplan_lines
    )
    assert not any(
        line.endswith("sky130_fd_sc_hd__nom.tlef") for line in floorplan_lines
    )


def test_stubbed_full_route_interconnect_corner_min_resolves_distinct_lef(
    tmp_path, monkeypatch
):
    """`interconnect_corner: "min"` resolves a different, correct tech LEF
    path than `"max"` above -- not just "any non-nominal corner works"."""
    request_path = _setup_success_env(
        tmp_path,
        monkeypatch,
        install_kwargs={"tech_lef_corners": ("min", "nom", "max")},
        pdk={
            "cell_library": "sky130_fd_sc_hd",
            "corner": "tt_025C_1v80",
            "interconnect_corner": "min",
        },
    )
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)

    report = run_place_and_route(request_path)

    assert report["interconnect_corner"] == "min"
    floorplan_script = os.path.join(
        os.path.dirname(request_path),
        ".klt",
        "place-and-route",
        "pnr_gcd_floorplan.tcl",
    )
    floorplan_lines = _script_lines(floorplan_script)
    assert any(
        line.startswith("read_lef") and line.endswith("sky130_fd_sc_hd__min.tlef")
        for line in floorplan_lines
    )


def test_run_interconnect_corner_unstaged_on_nominal_only_install_raises(
    tmp_path, monkeypatch
):
    """Issue #1100 edge case: a PDK install that only stages the `"nom"`
    tech LEF (this suite's own default fixture shape) must raise
    `PlaceAndRouteError` for `interconnect_corner: "max"` -- never silently
    fall back to `"nom"`, per `lef_files`'s existing `None`-means-absent
    convention."""
    request_path = _setup_success_env(
        tmp_path,
        monkeypatch,
        pdk={
            "cell_library": "sky130_fd_sc_hd",
            "corner": "tt_025C_1v80",
            "interconnect_corner": "max",
        },
    )

    with pytest.raises(
        PlaceAndRouteError,
        match=r"LEF not found for deck.*requested interconnect_corner 'max'",
    ):
        run_place_and_route(request_path)


def test_stubbed_full_route_with_power_emits_pdn_tapcell_and_filler_tcl(
    tmp_path, monkeypatch
):
    """Issue #1091: `request.power` drives real `tapcell`/
    `add_global_connection`/`global_connect`/`pdngen` Tcl at the end of the
    `"floorplan"` stage (right after `place_macro`/`make_tracks`, before
    that stage's own `write_db`), and `filler_placement`/`global_connect`
    at the end of the `"route"` stage (right after the antenna-repair loop,
    before `write_def`) -- see `_power_delivery_lines`/the module docstring
    "Power delivery" section for the exact insertion points."""
    request_path = _setup_success_env(
        tmp_path,
        monkeypatch,
        power={
            "power_net": "VDD",
            "ground_net": "VSS",
            "straps": _BASE_STRAPS,
        },
    )
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)

    report = run_place_and_route(request_path)

    assert report["status"] == "ok"
    assert report["power"] == {
        "pdn": True,
        "global_connect": True,
        "power_net": "VDD",
        "ground_net": "VSS",
        "tapcell_master": "sky130_fd_sc_hd__tapvpwrvgnd_1",
        "endcap_master": None,
        "filler_masters": [
            "sky130_fd_sc_hd__fill_1",
            "sky130_fd_sc_hd__fill_2",
            "sky130_fd_sc_hd__fill_4",
            "sky130_fd_sc_hd__fill_8",
        ],
        # issue #1133: no `spacing_um`/`connects[]` given -- echoed back as
        # `None`/no-tuning for every strap/pair, confirming the additive
        # fields don't fabricate anything when omitted.
        "straps": [
            {"layer": "met1", "spacing_um": None},
            {"layer": "met4", "spacing_um": None},
            {"layer": "met5", "spacing_um": None},
        ],
        "connects": [
            {
                "layers": ["met1", "met4"],
                "max_columns": None,
                "ongrid": None,
                "split_cuts": None,
            },
            {
                "layers": ["met4", "met5"],
                "max_columns": None,
                "ongrid": None,
                "split_cuts": None,
            },
        ],
        # Issue #1442: a `request.power`-bearing run's own `straps[]`
        # already carries a `met1` `-followpins` entry (`_BASE_STRAPS`
        # below) -- the row-rail fallback below is `request.power`'s own
        # complement, never active alongside it.
        "row_rail": {
            "emitted": False,
            "layer": None,
            "power_net": None,
            "ground_net": None,
            "filler_masters": [],
        },
    }

    floorplan_script = os.path.join(
        os.path.dirname(request_path),
        ".klt",
        "place-and-route",
        "pnr_gcd_floorplan.tcl",
    )
    floorplan_lines = _script_lines(floorplan_script)
    # `tapcell` -- well/substrate ties, before the PDN grid Tcl.
    assert (
        "tapcell -distance 14 -tapcell_master sky130_fd_sc_hd__tapvpwrvgnd_1"
        in floorplan_lines
    )
    # `add_global_connection` -- one call per `_POWER_PIN_PATTERNS` entry,
    # with the primary VDD/VSS pair carrying the `-power`/`-ground` flag.
    assert (
        "add_global_connection -net {VDD} -inst_pattern {.*} "
        "-pin_pattern {^VDD$} -power" in floorplan_lines
    )
    assert (
        "add_global_connection -net {VDD} -inst_pattern {.*} "
        "-pin_pattern {VPB}" in floorplan_lines
    )
    assert (
        "add_global_connection -net {VSS} -inst_pattern {.*} "
        "-pin_pattern {^VSS$} -ground" in floorplan_lines
    )
    assert floorplan_lines.count("global_connect") == 1
    assert (
        "set_voltage_domain -name {CORE} -power {VDD} -ground {VSS}" in floorplan_lines
    )
    assert (
        "define_pdn_grid -name {grid} -voltage_domains {CORE} -pins {met5}"
        in floorplan_lines
    )
    assert (
        "add_pdn_stripe -grid {grid} -layer {met1} -width {0.48} "
        "-pitch {5.44} -offset {0.0} -followpins" in floorplan_lines
    )
    assert (
        "add_pdn_stripe -grid {grid} -layer {met4} -width {1.6} "
        "-pitch {27.14} -offset {13.57}" in floorplan_lines
    )
    assert "add_pdn_connect -grid {grid} -layers {met1 met4}" in floorplan_lines
    assert "add_pdn_connect -grid {grid} -layers {met4 met5}" in floorplan_lines
    assert "pdngen" in floorplan_lines
    # PDN Tcl must come after `make_tracks` and before `write_db` -- the
    # same "end of floorplan stage" insertion point OpenROAD-flow-scripts
    # itself uses.
    assert floorplan_lines.index("make_tracks") < floorplan_lines.index(
        "tapcell -distance 14 -tapcell_master sky130_fd_sc_hd__tapvpwrvgnd_1"
    )
    floorplan_write_db_index = next(
        i for i, line in enumerate(floorplan_lines) if line.startswith("write_db ")
    )
    assert floorplan_lines.index("pdngen") < floorplan_write_db_index

    route_script = os.path.join(
        os.path.dirname(request_path), ".klt", "place-and-route", "pnr_gcd_route.tcl"
    )
    route_lines = _script_lines(route_script)
    assert (
        "filler_placement {sky130_fd_sc_hd__fill_1 sky130_fd_sc_hd__fill_2 "
        "sky130_fd_sc_hd__fill_4 sky130_fd_sc_hd__fill_8}" in route_lines
    )
    assert route_lines.count("global_connect") == 1
    filler_index = route_lines.index(
        "filler_placement {sky130_fd_sc_hd__fill_1 sky130_fd_sc_hd__fill_2 "
        "sky130_fd_sc_hd__fill_4 sky130_fd_sc_hd__fill_8}"
    )
    write_def_index = next(
        i for i, line in enumerate(route_lines) if line.startswith("write_def ")
    )
    assert filler_index < write_def_index
    # `write_verilog` strips the same physical-only masters via
    # `-remove_cells`, keeping the as-built netlist diffable against `klt
    # synthesize`'s own netlist (which never contains tap/fill cells).
    write_verilog_line = next(
        line for line in route_lines if line.startswith("write_verilog ")
    )
    assert "-remove_cells {sky130_fd_sc_hd__tapvpwrvgnd_1" in write_verilog_line
    assert "sky130_fd_sc_hd__fill_1" in write_verilog_line


def test_stubbed_floorplan_with_spacing_and_connect_tuning_reproduces_platform_pdn(
    tmp_path, monkeypatch
):
    """Issue #1133: `straps[].spacing_um` -> `add_pdn_stripe -spacing`, and
    `power.connects[]` -> `add_pdn_connect -max_columns`/`-ongrid`/
    `-split_cuts` -- reproducing gf180's own
    `flow/platforms/gf180/openROAD/pdn/pdn_grid_strategy_9t_6M.cfg` (cited
    verbatim in the issue):

        add_pdn_stripe  -grid {block} -layer {Metal4} -width {4.480}
            -spacing {0.56} -pitch {44.8} -offset {22.4}
        add_pdn_connect -grid {block} -layers {Metal1 Metal4}
            -max_columns {5} -ongrid {Metal2 Metal3 Metal4}
            -split_cuts {Metal3 0.128}
        add_pdn_connect -grid {block} -layers {Metal4 Metal5}
    """
    request_path = _setup_success_env(
        tmp_path,
        monkeypatch,
        target_stage="floorplan",
        power={
            "power_net": "VDD",
            "ground_net": "VSS",
            "straps": [
                {
                    "layer": "met1",
                    "width_um": 0.48,
                    "pitch_um": 5.44,
                    "followpins": True,
                },
                {
                    "layer": "met4",
                    "width_um": 4.480,
                    "pitch_um": 44.8,
                    "offset_um": 22.4,
                    "spacing_um": 0.56,
                },
                {"layer": "met5", "width_um": 1.6, "pitch_um": 27.2, "offset_um": 13.6},
            ],
            "connects": [
                {
                    "layers": ["met1", "met4"],
                    "max_columns": 5,
                    "ongrid": ["met2", "met3", "met4"],
                    "split_cuts": {"layer": "met3", "width_um": 0.128},
                }
                # `met4`/`met5` deliberately left untuned -- must fall back
                # to a plain `add_pdn_connect`, matching the platform
                # config's own `add_pdn_connect -grid {block} -layers
                # {Metal4 Metal5}` line (no via-stack flags).
            ],
        },
    )
    _stub_openroad_success(monkeypatch, stages=("floorplan",))

    report = run_place_and_route(request_path)

    assert report["status"] == "ok"
    assert report["power"]["straps"] == [
        {"layer": "met1", "spacing_um": None},
        {"layer": "met4", "spacing_um": 0.56},
        {"layer": "met5", "spacing_um": None},
    ]
    assert report["power"]["connects"] == [
        {
            "layers": ["met1", "met4"],
            "max_columns": 5,
            "ongrid": ["met2", "met3", "met4"],
            "split_cuts": {"layer": "met3", "width_um": 0.128},
        },
        {
            "layers": ["met4", "met5"],
            "max_columns": None,
            "ongrid": None,
            "split_cuts": None,
        },
    ]

    floorplan_script = os.path.join(
        os.path.dirname(request_path),
        ".klt",
        "place-and-route",
        "pnr_gcd_floorplan.tcl",
    )
    floorplan_lines = _script_lines(floorplan_script)
    assert (
        "add_pdn_stripe -grid {grid} -layer {met4} -width {4.48} "
        "-spacing {0.56} -pitch {44.8} -offset {22.4}" in floorplan_lines
    )
    assert (
        "add_pdn_connect -grid {grid} -layers {met1 met4} -max_columns {5} "
        "-ongrid {met2 met3 met4} -split_cuts {met3 0.128}" in floorplan_lines
    )
    # No tuning given for met4/met5 -- falls back to the plain call, matching
    # this module's prior (and the platform config's own) untuned pair.
    assert "add_pdn_connect -grid {grid} -layers {met4 met5}" in floorplan_lines


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


def test_stubbed_full_route_reports_nonzero_route_drc_violation_count(
    tmp_path, monkeypatch
):
    """`route_drc_violation_count` parses a multi-entry `-output_drc` report
    (issue #938), not just the DRC-clean/zero-violations happy path above."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch, route_drc_violations=5)
    _stub_merge_def_to_gds(monkeypatch)

    report = run_place_and_route(request_path)

    assert report["route_drc_violation_count"] == 5
    assert report["stages"][3]["route_drc_violation_count"] == 5


# --------------------------------------------------------------------------- #
# `request.post_route_spef` (issue #948, Epic #700 Phase 3): real routed-
# geometry parasitics via `klt extract --parasitics`, fed back into a fresh
# OpenSTA session via `read_spef`, alongside (never replacing) the `"route"`
# stage's own `estimate_parasitics -global_routing`-derived top-level fields.
# --------------------------------------------------------------------------- #


def _stub_run_extract_for_post_route_spef(
    monkeypatch, *, net_names: tuple[str, ...] = ("A", "B", "CLK")
) -> list[dict]:
    """Replace `klayout_tools.extract.run_extract` (the function
    `_post_route_spef_metrics` imports lazily) with a fake that writes a
    placeholder SPEF file and returns a minimal `parasitics` block -- this
    module's own `_post_route_spef_metrics` only reads `parasitics.nets[]`'s
    `net` field and the `spef_output` path it was given, so nothing here
    needs a real GDS/deck. Covered on its own merits by
    `tests/test_extract.py`'s SPEF-writer tests."""
    calls: list[dict] = []

    def fake_run_extract(path, deck_name, **kwargs):
        calls.append({"path": path, "deck_name": deck_name, **kwargs})
        spef_output = kwargs["spef_output"]
        Path(spef_output).write_text("fake spef\n")
        return {"parasitics": {"nets": [{"net": name} for name in net_names]}}

    monkeypatch.setattr(extract_module, "run_extract", fake_run_extract)
    return calls


def _stub_openroad_success_with_post_route_spef(
    monkeypatch,
    *,
    setup_violation_count: int = 0,
    hold_violation_count: int = 0,
    nets_annotated: int = 3,
    nets_total: int = 3,
    design_nets_annotated: int | None = None,
    design_nets_total: int | None = None,
    worst_slack_ns: float = -0.5,
    total_negative_slack_ns: float = -1.0,
    writes_sdf: bool | None = None,
) -> None:
    """Layer the second, `_route_spef.tcl`-only `openroad` invocation on top
    of `_stub_openroad_success`'s existing per-stage fake -- that helper's
    own `_stage_from_script_path` cannot classify this new script name (it
    is not one of `STAGE_ORDER`'s own `_<stage>.tcl` suffixes), so this
    wraps it rather than editing its shared behaviour.

    `writes_sdf` (issue #1002) stands in for the session's own `write_sdf`
    call: `True` creates the file the generated script names, `False`
    deliberately does not (OpenSTA reporting success while producing
    nothing), and `None` -- the default -- asserts no `write_sdf` line was
    generated at all."""
    _stub_openroad_success(monkeypatch)
    base_fake_run = place_and_route.subprocess.run

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["openroad", "-version"] or not cmd[5].endswith(
            "_route_spef.tcl"
        ):
            return base_fake_run(cmd, **kwargs)

        # Stand in for the script's own `write_sdf` call (issue #1002): read
        # the generated Tcl back and honour (or deliberately ignore) it.
        script_text = Path(cmd[5]).read_text()
        write_sdf_lines = [
            line for line in script_text.splitlines() if line.startswith("write_sdf")
        ]
        if writes_sdf is None:
            assert not write_sdf_lines
        else:
            assert len(write_sdf_lines) == 1
            if writes_sdf:
                Path(write_sdf_lines[0].split()[-1]).write_text("(DELAYFILE)\n")

        metrics_path = cmd[4]
        with open(metrics_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "timing__setup__ws": worst_slack_ns,
                    "timing__setup__tns": total_negative_slack_ns,
                },
                handle,
            )
        # Two lines since issue #951: the SPEF-side pair, then the
        # design-side pair `annotation_complete` is actually keyed on.
        # `design_nets_*` default to mirroring the SPEF-side pair so every
        # pre-#951 call site keeps its original meaning.
        design_annotated = (
            nets_annotated if design_nets_annotated is None else design_nets_annotated
        )
        design_total = nets_total if design_nets_total is None else design_nets_total
        stdout_lines = [
            place_and_route._SPEF_NET_CHECK_BEGIN,
            f"{nets_annotated} {nets_total}",
            f"{design_annotated} {design_total}",
            place_and_route._SPEF_NET_CHECK_END,
            place_and_route._SETUP_VIOLATIONS_BEGIN,
        ]
        stdout_lines += [f"pin_{i} (VIOLATED)" for i in range(setup_violation_count)]
        stdout_lines.append(place_and_route._SETUP_VIOLATIONS_END)
        stdout_lines.append(place_and_route._HOLD_VIOLATIONS_BEGIN)
        stdout_lines += [f"pin_{i} (VIOLATED)" for i in range(hold_violation_count)]
        stdout_lines.append(place_and_route._HOLD_VIOLATIONS_END)
        return fake_completed(returncode=0, stdout="\n".join(stdout_lines))

    monkeypatch.setattr(place_and_route.subprocess, "run", fake_run)


def test_post_route_spef_omitted_defaults_false_and_leaves_spef_sta_null(
    tmp_path, monkeypatch
):
    """`request.post_route_spef` omitted (the default) is byte-identical to
    before this feature existed: no second `openroad` invocation, no `klt
    extract` call, and the additive `spef_sta` field is `null`."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)

    report = run_place_and_route(request_path)

    assert report["spef_sta"] is None


def test_post_route_spef_must_be_boolean(tmp_path, monkeypatch):
    request_path = _setup_success_env(tmp_path, monkeypatch, post_route_spef="yes")
    with pytest.raises(PlaceAndRouteError, match="post_route_spef must be a boolean"):
        run_place_and_route(request_path)


def test_post_route_spef_reports_spef_sta_block(tmp_path, monkeypatch):
    """The acceptance-criteria shape (issue #948): opting in produces a
    `spef_sta` block with the `read_spef`-fed slack/violation counts and the
    net-name-correlation sanity check ("N of M nets annotated"), without
    disturbing the existing top-level (`estimate_parasitics -global_routing`)
    fields -- an A/B report, not a replacement."""
    request_path = _setup_success_env(tmp_path, monkeypatch, post_route_spef=True)
    _stub_openroad_success_with_post_route_spef(
        monkeypatch,
        setup_violation_count=2,
        hold_violation_count=1,
        nets_annotated=3,
        nets_total=3,
        worst_slack_ns=-0.123,
        total_negative_slack_ns=-0.456,
    )
    _stub_merge_def_to_gds(monkeypatch)
    extract_calls = _stub_run_extract_for_post_route_spef(monkeypatch)

    report = run_place_and_route(request_path)

    # The existing `estimate_parasitics -global_routing`-derived top-level
    # fields are untouched -- this is an A/B addition, never a replacement
    # (`_STAGE_METRICS["route"]`'s own values, unchanged from every other
    # stubbed-success test above).
    assert report["worst_slack_ns"] == pytest.approx(-2.18828)

    spef_sta = report["spef_sta"]
    assert spef_sta is not None
    assert spef_sta["worst_slack_ns"] == pytest.approx(-0.123)
    assert spef_sta["total_negative_slack_ns"] == pytest.approx(-0.456)
    assert spef_sta["setup_violation_count"] == 2
    assert spef_sta["hold_violation_count"] == 1
    assert spef_sta["nets_annotated"] == 3
    assert spef_sta["nets_total"] == 3
    assert spef_sta["spef_path"].endswith(".spef")
    assert os.path.isfile(spef_sta["spef_path"])

    # `klt extract --parasitics` ran against the merged routed GDS (never a
    # separate/second extraction target), with `--spef` wired to the same
    # path `spef_sta.spef_path` reports.
    assert len(extract_calls) == 1
    assert extract_calls[0]["path"] == report["gds_path"]
    assert extract_calls[0]["parasitics"] is True
    assert extract_calls[0]["spef_output"] == spef_sta["spef_path"]


def test_post_route_spef_flags_unmatched_nets(tmp_path, monkeypatch):
    """Net-name correlation is explicitly checked and reported, not assumed
    (issue #948 scope item 3) -- a partial match still returns a usable
    `spef_sta` block, with the design-side shortfall stated plainly."""
    request_path = _setup_success_env(tmp_path, monkeypatch, post_route_spef=True)
    _stub_openroad_success_with_post_route_spef(
        monkeypatch, nets_annotated=1, nets_total=3
    )
    _stub_merge_def_to_gds(monkeypatch)
    _stub_run_extract_for_post_route_spef(monkeypatch)

    report = run_place_and_route(request_path)

    assert report["spef_sta"]["nets_annotated"] == 1
    assert report["spef_sta"]["nets_total"] == 3
    # ...and stated *in words*, not left as two integers a caller has to
    # notice on their own -- the "do not silently drop annotation on
    # unmatched nets" half of issue #948's own scope item 3.
    assert report["spef_sta"]["annotation_complete"] is False
    warning = report["spef_sta"]["annotation_warning"]
    assert warning is not None
    assert "1 of 3" in warning


def test_post_route_spef_annotation_complete_keys_on_the_design_side_ratio(
    tmp_path, monkeypatch
):
    """Issue #951: a run whose SPEF names every net the design has is a real
    -parasitics measurement even though the SPEF *also* carries hundreds of
    intra-standard-cell nodes the gate-level design never had -- so
    `annotation_complete` is keyed on `design_nets_annotated ==
    design_nets_total`, not on the SPEF-side ratio, which cannot reach 1 by
    construction."""
    request_path = _setup_success_env(tmp_path, monkeypatch, post_route_spef=True)
    _stub_openroad_success_with_post_route_spef(
        monkeypatch,
        nets_annotated=458,
        nets_total=1199,
        design_nets_annotated=458,
        design_nets_total=458,
    )
    _stub_merge_def_to_gds(monkeypatch)
    _stub_run_extract_for_post_route_spef(monkeypatch)

    spef_sta = run_place_and_route(request_path)["spef_sta"]

    assert (spef_sta["nets_annotated"], spef_sta["nets_total"]) == (458, 1199)
    assert (spef_sta["design_nets_annotated"], spef_sta["design_nets_total"]) == (
        458,
        458,
    )
    assert spef_sta["annotation_complete"] is True
    assert spef_sta["annotation_warning"] is None


def test_post_route_spef_extracts_with_def_derived_net_names(tmp_path, monkeypatch):
    """Issue #951's actual fix: the `klt extract` pass behind `spef_sta`
    names routed nets from the DEF net name the DEF->GDS merge left on their
    geometry (`def_net_names=True`), not from GDS text labels -- which the
    merge only emits for top-level pins, leaving every internal net under a
    synthesized name no OpenSTA net is called (measured `0` annotated on all
    three corpus designs when #948 shipped)."""
    request_path = _setup_success_env(tmp_path, monkeypatch, post_route_spef=True)
    _stub_openroad_success_with_post_route_spef(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)
    extract_calls = _stub_run_extract_for_post_route_spef(monkeypatch)

    run_place_and_route(request_path)

    assert extract_calls[0]["def_net_names"] is True
    # This suite's own `_stub_openroad_success` writes a placeholder DEF with
    # no `PINS` section (issue #961's own fixtures never need a real one) --
    # `_def_pin_net_names` returns `None` for that, which is `run_extract`'s
    # own "no restriction" default, rather than an empty set that would
    # wrongly declare the design to have zero ports.
    assert extract_calls[0]["declared_pins"] is None


def _overlay_real_def_pins(monkeypatch, *, def_pins_text: str) -> None:
    """Wraps whatever ``place_and_route.subprocess.run`` fake is *currently*
    installed (call this *after* the other `_stub_openroad_success*` helpers
    below, not before -- each of those calls `_stub_openroad_success`
    internally and would otherwise reset this wrapper right back off):
    after the wrapped fake handles a call, overwrite any DEF it just wrote
    (the `"route"` stage's own placeholder `"fake def\\n"`) with
    ``def_pins_text`` -- a real ``PINS`` section -- so
    ``_post_route_spef_metrics``'s ``_def_pin_net_names`` parse (issue #961's
    defect-1 fix) has real DEF port declarations to read."""
    base_fake_run = place_and_route.subprocess.run

    def fake_run(cmd, **kwargs):
        result = base_fake_run(cmd, **kwargs)
        if cmd[:2] != ["openroad", "-version"]:
            script_path = cmd[5]
            if not script_path.endswith("_route_corners.tcl"):
                def_path = _script_write_def_path(script_path)
                if def_path is not None:
                    Path(def_path).write_text(def_pins_text)
        return result

    monkeypatch.setattr(place_and_route.subprocess, "run", fake_run)


def test_post_route_spef_declares_pins_from_def_pins_section(tmp_path, monkeypatch):
    """Issue #961 defect 1: with `def_net_names=True` every routed net now
    carries a real name, so without this fix `Netlist.make_top_level_pins()`
    would promote every one of them -- the written SPEF's `*PORTS`/`*P` list
    would wrongly declare all 537 of `gcd`'s routed nets top-level ports
    instead of just its actual I/O. The routed DEF's own `PINS` section is
    the ground truth for which nets are real design ports; `run_extract`
    must be called with exactly that set as `declared_pins`."""
    request_path = _setup_success_env(tmp_path, monkeypatch, post_route_spef=True)
    _stub_openroad_success_with_post_route_spef(monkeypatch)
    _overlay_real_def_pins(
        monkeypatch,
        def_pins_text=(
            "PINS 3 ;\n"
            "- clk + NET clk + DIRECTION INPUT + USE SIGNAL\n"
            "  + LAYER met3 ( -70 -70 ) ( 70 70 )\n"
            "  + PLACED ( 1000 2000 ) N ;\n"
            "- req_val + NET req_val + DIRECTION INPUT + USE SIGNAL ;\n"
            "- resp_val + NET resp_val + DIRECTION OUTPUT + USE SIGNAL ;\n"
            "END PINS\n"
        ),
    )
    _stub_merge_def_to_gds(monkeypatch)
    extract_calls = _stub_run_extract_for_post_route_spef(monkeypatch)

    run_place_and_route(request_path)

    assert extract_calls[0]["declared_pins"] == frozenset(
        {"clk", "req_val", "resp_val"}
    )


def test_post_route_spef_passes_def_net_connections_from_nets_section(
    tmp_path, monkeypatch
):
    """Issue #961's own remaining scope (device-terminal `*CONN` pin
    correlation): the routed DEF's own `NETS` section is parsed via
    `extract.def_net_instance_pins` and threaded through to `run_extract`'s
    `def_net_connections` kwarg, so the written SPEF can wire real
    `*I <inst>:<pin>` connectivity into its RC network."""
    request_path = _setup_success_env(tmp_path, monkeypatch, post_route_spef=True)
    _stub_openroad_success_with_post_route_spef(monkeypatch)
    _overlay_real_def_pins(
        monkeypatch,
        def_pins_text=(
            "NETS 1 ;\n    - net1 ( u1 A ) ( u2 Y ) + USE SIGNAL ;\nEND NETS\n"
        ),
    )
    _stub_merge_def_to_gds(monkeypatch)
    extract_calls = _stub_run_extract_for_post_route_spef(monkeypatch)

    run_place_and_route(request_path)

    assert extract_calls[0]["def_net_connections"] == {
        "net1": (("u1", "A"), ("u2", "Y"))
    }


def test_post_route_spef_zero_annotation_is_reported_not_hidden(tmp_path, monkeypatch):
    """The measured reality on the routed `gcd` corpus fixture today (`0 of
    981`, verified live against `openroad/orfs:latest` while implementing
    issue #948): `klt extract` names nets from GDS text labels, and the
    DEF->GDS merge labels top-level pins only, so no internal routed net's
    SPEF name resolves in the linked design. That case must still return a
    `spef_sta` block whose own fields say so plainly -- never a block that
    reads like a real-parasitics measurement."""
    request_path = _setup_success_env(tmp_path, monkeypatch, post_route_spef=True)
    _stub_openroad_success_with_post_route_spef(
        monkeypatch,
        nets_annotated=0,
        nets_total=981,
        design_nets_annotated=0,
        design_nets_total=458,
    )
    _stub_merge_def_to_gds(monkeypatch)
    _stub_run_extract_for_post_route_spef(monkeypatch)

    report = run_place_and_route(request_path)

    spef_sta = report["spef_sta"]
    assert spef_sta["nets_annotated"] == 0
    assert spef_sta["nets_total"] == 981
    assert spef_sta["design_nets_annotated"] == 0
    assert spef_sta["annotation_complete"] is False
    assert "0 of 458" in spef_sta["annotation_warning"]


def test_post_route_spef_full_annotation_sets_no_warning(tmp_path, monkeypatch):
    """The converse: a 100%-correlated run carries `annotation_complete:
    true` and a `null` warning, so the warning field is a real signal rather
    than boilerplate present on every response."""
    request_path = _setup_success_env(tmp_path, monkeypatch, post_route_spef=True)
    _stub_openroad_success_with_post_route_spef(
        monkeypatch, nets_annotated=3, nets_total=3
    )
    _stub_merge_def_to_gds(monkeypatch)
    _stub_run_extract_for_post_route_spef(monkeypatch)

    report = run_place_and_route(request_path)

    assert report["spef_sta"]["annotation_complete"] is True
    assert report["spef_sta"]["annotation_warning"] is None


# --------------------------------------------------------------------------- #
# `request.post_route_sdf` (issue #1002, survey section 4.3): `write_sdf`
# inside the same post-`read_spef` OpenSTA session, so a downstream
# `klt functional-verification --options.sdf` run re-simulates the design's
# own testbench with real routed delays.
# --------------------------------------------------------------------------- #


def test_post_route_sdf_omitted_leaves_sdf_path_null(tmp_path, monkeypatch):
    """The default is off, and the additive `sdf_path` member is `null` --
    `post_route_spef`'s own behaviour is unchanged by this feature's
    existence."""
    request_path = _setup_success_env(tmp_path, monkeypatch, post_route_spef=True)
    _stub_openroad_success_with_post_route_spef(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)
    _stub_run_extract_for_post_route_spef(monkeypatch)

    report = run_place_and_route(request_path)

    assert report["spef_sta"]["sdf_path"] is None
    script = (
        tmp_path / ".klt" / "place-and-route" / "pnr_gcd_route_spef.tcl"
    ).read_text()
    assert "write_sdf" not in script


def test_post_route_sdf_must_be_boolean(tmp_path, monkeypatch):
    request_path = _setup_success_env(
        tmp_path, monkeypatch, post_route_spef=True, post_route_sdf="yes"
    )
    with pytest.raises(PlaceAndRouteError, match="post_route_sdf must be a boolean"):
        run_place_and_route(request_path)


def test_post_route_sdf_requires_post_route_spef(tmp_path, monkeypatch):
    """A request error, not a silent no-op: an SDF written from a session fed
    by `estimate_parasitics -global_routing` would carry the coarse estimate's
    delays while looking exactly like a post-route measurement to every
    downstream simulation that annotates it."""
    request_path = _setup_success_env(tmp_path, monkeypatch, post_route_sdf=True)
    with pytest.raises(
        PlaceAndRouteError, match="post_route_sdf requires request.post_route_spef"
    ):
        run_place_and_route(request_path)


def test_post_route_sdf_writes_and_reports_the_sdf(tmp_path, monkeypatch):
    """Opting in emits the `write_sdf` Tcl line into the *same* session that
    just ran `read_spef`, and reports the written path as `spef_sta.sdf_path`
    -- the artifact `klt functional-verification`'s `options.sdf` consumes."""
    request_path = _setup_success_env(
        tmp_path, monkeypatch, post_route_spef=True, post_route_sdf=True
    )
    _stub_openroad_success_with_post_route_spef(monkeypatch, writes_sdf=True)
    _stub_merge_def_to_gds(monkeypatch)
    _stub_run_extract_for_post_route_spef(monkeypatch)

    report = run_place_and_route(request_path)

    sdf_path = report["spef_sta"]["sdf_path"]
    assert sdf_path.endswith("gcd_route.sdf")
    assert os.path.isfile(sdf_path)
    script = (
        tmp_path / ".klt" / "place-and-route" / "pnr_gcd_route_spef.tcl"
    ).read_text()
    assert f"write_sdf -divider . -include_typ {sdf_path}" in script
    # The SPEF half is untouched -- this is one added line, not a new pass.
    assert report["spef_sta"]["spef_path"].endswith(".spef")


def test_post_route_sdf_missing_output_file_is_an_error(tmp_path, monkeypatch):
    """Fail loud rather than promise an artifact that is not there: a
    `sdf_path` a downstream run cannot open surfaces there as a *silent*
    zero-delay simulation (Icarus treats an unopenable SDF as a non-fatal
    `SDF WARNING` and `vvp` still exits 0)."""
    request_path = _setup_success_env(
        tmp_path, monkeypatch, post_route_spef=True, post_route_sdf=True
    )
    # `writes_sdf=False`: OpenSTA reports success but produces no file.
    _stub_openroad_success_with_post_route_spef(monkeypatch, writes_sdf=False)
    _stub_merge_def_to_gds(monkeypatch)
    _stub_run_extract_for_post_route_spef(monkeypatch)

    with pytest.raises(PlaceAndRouteError, match="wrote no SDF file"):
        run_place_and_route(request_path)


def test_post_route_spef_not_run_before_route_stage(tmp_path, monkeypatch):
    """`post_route_spef: true` has no effect on a run that never reaches the
    `"route"` stage -- there is no routed GDS to extract parasitics from
    yet, mirroring `gds_path`/`def_path`'s own pre-`"route"` `null`."""
    request_path = _setup_success_env(
        tmp_path, monkeypatch, target_stage="place", post_route_spef=True
    )
    _stub_openroad_success(monkeypatch, stages=("floorplan", "place"))

    report = run_place_and_route(request_path)

    assert report["stage_reached"] == "place"
    assert report["spef_sta"] is None


def test_post_route_spef_unsupported_cell_library_raises(tmp_path, monkeypatch):
    """An unrecognised `cell_library` (no `_EXTRACT_DECK_FOR_CELL_LIBRARY`
    entry) fails loudly -- never a silent skip of the requested A/B pass."""
    monkeypatch.delitem(
        place_and_route._EXTRACT_DECK_FOR_CELL_LIBRARY, "sky130_fd_sc_hd"
    )
    request_path = _setup_success_env(tmp_path, monkeypatch, post_route_spef=True)
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)

    with pytest.raises(
        PlaceAndRouteError, match="post_route_spef requires a known klt extract deck"
    ):
        run_place_and_route(request_path)


# --------------------------------------------------------------------------- #
# `_def_pin_net_names` (issue #961 defect 1): the routed DEF's own `PINS`
# section, parsed for the design's genuine top-level port net names -- fed
# to `_post_route_spef_metrics`'s `run_extract` call as `declared_pins` so
# the written SPEF's `*PORTS`/`*P` list stops wrongly declaring every
# `def_net_names`-renamed routed net a top-level port.
# --------------------------------------------------------------------------- #


def test_def_pin_net_names_parses_a_real_pins_section(tmp_path):
    def_path = tmp_path / "top.def"
    def_path.write_text(
        "VERSION 5.8 ;\n"
        "DESIGN gcd ;\n"
        "PINS 3 ;\n"
        "- clk + NET clk + DIRECTION INPUT + USE SIGNAL\n"
        "  + LAYER met3 ( -70 -70 ) ( 70 70 )\n"
        "  + PLACED ( 1000 2000 ) N ;\n"
        "- req_val + NET req_val + DIRECTION INPUT + USE SIGNAL ;\n"
        "- resp_val + NET resp_val + DIRECTION OUTPUT + USE SIGNAL ;\n"
        "END PINS\n"
        "END DESIGN\n"
    )
    assert place_and_route._def_pin_net_names(str(def_path)) == frozenset(
        {"clk", "req_val", "resp_val"}
    )


def test_def_pin_net_names_uses_net_clause_when_pin_name_differs(tmp_path):
    """A bus pin's own name can legitimately differ from the net it
    connects to -- the `+ NET <netName>` clause, when present, is what
    `klt extract`'s own net names will match, so it (not the pin name) is
    what belongs in `declared_pins`."""
    def_path = tmp_path / "top.def"
    def_path.write_text(
        "PINS 1 ;\n- data_pin[0] + NET data_bus[0] + DIRECTION INPUT ;\nEND PINS\n"
    )
    assert place_and_route._def_pin_net_names(str(def_path)) == frozenset(
        {"data_bus[0]"}
    )


def test_def_pin_net_names_falls_back_to_pin_name_without_net_clause(tmp_path):
    def_path = tmp_path / "top.def"
    def_path.write_text("PINS 1 ;\n- clk + DIRECTION INPUT ;\nEND PINS\n")
    assert place_and_route._def_pin_net_names(str(def_path)) == frozenset({"clk"})


def test_def_pin_net_names_none_when_no_pins_section(tmp_path):
    """This suite's own placeholder DEF fixtures (`"fake def\\n"``) and any
    genuinely malformed/non-DEF file alike -- `None`, never an empty set, so
    the caller never mistakes "could not determine" for "this design
    declares zero ports"."""
    def_path = tmp_path / "top.def"
    def_path.write_text("fake def\n")
    assert place_and_route._def_pin_net_names(str(def_path)) is None


def test_def_pin_net_names_none_when_pins_section_is_empty(tmp_path):
    """A present-but-empty (or unparseable) `PINS` section is a parse
    failure, not a real design with no I/O -- anything that reached
    `_post_route_spef_metrics` has at least the required
    `constraints.clock_port`. `None` keeps `run_extract` unrestricted rather
    than demoting every net."""
    def_path = tmp_path / "top.def"
    def_path.write_text("DESIGN gcd ;\nPINS 0 ;\nEND PINS\nEND DESIGN\n")
    assert place_and_route._def_pin_net_names(str(def_path)) is None


def test_def_pin_net_names_none_when_file_missing(tmp_path):
    assert place_and_route._def_pin_net_names(str(tmp_path / "missing.def")) is None


def test_tcl_net_list_brace_quotes_each_name():
    assert place_and_route._tcl_net_list(["A", "B|C", "clk"]) == "{A} {B|C} {clk}"


def test_spef_sta_script_embeds_net_names_unescaped_in_the_generated_tcl():
    """The generated Tcl carries each name exactly as given (issue #951) --
    the check is only honest if `get_nets` sees the real net's own name, not
    a backslash-mangled one that matches nothing. A prior version of this
    code backslash-escaped `get_nets`'s glob metacharacters; because
    `_tcl_net_list` brace-quotes each name and Tcl brace-quoting performs no
    backslash substitution, that escaping reached `get_nets` as literal
    backslash characters and matched nothing -- invisible while #948's
    baseline correlation was 0%, and a live-reproduced bug the moment real
    names started matching."""
    lines = place_and_route._spef_sta_script_lines(
        checkpoint_in="/tmp/x.odb",
        liberty_path="/tmp/x.lib",
        clock_port="clk",
        clock_period_ns=1.1,
        spef_path="/tmp/x.spef",
        net_names=["a_in[13]", "_019_"],
    )
    net_list_line = next(line for line in lines if line.startswith("set klt_spef_nets"))
    assert "{a_in[13]}" in net_list_line
    assert "{_019_}" in net_list_line
    assert "\\" not in net_list_line
    # The correlation check runs *before* `read_spef`, so a `read_spef`
    # failure can never also swallow the check.
    assert lines.index(f'puts "{place_and_route._SPEF_NET_CHECK_END}"') < lines.index(
        "read_spef /tmp/x.spef"
    )


def test_spef_sta_script_also_counts_coverage_of_the_designs_own_nets():
    """Issue #951: the SPEF-side ratio alone cannot reach 1 -- flat
    extraction yields each standard cell's internal nodes, which a
    gate-level linked design has no counterpart for -- so the script also
    walks the *design's* nets and counts how many the SPEF names, which is
    the ratio `annotation_complete` is keyed on."""
    lines = place_and_route._spef_sta_script_lines(
        checkpoint_in="/tmp/x.odb",
        liberty_path="/tmp/x.lib",
        clock_port="clk",
        clock_period_ns=1.1,
        spef_path="/tmp/x.spef",
        net_names=["_019_"],
    )
    script = "\n".join(lines)
    assert "foreach klt_design_net [get_nets -quiet *]" in script
    assert "get_full_name $klt_design_net" in script
    assert 'puts "$klt_design_annotated $klt_design_total"' in script


def test_spef_sta_script_omits_write_sdf_unless_requested():
    """Byte-identical to the pre-#1002 script when `post_route_sdf` is off --
    the whole feature is one conditional line."""
    lines = place_and_route._spef_sta_script_lines(
        checkpoint_in="/tmp/x.odb",
        liberty_path="/tmp/x.lib",
        clock_port="clk",
        clock_period_ns=1.1,
        spef_path="/tmp/x.spef",
        net_names=["_019_"],
    )
    assert not any(line.startswith("write_sdf") for line in lines)


def test_spef_sta_script_writes_sdf_immediately_after_read_spef():
    """Survey section 4.3's own sequencing: the SDF must be written *after*
    section 4.1's `read_spef`, so its delays reflect the real extracted
    routed parasitics rather than the `estimate_parasitics -global_routing`
    estimate -- and before any report command, so nothing can sit between the
    parasitics and the delays derived from them."""
    lines = place_and_route._spef_sta_script_lines(
        checkpoint_in="/tmp/x.odb",
        liberty_path="/tmp/x.lib",
        clock_port="clk",
        clock_period_ns=1.1,
        spef_path="/tmp/x.spef",
        net_names=["_019_"],
        sdf_path="/tmp/gcd_route.sdf",
    )
    write_index = next(
        index for index, line in enumerate(lines) if line.startswith("write_sdf")
    )
    assert lines[write_index - 1] == "read_spef /tmp/x.spef"
    # `-divider .` (a Verilog simulator's hierarchy separator, not OpenSTA's
    # own `/` default) and `-include_typ` (Icarus's default `-T typ` selects
    # the triplet's typ member, which OpenSTA leaves empty by default) are
    # both load-bearing for the SDF to be consumable at all.
    assert lines[write_index] == "write_sdf -divider . -include_typ /tmp/gcd_route.sdf"
    assert write_index < next(
        index
        for index, line in enumerate(lines)
        if line.startswith("report_worst_slack_metric")
    )


def test_count_spef_nets_annotated_parses_marker_block():
    stdout = "\n".join(
        [
            "some preamble",
            place_and_route._SPEF_NET_CHECK_BEGIN,
            "7 10",
            "4 4",
            place_and_route._SPEF_NET_CHECK_END,
            "trailer",
        ]
    )
    assert place_and_route._count_spef_nets_annotated(stdout) == (7, 10, 4, 4)


def test_count_spef_nets_annotated_defensive_when_markers_missing():
    assert place_and_route._count_spef_nets_annotated("no markers here") is None


def test_stubbed_target_stage_place_reports_null_route_drc_violation_count(
    tmp_path, monkeypatch
):
    """A run that never reaches the `"route"` stage -- `detailed_route`
    never ran, so there is no `-output_drc` report to parse -- reports
    `route_drc_violation_count` as `null` (absent from the top level, via
    `_TOP_LEVEL_METRIC_KEYS`'s `.get` degrade), mirroring
    `antenna_violation_count`'s own pre-`"route"` `null` (issue #938)."""
    request_path = _setup_success_env(tmp_path, monkeypatch, target_stage="place")
    _stub_openroad_success(monkeypatch, stages=("floorplan", "place"))

    report = run_place_and_route(request_path)

    assert report["stage_reached"] == "place"
    assert report["route_drc_violation_count"] is None
    assert "route_drc_violation_count" not in report["stages"][-1]


def test_count_route_drc_violations_counts_violation_type_lines(tmp_path):
    """`_count_route_drc_violations` counts one occurrence of the literal
    `"violation type: "` header line per violation entry -- the exact
    format confirmed live via `strings` against a real
    `openroad/orfs:latest` build's `openroad` binary (issue #938), not
    guessed from documentation."""
    report_path = tmp_path / "route_drc.rpt"
    report_path.write_text(
        "violation type: Metal Short\n"
        "\tsrcs:\n net: net0\n"
        "\tbbox = ( 0.0, 0.0 ) - ( 0.1, 0.1 )\nLayer: met1\n"
        "violation type: Metal Short\n"
        "\tsrcs:\n net: net1\n"
        "\tbbox = ( 0.2, 0.2 ) - ( 0.3, 0.3 )\nLayer: met2\n"
    )
    assert place_and_route._count_route_drc_violations(str(report_path)) == 2


def test_count_route_drc_violations_zero_on_empty_report(tmp_path):
    """A 0-byte `-output_drc` report -- what a real, DRC-clean
    `detailed_route` run writes -- is a confirmed-zero count, not `None`."""
    report_path = tmp_path / "route_drc.rpt"
    report_path.write_text("")
    assert place_and_route._count_route_drc_violations(str(report_path)) == 0


def test_count_route_drc_violations_none_when_report_missing(tmp_path):
    """`_count_route_drc_violations` returns `None` (never `0`) when the
    report file itself cannot be read -- keeps a genuinely missing signal
    distinguishable from a confirmed-zero violation count, mirroring
    `_count_antenna_violations`'s own `None` sentinel."""
    missing_path = tmp_path / "does_not_exist.rpt"
    assert place_and_route._count_route_drc_violations(str(missing_path)) is None


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
    successful (exit 0) response with `def_path`/`gds_path`/`verilog_path`
    all `null` by design -- contract spike section 5's "Partial-completion
    design"."""
    request_path = _setup_success_env(tmp_path, monkeypatch, target_stage="place")
    _stub_openroad_success(monkeypatch, stages=("floorplan", "place"))
    merge_calls = _stub_merge_def_to_gds(monkeypatch)

    report = run_place_and_route(request_path)

    assert report["status"] == "ok"
    assert report["stage_reached"] == "place"
    assert report["def_path"] is None
    assert report["gds_path"] is None
    assert report["verilog_path"] is None
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
    assert report["verilog_path"] is None


def test_stubbed_engine_failure_mid_stage(tmp_path, monkeypatch):
    request_path = _setup_success_env(tmp_path, monkeypatch)

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["openroad", "-version"]:
            return fake_completed(stdout="26Q3-771-gdeadbeef \n")
        script_path = cmd[5]
        stage = _stage_from_script_path(script_path)
        if stage == "floorplan":
            checkpoint_out = _script_write_db_path(script_path)
            Path(checkpoint_out).write_text("fake odb\n")
            with open(cmd[4], "w", encoding="utf-8") as handle:
                json.dump(_STAGE_METRICS["floorplan"], handle)
            return fake_completed(returncode=0)
        return fake_completed(
            returncode=1, stderr="[ERROR PPL-0001] no valid pin placement found\n"
        )

    monkeypatch.setattr(place_and_route.subprocess, "run", fake_run)

    with pytest.raises(
        PlaceAndRouteError, match=r"openroad 'place' stage failed:.*PPL-0001"
    ):
        run_place_and_route(request_path)


# --------------------------------------------------------------------------- #
# `DRT-0305` constant-tie diagnosis (issue #854)
# --------------------------------------------------------------------------- #

#: The two lines a real `openroad` route stage prints when TritonRoute hits a
#: constant-tie net -- the informative one on stdout (utl's logger), the
#: uninformative Tcl-level summary on stderr. Copied verbatim from issue
#: #854's own transcript (`openroad/orfs:latest`, `26Q3-1080-gab6fd26351`).
_DRT_0305_STDOUT = (
    "[INFO DRT-0149] Post-processing.\n"
    "[ERROR DRT-0305] Net zero_ of signal type GROUND is not routable by "
    "TritonRoute. Move to special nets.\n"
)
_DRT_0305_STDERR = "Error: pnr_modexp_route.tcl, 6 DRT-0305\n"


def test_engine_error_message_explains_drt_0305_constant_tie(tmp_path):
    """`DRT-0305` is diagnosed, not passed through: the message names the
    offending net and says what to do about it, instead of surfacing only
    OpenROAD's own Tcl line-number summary (`Error: …route.tcl, 6
    DRT-0305`), which is what a caller saw before issue #854."""
    completed = fake_completed(
        returncode=1, stdout=_DRT_0305_STDOUT, stderr=_DRT_0305_STDERR
    )

    message = place_and_route._engine_error_message("route", completed)

    assert "DRT-0305" in message
    assert "zero_" in message
    assert "tie cell" in message
    assert "klt synthesize" in message


def test_engine_error_message_drt_0305_names_a_one_net_too(tmp_path):
    """The tie-high half (`one_`, signal type POWER) is diagnosed the same
    way -- the net name and type both come from the matched error line, not
    from a hardcoded `zero_`."""
    completed = fake_completed(
        returncode=1,
        stdout=(
            "[ERROR DRT-0305] Net one_ of signal type POWER is not routable "
            "by TritonRoute. Move to special nets.\n"
        ),
        stderr="Error: pnr_top_route.tcl, 6 DRT-0305\n",
    )

    message = place_and_route._engine_error_message("route", completed)

    assert "one_" in message
    assert "POWER" in message


def test_engine_error_message_without_drt_0305_is_unchanged(tmp_path):
    """Every other engine failure keeps exactly its pre-#854 message -- the
    hint is appended only for the error code it explains."""
    completed = fake_completed(
        returncode=1, stderr="[ERROR PPL-0001] no valid pin placement found\n"
    )

    message = place_and_route._engine_error_message("place", completed)

    assert message == (
        "openroad 'place' stage failed: [ERROR PPL-0001] no valid pin placement found"
    )


def test_engine_error_message_prefers_bracket_error_over_trailer(tmp_path):
    """OpenROAD's own bracketed diagnostic (`[ERROR IFP-0034] ...`) wins over
    the generic `Error: <script>.tcl, <line> <code>` trailer that reliably
    follows it at `-exit` on a Tcl-level failure, even though the trailer is
    the line the pre-#1079 "last match wins" logic surfaced (issue #1079)."""
    completed = fake_completed(
        returncode=1,
        stdout="[ERROR IFP-0034] no -core_space specified.\n",
        stderr="Error: pnr_trng_top_floorplan.tcl, 7 IFP-0034\n",
    )

    message = place_and_route._engine_error_message("floorplan", completed)

    assert message == (
        "openroad 'floorplan' stage failed: [ERROR IFP-0034] no -core_space specified."
    )


def test_engine_error_message_falls_back_to_trailer_without_bracket_error(tmp_path):
    """When no bracketed `[ERROR ...]` diagnostic is present at all, the bare
    `Error:` trailer is still surfaced rather than dropped -- it is
    uninformative but better than nothing."""
    completed = fake_completed(
        returncode=1, stderr="Error: pnr_top_floorplan.tcl, 7 IFP-0034\n"
    )

    message = place_and_route._engine_error_message("floorplan", completed)

    assert message == (
        "openroad 'floorplan' stage failed: Error: pnr_top_floorplan.tcl, 7 IFP-0034"
    )


def test_stubbed_route_stage_drt_0305_surfaces_the_hint(tmp_path, monkeypatch):
    """The diagnosis reaches the caller through `PlaceAndRouteError`, i.e.
    through `klt place-and-route`'s own JSON `error.message` -- not just
    through the helper that builds it."""
    request_path = _setup_success_env(tmp_path, monkeypatch)

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["openroad", "-version"]:
            return fake_completed(stdout="26Q3-1080-gab6fd26351 \n")
        script_path = cmd[5]
        stage = _stage_from_script_path(script_path)
        if stage == "route":
            return fake_completed(
                returncode=1, stdout=_DRT_0305_STDOUT, stderr=_DRT_0305_STDERR
            )
        checkpoint_out = _script_write_db_path(script_path)
        Path(checkpoint_out).write_text("fake odb\n")
        with open(cmd[4], "w", encoding="utf-8") as handle:
            json.dump(_STAGE_METRICS[stage], handle)
        return fake_completed(returncode=0)

    monkeypatch.setattr(place_and_route.subprocess, "run", fake_run)

    with pytest.raises(PlaceAndRouteError, match=r"DRT-0305.*zero_"):
        run_place_and_route(request_path)


def test_stubbed_missing_metrics_output(tmp_path, monkeypatch):
    request_path = _setup_success_env(
        tmp_path, monkeypatch, target_stage="floorplan", constraints=None, io=None
    )

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["openroad", "-version"]:
            return fake_completed(stdout="26Q3-771-gdeadbeef \n")
        script_path = cmd[5]
        checkpoint_out = _script_write_db_path(script_path)
        Path(checkpoint_out).write_text("fake odb\n")
        return fake_completed(returncode=0)  # no metrics file written

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
        return fake_completed(returncode=0)

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
    assert report["verilog_path"] is not None
    assert os.path.isfile(report["verilog_path"])
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


def test_route_stage_script_writes_the_as_built_verilog_netlist(tmp_path, monkeypatch):
    """Issue #996: the `"route"` stage must emit OpenROAD's own
    `write_verilog` (the `dbSta` command declared in
    `src/dbSta/src/dbReadVerilog.tcl`, forwarding to
    `sta::write_verilog_cmd` -- the same command ORFS calls in its
    `flow/scripts/final_outputs.tcl` to write `6_final.v`) alongside its
    existing `write_def`, so the netlist a golden-reference LVS run uses as
    its reference reflects the CTS buffers / `repair_timing` resizes /
    `repair_antennas` diodes this command itself inserted.

    Mirrors `tests/test_synthesize.py`'s own `_WRITE_VERILOG_RE` check for
    the Yosys-side `write_verilog` line in the generated `.ys` script.

    `request.power` is omitted here (the default `_setup_success_env`
    request), but `sky130_fd_sc_hd` is still covered by issue #1442's
    row-rail fallback, which drives this same stage's `filler_placement`
    call -- so the `write_verilog` line below does carry a trailing
    `-remove_cells` clause for those filler masters, unlike a cell library
    (or, before #1442, any `request.power`-less run) with no row-rail
    fallback at all."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)

    run_place_and_route(request_path)

    route_script = _stage_script(request_path, "route")
    route_lines = _script_lines(route_script)

    output_dir = os.path.join(os.path.dirname(request_path), ".klt", "place-and-route")
    expected = os.path.join(output_dir, "gcd.v")
    assert any(line.startswith(f"write_verilog {expected}") for line in route_lines)
    assert _script_write_verilog_path(route_script) == expected

    # Additive, not a replacement: `write_def` still runs, and the netlist
    # is written from the same design state immediately after it.
    def_index = route_lines.index(f"write_def {os.path.join(output_dir, 'gcd.def')}")
    verilog_index = next(
        i
        for i, line in enumerate(route_lines)
        if line.startswith(f"write_verilog {expected}")
    )
    assert verilog_index == def_index + 1

    # Both artifacts must be written *after* every cell-inserting/resizing
    # call -- a netlist snapshot taken before them would reproduce exactly
    # the pre-CTS divergence this issue exists to remove.
    last_mutating_index = max(
        i
        for i, line in enumerate(route_lines)
        if line.startswith(("repair_antennas", "detailed_route", "global_route"))
    )
    assert verilog_index > last_mutating_index

    # Flags deliberately not passed -- see `_stage_script_lines`'s own
    # `"route"` branch for why each is omitted.
    assert not any("-include_pwr_gnd" in line for line in route_lines)
    # Issue #1442: `-remove_cells` *does* now appear here -- the row-rail
    # fallback's own `filler_placement` call inserted physical-only filler
    # instances above, so this flag strips exactly those from the as-built
    # netlist (see `_stage_script_lines`'s own `physical_only_masters`
    # construction).
    assert (
        "-remove_cells {sky130_fd_sc_hd__fill_1 sky130_fd_sc_hd__fill_2 "
        "sky130_fd_sc_hd__fill_4 sky130_fd_sc_hd__fill_8}"
    ) in route_lines[verilog_index]
    assert not any("write_verilog -sort" in line for line in route_lines)


def test_only_the_route_stage_writes_the_as_built_verilog_netlist(
    tmp_path, monkeypatch
):
    """Issue #996's own open design question, resolved explicitly: the
    artifact is written at `"route"` only, never at `"cts"` (nor earlier).

    It exists to be the netlist counterpart of a *routed* layout, and
    `"route"` is the only stage that produces one -- a cts-stage snapshot
    would describe a state no shippable layout corresponds to (it predates
    `repair_antennas`'s diode insertions) and would need a second response
    field with no `def_path`/`gds_path` to pair with."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)

    run_place_and_route(request_path)

    for stage in ("floorplan", "place", "cts"):
        assert _script_write_verilog_path(_stage_script(request_path, stage)) is None
    assert _script_write_verilog_path(_stage_script(request_path, "route")) is not None


def test_verilog_path_response_field_names_the_script_written_artifact(
    tmp_path, monkeypatch
):
    """Issue #996: the response's additive `verilog_path` must be exactly
    the path the generated route-stage Tcl handed `write_verilog` -- the
    same recomputed-not-threaded convention `def_path` already follows."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)

    report = run_place_and_route(request_path)

    assert report["verilog_path"] == _script_write_verilog_path(
        _stage_script(request_path, "route")
    )
    assert os.path.isfile(report["verilog_path"])
    # Additive only -- `schema_version` stays at 1 per `docs/json-contract.md`
    # ("adding new fields does not [require a bump]"), and neither existing
    # artifact field is displaced.
    assert report["schema_version"] == 1
    assert report["def_path"].endswith("gcd.def")
    assert report["gds_path"].endswith("gcd.gds")
    assert report["verilog_path"].endswith("gcd.v")
    # Never collides with `klt synthesize`'s own `<top>_synth.v` netlist,
    # which is the *input* to this run.
    assert not report["verilog_path"].endswith("_synth.v")


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


def test_route_stage_omits_critical_nets_percentage_flag_by_default(
    tmp_path, monkeypatch
):
    """Issue #939: `request.route_critical_nets_percentage` omitted must
    reproduce the original `global_route` call exactly -- no flag emitted,
    the A/B disable path."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)

    run_place_and_route(request_path)

    route_lines = _script_lines(_stage_script(request_path, "route"))
    assert "global_route" in route_lines
    assert not any("critical_nets_percentage" in line for line in route_lines)


def test_route_stage_passes_critical_nets_percentage_flag_when_requested(
    tmp_path, monkeypatch
):
    """Issue #939 (native-routing survey section 4.1): a real
    `global_route -critical_nets_percentage <percent>` flag, confirmed
    against OpenROAD's own upstream `src/grt/src/GlobalRouter.tcl`/
    `src/grt/README.md` (no live container available in this pass -- see
    the module docstring's methodology note). Opt-in via
    `request.route_critical_nets_percentage`."""
    request_path = _setup_success_env(
        tmp_path, monkeypatch, route_critical_nets_percentage=30
    )
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)

    run_place_and_route(request_path)

    route_lines = _script_lines(_stage_script(request_path, "route"))
    assert "global_route -critical_nets_percentage 30" in route_lines


@pytest.mark.parametrize("value", [-1, 101, 1.5, "30", True])
def test_route_critical_nets_percentage_rejects_invalid_values(
    tmp_path, monkeypatch, value
):
    request_path = _setup_success_env(
        tmp_path, monkeypatch, route_critical_nets_percentage=value
    )
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)

    with pytest.raises(PlaceAndRouteError, match="route_critical_nets_percentage"):
        run_place_and_route(request_path)


def test_route_stage_repeats_antenna_repair_pass_bounded_multi_pass(
    tmp_path, monkeypatch
):
    """Issue #939 (native-routing survey section 4.1): `-iterations` on
    `repair_antennas` itself is the wrong tool once `detailed_route` has
    already run (OpenROAD's own source warns against it -- see the module
    docstring's citation), so the bounded multi-pass option is built at the
    flow level instead: `request.max_antenna_repair_iterations` repeats the
    `repair_antennas`/`detailed_route` reroute pair that many times."""
    request_path = _setup_success_env(
        tmp_path, monkeypatch, max_antenna_repair_iterations=3
    )
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)

    run_place_and_route(request_path)

    route_lines = _script_lines(_stage_script(request_path, "route"))
    detailed_route_indices = [
        i for i, line in enumerate(route_lines) if line.startswith("detailed_route")
    ]
    repair_indices = [
        i for i, line in enumerate(route_lines) if line.startswith("repair_antennas")
    ]
    # 1 initial detailed_route + 3 repair/reroute passes = 4 detailed_route
    # calls, 3 repair_antennas calls, strictly interleaved.
    assert len(detailed_route_indices) == 4
    assert len(repair_indices) == 3
    for repair_idx, before_dr, after_dr in zip(
        repair_indices,
        detailed_route_indices[:-1],
        detailed_route_indices[1:],
        strict=True,
    ):
        assert before_dr < repair_idx < after_dr


def test_max_antenna_repair_iterations_defaults_to_single_pass(tmp_path, monkeypatch):
    """Issue #939: omitting `request.max_antenna_repair_iterations`
    reproduces the original single repair+reroute pass byte-for-byte."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)

    run_place_and_route(request_path)

    route_lines = _script_lines(_stage_script(request_path, "route"))
    repair_indices = [
        i for i, line in enumerate(route_lines) if line.startswith("repair_antennas")
    ]
    assert len(repair_indices) == 1


@pytest.mark.parametrize("value", [0, 9, 1.5, "3", True])
def test_max_antenna_repair_iterations_rejects_invalid_values(
    tmp_path, monkeypatch, value
):
    request_path = _setup_success_env(
        tmp_path, monkeypatch, max_antenna_repair_iterations=value
    )
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)

    with pytest.raises(PlaceAndRouteError, match="max_antenna_repair_iterations"):
        run_place_and_route(request_path)


# --------------------------------------------------------------------------- #
# Post-route multi-corner setup/hold sweep (issue #949,
# `docs/design/post-route-sta-survey.md` section 4.2) --
# `worst_setup_slack_ns`/`worst_hold_slack_ns`.
# --------------------------------------------------------------------------- #


def _corner_sweep_script(request_path: str, hdl_toplevel: str = "gcd") -> str:
    return os.path.join(
        os.path.dirname(request_path),
        ".klt",
        "place-and-route",
        f"pnr_{hdl_toplevel}_route_corners.tcl",
    )


def test_corner_sweep_script_defines_every_shipped_corner(tmp_path, monkeypatch):
    """The generated sweep script `define_corners`s every `.lib` file the
    resolved cell library ships (not only the request's own nominal
    `pdk.corner`), then `read_liberty -corner <name>`s each -- confirmed
    live against a real `openroad/orfs:latest` container that
    `report_worst_slack_metric -setup`/`-hold` then automatically reports
    the worst value across every *loaded* corner (issue #949)."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A", corner="ss_100C_1v60")
    _make_pdk_install(install_root, "sky130A", corner="ff_n40C_1v95")
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)

    run_place_and_route(request_path)

    sweep_lines = _script_lines(_corner_sweep_script(request_path))
    define_corners_line = next(
        line for line in sweep_lines if line.startswith("define_corners")
    )
    tags = define_corners_line.split()[1:]
    assert sorted(tags) == ["ff_n40C_1v95", "ss_100C_1v60", "tt_025C_1v80"]
    for tag in tags:
        assert f"read_liberty -corner {tag} " in "\n".join(sweep_lines)

    # `define_corners` must precede every `read_liberty` in the session
    # (OpenSTA's own `STA-482` ordering rule).
    define_index = sweep_lines.index(define_corners_line)
    read_liberty_indices = [
        i for i, line in enumerate(sweep_lines) if line.startswith("read_liberty")
    ]
    assert all(define_index < i for i in read_liberty_indices)

    assert f"read_db {os.path.dirname(request_path)}" in "".join(
        line for line in sweep_lines if line.startswith("read_db")
    )
    assert sweep_lines[-2:] == [
        "report_worst_slack_metric -setup",
        "report_worst_slack_metric -hold",
    ]
    # No `write_db`/`write_def`/`read_verilog`/`link_design` -- this session
    # only re-derives timing over the route stage's own already-written
    # checkpoint.
    assert not any(line.startswith("write_db") for line in sweep_lines)
    assert not any(line.startswith("write_def") for line in sweep_lines)
    assert not any(line.startswith("read_verilog") for line in sweep_lines)
    assert not any(line.startswith("link_design") for line in sweep_lines)


def test_corner_sweep_excludes_ccsnoise_variant(tmp_path, monkeypatch):
    """A `_ccsnoise` `.lib` view must not reach `define_corners`/
    `read_liberty` -- see `pdk.list_lib_corners`'s own docstring for the
    live-verified `STA-1140` collision this avoids (issue #949)."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    install_root = tmp_path / "install"
    lib_views_dir = install_root / "sky130A" / "libs.ref" / "sky130_fd_sc_hd" / "lib"
    (lib_views_dir / "sky130_fd_sc_hd__tt_025C_1v80_ccsnoise.lib").write_text(
        '    default_operating_conditions : "tt_025C_1v80";\n'
        "    nom_process : 1.0;\n"
        "    nom_temperature : 25.0;\n"
        "    nom_voltage : 1.8;\n",
        encoding="utf-8",
    )
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)

    run_place_and_route(request_path)

    sweep_lines = _script_lines(_corner_sweep_script(request_path))
    define_corners_line = next(
        line for line in sweep_lines if line.startswith("define_corners")
    )
    assert define_corners_line == "define_corners tt_025C_1v80"
    assert not any("ccsnoise" in line for line in sweep_lines)


def test_corner_sweep_populates_worst_setup_and_hold_slack_fields(
    tmp_path, monkeypatch
):
    """End-to-end: the sweep's own `-metrics` dump populates
    `worst_setup_slack_ns`/`worst_hold_slack_ns` at both top level and the
    `"route"` stage entry, leaving the existing nominal-corner-only
    `worst_slack_ns` field untouched (issue #949's own backward-
    compatibility acceptance criterion)."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(
        monkeypatch,
        corner_sweep_metrics={
            "timing__setup__ws": -7.5,
            "timing__hold__ws": 0.25,
        },
    )
    _stub_merge_def_to_gds(monkeypatch)

    report = run_place_and_route(request_path)

    assert report["worst_setup_slack_ns"] == pytest.approx(-7.5)
    assert report["worst_hold_slack_ns"] == pytest.approx(0.25)
    assert report["worst_slack_ns"] == pytest.approx(-2.18828)  # unchanged
    route_stage = report["stages"][-1]
    assert route_stage["name"] == "route"
    assert route_stage["worst_setup_slack_ns"] == pytest.approx(-7.5)
    assert route_stage["worst_hold_slack_ns"] == pytest.approx(0.25)
    # Issue #1092: a single-shipped-corner library's per-corner breakdown is
    # a one-element array naming that corner, built directly from the
    # aggregate above -- no second OpenROAD invocation needed (see
    # `_run_corner_sweep`'s own `len(corners) == 1` docstring note).
    assert report["corners"] == [
        {"name": "tt_025C_1v80", "setup_slack_ns": -7.5, "hold_slack_ns": 0.25}
    ]
    assert route_stage["corners"] == report["corners"]


def test_corner_sweep_skipped_when_target_stage_before_route(tmp_path, monkeypatch):
    """A `target_stage: "place"` run never reaches `"route"`, so the corner
    sweep never runs -- `worst_setup_slack_ns`/`worst_hold_slack_ns` are
    `null`, mirroring `route_drc_violation_count`'s own pre-`"route"`
    convention (issue #949)."""
    request_path = _setup_success_env(tmp_path, monkeypatch, target_stage="place")
    _stub_openroad_success(monkeypatch, stages=("floorplan", "place"))

    report = run_place_and_route(request_path)

    assert report["stage_reached"] == "place"
    assert report["worst_setup_slack_ns"] is None
    assert report["worst_hold_slack_ns"] is None
    # Issue #1092: `corners` is `null`, not `[]`, pre-"route" -- same
    # `.get`-degrades-to-absent convention `worst_setup_slack_ns` follows.
    assert report["corners"] is None
    assert not os.path.isfile(_corner_sweep_script(request_path))


# --------------------------------------------------------------------------- #
# Corner-sweep per-corner breakdown + request-scoped `sweep_corners`
# (issue #1092).
# --------------------------------------------------------------------------- #


def test_corner_sweep_reports_per_corner_setup_and_hold_slack(tmp_path, monkeypatch):
    """A multi-corner library's `corners[]` breakdown names each swept
    corner's own setup/hold slack -- one single-corner OpenROAD invocation
    per corner (`_run_corner_sweep`'s `len(corners) > 1` branch), letting a
    caller identify which corner decided `worst_setup_slack_ns`/
    `worst_hold_slack_ns` without re-deriving it externally (issue #1092)."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A", corner="ss_100C_1v60")
    _make_pdk_install(install_root, "sky130A", corner="ff_n40C_1v95")
    _stub_openroad_success(
        monkeypatch,
        corner_sweep_metrics={
            "timing__setup__ws": -22.2093,
            "timing__hold__ws": 0.28443,
        },
        per_corner_sweep_metrics={
            "tt_025C_1v80": {
                "timing__setup__ws": -1.94402,
                "timing__hold__ws": 0.5,
            },
            "ss_100C_1v60": {
                "timing__setup__ws": -22.2093,
                "timing__hold__ws": 1.2,
            },
            "ff_n40C_1v95": {
                "timing__setup__ws": -3.1,
                "timing__hold__ws": 0.28443,
            },
        },
    )
    _stub_merge_def_to_gds(monkeypatch)

    report = run_place_and_route(request_path)

    assert report["worst_setup_slack_ns"] == pytest.approx(-22.2093)
    assert report["worst_hold_slack_ns"] == pytest.approx(0.28443)
    corners_by_name = {entry["name"]: entry for entry in report["corners"]}
    assert set(corners_by_name) == {"tt_025C_1v80", "ss_100C_1v60", "ff_n40C_1v95"}
    assert corners_by_name["tt_025C_1v80"] == {
        "name": "tt_025C_1v80",
        "setup_slack_ns": pytest.approx(-1.94402),
        "hold_slack_ns": pytest.approx(0.5),
    }
    assert corners_by_name["ss_100C_1v60"]["setup_slack_ns"] == pytest.approx(-22.2093)
    assert corners_by_name["ff_n40C_1v95"]["hold_slack_ns"] == pytest.approx(0.28443)
    # The aggregate's deciding corner is derivable from the array: the
    # minimum (worst) setup slack among the three is `ss_100C_1v60`'s own
    # -22.2093, matching `worst_setup_slack_ns` above; the minimum hold
    # slack is `ff_n40C_1v95`'s own 0.28443, matching `worst_hold_slack_ns`.
    assert (
        min(report["corners"], key=lambda c: c["setup_slack_ns"])["name"]
        == "ss_100C_1v60"
    )
    assert (
        min(report["corners"], key=lambda c: c["hold_slack_ns"])["name"]
        == "ff_n40C_1v95"
    )


def test_sweep_corners_scopes_the_sweep_to_named_corners(tmp_path, monkeypatch):
    """`request.pdk.sweep_corners` narrows the swept set below the full
    shipped-corner enumeration -- the combined sweep script only
    `define_corners`s the requested subset, and `corners[]` only reports
    entries for the requested names (issue #1092)."""
    request_path = _setup_success_env(
        tmp_path,
        monkeypatch,
        pdk={
            "cell_library": "sky130_fd_sc_hd",
            "corner": "tt_025C_1v80",
            "sweep_corners": ["tt_025C_1v80", "ff_n40C_1v95"],
        },
    )
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A", corner="ss_100C_1v60")
    _make_pdk_install(install_root, "sky130A", corner="ff_n40C_1v95")
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)

    report = run_place_and_route(request_path)

    sweep_lines = _script_lines(_corner_sweep_script(request_path))
    define_corners_line = next(
        line for line in sweep_lines if line.startswith("define_corners")
    )
    tags = define_corners_line.split()[1:]
    assert sorted(tags) == ["ff_n40C_1v95", "tt_025C_1v80"]
    assert "ss_100C_1v60" not in "\n".join(sweep_lines)

    assert {entry["name"] for entry in report["corners"]} == {
        "tt_025C_1v80",
        "ff_n40C_1v95",
    }


def test_sweep_corners_empty_list_sweeps_nothing(tmp_path, monkeypatch):
    """An explicit `sweep_corners: []` is honoured literally -- distinct
    from omitting the field entirely (which sweeps everything) -- degrading
    `worst_setup_slack_ns`/`worst_hold_slack_ns`/`corners` to `null`/`[]`
    exactly like `_run_corner_sweep`'s own empty-``corners``-list
    short-circuit (issue #1092)."""
    request_path = _setup_success_env(
        tmp_path,
        monkeypatch,
        pdk={
            "cell_library": "sky130_fd_sc_hd",
            "corner": "tt_025C_1v80",
            "sweep_corners": [],
        },
    )
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)

    report = run_place_and_route(request_path)

    assert report["worst_setup_slack_ns"] is None
    assert report["worst_hold_slack_ns"] is None
    assert report["corners"] == []
    assert not os.path.isfile(_corner_sweep_script(request_path))


def test_sweep_corners_unknown_name_raises(tmp_path, monkeypatch):
    """An unresolvable `sweep_corners` entry raises `PlaceAndRouteError`
    naming the unknown corner(s) and the corners that *are* valid --
    matching this module's existing unresolvable-request-field convention
    (issue #1092)."""
    request_path = _setup_success_env(
        tmp_path,
        monkeypatch,
        pdk={
            "cell_library": "sky130_fd_sc_hd",
            "corner": "tt_025C_1v80",
            "sweep_corners": ["tt_025C_1v80", "does_not_exist_corner"],
        },
    )
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)

    with pytest.raises(
        PlaceAndRouteError, match="unknown corner.*does_not_exist_corner"
    ):
        run_place_and_route(request_path)


def test_sweep_corners_rejects_non_list(tmp_path, monkeypatch):
    request_path = _setup_success_env(
        tmp_path,
        monkeypatch,
        pdk={
            "cell_library": "sky130_fd_sc_hd",
            "corner": "tt_025C_1v80",
            "sweep_corners": "tt_025C_1v80",
        },
    )
    with pytest.raises(PlaceAndRouteError, match="sweep_corners must be a list"):
        run_place_and_route(request_path)


def test_sweep_corners_rejects_non_string_elements(tmp_path, monkeypatch):
    request_path = _setup_success_env(
        tmp_path,
        monkeypatch,
        pdk={
            "cell_library": "sky130_fd_sc_hd",
            "corner": "tt_025C_1v80",
            "sweep_corners": ["tt_025C_1v80", 3],
        },
    )
    with pytest.raises(PlaceAndRouteError, match="sweep_corners must be a list"):
        run_place_and_route(request_path)


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


# --------------------------------------------------------------------------- #
# `_resolve_layer_map` -- variant-exact vs. family-fallback vs. none found
# (issue #1029: gf180mcu ships a single family-level `gf180mcu.map` shared
# across every variant rather than a `<variant>.map` per variant, unlike
# sky130).
# --------------------------------------------------------------------------- #


def test_resolve_layer_map_prefers_exact_variant_match(tmp_path):
    klayout_dir = tmp_path / "klayout"
    tech_dir = klayout_dir / "tech"
    tech_dir.mkdir(parents=True)
    (tech_dir / "sky130A.map").write_text("exact\n", encoding="utf-8")
    (tech_dir / "sky130.map").write_text("family\n", encoding="utf-8")

    pdk_info = _fake_pdk_info(tmp_path, variant="sky130A")
    pdk_info["assets"]["klayout"] = str(klayout_dir)

    path, resolution = place_and_route._resolve_layer_map(pdk_info)

    assert path == str(tech_dir / "sky130A.map")
    assert resolution == "exact"


def test_resolve_layer_map_falls_back_to_family_level_file(tmp_path):
    """gf180mcu-shaped fixture: only the bare-family `gf180mcu.map` exists,
    never a `gf180mcuC.map`/`gf180mcuD.map` -- verified against a real
    open_pdks install for issue #1029."""
    klayout_dir = tmp_path / "klayout"
    tech_dir = klayout_dir / "tech"
    tech_dir.mkdir(parents=True)
    (tech_dir / "gf180mcu.map").write_text("family\n", encoding="utf-8")

    pdk_info = _fake_pdk_info(tmp_path, variant="gf180mcuC")
    pdk_info["assets"]["klayout"] = str(klayout_dir)

    path, resolution = place_and_route._resolve_layer_map(pdk_info)

    assert path == str(tech_dir / "gf180mcu.map")
    assert resolution == "family"


def test_resolve_layer_map_returns_none_when_neither_file_exists(tmp_path):
    klayout_dir = tmp_path / "klayout"
    tech_dir = klayout_dir / "tech"
    tech_dir.mkdir(parents=True)

    pdk_info = _fake_pdk_info(tmp_path, variant="gf180mcuC")
    pdk_info["assets"]["klayout"] = str(klayout_dir)

    path, resolution = place_and_route._resolve_layer_map(pdk_info)

    assert path is None
    assert resolution == "none"


def test_resolve_layer_map_returns_none_when_no_klayout_asset(tmp_path):
    pdk_info = _fake_pdk_info(tmp_path, variant="sky130A")
    assert pdk_info["assets"]["klayout"] is None

    path, resolution = place_and_route._resolve_layer_map(pdk_info)

    assert path is None
    assert resolution == "none"


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

    result_info = place_and_route._merge_def_to_gds(
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
    # `_fake_pdk_info`'s `assets.klayout` is `None` -- no map file to find.
    assert result_info == {"path": None, "resolution": "none"}


def test_merge_def_to_gds_is_byte_reproducible_across_runs(tmp_path):
    """Issue #1367: the merged GDS's BGNLIB/BGNSTR records must not carry
    KLayout's default wall-clock timestamp, so re-running an identical merge
    twice produces byte-identical output. Before the fix, a bare `.write()`
    call with no `SaveLayoutOptions` stamps those records with the merge's
    own wall-clock time; without the `time.sleep()` below (crossing a whole
    GDS2 timestamp second between the two runs) that pre-fix failure could
    be flaky -- passing by accident if both runs land within the same
    wall-clock second -- rather than reliably reproducing the bug. The fix
    removes the wall-clock dependency entirely, so this stays reliable
    (and fast: the sleep is the only appreciable cost) either way."""
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

    out_path_1 = tmp_path / "out1.gds"
    out_path_2 = tmp_path / "out2.gds"

    for i, out_path in enumerate((out_path_1, out_path_2)):
        if i:
            # Force the two runs to land in different wall-clock seconds so
            # a pre-fix regression (a bare `.write()` stamping GDS2's
            # whole-second-resolution BGNLIB/BGNSTR timestamp fields)
            # reliably fails this assertion instead of merely being flaky.
            time.sleep(1.1)
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

    assert out_path_1.read_bytes() == out_path_2.read_bytes()


def test_merge_def_to_gds_applies_family_fallback_layer_map_for_gf180mcu(tmp_path):
    """A real gf180mcuC/gf180mcuD open_pdks install ships only the
    family-level `libs.tech/klayout/tech/gf180mcu.map`, never a
    variant-named `gf180mcuC.map`/`gf180mcuD.map` (issue #1029). The merge
    must still resolve and apply a layer map instead of silently proceeding
    without one."""
    root = tmp_path / "install"
    cell_name = "testcell"
    tech_lef = tmp_path / "tech.tlef"
    cell_lef = tmp_path / "cells.lef"
    _write_tiny_tech_lef(tech_lef)
    _write_tiny_cell_lef(cell_lef, cell_name)

    gds_dir = root / "gf180mcuC" / "libs.ref" / "gf180mcu_fd_sc_mcu9t5v0" / "gds"
    gds_dir.mkdir(parents=True)
    _write_matching_cell_gds(gds_dir / "gf180mcu_fd_sc_mcu9t5v0.gds", cell_name)

    klayout_tech_dir = root / "gf180mcuC" / "libs.tech" / "klayout" / "tech"
    klayout_tech_dir.mkdir(parents=True)
    map_path = klayout_tech_dir / "gf180mcu.map"
    map_path.write_text("met1 NET,SPNET,VIA 68 20\n", encoding="utf-8")

    def_path = tmp_path / "design.def"
    _write_tiny_def(def_path, design_name="top", cell_name=cell_name)

    out_path = tmp_path / "out.gds"

    pdk_info = _fake_pdk_info(root, variant="gf180mcuC")
    pdk_info["assets"]["klayout"] = str(root / "gf180mcuC" / "libs.tech" / "klayout")

    result_info = place_and_route._merge_def_to_gds(
        def_path=str(def_path),
        tech_lef=str(tech_lef),
        cell_lef=str(cell_lef),
        pdk_info=pdk_info,
        cell_library="gf180mcu_fd_sc_mcu9t5v0",
        hdl_toplevel="top",
        macros=[],
        out_path=str(out_path),
    )

    assert out_path.is_file()
    assert result_info == {"path": str(map_path), "resolution": "family"}


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
    path: Path,
    *,
    design_name: str,
    cell_name: str,
    macro_cell_name: str,
    database_microns: int = 1000,
) -> None:
    path.write_text(
        "VERSION 5.8 ;\n"
        'DIVIDERCHAR "/" ;\n'
        'BUSBITCHARS "[]" ;\n'
        f"DESIGN {design_name} ;\n"
        f"UNITS DISTANCE MICRONS {database_microns} ;\n"
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
# DBU/DEF-UNITS mismatch (issue #1032): a tech LEF whose own `DATABASE
# MICRONS` differs from KLayout's compiled-in reader default (0.001, i.e.
# `DATABASE MICRONS 1000`) -- e.g. a gf180mcu-class tech LEF declaring 2000
# -- must not silently mismatch and drop/corrupt via-cut geometry.
# --------------------------------------------------------------------------- #


def _write_matching_cell_gds_at_dbu(path: Path, cell_name: str, dbu: float) -> None:
    """Same shape as `_write_matching_cell_gds`, but at an explicit `dbu`
    rather than always 0.001 -- a real PDK install's own std-cell GDS views
    share one resolution with its tech LEF's `DATABASE MICRONS` (e.g. every
    gf180mcu asset is 0.0005, matching `DATABASE MICRONS 2000`, never mixed
    with sky130-style 0.001 views). `_merge_def_to_gds` merges this cell GDS
    into the DEF-derived layout via a second, options-less `Layout.read()`
    call (`place_and_route.py`'s own merge loop) that resets the shared
    layout's `dbu` to match whatever it reads -- rescaling only *new*
    shapes, not shapes already inserted by the DEF/LEF read. A cell GDS at
    the wrong dbu would corrupt the DEF-derived via-cut geometry checked
    below the exact same way a DEF/tech-LEF DBU mismatch would (this is why
    every asset in a real PDK install shares one dbu, not test-fixture
    sloppiness)."""
    layout = kdb.Layout()
    layout.dbu = dbu
    cell = layout.create_cell(cell_name)
    layer = layout.layer(kdb.LayerInfo(68, 20))  # met1 drawing, sky130-ish
    cell.shapes(layer).insert(kdb.Box(0, 0, 100, 100))
    layout.write(str(path))


def _write_via_tech_lef(path: Path, *, database_microns: int | None) -> None:
    """Same shape as `_write_tiny_tech_lef` but adds a CUT layer plus a
    fixed (non-`GENERATE`) `VIA` definition between met1/met2, and -- the
    part this issue is about -- accepts an arbitrary (or omitted)
    `DATABASE MICRONS` value instead of always hard-coding 1000."""
    units_block = (
        f"UNITS\n  DATABASE MICRONS {database_microns} ;\nEND UNITS\n"
        if database_microns is not None
        else ""
    )
    path.write_text(
        "VERSION 5.7 ;\n"
        'BUSBITCHARS "[]" ;\n'
        'DIVIDERCHAR "/" ;\n'
        f"{units_block}"
        "MANUFACTURINGGRID 0.0005 ;\n"
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
        "END met1\n"
        "LAYER via1\n"
        "  TYPE CUT ;\n"
        "END via1\n"
        "LAYER met2\n"
        "  TYPE ROUTING ;\n"
        "  DIRECTION VERTICAL ;\n"
        "  WIDTH 0.14 ;\n"
        "  PITCH 0.34 ;\n"
        "END met2\n"
        "VIA VIA1_0 DEFAULT\n"
        "  LAYER via1 ;\n"
        "    RECT -0.0705 -0.0705 -0.0700 -0.0700 ;\n"
        "    RECT 0.0700 0.0700 0.0705 0.0705 ;\n"
        "  LAYER met1 ;\n"
        "    RECT -0.1 -0.1 0.1 0.1 ;\n"
        "  LAYER met2 ;\n"
        "    RECT -0.1 -0.1 0.1 0.1 ;\n"
        "END VIA1_0\n",
        encoding="utf-8",
    )


def _write_via_def(path: Path, *, database_microns: int, cell_name: str) -> None:
    """A DEF at the given `UNITS DISTANCE MICRONS` resolution, placing one
    standard cell and routing its pin through the fixed `VIA1_0` defined by
    `_write_via_tech_lef` -- exercising the DEF->GDS via-cut merge path
    `_merge_def_to_gds` actually goes through, not just placement."""
    path.write_text(
        "VERSION 5.8 ;\n"
        'DIVIDERCHAR "/" ;\n'
        'BUSBITCHARS "[]" ;\n'
        "DESIGN top ;\n"
        f"UNITS DISTANCE MICRONS {database_microns} ;\n"
        "DIEAREA ( 0 0 ) ( 4600 2720 ) ;\n"
        "ROW ROW_0 unithd 0 0 N DO 10 BY 1 STEP 460 0 ;\n"
        "COMPONENTS 1 ;\n"
        f"- inst1 {cell_name} + PLACED ( 0 0 ) N ;\n"
        "END COMPONENTS\n"
        "NETS 1 ;\n"
        f"- net1 ( inst1 A )\n"
        "  + ROUTED met1 ( 1000 1000 ) VIA1_0 ;\n"
        "END NETS\n"
        "END DESIGN\n",
        encoding="utf-8",
    )


def test_merge_def_to_gds_resolves_dbu_from_tech_lef_database_microns(tmp_path, capfd):
    """The core regression: a tech LEF declaring `DATABASE MICRONS 2000`
    (gf180mcu-shaped, per issue #1032's own repro) merged against a DEF at
    the *same* declared units must not trigger KLayout's `DEF UNITS does
    not match reader DBU` warning, and the via-cut geometry it places must
    come out at the correct real-micron position/size.

    Before the fix, `_merge_def_to_gds` left `lefdef_config.dbu` at
    KLayout's compiled-in default (0.001, i.e. `DATABASE MICRONS 1000`)
    regardless of what the tech LEF itself declared -- confirmed live
    (`test_merge_def_to_gds_without_fix_logs_dbu_mismatch_warning` below
    reproduces exactly that warning against this same fixture by
    monkeypatching the resolution step back out)."""
    root = tmp_path / "install"
    cell_name = "testcell"
    tech_lef = tmp_path / "tech.tlef"
    cell_lef = tmp_path / "cells.lef"
    _write_via_tech_lef(tech_lef, database_microns=2000)
    _write_tiny_cell_lef(cell_lef, cell_name)

    gds_dir = root / "gf180mcuC" / "libs.ref" / "gf180mcu_fd_sc_mcu9t5v0" / "gds"
    gds_dir.mkdir(parents=True)
    _write_matching_cell_gds_at_dbu(
        gds_dir / "gf180mcu_fd_sc_mcu9t5v0.gds", cell_name, dbu=0.0005
    )

    def_path = tmp_path / "design.def"
    _write_via_def(def_path, database_microns=2000, cell_name=cell_name)

    out_path = tmp_path / "out.gds"

    capfd.readouterr()  # drain anything buffered before this test's own read
    place_and_route._merge_def_to_gds(
        def_path=str(def_path),
        tech_lef=str(tech_lef),
        cell_lef=str(cell_lef),
        pdk_info=_fake_pdk_info(root, variant="gf180mcuC"),
        cell_library="gf180mcu_fd_sc_mcu9t5v0",
        hdl_toplevel="top",
        macros=[],
        out_path=str(out_path),
    )
    captured = capfd.readouterr()
    assert "DEF UNITS does not match reader DBU" not in captured.out
    assert "Invalid via name" not in captured.out

    assert out_path.is_file()
    result = kdb.Layout()
    result.read(str(out_path))
    via_cell = result.cell("VIA_VIA1_0")
    assert via_cell is not None
    via1_layer = result.layer(kdb.LayerInfo(2, 0))
    cut_shapes = list(via_cell.shapes(via1_layer).each())
    assert len(cut_shapes) == 2
    # Real-micron positions, independent of whatever `result.dbu` ended up
    # being -- the fixed via's own two cut RECTs from `_write_via_tech_lef`.
    boxes_um = sorted(
        tuple(round(v * result.dbu, 4) for v in (b.left, b.bottom, b.right, b.top))
        for b in (s.bbox() for s in cut_shapes)
    )
    assert boxes_um == [
        (-0.0705, -0.0705, -0.07, -0.07),
        (0.07, 0.07, 0.0705, 0.0705),
    ]


def test_merge_def_to_gds_without_fix_logs_dbu_mismatch_warning(
    tmp_path, monkeypatch, capfd
):
    """Counterfactual proving the fixture above is a real regression guard,
    not a vacuous one: with the exact same DATABASE-MICRONS-2000 tech LEF
    and DEF, forcing `_merge_def_to_gds` to skip resolving `dbu` (as it did
    before this issue's fix -- simulated here by making `read_lef_header`
    report `database_microns: None` regardless of the LEF's actual content)
    reproduces KLayout's own `DEF UNITS does not match reader DBU` warning
    verbatim."""
    root = tmp_path / "install"
    cell_name = "testcell"
    tech_lef = tmp_path / "tech.tlef"
    cell_lef = tmp_path / "cells.lef"
    _write_via_tech_lef(tech_lef, database_microns=2000)
    _write_tiny_cell_lef(cell_lef, cell_name)

    gds_dir = root / "gf180mcuC" / "libs.ref" / "gf180mcu_fd_sc_mcu9t5v0" / "gds"
    gds_dir.mkdir(parents=True)
    _write_matching_cell_gds_at_dbu(
        gds_dir / "gf180mcu_fd_sc_mcu9t5v0.gds", cell_name, dbu=0.0005
    )

    def_path = tmp_path / "design.def"
    _write_via_def(def_path, database_microns=2000, cell_name=cell_name)

    real_read_lef_header = place_and_route.read_lef_header

    def _fake_read_lef_header(path):
        result = dict(real_read_lef_header(path))
        result["database_microns"] = None
        return result

    monkeypatch.setattr(place_and_route, "read_lef_header", _fake_read_lef_header)

    out_path = tmp_path / "out.gds"
    capfd.readouterr()
    place_and_route._merge_def_to_gds(
        def_path=str(def_path),
        tech_lef=str(tech_lef),
        cell_lef=str(cell_lef),
        pdk_info=_fake_pdk_info(root, variant="gf180mcuC"),
        cell_library="gf180mcu_fd_sc_mcu9t5v0",
        hdl_toplevel="top",
        macros=[],
        out_path=str(out_path),
    )
    captured = capfd.readouterr()
    assert "DEF UNITS does not match reader DBU" in captured.out
    # Falls back to KLayout's own default rather than crashing (issue
    # #1032's second acceptance criterion) -- the merge still completes.
    assert out_path.is_file()


def test_merge_def_to_gds_falls_back_when_tech_lef_omits_database_microns(tmp_path):
    """A tech LEF that never declares `DATABASE MICRONS` at all (the
    regex-based `read_lef_header` parser returns `None`, never raises) must
    fall back to KLayout's existing default DBU behavior rather than
    crashing -- issue #1032's second acceptance criterion."""
    root = tmp_path / "install"
    cell_name = "testcell"
    tech_lef = tmp_path / "tech.tlef"
    cell_lef = tmp_path / "cells.lef"
    _write_via_tech_lef(tech_lef, database_microns=None)
    _write_tiny_cell_lef(cell_lef, cell_name)

    gds_dir = root / "sky130A" / "libs.ref" / "sky130_fd_sc_hd" / "gds"
    gds_dir.mkdir(parents=True)
    _write_matching_cell_gds(gds_dir / "sky130_fd_sc_hd.gds", cell_name)

    def_path = tmp_path / "design.def"
    # KLayout's own default DBU (0.001) corresponds to `DATABASE MICRONS
    # 1000` -- matching units here so the fallback path is a clean success,
    # not itself another mismatch.
    _write_via_def(def_path, database_microns=1000, cell_name=cell_name)

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
    assert result.cell("VIA_VIA1_0") is not None


# --------------------------------------------------------------------------- #
# Second-read DBU mismatch (issue #1090, follow-on to #1032): the DEF/tech
# LEF read itself is correct (#1032's own fix), but the *standard-cell* or
# *macro* GDS view is written at a differing DBU -- `main_layout.read(...)`
# on that second read silently adopts the incoming stream's own DBU without
# rescaling geometry already present, doubling every DEF-derived shape.
# --------------------------------------------------------------------------- #


def test_merge_def_to_gds_preserves_dbu_when_cell_gds_differs(tmp_path):
    """The DEF and tech LEF agree (`UNITS DISTANCE MICRONS 2000` /
    `DATABASE MICRONS 2000`, dbu 0.0005) -- this is deliberately *not*
    #1032's mismatch. The standard-cell GDS view is written at a *different*
    dbu (0.001, i.e. what a `DATABASE MICRONS 1000`-class PDK's own cell
    views use). Before this issue's fix, the second, options-less
    `main_layout.read(cell_gds)` call silently dragged the whole shared
    layout's dbu from 0.0005 to 0.001, doubling the DEF-derived via-cut
    geometry already present (real cause verified live against klayout
    0.30.10 -- see `_merge_gds_view`'s own docstring)."""
    root = tmp_path / "install"
    cell_name = "testcell"
    tech_lef = tmp_path / "tech.tlef"
    cell_lef = tmp_path / "cells.lef"
    _write_via_tech_lef(tech_lef, database_microns=2000)
    _write_tiny_cell_lef(cell_lef, cell_name)

    gds_dir = root / "gf180mcuC" / "libs.ref" / "gf180mcu_fd_sc_mcu9t5v0" / "gds"
    gds_dir.mkdir(parents=True)
    # Deliberate mismatch: tech LEF/DEF are DATABASE MICRONS 2000 (dbu
    # 0.0005), but the cell GDS view is at dbu 0.001 -- the class of defect
    # this issue reports, independent of #1032's DEF-read mismatch.
    _write_matching_cell_gds_at_dbu(
        gds_dir / "gf180mcu_fd_sc_mcu9t5v0.gds", cell_name, dbu=0.001
    )

    def_path = tmp_path / "design.def"
    _write_via_def(def_path, database_microns=2000, cell_name=cell_name)

    out_path = tmp_path / "out.gds"
    place_and_route._merge_def_to_gds(
        def_path=str(def_path),
        tech_lef=str(tech_lef),
        cell_lef=str(cell_lef),
        pdk_info=_fake_pdk_info(root, variant="gf180mcuC"),
        cell_library="gf180mcu_fd_sc_mcu9t5v0",
        hdl_toplevel="top",
        macros=[],
        out_path=str(out_path),
    )

    assert out_path.is_file()
    result = kdb.Layout()
    result.read(str(out_path))

    # DEF-derived via-cut geometry (`_write_via_tech_lef`'s own fixed
    # `VIA1_0`, placed by the DEF's `ROUTED` statement) must land at its
    # true real-micron position -- not doubled by the mismatched cell-GDS
    # read that follows it.
    via_cell = result.cell("VIA_VIA1_0")
    assert via_cell is not None
    via1_layer = result.layer(kdb.LayerInfo(2, 0))
    cut_shapes = list(via_cell.shapes(via1_layer).each())
    assert len(cut_shapes) == 2
    boxes_um = sorted(
        tuple(round(v * result.dbu, 4) for v in (b.left, b.bottom, b.right, b.top))
        for b in (s.bbox() for s in cut_shapes)
    )
    assert boxes_um == [
        (-0.0705, -0.0705, -0.07, -0.07),
        (0.07, 0.07, 0.0705, 0.0705),
    ]

    # The standard-cell GDS view's own drawn box (100x100 raw units at
    # dbu=0.001 -- 0.1 x 0.1 real microns) must also come out at its true
    # size, not further rescaled by the DBU unification.
    cell = result.cell(cell_name)
    assert cell is not None
    met1_layer = result.layer(kdb.LayerInfo(68, 20))
    cell_shapes = list(cell.shapes(met1_layer).each())
    assert len(cell_shapes) == 1
    box_um = cell_shapes[0].bbox().to_dtype(result.dbu)
    assert round(box_um.width(), 4) == pytest.approx(0.1)
    assert round(box_um.height(), 4) == pytest.approx(0.1)


def test_merge_def_to_gds_preserves_dbu_when_macro_gds_differs(tmp_path):
    """Same DBU-mismatch shape as
    `test_merge_def_to_gds_preserves_dbu_when_cell_gds_differs`, but for the
    `macros[].gds` merge path (issue #1090's second acceptance criterion):
    the standard-cell GDS view matches the tech LEF's own dbu (0.0005), but
    the macro's own declared `gds` is written at a differing dbu (0.001)."""
    root = tmp_path / "install"
    cell_name = "testcell"
    macro_cell_name = "analog_block"
    tech_lef = tmp_path / "tech.tlef"
    cell_lef = tmp_path / "cells.lef"
    macro_lef = tmp_path / "analog_block.lef"
    macro_gds = tmp_path / "analog_block.gds"
    _write_via_tech_lef(tech_lef, database_microns=2000)
    _write_tiny_cell_lef(cell_lef, cell_name)
    _write_tiny_macro_lef(macro_lef, macro_cell_name, (5.0, 5.0))
    _write_matching_cell_gds_at_dbu(macro_gds, macro_cell_name, dbu=0.001)

    gds_dir = root / "gf180mcuC" / "libs.ref" / "gf180mcu_fd_sc_mcu9t5v0" / "gds"
    gds_dir.mkdir(parents=True)
    _write_matching_cell_gds_at_dbu(
        gds_dir / "gf180mcu_fd_sc_mcu9t5v0.gds", cell_name, dbu=0.0005
    )

    def_path = tmp_path / "design.def"
    _write_tiny_def_with_macro(
        def_path,
        design_name="top",
        cell_name=cell_name,
        macro_cell_name=macro_cell_name,
        database_microns=2000,
    )

    out_path = tmp_path / "out.gds"
    place_and_route._merge_def_to_gds(
        def_path=str(def_path),
        tech_lef=str(tech_lef),
        cell_lef=str(cell_lef),
        pdk_info=_fake_pdk_info(root, variant="gf180mcuC"),
        cell_library="gf180mcu_fd_sc_mcu9t5v0",
        hdl_toplevel="top",
        macros=[
            {
                "instance": "u_macro",
                "lef": str(macro_lef),
                "cell_name": macro_cell_name,
                "x_um": 5.0,
                "y_um": 5.0,
                "orientation": "R0",
                "gds": str(macro_gds),
            }
        ],
        out_path=str(out_path),
    )

    assert out_path.is_file()
    result = kdb.Layout()
    result.read(str(out_path))

    # The macro's own drawn box (100x100 raw units at dbu=0.001 -- 0.1x0.1
    # real microns) must come out at its true size, not rescaled by the
    # DBU-mismatched macro-GDS read.
    macro_cell = result.cell(macro_cell_name)
    assert macro_cell is not None
    met1_layer = result.layer(kdb.LayerInfo(68, 20))
    macro_shapes = list(macro_cell.shapes(met1_layer).each())
    assert len(macro_shapes) == 1
    box_um = macro_shapes[0].bbox().to_dtype(result.dbu)
    assert round(box_um.width(), 4) == pytest.approx(0.1)
    assert round(box_um.height(), 4) == pytest.approx(0.1)

    # The DEF-derived instance placement (raw 10000 / DATABASE MICRONS 2000
    # = 5.0 real microns) must also survive the subsequent macro-GDS read
    # unrescaled.
    top_cell = result.top_cell()
    macro_insts = [
        inst for inst in top_cell.each_inst() if inst.cell.name == macro_cell_name
    ]
    assert len(macro_insts) == 1
    disp_um = macro_insts[0].dtrans.disp
    assert round(disp_um.x, 4) == pytest.approx(5.0)
    assert round(disp_um.y, 4) == pytest.approx(5.0)


def test_merge_def_to_gds_rejects_merged_bbox_outside_diearea(tmp_path):
    """Regression guard for issue #1090's second, independent fix: a merged
    top-cell bounding box that extends beyond the DEF's own `DIEAREA` -- the
    same symptom a DBU mismatch produces -- must raise, not silently write a
    wrong GDS. Engineered directly (a standard-cell GDS view whose own drawn
    geometry sits far outside the die boundary) rather than via an actual
    DBU mismatch, so this test exercises the sanity check itself,
    independent of whether the read-side fix above is doing its job.

    Issue #1335 made the guard's tolerance the merged cell library's own
    *measured* abutment-box overhang (capped at the tech LEF's own row
    height, see `test_merge_def_to_gds_caps_measured_overhang_at_site_height`
    below) rather than a fixed 0.01 um -- this fixture's own shape is so far
    outside the die (tens of thousands of microns) that even a
    measurement taken directly from it, before any capping, would still
    leave the merged bbox far outside the expanded die, so this stays a
    valid regression guard for the doubling/halving symptom either way."""
    root = tmp_path / "install"
    cell_name = "testcell"
    tech_lef = tmp_path / "tech.tlef"
    cell_lef = tmp_path / "cells.lef"
    _write_tiny_tech_lef(tech_lef)
    _write_tiny_cell_lef(cell_lef, cell_name)

    gds_dir = root / "sky130A" / "libs.ref" / "sky130_fd_sc_hd" / "gds"
    gds_dir.mkdir(parents=True)
    # A cell GDS view whose own shape sits far outside the DEF's declared
    # `DIEAREA ( 0 0 ) ( 4600 2720 )` (4.6 x 2.72 um) -- e.g. a box spanning
    # 100..200 um, more than 20x the die's own extent.
    layout = kdb.Layout()
    layout.dbu = 0.001
    cell = layout.create_cell(cell_name)
    layer = layout.layer(kdb.LayerInfo(68, 20))
    cell.shapes(layer).insert(
        kdb.Box(100_000_000, 100_000_000, 200_000_000, 200_000_000)
    )
    layout.write(str(gds_dir / "sky130_fd_sc_hd.gds"))

    def_path = tmp_path / "design.def"
    _write_tiny_def(def_path, design_name="top", cell_name=cell_name)

    with pytest.raises(PlaceAndRouteError, match="DIEAREA"):
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


def test_merge_def_to_gds_tolerates_cell_library_abutment_overhang(tmp_path):
    """Issue #1335: open_pdks standard-cell libraries (gf180mcu in
    particular) deliberately draw geometry beyond their own LEF abutment
    box so abutting cells' wells/implants merge across a row -- e.g.
    gf180mcu_fd_sc_mcu9t5v0's own cells overhang by ~0.43/0.45 um, its
    `__endcap` cell by 2.93 um. A merge whose only "defect" is this kind of
    legitimate, small, library-declared overhang must succeed, not raise --
    before this fix, the guard's fixed 0.01 um tolerance rejected it."""
    root = tmp_path / "install"
    cell_name = "testcell"
    tech_lef = tmp_path / "tech.tlef"
    cell_lef = tmp_path / "cells.lef"
    _write_tiny_tech_lef(tech_lef)  # SITE unithd SIZE 0.46 BY 2.72
    _write_tiny_cell_lef(cell_lef, cell_name)  # MACRO testcell SIZE 0.46 BY 2.72

    gds_dir = root / "sky130A" / "libs.ref" / "sky130_fd_sc_hd" / "gds"
    gds_dir.mkdir(parents=True)
    # A cell GDS view that draws 0.1 um beyond its own LEF `SIZE` box on the
    # low edges -- the same shape as open_pdks' own deliberate well/implant-
    # merge overhang, just smaller in magnitude than gf180mcu's real
    # ~0.43 um (a 0.1 um overhang already exceeds the fixed 0.01 um
    # tolerance this test would have tripped on before this fix).
    layout = kdb.Layout()
    layout.dbu = 0.001
    cell = layout.create_cell(cell_name)
    layer = layout.layer(kdb.LayerInfo(68, 20))
    cell.shapes(layer).insert(kdb.Box(-100, -100, 460, 2720))
    layout.write(str(gds_dir / "sky130_fd_sc_hd.gds"))

    def_path = tmp_path / "design.def"
    # A single instance placed flush at the DEF's own origin (matching
    # `DIEAREA ( 0 0 ) ( 4600 2720 )`), so the cell's own left/bottom
    # overhang lands exactly at (and slightly past) the die boundary.
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


def test_merge_def_to_gds_caps_measured_overhang_at_site_height(tmp_path):
    """Issue #1335: the DIEAREA guard's tolerance is the merged cell
    library's own measured abutment-box overhang, but capped at the tech
    LEF's own row (`SITE`) height -- well/implant-merge geometry never
    spans multiple rows, so a measurement larger than that (however it
    arose) must not blindly widen the tolerance far enough to accept a
    genuinely out-of-die placement."""
    root = tmp_path / "install"
    cell_name = "testcell"
    tech_lef = tmp_path / "tech.tlef"
    cell_lef = tmp_path / "cells.lef"
    _write_tiny_tech_lef(tech_lef)  # SITE unithd SIZE 0.46 BY 2.72 -> 2.72 um cap
    _write_tiny_cell_lef(cell_lef, cell_name)

    gds_dir = root / "sky130A" / "libs.ref" / "sky130_fd_sc_hd" / "gds"
    gds_dir.mkdir(parents=True)
    # A cell GDS view that overhangs its own LEF `SIZE` box by 5 um on the
    # left -- larger than the 2.72 um row-height cap, so a die-edge
    # instance below must still be rejected rather than have its tolerance
    # widened to accept a 5 um excursion.
    layout = kdb.Layout()
    layout.dbu = 0.001
    cell = layout.create_cell(cell_name)
    layer = layout.layer(kdb.LayerInfo(68, 20))
    cell.shapes(layer).insert(kdb.Box(-5_000, 0, 460, 2_720))
    layout.write(str(gds_dir / "sky130_fd_sc_hd.gds"))

    def_path = tmp_path / "design.def"
    _write_tiny_def(def_path, design_name="top", cell_name=cell_name)

    with pytest.raises(PlaceAndRouteError, match="DIEAREA"):
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


# --------------------------------------------------------------------------- #
# Integration: real OpenROAD + a real, host-resolved sky130 PDK
# (skipped when either is unavailable -- never required for CI, see this
# module's own docstring)
# --------------------------------------------------------------------------- #

HAVE_OPENROAD = shutil.which("openroad") is not None


def _techlef_declares_layers(tlef_path: str, layers: tuple[str, ...]) -> bool:
    """True when ``tlef_path`` declares a ``LAYER <name>`` for every entry in
    ``layers`` (issue #1331).

    A cheap textual scan rather than a LEF parse: the gate only needs to know
    whether a metal name the request will reference exists in this variant's
    stack at all, and every open_pdks tech LEF writes those declarations one
    per line as ``LAYER <name>``."""
    try:
        with open(tlef_path, encoding="utf-8", errors="replace") as handle:
            declared = {
                line.split()[1]
                for line in handle
                if line.startswith("LAYER ") and len(line.split()) >= 2
            }
    except OSError:
        return False
    return all(layer in declared for layer in layers)


def _find_real_pnr_variant(
    cell_library: str, required_layers: tuple[str, ...] = ()
) -> tuple[str, str] | None:
    """Search every install/variant `list_pdks()` discovers for one shipping
    a real ``cell_library`` liberty + tech/cell LEF + GDS view. Returns
    ``(root, variant)`` or ``None``.

    Parameterized by cell library (issue #637) so the same gate serves both
    the sky130hd and gf180mcu live worked examples -- the four asset paths
    it probes are exactly what `lef_files()`/`_resolve_liberty`/
    `_resolve_gds_view` resolve, and none of them is PDK-family-specific.

    ``required_layers`` additionally requires the variant's ``__nom.tlef`` to
    declare each named routing layer (issue #1331). Asset *presence* is not
    sufficient for gf180mcu: a stock volare/open_pdks gf180mcu install ships
    four sibling variants that differ **only** in metal-stack depth --
    ``gf180mcuA`` is 3LM (``Metal1``..``Metal3``), ``gf180mcuB`` 4LM,
    ``gf180mcuC``/``gf180mcuD`` 5LM -- and all four ship a complete
    ``gf180mcu_fd_sc_mcu9t5v0`` liberty/LEF/GDS set. Without this check the
    first variant sorted alphabetically (``gf180mcuA``) wins the gate, and
    the run dies mid-flow in the ``place`` stage with
    ``[ERROR PPL-0051] Layer Metal4 not found`` -- observed live against
    ``openroad 26Q3-1080-gab6fd26351`` + volare
    ``open_pdks c6d73a35f524070e85faff4a6a9eef49553ebc2b``. The layers this
    module's gf180mcu request actually references are the ORFS-sourced
    ``Metal2``..``Metal5`` signal-routing range in
    ``place_and_route._ROUTING_LAYER_RANGE`` plus the ``Metal3``/``Metal4``
    IO layers below, i.e. a 5LM variant."""
    try:
        result = pdk_module.list_pdks()
    except Exception:
        return None
    for install in result["installs"]:
        for variant in install["variants"]:
            lib_dir = os.path.join(
                install["root"], variant["name"], "libs.ref", cell_library
            )
            tlef = os.path.join("techlef", f"{cell_library}__nom.tlef")
            if not all(
                os.path.exists(os.path.join(lib_dir, sub))
                for sub in (
                    "lib",
                    tlef,
                    os.path.join("lef", f"{cell_library}.lef"),
                    os.path.join("gds", f"{cell_library}.gds"),
                )
            ):
                continue
            if required_layers and not _techlef_declares_layers(
                os.path.join(lib_dir, tlef), required_layers
            ):
                continue
            return install["root"], variant["name"]
    return None


_REAL_SKY130_PNR_VARIANT = _find_real_pnr_variant("sky130_fd_sc_hd")
#: gf180mcu needs a 5LM variant -- see `_find_real_pnr_variant`'s docstring.
_REAL_GF180MCU_PNR_VARIANT = _find_real_pnr_variant(
    _GF180MCU_CELL_LIBRARY,
    required_layers=("Metal2", "Metal3", "Metal4", "Metal5"),
)


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
        "no real gf180mcu_fd_sc_mcu9t5v0 LEF/liberty/GDS set on a 5LM "
        "variant (tech LEF declaring Metal2-Metal5) resolves via list_pdks()"
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


@pytest.mark.skipif(
    not HAVE_OPENROAD, reason="openroad is not installed on this machine"
)
@pytest.mark.skipif(
    _REAL_SKY130_PNR_VARIANT is None,
    reason="no real sky130_fd_sc_hd LEF/liberty/GDS set resolves via list_pdks()",
)
def test_integration_real_openroad_def_pins_extract_lvs_pipeline(tmp_path, monkeypatch):
    """The end-to-end `klt place-and-route` -> `klt extract --def-pins` ->
    `klt lvs` pipeline (issue #1390's own acceptance criterion), against a
    real `openroad` binary and a real, host-resolved sky130 PDK install.

    Feeds OpenROAD a pre-synthesized structural netlist directly
    (`tests/corpus/statime/gcd_netlist.v`, already mapped to real
    `sky130_fd_sc_hd` cells) rather than going through `klt synthesize`'s own
    Yosys step -- issue #1390's own gap is in the DEF->GDS-merged layout's
    pin promotion, independent of how the gate-level netlist reaching `klt
    place-and-route` was produced (and this sandbox's own `yosys` happens to
    be a WASI-sandboxed build that cannot read a script path under
    `/tmp` -- a pre-existing, unrelated environment limitation this test
    sidesteps rather than depends on).

    Confirms `def_pins` -- derived automatically from the routed DEF's own
    `PINS` section via `place_and_route.def_pin_names`, *never* a
    hand-derived `--pins` list -- both (a) restricts the promoted pin set to
    the design's real 52-port interface on this real, densely-routed macro
    (unlike the unrestricted default, which -- measured live against this
    same fixture while implementing this test -- promotes several hundred
    spurious, collided/comma-joined pins from internal DEF `NETS`
    connection points) and (b) still reaches a clean `klt lvs` `match`
    verdict, mirroring `tests/test_lvs.py`'s own
    `test_pnr_gcd_fixture_self_compare_matches_cleanly`'s self-compare
    convention for a macro-scale corpus fixture.
    """
    root, variant = _REAL_SKY130_PNR_VARIANT
    monkeypatch.setenv("PDK_ROOT", root)
    monkeypatch.setenv("PDK", variant)

    netlist_src = Path(__file__).parent / "corpus" / "statime" / "gcd_netlist.v"
    netlist_path = tmp_path / "gcd_synth.v"
    netlist_path.write_text(netlist_src.read_text(encoding="utf-8"), encoding="utf-8")

    request_path = _write_request(
        tmp_path / "pnr_request.json",
        _base_request(netlist=str(netlist_path)),
    )
    report = run_place_and_route(request_path)

    assert report["status"] == "ok"
    assert report["stage_reached"] == "route"
    def_path = report["def_path"]
    gds_path = report["gds_path"]
    assert def_path is not None and os.path.isfile(def_path)
    assert gds_path is not None and os.path.isfile(gds_path)

    def_pins = place_and_route.def_pin_names(def_path)
    assert def_pins is not None
    # `gcd`'s real interface: clk, rst_n, start, done, plus 3 16-bit buses
    # (a_in/b_in/result).
    assert len(def_pins) == 52

    # The unrestricted default confirms the collision problem this issue
    # fixes is real on this fixture: a majority of promoted pins carry a
    # `|`-joined name (KLayout's own comma-join, rewritten to `|` for the
    # JSON report/written netlist by `spice_safe_net_name`).
    default_report = extract_module.run_extract(
        gds_path, "sky130", output=str(tmp_path / "gcd_default.spice")
    )
    default_pins = {n["name"] for n in default_report["nets"] if n["pin"]}
    collided_default = {name for name in default_pins if "|" in name}
    assert len(collided_default) > 100

    restricted_path = tmp_path / "gcd_def_pins.spice"
    restricted_report = extract_module.run_extract(
        gds_path, "sky130", output=str(restricted_path), def_pins=def_pins
    )
    restricted_pins = {n["name"] for n in restricted_report["nets"] if n["pin"]}

    # Every declared DEF port resolved to a real promoted net -- nothing
    # silently missing.
    assert not any(
        "matched no promoted net's label set" in w
        for w in restricted_report["warnings"]
    )
    # Every promoted pin's own label set actually contains a real declared
    # DEF port name -- no leftover, purely-internal DEF `NETS` collision
    # slipped through.
    for name in restricted_pins:
        assert set(name.split("|")) & def_pins, name

    from klayout_tools.lvs import run_lvs

    lvs_request_path = _write_request(
        tmp_path / "lvs_request.json",
        {
            "layout": {
                "netlist": str(restricted_path),
                "top": restricted_report["top"],
            },
            "reference": {
                "netlist": str(restricted_path),
                "top": restricted_report["top"],
            },
        },
    )
    lvs_report = run_lvs(lvs_request_path)

    assert lvs_report["status"] == "match"


# Sanity: `subprocess` really is the module this file's stubs patch (guards
# against a future refactor silently making the stubs a no-op).
def test_place_and_route_uses_stdlib_subprocess():
    assert place_and_route.subprocess is subprocess
