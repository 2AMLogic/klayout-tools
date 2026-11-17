"""Tests for ``klt lef-abstract``'s ``routing_resource`` figure and the
``klayout_tools.lef_abstract_resource`` module behind it (issue #5677).

The PDK-install / layout fixtures mirror ``tests/test_lef_abstract.py``'s
convention (fabricated open_pdks-shaped install under ``tmp_path``, layout
built programmatically with ``klayout.db``) -- kept self-contained here so
the ratchet-pinned ``test_lef_abstract.py`` stays untouched.

Expected track counts below are derived from the fixture tech LEF's own
grid (li1: VERTICAL, PITCH 0.46, OFFSET 0.23, WIDTH 0.17; met1: HORIZONTAL,
PITCH 0.34, OFFSET 0.17, WIDTH 0.14) over a 5x5 um outline: li1 has 11
in-bounds track columns (x = 0.23 + 0.46k <= 5 - 0.085), met1 has 15
in-bounds track rows (y = 0.17 + 0.34k <= 5 - 0.07).
"""

from __future__ import annotations

import json
from pathlib import Path

import klayout.db as kdb

from klayout_tools import pdk as pdk_module
from klayout_tools.lef_abstract import run_lef_abstract
from klayout_tools.lef_abstract_resource import (
    resource_entry,
    routing_resource,
)

DBU_UM = 0.001

LI1_NET = (67, 20)
MET1_NET = (68, 20)
MET3_NET = (69, 20)
NWELL = (64, 20)  # deliberately not in the routing-layer map


