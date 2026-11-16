"""Real-OpenROAD ASAP7 ``post_route_spef`` + ``post_route_sdf`` run (issue #4702).

Every other ASAP7 ``post_route_spef`` test in ``tests/test_place_and_route.py``
stubs OpenROAD and ``run_extract``. This module runs the actual flow
(floorplan -> place -> cts -> route -> ``klt extract --deck asap7`` -> OpenSTA
``read_spef`` / ``write_sdf``) on a two-inverter block of pinned ASAP7 RVT
cells, so an ASAP7 consumer (asap7-ucie-phy#23) can point at a test that
exercises the whole post-route A/B path.

The lambdapdk ASAP7 tree is not open_pdks-shaped (split ``nldm/*.lib.gz``
liberties, tech LEF under ``base/apr``), which is the shape ``pdk.py``'s
liberty/LEF resolver reads. The fixture therefore assembles a minimal
open_pdks-shaped ``asap7A`` variant from the pinned lambdapdk files under
``tmp_path`` -- no repo or host state is touched.

Skips, each with an explicit reason: ``openroad`` not on PATH,
``KLT_SKIP_OPENROAD_TESTS=1``, or ``pdks/lambdapdk`` not fetched
(``scripts/fetch-pdks.sh``). Run single-process; the flow takes a few seconds.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
from pathlib import Path

import pytest

from klayout_tools.place_and_route import run_place_and_route

_ASAP7 = Path(__file__).resolve().parent.parent / "pdks/lambdapdk/lambdapdk/asap7"
_RVT = _ASAP7 / "libs/asap7sc7p5t_rvt"
_LIB_GZ = _RVT / "nldm/asap7sc7p5t_INVBUF_RVT_TT_nldm.lib.gz"
_CELL_LEF = _RVT / "lef/asap7sc7p5t_28_R.lef"
_TECH_LEF = _ASAP7 / "base/apr/asap7_tech.lef"
_LAYERMAP = _ASAP7 / "base/apr/asap7.layermap"
_CELL_GDS_GZ = _RVT / "gds/asap7sc7p5t_28_R.gds.gz"
_CELL_LIBRARY = "asap7sc7p5t_rvt"
_INV = "INVx1_ASAP7_75t_R"

if shutil.which("openroad") is None:
    _SKIP_REASON = "openroad is not installed on this machine"
elif os.environ.get("KLT_SKIP_OPENROAD_TESTS") == "1":
    _SKIP_REASON = "KLT_SKIP_OPENROAD_TESTS=1 is set"
elif not all(
    p.is_file() for p in (_LIB_GZ, _CELL_LEF, _TECH_LEF, _CELL_GDS_GZ, _LAYERMAP)
):
    _SKIP_REASON = (
        "pinned lambdapdk ASAP7 libraries not found under pdks/lambdapdk -- "
        "run scripts/fetch-pdks.sh to enable this test"
    )
else:
    _SKIP_REASON = ""

pytestmark = pytest.mark.skipif(bool(_SKIP_REASON), reason=_SKIP_REASON)

_NETLIST = f"""\
module top (input clk, input a, output y);
  wire n;
  {_INV} u1 (.A(a), .Y(n));
  {_INV} u2 (.A(n), .Y(y));
