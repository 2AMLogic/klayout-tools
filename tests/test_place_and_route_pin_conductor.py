"""Top-level DEF pins are drawn on the conductor layer too (issue #5700).

An open_pdks-style KLayout layer map that declares a routing layer's ``NET``
and ``PIN`` purposes on *separate* lines makes KLayout's DEF reader draw a
top-level ``PINS`` rectangle on the pin-purpose ``(layer, datatype)`` only.
Its name label sits at the centre of that rectangle; when the router's wire
merely touches one end of a long rectangle, no conductor covers the label
point, so ``klt extract`` never attaches the name and promotes no pin.

``_merge_def_to_gds`` therefore reads the DEF through a derived map that also
draws each such pin on the conductor pair. These tests pin the map derivation
(unit), the geometry on a real LEF/DEF merge, and the extraction outcome (the
pin is promoted *by name*).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from klayout_tools import place_and_route
from klayout_tools.extract import run_extract
from klayout_tools.place_and_route_gds_merge import (
    _pin_conductor_map_entries,
    _stage_pin_conductor_layer_map,
)

kdb = pytest.importorskip("klayout.db")

_MET1_NET = (68, 20)
_MET1_PIN = (68, 16)
#: Where the sky130 deck reads pin-name labels (`68/5`); the map's `NAME`
#: entry puts the DEF pin's label there.
_MET1_LABEL = (68, 5)

#: The issue's map shape: NET and PIN purposes on separate lines, with the
#: name label entry on the pin purpose (sky130's own pin-label convention).
_SPLIT_MAP = (
    "# header comment\n"
    "li1 NET,SPNET,VIA 67 20\n"
    "met1 NET,SPNET,VIA 68 20\n"
    "met1 PIN 68 16\n"
    "NAME met1/PIN 68 5\n"
    "met2 NET 69 20\n"  # NET only: nothing to synthesize
    "met3 PIN 70 16\n"  # PIN only: nothing to synthesize
    "met5 NET,PIN 72 20\n"  # one line covers both: already on conductor
    "DIEAREA ALL 235 0\n"
)


# --------------------------------------------------------------------------- #
# Map derivation (unit).
# --------------------------------------------------------------------------- #


def test_entries_cover_only_layers_with_split_net_and_pin_purposes(tmp_path):
    map_path = tmp_path / "split.map"
    map_path.write_text(_SPLIT_MAP, encoding="utf-8")

    assert _pin_conductor_map_entries(str(map_path)) == [("met1", 68, 20)]


def test_staged_map_preserves_every_original_line_and_adds_one_entry(tmp_path):
    map_path = tmp_path / "split.map"
    map_path.write_text(_SPLIT_MAP, encoding="utf-8")
    before = map_path.read_bytes()
    staged = tmp_path / "staged.map"

    info = _stage_pin_conductor_layer_map(str(map_path), str(staged))

    assert info == {
        "status": "staged",
        "layers": [{"layer_name": "met1", "layer": 68, "datatype": 20}],
    }
    staged_lines = staged.read_text(encoding="utf-8").splitlines()
    assert staged_lines[: len(_SPLIT_MAP.splitlines())] == _SPLIT_MAP.splitlines()
    added = staged_lines[len(_SPLIT_MAP.splitlines()) :]
    assert [line for line in added if not line.startswith("#")] == ["met1 PIN 68 20"]
    assert map_path.read_bytes() == before  # the PDK's own file is untouched


def test_layers_without_a_split_pair_are_passed_through_unchanged(tmp_path):
    map_path = tmp_path / "plain.map"
    map_path.write_text(
        "met1 LEFPIN,NET,SPNET,PIN,VIA 68 20\nmet2 NET 69 20\nmet3 PIN 70 16\n",
        encoding="utf-8",
    )
    staged = tmp_path / "staged.map"

    assert _stage_pin_conductor_layer_map(str(map_path), str(staged)) == {
        "status": "unchanged",
        "layers": [],
    }
    assert not staged.exists()


def test_no_resolved_map_is_skipped_not_an_error(tmp_path):
    info = _stage_pin_conductor_layer_map(None, str(tmp_path / "staged.map"))

    assert info["status"] == "skipped"
    assert info["layers"] == []
    assert not (tmp_path / "staged.map").exists()


def test_unreadable_map_degrades_to_the_pdk_map(tmp_path):
    missing = str(tmp_path / "does-not-exist.map")

    assert _pin_conductor_map_entries(missing) == []
    assert (
        _stage_pin_conductor_layer_map(missing, str(tmp_path / "s.map"))["status"]
        == "unchanged"
    )
    garbage = tmp_path / "garbage.map"
    garbage.write_bytes(b"\xff\xfe met1 NET x y\nmet1 PIN\n")
    assert _pin_conductor_map_entries(str(garbage)) == []


# --------------------------------------------------------------------------- #
# A real LEF/DEF merge.
# --------------------------------------------------------------------------- #

_CELL = "testcell"
# The pin rectangle is 1.0 um x 0.1 um centred on (1.0, 1.0); the routed wire
# starts at its right end, so the label point (the rectangle centre) is not
# covered by any wire geometry -- the issue's measured case.
_PIN_CENTRE = (1000, 1000)


def _write_tech_lef(path: Path) -> None:
    path.write_text(
        "VERSION 5.7 ;\n"
        'BUSBITCHARS "[]" ;\n'
        'DIVIDERCHAR "/" ;\n'
        "UNITS\n  DATABASE MICRONS 1000 ;\nEND UNITS\n"
        "MANUFACTURINGGRID 0.005 ;\n"
        "SITE unithd\n  SYMMETRY Y ;\n  CLASS CORE ;\n"
        "  SIZE 0.46 BY 2.72 ;\nEND unithd\n"
        "LAYER met1\n  TYPE ROUTING ;\n  DIRECTION HORIZONTAL ;\n"
        "  WIDTH 0.1 ;\n  PITCH 0.34 ;\nEND met1\n"
        "END LIBRARY\n",
        encoding="utf-8",
    )


def _write_cell_lef(path: Path) -> None:
    path.write_text(
        "VERSION 5.7 ;\n"
        'BUSBITCHARS "[]" ;\n'
        'DIVIDERCHAR "/" ;\n'
        "UNITS\n  DATABASE MICRONS 1000 ;\nEND UNITS\n"
        f"MACRO {_CELL}\n  CLASS CORE ;\n  SITE unithd ;\n  SIZE 0.46 BY 2.72 ;\n"
        "  PIN X\n    DIRECTION OUTPUT ;\n    PORT\n"
        "      LAYER met1 ; RECT 0 0 0.1 0.1 ;\n    END\n  END X\n"
        f"END {_CELL}\n"
        "END LIBRARY\n",
        encoding="utf-8",
    )


def _write_cell_gds(path: Path) -> None:
    layout = kdb.Layout()
    layout.dbu = 0.001
    cell = layout.create_cell(_CELL)
    cell.shapes(layout.layer(kdb.LayerInfo(*_MET1_NET))).insert(
        kdb.Box(0, 0, 460, 2720)
    )
    layout.write(str(path))


def _write_def(path: Path) -> None:
    path.write_text(
        "VERSION 5.8 ;\n"
        'DIVIDERCHAR "/" ;\n'
        'BUSBITCHARS "[]" ;\n'
        "DESIGN top ;\n"
        "UNITS DISTANCE MICRONS 1000 ;\n"
        "DIEAREA ( 0 0 ) ( 40000 40000 ) ;\n"
        "ROW ROW_0 unithd 0 0 N DO 10 BY 1 STEP 460 0 ;\n"
        "COMPONENTS 1 ;\n"
        f"- inst1 {_CELL} + PLACED ( 20000 20000 ) N ;\n"
        "END COMPONENTS\n"
        "PINS 1 ;\n"
        "- A + NET A + DIRECTION INPUT + USE SIGNAL\n"
        "  + LAYER met1 ( -500 -50 ) ( 500 50 )\n"
        f"  + PLACED ( {_PIN_CENTRE[0]} {_PIN_CENTRE[1]} ) N ;\n"
        "END PINS\n"
        "NETS 1 ;\n"
        "- A ( PIN A )\n"
        "  + ROUTED met1 ( 1500 1000 ) ( 5000 1000 ) ;\n"
        "END NETS\n"
        "END DESIGN\n",
        encoding="utf-8",
    )


def _build_fixture(tmp_path: Path, map_text: str | None = _SPLIT_MAP) -> dict:
    root = tmp_path / "install"
    tech_lef = tmp_path / "tech.tlef"
    cell_lef = tmp_path / "cells.lef"
    _write_tech_lef(tech_lef)
    _write_cell_lef(cell_lef)

    gds_dir = root / "sky130A" / "libs.ref" / "sky130_fd_sc_hd" / "gds"
    gds_dir.mkdir(parents=True)
    _write_cell_gds(gds_dir / "sky130_fd_sc_hd.gds")

    klayout_dir = root / "sky130A" / "libs.tech" / "klayout"
    tech_dir = klayout_dir / "tech"
    tech_dir.mkdir(parents=True)
    if map_text is not None:
        (tech_dir / "sky130A.map").write_text(map_text, encoding="utf-8")

    def_path = tmp_path / "design.def"
    _write_def(def_path)
    return {
        "def_path": str(def_path),
        "tech_lef": str(tech_lef),
        "cell_lef": str(cell_lef),
        "pdk_info": {
            "schema_version": 1,
            "root": str(root),
            "variant": "sky130A",
            "version": None,
            "resolved_via": "test",
            "assets": {
                "libs_ref": str(root / "sky130A" / "libs.ref"),
                "klayout": str(klayout_dir),
            },
        },
        "cell_library": "sky130_fd_sc_hd",
        "hdl_toplevel": "top",
        "macros": [],
        "out_path": str(tmp_path / "out.gds"),
    }


def _region(layout, top, pair):
    index = layout.find_layer(*pair)
    assert index is not None, f"layer {pair} missing from merged GDS"
    return kdb.Region(top.begin_shapes_rec(index)).merged()


def test_fixture_reproduces_the_unfixed_reader_behaviour(tmp_path):
    """Guard on the fixture, not on the fix: read the DEF with the PDK's own
    map directly and confirm the pin rectangle lands on the pin purpose only,
    leaving the label point uncovered by conductor. If this stopped holding,
    the geometry assertion below would pass vacuously."""
    kwargs = _build_fixture(tmp_path)
    opts = kdb.LoadLayoutOptions()
    opts.lefdef_config.lef_files = [kwargs["tech_lef"], kwargs["cell_lef"]]
    opts.lefdef_config.map_file = str(
        Path(kwargs["pdk_info"]["assets"]["klayout"]) / "tech" / "sky130A.map"
    )
    opts.lefdef_config.dbu = 0.001
    layout = kdb.Layout()
    layout.read(kwargs["def_path"], opts)
    top = layout.cell("top")

    pin = _region(layout, top, _MET1_PIN)
    net = _region(layout, top, _MET1_NET)
    assert pin.bbox() == kdb.Box(500, 950, 1500, 1050)
    point = kdb.Region(kdb.Box(*_PIN_CENTRE, *_PIN_CENTRE).enlarged(1, 1))
    assert (point & net).is_empty()


def test_merge_draws_the_pin_on_conductor_and_keeps_the_label_on_the_pin_layer(
    tmp_path,
):
    kwargs = _build_fixture(tmp_path)
    pdk_map = Path(kwargs["pdk_info"]["assets"]["klayout"]) / "tech" / "sky130A.map"
    before = pdk_map.read_bytes()

    info = place_and_route._merge_def_to_gds(**kwargs)

    layout = kdb.Layout()
    layout.read(kwargs["out_path"])
    top = layout.top_cell()
    net = _region(layout, top, _MET1_NET)
    # The pin rectangle is conductor now, and one net with the routed wire.
    point = kdb.Region(kdb.Box(*_PIN_CENTRE, *_PIN_CENTRE).enlarged(1, 1))
    assert (point - net).is_empty()
    # Pin rectangle + routed wire merge into one polygon (the other polygon is
    # the placed cell's own met1).
    assert kdb.Box(500, 950, 5050, 1050) in [poly.bbox() for poly in net.each()]

    # The label stays on its own layer, and is not duplicated on conductor.
    def texts(pair):
        index = layout.find_layer(*pair)
        return [
            it.shape().text_string
            for it in top.begin_shapes_rec(index)
            if it.shape().is_text()
        ]

    assert texts(_MET1_LABEL) == ["A"]
    assert texts(_MET1_NET) == []

    assert info["pin_conductor"] == {
        "status": "staged",
        "layers": [{"layer_name": "met1", "layer": 68, "datatype": 20}],
    }
    # `layer_map` keeps naming the PDK's own file, byte-identical.
    assert info["path"] == str(pdk_map)
    assert info["resolution"] != "none"
    assert pdk_map.read_bytes() == before


def test_extraction_promotes_the_declared_pin_by_name(tmp_path):
    """The acceptance outcome: with the staged map the top-level pin is
    promoted under its DEF name. A pin *count* alone could pass by
    coincidence, so assert the name."""
    kwargs = _build_fixture(tmp_path)
    place_and_route._merge_def_to_gds(**kwargs)

    report = run_extract(
        kwargs["out_path"],
        "sky130",
        output=str(tmp_path / "out.spice"),
        top="top",
    )

    assert report["pin_count"] == 1
    assert "A" in {net["name"] for net in report["nets"]}


def test_extraction_names_nothing_without_the_staged_conductor(tmp_path, monkeypatch):
    """The regression control that must bite: with staging disabled (the
    pre-change behaviour) the same fixture promotes no pin, so the test above
    fails against the old code for the right reason."""
    monkeypatch.setattr(
        "klayout_tools.place_and_route_gds_merge._stage_pin_conductor_layer_map",
        lambda map_path, staged_path: {"status": "unchanged", "layers": []},
    )
    kwargs = _build_fixture(tmp_path)
    place_and_route._merge_def_to_gds(**kwargs)

    report = run_extract(
        kwargs["out_path"],
        "sky130",
        output=str(tmp_path / "out.spice"),
        top="top",
    )

    assert report["pin_count"] == 0


def test_no_layer_map_behaves_as_before(tmp_path):
    kwargs = _build_fixture(tmp_path, map_text=None)

    info = place_and_route._merge_def_to_gds(**kwargs)

    assert info["resolution"] == "none"
    assert info["path"] is None
    assert info["pin_conductor"]["status"] == "skipped"


def test_map_without_a_dual_purpose_layer_is_not_staged(tmp_path):
    kwargs = _build_fixture(
        tmp_path, map_text="met1 LEFPIN,NET,SPNET,PIN,VIA 68 20\nNAME met1/PIN 68 5\n"
    )

    info = place_and_route._merge_def_to_gds(**kwargs)

    assert info["pin_conductor"] == {"status": "unchanged", "layers": []}