def _isolate_pdk(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(pdk_module, "STORE_DIRS", [])
    monkeypatch.setattr(pdk_module, "CONVENTIONAL_PREFIXES", [])


_TECH_LEF_TEXT = """\
VERSION 5.7 ;
UNITS
  DATABASE MICRONS 1000 ;
END UNITS
MANUFACTURINGGRID 0.005 ;

SITE unithd
  SYMMETRY Y ;
  CLASS CORE ;
  SIZE 0.46 BY 2.72 ;
END unithd

LAYER nwell
  TYPE MASTERSLICE ;
END nwell

LAYER li1
  TYPE ROUTING ;
  DIRECTION VERTICAL ;
  PITCH 0.46 0.34 ;
  OFFSET 0.23 0.17 ;
  WIDTH 0.17 ;
END li1

LAYER met1
  TYPE ROUTING ;
  DIRECTION HORIZONTAL ;
  PITCH 0.34 ;
  OFFSET 0.17 ;
  WIDTH 0.14 ;
END met1
"""

_MAP_FILE_TEXT = """\
li1     NET,SPNET,VIA            67  20
li1     LEFPIN,PIN               67  16
li1     LEFOBS                   67  5
nwell   LEFPIN                   64  16
met1    LEFPIN,NET,SPNET,PIN,VIA 68  20
met1    LEFPIN,PIN               68  16
met1    LEFOBS                   68  4
"""

_MET3_LAYER_TEXT = """
LAYER met3
  TYPE ROUTING ;
  DIRECTION HORIZONTAL ;
  PITCH 0.6 ;
  OFFSET 0.3 ;
  WIDTH 0.2 ;
END met3
"""

_MET3_MAP_LINE = "met3    NET,SPNET,PIN           69  20\n"


def _tech_lef_with_met3(*, omit: str) -> str:
    """The fixture tech LEF plus a ``met3`` routing layer whose ``omit``
    ("PITCH" or "DIRECTION") statement is left out."""
    line = {
        "PITCH": "  PITCH 0.6 ;\n",
        "DIRECTION": "  DIRECTION HORIZONTAL ;\n",
    }[omit]
    return _TECH_LEF_TEXT + _MET3_LAYER_TEXT.replace(line, "")


def _map_text_with_met3() -> str:
    return _MAP_FILE_TEXT + _MET3_MAP_LINE


def _make_pdk_install(
    root: Path,
    variant: str,
    cell_library: str = "sky130_fd_sc_hd",
    *,
    tech_lef_text: str = _TECH_LEF_TEXT,
    map_text: str = _MAP_FILE_TEXT,
) -> None:
    variant_dir = root / variant
    (variant_dir / "libs.tech").mkdir(parents=True, exist_ok=True)

    techlef_dir = variant_dir / "libs.ref" / cell_library / "techlef"
    techlef_dir.mkdir(parents=True, exist_ok=True)
    (techlef_dir / f"{cell_library}__nom.tlef").write_text(
        tech_lef_text, encoding="utf-8"
    )

    klayout_tech_dir = variant_dir / "libs.tech" / "klayout" / "tech"
    klayout_tech_dir.mkdir(parents=True, exist_ok=True)
    (klayout_tech_dir / f"{variant}.map").write_text(map_text, encoding="utf-8")


def _box_um(x0: float, y0: float, x1: float, y1: float) -> kdb.Box:
    return kdb.Box(
        round(x0 / DBU_UM), round(y0 / DBU_UM), round(x1 / DBU_UM), round(y1 / DBU_UM)
    )


def _basic_layout(**overrides) -> kdb.Layout:
    """VOUT port on li1, VDD port + one general obstruction on met1 (the
    same shape ``tests/test_lef_abstract.py``'s basic fixture draws)."""
    obstruction = overrides.get("obstruction", (1.0, 1.0, 4.0, 1.5))
    layout = kdb.Layout()
    layout.dbu = DBU_UM
    top = layout.create_cell("ANALOG_BLOCK")
    li1 = layout.layer(*LI1_NET)
    met1 = layout.layer(*MET1_NET)
    top.shapes(li1).insert(_box_um(2.0, 0.0, 3.0, 0.2))  # VOUT port
    top.shapes(met1).insert(_box_um(0.0, 2.0, 0.2, 3.0))  # VDD port
    top.shapes(met1).insert(_box_um(*obstruction))  # general obstruction
    return layout


def _met3_layout() -> kdb.Layout:
    layout = kdb.Layout()
    layout.dbu = DBU_UM
    top = layout.create_cell("ANALOG_BLOCK")
    met3 = layout.layer(*MET3_NET)
    top.shapes(met3).insert(_box_um(0.5, 2.0, 4.5, 2.6))
    return layout


def _basic_descriptor(**overrides) -> dict:
    descriptor = {
        "schema": "klt.socket/1",
        "name": "analog_block",
        "outline": {"x0": 0.0, "y0": 0.0, "x1": 5.0, "y1": 5.0},
        "pins": [
            {"name": "VOUT", "layer": list(LI1_NET), "x": 2.5, "y": 0.1},
            {"name": "VDD", "layer": list(MET1_NET), "x": 0.1, "y": 2.5},
        ],
        "reserved_layers": [],
        "budgets": [],
    }
    descriptor.update(overrides)
    return descriptor


def _run(
    tmp_path: Path,
    monkeypatch,
    *,
    layout: kdb.Layout | None = None,
    descriptor: dict | None = None,
    tech_lef_text: str = _TECH_LEF_TEXT,
    map_text: str = _MAP_FILE_TEXT,
    **kwargs,
) -> dict:
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(
        install_root, "sky130A", tech_lef_text=tech_lef_text, map_text=map_text
    )
    monkeypatch.setenv("PDK_ROOT", str(install_root))

    layout_path = tmp_path / "block.gds"
    (layout or _basic_layout()).write(str(layout_path))
    socket_path = tmp_path / "socket.json"
    socket_path.write_text(json.dumps(descriptor or _basic_descriptor()))
    output_path = str(tmp_path / "out.lef")

    kwargs.setdefault("macro_name", "analog_block")
    kwargs.setdefault("cell_library", "sky130_fd_sc_hd")
    kwargs.setdefault("output_path", output_path)
    return run_lef_abstract(str(layout_path), str(socket_path), **kwargs)


def _entry(report: dict, layer: str) -> dict:
    return next(e for e in report["routing_resource"] if e["layer"] == layer)


# --------------------------------------------------------------------------- #
# report-level: the figure over the basic fixture
# --------------------------------------------------------------------------- #


def test_resource_covers_free_and_obstructed_layers_detailed(tmp_path, monkeypatch):
    report = _run(tmp_path, monkeypatch)
    assert [e["layer"] for e in report["routing_resource"]] == ["li1", "met1"]

    li1 = _entry(report, "li1")
    assert li1 == {
        "layer": "li1",
        "direction": "VERTICAL",
        "pitch_um": 0.46,
        "obstructed_fraction": 0.0,
        "free_tracks": 11,
    }

    # OBS = the (1,1)-(4,1.5) obstruction alone (the VDD port is subtracted):
    # 3x0.5 um of 25 um^2, and it fully blocks the 2 track rows it spans.
    met1 = _entry(report, "met1")
    assert met1 == {
        "layer": "met1",
        "direction": "HORIZONTAL",
        "pitch_um": 0.34,
        "obstructed_fraction": 0.06,
        "free_tracks": 13,
    }


def test_coarse_reports_the_bbox_figures_not_detailed_ones(tmp_path, monkeypatch):
    detailed = _run(tmp_path, monkeypatch)
    coarse = _run(tmp_path, monkeypatch, obs_mode="coarse")

    # Same layout, same geometry source: coarse's bounding rectangle
    # (0,1)-(4,3) minus the VDD port blocks 6 more rows than the detailed
    # region did, and covers 7.8 of 25 um^2 instead of 1.5.
    assert _entry(detailed, "met1")["free_tracks"] == 13
    assert _entry(coarse, "met1")["free_tracks"] == 9
    assert _entry(coarse, "met1")["obstructed_fraction"] == 0.312
    # li1 has no obstruction geometry in either mode -> identical entries.
    assert _entry(coarse, "li1") == _entry(detailed, "li1")


def test_coarse_full_span_obstruction_blocks_every_track(tmp_path, monkeypatch):
    # Pinless descriptor: a full-span obstruction overlapping a declared pin
    # position would be absorbed into that pin's port (existing pin
    # resolution) and subtracted back out of the OBS, defeating the point.
    report = _run(
        tmp_path,
        monkeypatch,
        layout=_basic_layout(obstruction=(0.0, 0.0, 5.0, 5.0)),
        descriptor=_basic_descriptor(pins=[]),
        obs_mode="coarse",
    )
    met1 = _entry(report, "met1")
    assert met1["free_tracks"] == 0
    assert met1["obstructed_fraction"] == 1.0


# --------------------------------------------------------------------------- #
# track-grid edges
# --------------------------------------------------------------------------- #


def test_macro_narrower_than_one_pitch_reports_zero_tracks(tmp_path, monkeypatch):
    outline = {"x0": 0.0, "y0": 0.0, "x1": 5.0, "y1": 0.2}
    report = _run(tmp_path, monkeypatch, descriptor=_basic_descriptor(outline=outline))
    assert _entry(report, "met1")["free_tracks"] == 0
    assert _entry(report, "li1")["free_tracks"] == 11  # cross axis unaffected


def test_missing_offset_defaults_track_origin_to_zero(tmp_path, monkeypatch):
    tech_lef_text = _TECH_LEF_TEXT.replace("  OFFSET 0.17 ;\n", "")
    report = _run(tmp_path, monkeypatch, tech_lef_text=tech_lef_text)
    # Tracks start at y=0 instead of 0.17: rows 0.34k land differently --
    # 14 in-bounds rows, 2 of them blocked by the obstruction.
    assert _entry(report, "met1")["free_tracks"] == 12


def test_first_track_obstructed_when_obstruction_covers_its_strip(
    tmp_path, monkeypatch
):
    outline = {"x0": 0.0, "y0": 0.0, "x1": 5.0, "y1": 1.2}
    report = _run(
        tmp_path,
        monkeypatch,
        layout=_basic_layout(obstruction=(0.0, 0.1, 5.0, 0.24)),
        descriptor=_basic_descriptor(outline=outline),
    )
    # The obstruction hugs exactly the first track's strip (y=0.17 +- 0.07);
    # rows 0.51 and 0.85 stay free, row 1.19 hangs over the 1.2 um edge.
    met1 = _entry(report, "met1")
    assert met1["free_tracks"] == 2
    assert met1["obstructed_fraction"] == 0.116667  # 0.7 of 6 um^2


# --------------------------------------------------------------------------- #
# unknown track grid: null + warning, never a guess
# --------------------------------------------------------------------------- #


def test_layer_missing_pitch_reports_null_and_warns(tmp_path, monkeypatch):
    report = _run(
        tmp_path,
        monkeypatch,
        layout=_met3_layout(),
        tech_lef_text=_tech_lef_with_met3(omit="PITCH"),
        map_text=_map_text_with_met3(),
    )
    met3 = _entry(report, "met3")
    assert met3["free_tracks"] is None
    assert met3["pitch_um"] is None
    assert met3["direction"] == "HORIZONTAL"
    # The fraction needs no track grid: still the plain 4x0.6 of 25 um^2.
    assert met3["obstructed_fraction"] == 0.096
    assert any("no PITCH" in w and "met3" in w for w in report["warnings"])


def test_layer_missing_direction_reports_null_and_warns(tmp_path, monkeypatch):
    report = _run(
        tmp_path,
        monkeypatch,
        layout=_met3_layout(),
        tech_lef_text=_tech_lef_with_met3(omit="DIRECTION"),
        map_text=_map_text_with_met3(),
    )
    met3 = _entry(report, "met3")
    assert met3["free_tracks"] is None
    assert met3["pitch_um"] is None
    assert met3["direction"] is None
    assert any("DIRECTION" in w and "met3" in w for w in report["warnings"])


# --------------------------------------------------------------------------- #
# unit: the pure helpers, no PDK install
# --------------------------------------------------------------------------- #


def test_routing_resource_covers_every_routing_layer_sorted():
    layers = {
        "met1": _layer("met1", "HORIZONTAL"),
        "met2": _layer("met2", "VERTICAL"),
        "met3": {"name": "met3", "type": "ROUTING", "direction": None},
    }
    regions = {"met1": kdb.Region(kdb.Box(0, 1000, 3000, 1500))}
    warnings: list[str] = []
    entries = routing_resource(
        regions_by_layer=regions,
        routing_layers=layers,
        width_dbu=5000,
        height_dbu=5000,
        dbu_um=DBU_UM,
        warnings=warnings,
    )
    assert [e["layer"] for e in entries] == ["met1", "met2", "met3"]
    assert entries[0]["free_tracks"] == 13
    assert entries[1]["free_tracks"] == 15
    assert entries[2]["free_tracks"] is None
    assert len(warnings) == 1 and "met3" in warnings[0]


def test_resource_entry_empty_region_is_the_fully_free_case():
    warnings: list[str] = []
    entry = resource_entry(
        region=None,
        layer=_layer("li1", "VERTICAL", pitch=0.46, offset=0.23, width=0.17),
        width_dbu=5000,
        height_dbu=5000,
        dbu_um=DBU_UM,
        warnings=warnings,
    )
    assert entry == {
        "layer": "li1",
        "direction": "VERTICAL",
        "pitch_um": 0.46,
        "obstructed_fraction": 0.0,
        "free_tracks": 11,
    }
    assert warnings == []


def _layer(
    name: str,
    direction: str,
    *,
    pitch: float = 0.34,
    offset: float = 0.17,
    width: float = 0.14,
) -> dict:
    return {
        "name": name,
        "type": "ROUTING",
        "direction": direction,
        "width_um": width,
        "pitch_x_um": pitch,
        "pitch_y_um": pitch,
        "offset_x_um": offset,
        "offset_y_um": offset,
    }