endmodule
"""

#: ASAP7 RVT routing grid from docs/cli/place-and-route.md ("ASAP7 RVT example"),
#: extended down to M1-M3 because unlisted layers get no tracks and the tech
#: LEF's negative M2 OFFSET makes OpenROAD's automatic make_tracks fail.
_TRACKS = [
    {
        "layer": layer,
        "x_offset_um": offset,
        "x_pitch_um": pitch,
        "y_offset_um": offset,
        "y_pitch_um": pitch,
    }
    for layer, pitch, offset in (
        ("M1", 0.036, 0.0),
        ("M2", 0.036, 0.0),
        ("M3", 0.036, 0.0),
        ("M4", 0.048, 0.012),
        ("M5", 0.048, 0.012),
        ("M6", 0.064, 0.016),
        ("M7", 0.064, 0.016),
    )
]


def _gunzip(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(src, "rb") as fin, open(dest, "wb") as fout:
        shutil.copyfileobj(fin, fout)


def _make_asap7_install(root: Path) -> None:
    """Minimal open_pdks-shaped ``asap7A`` variant from the pinned lambdapdk."""
    lib = root / "asap7A/libs.ref" / _CELL_LIBRARY
    klayout_tech = root / "asap7A/libs.tech/klayout/tech"
    klayout_tech.mkdir(parents=True)
    # Without a LEF/DEF layer map the DEF->GDS merge falls back to KLayout's
    # sequential layer numbering, which the asap7 extraction deck cannot see
    # (issue #4705): extraction then finds no routed nets.
    shutil.copy(_LAYERMAP, klayout_tech / "asap7A.map")
    # The INVBUF liberty is the one library group holding the inverter and
    # the clock buffer; the other ASAP7 liberties are separate groups and
    # cannot be concatenated into one file.
    _gunzip(_LIB_GZ, lib / "lib" / f"{_CELL_LIBRARY}__TT.lib")
    _gunzip(_CELL_GDS_GZ, lib / "gds" / f"{_CELL_LIBRARY}.gds")
    (lib / "lef").mkdir()
    (lib / "techlef").mkdir()
    shutil.copy(_CELL_LEF, lib / "lef" / f"{_CELL_LIBRARY}.lef")
    shutil.copy(_TECH_LEF, lib / "techlef" / f"{_CELL_LIBRARY}__nom.tlef")


def _request() -> dict:
    return {
        "engine": "openroad",
        "netlist": "top.v",
        "hdl_toplevel": "top",
        "pdk": {"cell_library": _CELL_LIBRARY, "corner": "TT"},
        "floorplan": {
            "method": "utilization",
            "utilization_pct": 20,
            "aspect_ratio": 1.0,
            "core_margin_um": 1.0,
            "site": "asap7sc7p5t",
            "tracks": _TRACKS,
        },
        "io": {"layer_h": "M4", "layer_v": "M5"},
        "constraints": {"clock_port": "clk", "clock_period_ns": 1.0},
        "library": {
            "cts_buffer": "BUFx4_ASAP7_75t_R",
            "route_layers": {"low": "M2", "high": "M7"},
            "antenna_diode": None,
            "wire_rc": {
                "resistance_ohm_per_um": 0.0000432,
                "capacitance_pf_per_um": 0.00016579,
            },
        },
        # The packaged deck measures FinFET geometry at 0.25 nm.
        "output_dbu_um": 0.00025,
        "post_route_spef": True,
        "post_route_sdf": True,
        "seed": 1,
        "target_stage": "route",
    }


def test_asap7_route_with_post_route_spef_and_sdf(
    tmp_path, monkeypatch, record_property
):
    import time

    pdk_root = tmp_path / "pdkroot"
    _make_asap7_install(pdk_root)
    monkeypatch.setenv("PDK_ROOT", str(pdk_root))
    monkeypatch.setenv("PDK", "asap7A")
    work = tmp_path / "work"
    work.mkdir()
    (work / "top.v").write_text(_NETLIST, encoding="utf-8")
    request_path = work / "request.json"
    request_path.write_text(json.dumps(_request()), encoding="utf-8")
    monkeypatch.chdir(work)

    started = time.monotonic()
    report = run_place_and_route(str(request_path))
    runtime_s = round(time.monotonic() - started, 2)
    record_property("runtime_s", runtime_s)
    print(f"asap7 post_route_spef+sdf runtime: {runtime_s}s")

    assert report["status"] == "ok"
    assert report["stage_reached"] == "route"

    out_dir = Path(report["def_path"]).parent
    spef = out_dir / "top_route.spef"
    sdf = out_dir / "top_route.sdf"
    assert spef.is_file() and spef.stat().st_size > 0
    assert sdf.is_file() and sdf.stat().st_size > 0

    # Names the routed DEF carries: internal net ``n`` and instances u1/u2.
    def_text = Path(report["def_path"]).read_text(encoding="utf-8")
    def_nets = set(re.findall(r"^\s*-\s+(\S+)\s+\(", def_text, re.M))
    assert "n" in def_nets

    spef_text = spef.read_text(encoding="utf-8")
    d_nets = re.findall(r"^\*D_NET\s+(\S+)", spef_text, re.M)
    assert d_nets, "SPEF has no *D_NET blocks"
    assert set(d_nets) & def_nets, (d_nets, def_nets)

    sdf_text = sdf.read_text(encoding="utf-8")
    sdf_instances = re.findall(r"\(INSTANCE\s+(\S+?)\)", sdf_text)
    assert sdf_instances, "SDF has no (INSTANCE ...) entries"
    assert {"u1", "u2"} & set(sdf_instances), sdf_instances
