"""Tests for `klt lef-abstract` and the `klayout_tools.lef_abstract` library
(issue #438, Epic #393 Phase 2 Capability A).

Every unit test runs against a **fabricated** open_pdks-layout PDK install
(tech LEF + KLayout GDS<->LEF map file) built under `tmp_path` -- never a
real PDK, mirroring `tests/test_place_and_route.py`'s own fixture
convention. Layout fixtures are built programmatically with `klayout.db`,
mirroring `tests/test_socket_check.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools import pdk as pdk_module
from klayout_tools.cli import main
from klayout_tools.lef_abstract import (
    LefAbstractError,
    _polygon_to_um_shape,
    _resolve_layer_map,
    run_lef_abstract,
)
from klayout_tools.lef_header import read_lef_header

DBU_UM = 0.001

# GDS (layer, datatype) pairs used by the fixtures below, matching a
# simplified sky130-shaped layer map (see `_write_layer_map`).
LI1_NET = (67, 20)
MET1_NET = (68, 20)
# met1's dedicated pin/net-label datatype (per `_MAP_FILE_TEXT` below) --
# resolves to the same LEF layer name ("met1") as `MET1_NET` but is a
# *different* GDS datatype, mirroring gf180mcu's real `Metal1` PIN/drawing
# datatype split (issue #1614).
MET1_PIN = (68, 16)
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


def _make_pdk_install(
    root: Path,
    variant: str,
    cell_library: str = "sky130_fd_sc_hd",
    *,
    map_filename: str | None = None,
) -> None:
    """``map_filename`` (default ``"<variant>.map"``, sky130's own naming
    convention) lets a caller instead write the family-level filename a
    real gf180mcu install ships (``"gf180mcu.map"`` shared across
    ``gf180mcuC``/``gf180mcuD``, never a per-variant file -- issue #1029),
    to exercise :func:`klayout_tools.lef_abstract._resolve_layer_map`'s
    family fallback end to end."""
    variant_dir = root / variant
    (variant_dir / "libs.tech").mkdir(parents=True, exist_ok=True)

    techlef_dir = variant_dir / "libs.ref" / cell_library / "techlef"
    techlef_dir.mkdir(parents=True, exist_ok=True)
    (techlef_dir / f"{cell_library}__nom.tlef").write_text(
        _TECH_LEF_TEXT, encoding="utf-8"
    )

    klayout_tech_dir = variant_dir / "libs.tech" / "klayout" / "tech"
    klayout_tech_dir.mkdir(parents=True, exist_ok=True)
    (klayout_tech_dir / (map_filename or f"{variant}.map")).write_text(
        _MAP_FILE_TEXT, encoding="utf-8"
    )


def _box_um(x0: float, y0: float, x1: float, y1: float) -> kdb.Box:
    return kdb.Box(
        round(x0 / DBU_UM), round(y0 / DBU_UM), round(x1 / DBU_UM), round(y1 / DBU_UM)
    )


def _write_layout(layout: kdb.Layout, path: Path) -> str:
    layout.write(str(path))
    return str(path)


def _write_socket(descriptor: dict, path: Path) -> str:
    path.write_text(json.dumps(descriptor), encoding="utf-8")
    return str(path)


def _basic_layout() -> kdb.Layout:
    """A tiny analog block: a real drawn VOUT port on li1, a real drawn VDD
    port on met1, and a general met1 obstruction shape elsewhere -- mirrors
    the manually-verified example in this module's own docstring."""
    layout = kdb.Layout()
    layout.dbu = DBU_UM
    top = layout.create_cell("ANALOG_BLOCK")

    li1 = layout.layer(*LI1_NET)
    met1 = layout.layer(*MET1_NET)
    nwell = layout.layer(*NWELL)

    top.shapes(li1).insert(_box_um(2.0, 0.0, 3.0, 0.2))  # VOUT port
    top.shapes(met1).insert(_box_um(0.0, 2.0, 0.2, 3.0))  # VDD port
    top.shapes(met1).insert(_box_um(1.0, 1.0, 4.0, 1.5))  # general obstruction
    top.shapes(nwell).insert(_box_um(0.0, 0.0, 5.0, 5.0))  # non-routing: ignored

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
    **kwargs,
) -> dict:
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")
    monkeypatch.setenv("PDK_ROOT", str(install_root))

    layout_path = _write_layout(layout or _basic_layout(), tmp_path / "block.gds")
    socket_path = _write_socket(
        descriptor or _basic_descriptor(), tmp_path / "socket.json"
    )
    output_path = str(tmp_path / "out.lef")

    kwargs.setdefault("macro_name", "analog_block")
    kwargs.setdefault("cell_library", "sky130_fd_sc_hd")
    kwargs.setdefault("output_path", output_path)
    return run_lef_abstract(layout_path, socket_path, **kwargs)


# --------------------------------------------------------------------------- #
# happy path: MACRO/SIZE/PIN/OBS shape
# --------------------------------------------------------------------------- #


def test_basic_run_emits_macro_with_class_and_size(tmp_path, monkeypatch):
    report = _run(tmp_path, monkeypatch)

    assert report["schema_version"] == 1
    assert report["macro_name"] == "analog_block"
    assert report["class"] == "BLOCK"
    assert report["size_um"] == {"width": 5.0, "height": 5.0}
    assert Path(report["output"]).is_file()


def test_drawn_pin_geometry_used_when_metal_present(tmp_path, monkeypatch):
    report = _run(tmp_path, monkeypatch)

    pins = {pin["name"]: pin for pin in report["pins"]}
    assert pins["VOUT"]["geometry_source"] == "drawn"
    assert pins["VOUT"]["layer"] == "li1"
    assert pins["VOUT"]["rects_um"] == [[2.0, 0.0, 3.0, 0.2]]
    assert pins["VDD"]["geometry_source"] == "drawn"
    assert pins["VDD"]["layer"] == "met1"
    assert not report["warnings"]


def test_synthesized_pin_geometry_when_no_drawn_shape(tmp_path, monkeypatch):
    descriptor = _basic_descriptor(
        pins=[{"name": "VSS", "layer": list(MET1_NET), "x": 4.9, "y": 4.9}]
    )
    report = _run(tmp_path, monkeypatch, descriptor=descriptor)

    pin = report["pins"][0]
    assert pin["geometry_source"] == "synthesized"
    assert len(pin["rects_um"]) == 1
    x0, y0, x1, y1 = pin["rects_um"][0]
    assert x1 - x0 == pytest.approx(0.14, abs=1e-6)  # falls back to met1's tech WIDTH
    assert any("synthesized a placeholder" in w for w in report["warnings"])


def test_synthesized_pin_uses_declared_width_height(tmp_path, monkeypatch):
    descriptor = _basic_descriptor(
        pins=[
            {
                "name": "VSS",
                "layer": list(MET1_NET),
                "x": 4.5,
                "y": 4.5,
                "width_um": 1.0,
                "height_um": 0.5,
            }
        ]
    )
    report = _run(tmp_path, monkeypatch, descriptor=descriptor)

    x0, y0, x1, y1 = report["pins"][0]["rects_um"][0]
    assert x1 - x0 == pytest.approx(1.0, abs=1e-6)
    assert y1 - y0 == pytest.approx(0.5, abs=1e-6)


def test_drawn_pin_geometry_found_via_sibling_datatype_of_same_lef_layer(
    tmp_path, monkeypatch
):
    """Issue #1614: a pin declared on a PDK's dedicated pin/label datatype
    (e.g. gf180mcu's ``met1`` ``(68, 16)`` ``LEFPIN,PIN`` purpose) must still
    resolve ``geometry_source: "drawn"`` when real metal is drawn on a
    *different* datatype that maps to the same LEF layer name (``met1``'s
    ``(68, 20)`` ``NET,SPNET,PIN,VIA`` purpose) -- not just the exact
    declared datatype, which is where `klt socket-check`'s own text-label
    lookup requires the pin to be declared and where no polygon geometry
    exists in this fixture."""
    layout = kdb.Layout()
    layout.dbu = DBU_UM
    top = layout.create_cell("ANALOG_BLOCK")
    met1_net = layout.layer(*MET1_NET)
    # Real drawn metal lives only on the *drawing* datatype -- nothing is
    # drawn on the declared pin/label datatype (68, 16) itself.
    top.shapes(met1_net).insert(_box_um(0.0, 2.0, 0.2, 3.0))

    descriptor = _basic_descriptor(
        pins=[{"name": "VDD", "layer": list(MET1_PIN), "x": 0.1, "y": 2.5}]
    )
    report = _run(tmp_path, monkeypatch, layout=layout, descriptor=descriptor)

    pin = report["pins"][0]
    assert pin["geometry_source"] == "drawn"
    assert pin["layer"] == "met1"
    assert pin["rects_um"] == [[0.0, 2.0, 0.2, 3.0]]
    assert not report["warnings"]

    # `_resolve_obs`'s pin-subtraction logic still correctly removes this
    # now-correctly-detected drawn geometry from the OBS region -- the only
    # met1 shape in the layout was the pin's own port, so no OBS remains.
    met1_obs = [entry for entry in report["obs"] if entry["layer"] == "met1"]
    assert met1_obs == []


def test_synthesized_fallback_still_used_when_no_sibling_datatype_has_geometry(
    tmp_path, monkeypatch
):
    """A pin declared on a datatype that shares a LEF layer name with other
    datatypes (``met1``'s ``(68, 16)``/``(68, 20)`` split) still falls back
    to ``geometry_source: "synthesized"`` when genuinely no drawn geometry
    exists on *any* of them -- issue #1614's fix must not report false
    positives."""
    descriptor = _basic_descriptor(
        pins=[{"name": "VSS", "layer": list(MET1_PIN), "x": 4.9, "y": 4.9}]
    )
    report = _run(tmp_path, monkeypatch, descriptor=descriptor)

    pin = report["pins"][0]
    assert pin["geometry_source"] == "synthesized"
    assert any("synthesized a placeholder" in w for w in report["warnings"])
    assert any("sibling datatype" in w for w in report["warnings"])


def test_obs_excludes_declared_pin_geometry(tmp_path, monkeypatch):
    report = _run(tmp_path, monkeypatch)

    met1_obs = next(entry for entry in report["obs"] if entry["layer"] == "met1")
    # Only the general obstruction shape remains -- the VDD port (also on
    # met1) was subtracted back out.
    assert met1_obs["shape_count"] == 1


def test_obs_skips_non_routing_layers():
    pass  # covered implicitly: the nwell box in `_basic_layout` never appears


def test_non_routing_layer_shape_does_not_appear_in_obs(tmp_path, monkeypatch):
    report = _run(tmp_path, monkeypatch)
    assert all(entry["layer"] != "nwell" for entry in report["obs"])


# --------------------------------------------------------------------------- #
# non-rectangular OBS geometry (issue #1613)
# --------------------------------------------------------------------------- #


def _l_shaped_layout() -> kdb.Layout:
    """Two overlapping met1 boxes that merge into a single L-shaped (non-box)
    obstruction polygon -- the minimal reproduction from issue #1613, where
    formatting it crashed with ``AttributeError: 'SimplePolygon' object has
    no attribute 'each_point_hull'``."""
    layout = kdb.Layout()
    layout.dbu = DBU_UM
    top = layout.create_cell("ANALOG_BLOCK")

    met1 = layout.layer(*MET1_NET)
    top.shapes(met1).insert(_box_um(0.0, 0.0, 5.0, 1.0))
    top.shapes(met1).insert(_box_um(0.0, 0.0, 1.0, 5.0))

    return layout


def test_non_rectangular_obs_does_not_raise_and_emits_polygon(tmp_path, monkeypatch):
    """Regression for issue #1613: a merged, non-box OBS polygon used to
    raise ``AttributeError`` instead of emitting a LEF ``POLYGON``."""
    report = _run(
        tmp_path,
        monkeypatch,
        layout=_l_shaped_layout(),
        descriptor=_basic_descriptor(pins=[]),
    )

    met1_obs = next(entry for entry in report["obs"] if entry["layer"] == "met1")
    assert met1_obs["shape_count"] == 1

    text = Path(report["output"]).read_text(encoding="utf-8")
    polygon_lines = [
        line.strip() for line in text.splitlines() if line.strip().startswith("POLYGON")
    ]
    assert len(polygon_lines) == 1
    assert "RECT" not in text.split("  OBS")[1]
    # The L-shape's six corners, in the order `SimplePolygon.each_point()`
    # yields them (counter-clockwise from the origin).
    assert polygon_lines[0] == (
        "POLYGON "
        "0.0000 0.0000 "
        "0.0000 5.0000 "
        "1.0000 5.0000 "
        "1.0000 1.0000 "
        "5.0000 1.0000 "
        "5.0000 0.0000 ;"
    )


def test_polygon_shape_points_match_simple_polygon_each_point():
    """`_polygon_to_um_shape` must reproduce `each_point()`'s own sequence --
    no silent point-order or winding change (issue #1613)."""
    region = kdb.Region()
    region.insert(_box_um(0.0, 0.0, 5.0, 1.0))
    region.insert(_box_um(0.0, 0.0, 1.0, 5.0))
    region.merge()
    (polygon,) = list(region.each_merged())
    assert not polygon.is_box()

    shape = _polygon_to_um_shape(polygon, DBU_UM)
    expected = [
        [round(pt.x * DBU_UM, 6), round(pt.y * DBU_UM, 6)]
        for pt in polygon.to_simple_polygon().each_point()
    ]
    assert shape == {"kind": "polygon", "points_um": expected}


def test_box_shape_still_emits_rect():
    """The `is_box()` fast path is untouched by the issue #1613 fix."""
    region = kdb.Region()
    region.insert(_box_um(1.0, 2.0, 3.0, 4.0))
    region.merge()
    (polygon,) = list(region.each_merged())

    assert _polygon_to_um_shape(polygon, DBU_UM) == {
        "kind": "rect",
        "rect_um": [1.0, 2.0, 3.0, 4.0],
    }


# --------------------------------------------------------------------------- #
# pin direction/use classification
# --------------------------------------------------------------------------- #


def test_power_ground_heuristic_from_pin_name(tmp_path, monkeypatch):
    descriptor = _basic_descriptor(
        pins=[
            {"name": "VDD", "layer": list(MET1_NET), "x": 0.1, "y": 2.5},
            {"name": "VSS", "layer": list(MET1_NET), "x": 4.9, "y": 4.9},
            {"name": "VOUT", "layer": list(LI1_NET), "x": 2.5, "y": 0.1},
        ]
    )
    report = _run(tmp_path, monkeypatch, descriptor=descriptor)
    pins = {pin["name"]: pin for pin in report["pins"]}

    assert pins["VDD"]["use"] == "POWER"
    assert pins["VSS"]["use"] == "GROUND"
    assert pins["VOUT"]["use"] == "ANALOG"
    assert all(pin["direction"] == "INOUT" for pin in pins.values())


def test_explicit_direction_use_overrides_heuristic(tmp_path, monkeypatch):
    descriptor = _basic_descriptor(
        pins=[
            {
                "name": "VOUT",
                "layer": list(LI1_NET),
                "x": 2.5,
                "y": 0.1,
                "direction": "OUTPUT",
                "use": "SIGNAL",
            }
        ]
    )
    report = _run(tmp_path, monkeypatch, descriptor=descriptor)
    pin = report["pins"][0]
    assert pin["direction"] == "OUTPUT"
    assert pin["use"] == "SIGNAL"


# --------------------------------------------------------------------------- #
# LEF text round-trip through lef_header (self-consistency)
# --------------------------------------------------------------------------- #


def test_emitted_lef_round_trips_through_lef_header(tmp_path, monkeypatch):
    report = _run(tmp_path, monkeypatch)

    parsed = read_lef_header(report["output"])
    assert len(parsed["macros"]) == 1
    macro = parsed["macros"][0]
    assert macro["name"] == "analog_block"
    assert macro["class"] == "BLOCK"
    assert macro["width_um"] == pytest.approx(5.0)
    assert macro["height_um"] == pytest.approx(5.0)

    pins_by_name = {pin["name"]: pin for pin in macro["pins"]}
    assert pins_by_name["VOUT"]["direction"] == "INOUT"
    assert pins_by_name["VOUT"]["use"] == "ANALOG"
    assert pins_by_name["VDD"]["use"] == "POWER"


def test_emitted_lef_ends_with_matching_end_and_end_library(tmp_path, monkeypatch):
    report = _run(tmp_path, monkeypatch)
    text = Path(report["output"]).read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines[-1] == "END LIBRARY"
    assert lines[-2] == "END analog_block"


# --------------------------------------------------------------------------- #
# symmetry / macro class overrides
# --------------------------------------------------------------------------- #


def test_symmetry_and_class_overrides_are_echoed_and_written(tmp_path, monkeypatch):
    report = _run(tmp_path, monkeypatch, macro_class="BLOCK", symmetry=["X", "Y"])
    assert report["symmetry"] == ["X", "Y"]
    text = Path(report["output"]).read_text(encoding="utf-8")
    assert "SYMMETRY X Y ;" in text


# --------------------------------------------------------------------------- #
# error paths
# --------------------------------------------------------------------------- #


def test_missing_layout_file_raises(tmp_path, monkeypatch):
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    socket_path = _write_socket(_basic_descriptor(), tmp_path / "socket.json")

    with pytest.raises(LefAbstractError, match="file not found"):
        run_lef_abstract(
            str(tmp_path / "nope.gds"),
            socket_path,
            macro_name="m",
            cell_library="sky130_fd_sc_hd",
            output_path=str(tmp_path / "out.lef"),
        )


def test_multiple_top_cells_without_top_flag_raises(tmp_path, monkeypatch):
    layout = _basic_layout()
    layout.create_cell("OTHER_TOP")
    with pytest.raises(LefAbstractError, match="2 top cells"):
        _run(tmp_path, monkeypatch, layout=layout)


def test_top_flag_selects_cell(tmp_path, monkeypatch):
    layout = _basic_layout()
    layout.create_cell("OTHER_TOP")
    report = _run(tmp_path, monkeypatch, layout=layout, top="ANALOG_BLOCK")
    assert report["macro_name"] == "analog_block"


def test_unknown_top_cell_raises(tmp_path, monkeypatch):
    with pytest.raises(LefAbstractError, match="cell 'nope' not found"):
        _run(tmp_path, monkeypatch, top="nope")


def test_empty_macro_name_raises(tmp_path, monkeypatch):
    with pytest.raises(LefAbstractError, match="macro_name"):
        _run(tmp_path, monkeypatch, macro_name="")


def test_bad_socket_descriptor_raises_lef_abstract_error(tmp_path, monkeypatch):
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    layout_path = _write_layout(_basic_layout(), tmp_path / "block.gds")
    socket_path = tmp_path / "socket.json"
    socket_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(LefAbstractError, match="not valid JSON"):
        run_lef_abstract(
            layout_path,
            str(socket_path),
            macro_name="m",
            cell_library="sky130_fd_sc_hd",
            output_path=str(tmp_path / "out.lef"),
        )


def test_no_pdk_raises(tmp_path, monkeypatch):
    _isolate_pdk(monkeypatch, tmp_path)
    layout_path = _write_layout(_basic_layout(), tmp_path / "block.gds")
    socket_path = _write_socket(_basic_descriptor(), tmp_path / "socket.json")

    with pytest.raises(LefAbstractError):
        run_lef_abstract(
            layout_path,
            socket_path,
            macro_name="m",
            cell_library="sky130_fd_sc_hd",
            output_path=str(tmp_path / "out.lef"),
        )


def test_unknown_cell_library_raises(tmp_path, monkeypatch):
    with pytest.raises(LefAbstractError, match="tech LEF not found"):
        _run(tmp_path, monkeypatch, cell_library="does_not_exist")


def test_no_layer_map_raises(tmp_path, monkeypatch):
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")
    # Remove the klayout map file this test's own fixture wrote.
    map_path = (
        install_root / "sky130A" / "libs.tech" / "klayout" / "tech" / "sky130A.map"
    )
    map_path.unlink()
    monkeypatch.setenv("PDK_ROOT", str(install_root))

    layout_path = _write_layout(_basic_layout(), tmp_path / "block.gds")
    socket_path = _write_socket(_basic_descriptor(), tmp_path / "socket.json")

    with pytest.raises(LefAbstractError, match="layer map"):
        run_lef_abstract(
            layout_path,
            socket_path,
            macro_name="m",
            cell_library="sky130_fd_sc_hd",
            output_path=str(tmp_path / "out.lef"),
        )


# --------------------------------------------------------------------------- #
# `_resolve_layer_map` -- variant-exact vs. family-fallback vs. none found
# (issue #1029), this module's own duplicate of
# `place_and_route._resolve_layer_map`'s equivalent coverage.
# --------------------------------------------------------------------------- #


def test_resolve_layer_map_prefers_exact_variant_match(tmp_path, monkeypatch):
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")

    pdk_info = {
        "variant": "sky130A",
        "assets": {"klayout": str(install_root / "sky130A" / "libs.tech" / "klayout")},
    }

    path, resolution = _resolve_layer_map(pdk_info)

    assert path == str(
        install_root / "sky130A" / "libs.tech" / "klayout" / "tech" / "sky130A.map"
    )
    assert resolution == "exact"


def test_resolve_layer_map_falls_back_to_family_level_file(tmp_path, monkeypatch):
    """gf180mcu-shaped fixture: only the bare-family `gf180mcu.map` exists,
    never a `gf180mcuC.map` -- verified against a real open_pdks install for
    issue #1029."""
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "gf180mcuC", map_filename="gf180mcu.map")

    pdk_info = {
        "variant": "gf180mcuC",
        "assets": {
            "klayout": str(install_root / "gf180mcuC" / "libs.tech" / "klayout")
        },
    }

    path, resolution = _resolve_layer_map(pdk_info)

    assert path == str(
        install_root / "gf180mcuC" / "libs.tech" / "klayout" / "tech" / "gf180mcu.map"
    )
    assert resolution == "family"


def test_resolve_layer_map_returns_none_when_neither_file_exists(tmp_path):
    pdk_info = {
        "variant": "gf180mcuC",
        "assets": {"klayout": str(tmp_path / "no-such-klayout-dir")},
    }

    path, resolution = _resolve_layer_map(pdk_info)

    assert path is None
    assert resolution == "none"


def test_run_lef_abstract_succeeds_with_only_family_level_map_file(
    tmp_path, monkeypatch
):
    """End-to-end: `run_lef_abstract` against a gf180mcu-shaped install
    shipping only `gf180mcu.map` (no `gf180mcuC.map`) still resolves a
    layer map and succeeds, instead of raising "no ... layer map found"."""
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(
        install_root,
        "gf180mcuC",
        cell_library="gf180mcu_fd_sc_mcu9t5v0",
        map_filename="gf180mcu.map",
    )
    monkeypatch.setenv("PDK_ROOT", str(install_root))

    layout_path = _write_layout(_basic_layout(), tmp_path / "block.gds")
    socket_path = _write_socket(_basic_descriptor(), tmp_path / "socket.json")

    report = run_lef_abstract(
        layout_path,
        socket_path,
        macro_name="analog_block",
        cell_library="gf180mcu_fd_sc_mcu9t5v0",
        output_path=str(tmp_path / "out.lef"),
    )

    assert report["macro_name"] == "analog_block"


def test_pin_layer_with_no_lef_mapping_gets_no_geometry_and_a_warning(
    tmp_path, monkeypatch
):
    descriptor = _basic_descriptor(
        pins=[{"name": "WEIRD", "layer": [99, 99], "x": 1.0, "y": 1.0}]
    )
    report = _run(tmp_path, monkeypatch, descriptor=descriptor)
    pin = report["pins"][0]
    assert pin["geometry_source"] == "none"
    assert pin["layer"] is None
    assert any(
        "does not resolve to a known routing LEF layer" in w for w in report["warnings"]
    )
    # Issue #464: the same condition is also promoted to a top-level,
    # programmatically-checkable `unroutable_pins[]` echo -- not just a
    # `warnings[]` string a caller would otherwise have to grep.
    assert report["unroutable_pins"] == [{"name": "WEIRD", "layer": [99, 99]}]


def test_unroutable_pins_empty_when_every_pin_resolves(tmp_path, monkeypatch):
    """A run with no `geometry_source: "none"` pins reports an empty
    `unroutable_pins[]` -- not omitted, per this repo's own
    empty-list-not-null convention for echoed arrays."""
    report = _run(tmp_path, monkeypatch)
    assert all(pin["geometry_source"] != "none" for pin in report["pins"])
    assert report["unroutable_pins"] == []


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_success_json(tmp_path, monkeypatch, capsys):
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")
    monkeypatch.setenv("PDK_ROOT", str(install_root))

    layout_path = _write_layout(_basic_layout(), tmp_path / "block.gds")
    socket_path = _write_socket(_basic_descriptor(), tmp_path / "socket.json")
    output_path = tmp_path / "out.lef"

    exit_code = main(
        [
            "lef-abstract",
            layout_path,
            "--socket",
            socket_path,
            "--macro-name",
            "analog_block",
            "--cell-library",
            "sky130_fd_sc_hd",
            "--output",
            str(output_path),
            "--format",
            "json",
        ]
    )
    assert exit_code == 0
    assert output_path.is_file()
    payload = json.loads(capsys.readouterr().out)
    assert payload["macro_name"] == "analog_block"


def test_cli_text_format(tmp_path, monkeypatch, capsys):
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")
    monkeypatch.setenv("PDK_ROOT", str(install_root))

    layout_path = _write_layout(_basic_layout(), tmp_path / "block.gds")
    socket_path = _write_socket(_basic_descriptor(), tmp_path / "socket.json")

    exit_code = main(
        [
            "lef-abstract",
            layout_path,
            "--socket",
            socket_path,
            "--macro-name",
            "analog_block",
            "--cell-library",
            "sky130_fd_sc_hd",
            "-o",
            str(tmp_path / "out.lef"),
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "macro_name: analog_block" in out
    assert "pins (2):" in out


def test_cli_error_exit_code_and_message(tmp_path, monkeypatch, capsys):
    exit_code = main(
        [
            "lef-abstract",
            str(tmp_path / "nope.gds"),
            "--socket",
            str(tmp_path / "socket.json"),
            "--macro-name",
            "m",
            "--cell-library",
            "sky130_fd_sc_hd",
            "--format",
            "json",
        ]
    )
    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["error"]["command"] == "lef-abstract"


def test_default_output_path_is_macro_name_lef(tmp_path, monkeypatch):
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")
    monkeypatch.setenv("PDK_ROOT", str(install_root))

    layout_path = _write_layout(_basic_layout(), tmp_path / "block.gds")
    socket_path = _write_socket(_basic_descriptor(), tmp_path / "socket.json")

    monkeypatch.chdir(tmp_path)
    exit_code = main(
        [
            "lef-abstract",
            layout_path,
            "--socket",
            socket_path,
            "--macro-name",
            "analog_block",
            "--cell-library",
            "sky130_fd_sc_hd",
            "--format",
            "json",
        ]
    )
    assert exit_code == 0
    assert (tmp_path / "analog_block.lef").is_file()


# --------------------------------------------------------------------------- #
# structural self-check of the emitted LEF (issue #2769)
# --------------------------------------------------------------------------- #

_CLEAN_LEF = """\
VERSION 5.7 ;
BUSBITCHARS "[]" ;
DIVIDERCHAR "/" ;

MACRO blk
  CLASS BLOCK ;
  FOREIGN blk 0 0 ;
  ORIGIN 0 0 ;
  SIZE 5.0000 BY 5.0000 ;
  PIN A
    DIRECTION INOUT ;
    USE ANALOG ;
    PORT
      LAYER met1 ;
        RECT 0.0000 0.0000 1.0000 1.0000 ;
    END
  END A
  PIN B
    DIRECTION INOUT ;
    USE ANALOG ;
  END B
  OBS
    LAYER met1 ;
        RECT 2.0000 2.0000 3.0000 3.0000 ;
  END
END blk
END LIBRARY
"""

_EXPECTED = {"blk": ["A", "B"]}


def _codes(result: dict) -> list[str]:
    return [finding["code"] for finding in result["findings"]]


def _replace(old: str, new: str, text: str = _CLEAN_LEF) -> str:
    assert old in text, old
    return text.replace(old, new, 1)


def test_self_check_clean_fixture_passes():
    from klayout_tools.lef_abstract import check_lef_structure

    result = check_lef_structure(_CLEAN_LEF, _EXPECTED)
    assert result == {
        "status": "pass",
        "findings": [],
        "missing_macros": [],
        "unexpected_macros": [],
    }


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            "VERSION 5.7 ;\nMACRO blk\nEND blk\nEND LIBRARY\n",
            {"blk": []},
            id="zero-declared-pins",
        ),
        pytest.param(
            "VERSION 5.7 ;\nMACRO blk\n"
            + "".join(f"  PIN P{i}\n  END P{i}\n" for i in range(5))
            + "END blk\nEND LIBRARY\n",
            {"blk": [f"P{i}" for i in range(5)]},
            id="multiple-pins",
        ),
        pytest.param(
            "# header comment\n\tVERSION   5.7;   # trailing\n\n"
            "MACRO   blk   # the macro\n   PIN a_b[0]\n\t\tPORT\n"
            "LAYER met1 ;\nRECT 0 0 1 1;\n  END\n  END a_b[0]\n"
            "END blk\n  \nEND LIBRARY   # done\n# trailing comment\n",
            {"blk": ["a_b[0]"]},
            id="whitespace-and-comments",
        ),
        pytest.param(
            "VERSION 5.7 ;\nMACRO blk\n  PIN A\n    PORT\n      LAYER li1 ;\n"
            "      RECT 0 0 1 1 ;\n    END\n    PORT\n      LAYER met1 ;\n"
            "      RECT 0 0 1 1 ;\n    END\n  END A\n  OBS\n    LAYER li1 ;\n"
            "  END\n  OBS\n    LAYER met1 ;\n  END\nEND blk\nEND LIBRARY\n",
            {"blk": ["A"]},
            id="port-and-obs-nesting",
        ),
        pytest.param(
            "VERSION 5.7 ;\nMACRO Blk\n  PIN vdd\n  END vdd\nEND Blk\nEND LIBRARY\n",
            {"Blk": ["vdd"]},
            id="identifier-case-preserved",
        ),
    ],
)
def test_self_check_valid_variants_pass(text, expected):
    from klayout_tools.lef_abstract import check_lef_structure

    result = check_lef_structure(text, expected)
    assert result["status"] == "pass", result["findings"]


@pytest.mark.parametrize(
    ("text", "expected", "must_include"),
    [
        pytest.param(
            _replace("VERSION 5.7 ;\n", ""),
            _EXPECTED,
            {"missing_version"},
            id="missing-version",
        ),
        pytest.param(
            _replace("VERSION 5.7 ;", "VERSION ;"),
            _EXPECTED,
            {"missing_version"},
            id="version-without-value",
        ),
        pytest.param(
            _replace("  END A\n", ""),
            _EXPECTED,
            {"unclosed_block", "misplaced_block"},
            id="pin-end-missing",
        ),
        pytest.param(
            _replace("  END A\n", "  END B\n"),
            _EXPECTED,
            {"end_name_mismatch"},
            id="pin-end-wrong-name",
        ),
        pytest.param(
            _replace("END blk\n", "END other\n"),
            _EXPECTED,
            {"end_name_mismatch"},
            id="macro-end-wrong-name",
        ),
        pytest.param(
            _replace("  END A\n", "  END\n"),
            _EXPECTED,
            {"unnamed_end"},
            id="anonymous-end-for-pin",
        ),
        pytest.param(
            _replace("END blk\n", "END\n"),
            _EXPECTED,
            {"unnamed_end"},
            id="anonymous-end-for-macro",
        ),
        pytest.param(
            _replace("    END\n  END A\n", "  END A\n"),
            _EXPECTED,
            {"unclosed_block"},
            id="port-end-missing",
        ),
        pytest.param(
            _replace("END blk\nEND LIBRARY\n", "END blk\nEND blk\nEND LIBRARY\n"),
            _EXPECTED,
            {"orphan_end"},
            id="orphan-end",
        ),
        pytest.param(
            _replace("END LIBRARY\n", ""),
            _EXPECTED,
            {"missing_end_library"},
            id="missing-end-library",
        ),
        pytest.param(
            _replace("END blk\n", ""),
            _EXPECTED,
            {"unclosed_block"},
            id="end-library-with-open-macro",
        ),
        pytest.param(
            _CLEAN_LEF[: _CLEAN_LEF.index("  PIN B")],
            _EXPECTED,
            {"unclosed_block", "missing_end_library", "missing_pin"},
            id="truncated-eof",
        ),
        pytest.param(
            _CLEAN_LEF[: _CLEAN_LEF.index("RECT 0.0000") + 10],
            _EXPECTED,
            {"unterminated_statement", "unclosed_block", "missing_end_library"},
            id="truncated-mid-statement",
        ),
        pytest.param(
            _replace("    USE ANALOG ;\n  END B", "    USE ANALOG\n  END B"),
            _EXPECTED,
            {"unterminated_statement"},
            id="missing-semicolon-does-not-swallow-end",
        ),
        pytest.param(
            _CLEAN_LEF + "MACRO extra\nEND extra\n",
            _EXPECTED,
            {"content_after_end_library"},
            id="content-after-end-library",
        ),
        pytest.param(
            _replace("MACRO blk", "MACRO other").replace("END blk", "END other"),
            _EXPECTED,
            {"missing_macro", "unexpected_macro"},
            id="equal-count-wrong-macro-name",
        ),
        pytest.param(
            _replace(
                "END blk\nEND LIBRARY\n", "END blk\nMACRO more\nEND more\nEND LIBRARY\n"
            ),
            _EXPECTED,
            {"unexpected_macro"},
            id="unexpected-extra-macro",
        ),
        pytest.param(
            _replace(
                "END blk\nEND LIBRARY\n", "END blk\nMACRO blk\nEND blk\nEND LIBRARY\n"
            ),
            _EXPECTED,
            {"duplicate_macro"},
            id="duplicate-macro",
        ),
        pytest.param(
            _replace("  PIN B\n", "  PIN A\n").replace("  END B\n", "  END A\n"),
            _EXPECTED,
            {"duplicate_pin", "missing_pin"},
            id="duplicate-pin-equal-count",
        ),
        pytest.param(
            _replace("  PIN B\n", "  PIN C\n").replace("  END B\n", "  END C\n"),
            _EXPECTED,
            {"missing_pin", "unexpected_pin"},
            id="equal-count-wrong-pin-name",
        ),
        pytest.param(
            _replace("  PIN B\n    DIRECTION INOUT ;\n    USE ANALOG ;\n  END B\n", ""),
            _EXPECTED,
            {"missing_pin"},
            id="missing-declared-pin",
        ),
        pytest.param(
            _replace("MACRO blk\n", "MACRO blk\n  MACRO inner\n  END inner\n"),
            _EXPECTED,
            {"misplaced_block"},
            id="nested-macro",
        ),
        pytest.param(
            _replace("END LIBRARY\n", "PIN loose\nEND loose\nEND LIBRARY\n"),
            _EXPECTED,
            {"misplaced_block"},
            id="pin-outside-macro",
        ),
        pytest.param(
            _replace("  OBS\n", "  PIN Z\n  OBS\n").replace(
                "END blk\n", "  END Z\nEND blk\n"
            ),
            _EXPECTED,
            {"misplaced_block"},
            id="obs-inside-pin",
        ),
    ],
)
def test_self_check_fault_injection_fails(text, expected, must_include):
    from klayout_tools.lef_abstract import check_lef_structure

    result = check_lef_structure(text, expected)
    assert result["status"] == "fail"
    assert must_include <= set(_codes(result)), result["findings"]
    for finding in result["findings"]:
        assert set(finding) == {"code", "message", "line", "macro", "pin"}


def test_self_check_missing_pin_identifies_macro():
    from klayout_tools.lef_abstract import check_lef_structure

    result = check_lef_structure(_CLEAN_LEF, {"blk": ["A", "B", "C"]})
    (finding,) = result["findings"]
    assert finding["code"] == "missing_pin"
    assert finding["macro"] == "blk"
    assert finding["pin"] == "C"


def test_self_check_pin_in_wrong_macro_is_caught():
    from klayout_tools.lef_abstract import check_lef_structure

    text = (
        "VERSION 5.7 ;\n"
        "MACRO m1\n  PIN Y\n  END Y\nEND m1\n"
        "MACRO m2\n  PIN X\n  END X\nEND m2\n"
        "END LIBRARY\n"
    )
    result = check_lef_structure(text, {"m1": ["X"], "m2": ["Y"]})
    assert result["status"] == "fail"
    pairs = {(f["code"], f["macro"], f["pin"]) for f in result["findings"]}
    assert ("missing_pin", "m1", "X") in pairs
    assert ("missing_pin", "m2", "Y") in pairs
    assert ("unexpected_pin", "m1", "Y") in pairs
    assert ("unexpected_pin", "m2", "X") in pairs


def test_self_check_missing_and_unexpected_macros_are_sorted():
    from klayout_tools.lef_abstract import check_lef_structure

    text = "VERSION 5.7 ;\nMACRO zz\nEND zz\nMACRO aa\nEND aa\nEND LIBRARY\n"
    result = check_lef_structure(text, {"q": [], "b": []})
    assert result["missing_macros"] == ["b", "q"]
    assert result["unexpected_macros"] == ["aa", "zz"]


def test_self_check_case_mismatch_is_not_a_match():
    from klayout_tools.lef_abstract import check_lef_structure

    result = check_lef_structure(_CLEAN_LEF, {"BLK": ["A", "B"]})
    assert result["missing_macros"] == ["BLK"]
    assert result["unexpected_macros"] == ["blk"]


def test_self_check_reports_independent_faults_together():
    """One fault must not conceal another: VERSION missing, a wrong pin END
    and a missing pin all show up in the same report."""
    from klayout_tools.lef_abstract import check_lef_structure

    text = _replace("VERSION 5.7 ;\n", "")
    text = _replace("  END A\n", "  END Q\n", text)
    result = check_lef_structure(text, {"blk": ["A", "B", "C"]})
    codes = set(_codes(result))
    assert {"missing_version", "end_name_mismatch", "missing_pin"} <= codes


def test_self_check_finding_lines_point_at_defect():
    from klayout_tools.lef_abstract import check_lef_structure

    text = _replace("  END A\n", "  END B\n")
    result = check_lef_structure(text, _EXPECTED)
    mismatch = next(f for f in result["findings"] if f["code"] == "end_name_mismatch")
    expected_line = text.splitlines().index("  END B") + 1
    assert mismatch["line"] == expected_line
    assert mismatch["pin"] == "A"
    assert mismatch["macro"] == "blk"


# -- library integration -----------------------------------------------------


def test_run_reports_self_check_pass_on_clean_output(tmp_path, monkeypatch):
    report = _run(tmp_path, monkeypatch)
    assert report["self_check"] == {
        "status": "pass",
        "findings": [],
        "missing_macros": [],
        "unexpected_macros": [],
    }


def test_synthesized_and_portless_pins_pass_self_check(tmp_path, monkeypatch):
    descriptor = _basic_descriptor(
        pins=[
            {"name": "VSS", "layer": list(MET1_NET), "x": 4.9, "y": 4.9},
            {"name": "WEIRD", "layer": [99, 99], "x": 1.0, "y": 1.0},
        ]
    )
    report = _run(tmp_path, monkeypatch, descriptor=descriptor)
    sources = {pin["name"]: pin["geometry_source"] for pin in report["pins"]}
    assert sources == {"VSS": "synthesized", "WEIRD": "none"}
    assert report["unroutable_pins"] == [{"name": "WEIRD", "layer": [99, 99]}]
    assert len(report["warnings"]) == 2
    assert report["self_check"]["status"] == "pass"


def test_non_rectangular_obs_passes_self_check(tmp_path, monkeypatch):
    report = _run(
        tmp_path,
        monkeypatch,
        layout=_l_shaped_layout(),
        descriptor=_basic_descriptor(pins=[]),
    )
    assert report["self_check"]["status"] == "pass"


def _corrupting_writer(monkeypatch, mutate):
    """Wrap the real `_write_lef` so the on-disk file is corrupted after a
    normal write -- the in-memory pin metadata stays untouched."""
    from klayout_tools import lef_abstract

    real_write = lef_abstract._write_lef

    def write(**kwargs):
        real_write(**kwargs)
        path = Path(kwargs["output_path"])
        path.write_text(mutate(path.read_text(encoding="utf-8")), encoding="utf-8")

    monkeypatch.setattr(lef_abstract, "_write_lef", write)


def _drop_pin(name: str):
    def mutate(text: str) -> str:
        lines = text.splitlines(keepends=True)
        start = lines.index(f"  PIN {name}\n")
        end = lines.index(f"  END {name}\n")
        return "".join(lines[:start] + lines[end + 1 :])

    return mutate


def test_run_self_check_catches_on_disk_defect(tmp_path, monkeypatch):
    _corrupting_writer(monkeypatch, _drop_pin("VOUT"))
    report = _run(tmp_path, monkeypatch)

    # In-memory metadata still claims both pins...
    assert {pin["name"] for pin in report["pins"]} == {"VDD", "VOUT"}
    # ...but the check read the file and found the dropped one.
    check = report["self_check"]
    assert check["status"] == "fail"
    assert [(f["code"], f["macro"], f["pin"]) for f in check["findings"]] == [
        ("missing_pin", "analog_block", "VOUT")
    ]
    assert Path(report["output"]).is_file()  # left on disk for diagnosis


def test_run_self_check_catches_truncation(tmp_path, monkeypatch):
    _corrupting_writer(monkeypatch, lambda text: text[: len(text) // 2])
    report = _run(tmp_path, monkeypatch)
    codes = {f["code"] for f in report["self_check"]["findings"]}
    assert report["self_check"]["status"] == "fail"
    assert {"unclosed_block", "missing_end_library"} <= codes


def test_run_readback_failure_raises_lef_abstract_error(tmp_path, monkeypatch):
    from klayout_tools import lef_abstract

    real_write = lef_abstract._write_lef

    def write(**kwargs):
        real_write(**kwargs)
        Path(kwargs["output_path"]).unlink()

    monkeypatch.setattr(lef_abstract, "_write_lef", write)
    with pytest.raises(LefAbstractError, match="could not read back"):
        _run(tmp_path, monkeypatch)


# -- CLI contract --------------------------------------------------------------


def _cli_args(tmp_path: Path, monkeypatch, *extra: str) -> list[str]:
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    layout_path = _write_layout(_basic_layout(), tmp_path / "block.gds")
    socket_path = _write_socket(_basic_descriptor(), tmp_path / "socket.json")
    return [
        "lef-abstract",
        layout_path,
        "--socket",
        socket_path,
        "--macro-name",
        "analog_block",
        "--cell-library",
        "sky130_fd_sc_hd",
        "-o",
        str(tmp_path / "out.lef"),
        *extra,
    ]


def test_cli_self_check_pass_json_exit_0(tmp_path, monkeypatch, capsys):
    exit_code = main(_cli_args(tmp_path, monkeypatch, "--format", "json"))
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["self_check"]["status"] == "pass"


def test_cli_self_check_pass_text(tmp_path, monkeypatch, capsys):
    exit_code = main(_cli_args(tmp_path, monkeypatch))
    assert exit_code == 0
    assert "self_check: pass" in capsys.readouterr().out


def test_cli_self_check_fail_json_exit_3(tmp_path, monkeypatch, capsys):
    _corrupting_writer(monkeypatch, _drop_pin("VOUT"))
    exit_code = main(_cli_args(tmp_path, monkeypatch, "--format", "json"))
    assert exit_code == 3
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["self_check"]["status"] == "fail"
    assert payload["self_check"]["findings"][0]["code"] == "missing_pin"
    assert payload["macro_name"] == "analog_block"  # regular report fields intact
    assert (tmp_path / "out.lef").is_file()


def test_cli_self_check_fail_text_exit_3(tmp_path, monkeypatch, capsys):
    _corrupting_writer(monkeypatch, lambda text: text.replace("VERSION 5.7 ;\n", ""))
    exit_code = main(_cli_args(tmp_path, monkeypatch))
    assert exit_code == 3
    out = capsys.readouterr().out
    assert "self_check: FAIL (1 finding(s))" in out
    assert "[missing_version]" in out


def test_cli_readback_failure_is_error_envelope_exit_1(tmp_path, monkeypatch, capsys):
    from klayout_tools import lef_abstract

    real_write = lef_abstract._write_lef

    def write(**kwargs):
        real_write(**kwargs)
        Path(kwargs["output_path"]).unlink()

    monkeypatch.setattr(lef_abstract, "_write_lef", write)
    exit_code = main(_cli_args(tmp_path, monkeypatch, "--format", "json"))
    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["error"]["command"] == "lef-abstract"
    assert "could not read back" in payload["error"]["message"]


# --------------------------------------------------------------------------- #
# power_pins verdict + T1 item 11 port accessibility end to end (issue #2839)
# --------------------------------------------------------------------------- #


def _supply_block(
    *,
    vdd_box: tuple[float, float, float, float] = (0.0, 2.0, 0.2, 3.0),
    vdd_layer: tuple[int, int] = MET1_NET,
    draw_vss: bool = True,
) -> kdb.Layout:
    """A 5x5 um block with a VDD port (left edge by default) and a VSS port
    on the right edge, each a drawn met1 box labelled at its centre -- so
    `klt socket-check` (which needs the labels) and `klt lef-abstract`
    (which needs the metal) can both run on the same stream."""
    layout = kdb.Layout()
    layout.dbu = DBU_UM
    top = layout.create_cell("ANALOG_BLOCK")
    ports = [("VDD", vdd_box, vdd_layer)]
    vss_box = (4.8, 2.0, 5.0, 3.0)
    ports.append(("VSS", vss_box, MET1_NET))
    for name, (x0, y0, x1, y1), layer in ports:
        index = layout.layer(*layer)
        if name != "VSS" or draw_vss:
            top.shapes(index).insert(_box_um(x0, y0, x1, y1))
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        top.shapes(index).insert(
            kdb.Text(
                name, kdb.Trans(kdb.Vector(round(cx / DBU_UM), round(cy / DBU_UM)))
            )
        )
    return layout


def _supply_socket(
    *,
    vdd_box: tuple[float, float, float, float] = (0.0, 2.0, 0.2, 3.0),
    vdd_layer: tuple[int, int] = MET1_NET,
    vdd_use: str = "POWER",
    with_vss: bool = True,
) -> dict:
    pins = [
        {
            "name": "VDD",
            "layer": list(vdd_layer),
            "x": (vdd_box[0] + vdd_box[2]) / 2,
            "y": (vdd_box[1] + vdd_box[3]) / 2,
            "use": vdd_use,
            "direction": "INOUT",
        }
    ]
    if with_vss:
        pins.append(
            {
                "name": "VSS",
                "layer": list(MET1_NET),
                "x": 4.9,
                "y": 2.5,
                "use": "GROUND",
                "direction": "INOUT",
            }
        )
    return _basic_descriptor(pins=pins)


def test_power_pins_pass_for_drawn_power_and_ground(tmp_path, monkeypatch):
    report = _run(
        tmp_path, monkeypatch, layout=_supply_block(), descriptor=_supply_socket()
    )

    assert report["power_pins"]["status"] == "pass", report["power_pins"]
    assert report["power_pins"]["pins"] == [
        {
            "name": "VDD",
            "use": "POWER",
            "use_read_back": "POWER",
            "layer": "met1",
            "geometry_source": "drawn",
        },
        {
            "name": "VSS",
            "use": "GROUND",
            "use_read_back": "GROUND",
            "layer": "met1",
            "geometry_source": "drawn",
        },
    ]
    # Additive per-pin echo of the descriptor's declared GDS layer.
    assert {pin["name"]: pin["gds_layer"] for pin in report["pins"]} == {
        "VDD": [68, 20],
        "VSS": [68, 20],
    }


def test_power_pins_fail_without_ground_and_for_synthesized_port(tmp_path, monkeypatch):
    no_ground = _run(
        tmp_path,
        monkeypatch,
        layout=_supply_block(),
        descriptor=_supply_socket(with_vss=False),
    )
    assert [f["code"] for f in no_ground["power_pins"]["findings"]] == ["no_ground_pin"]

    synthesized = _run(
        tmp_path,
        monkeypatch,
        layout=_supply_block(draw_vss=False),
        descriptor=_supply_socket(),
    )
    findings = synthesized["power_pins"]["findings"]
    assert [(f["code"], f["pin"]) for f in findings] == [("port_not_drawn", "VSS")]
    assert synthesized["power_pins"]["status"] == "fail"
    # The structural self-check is unaffected -- a synthesized PORT is not a
    # structural defect.
    assert synthesized["self_check"]["status"] == "pass"


def test_read_back_pin_uses_reads_the_written_use():
    from klayout_tools.lef_abstract import _read_back_pin_uses, check_power_pins

    text = (
        "VERSION 5.7 ;\nMACRO m\n  PIN VDD\n    DIRECTION INOUT ;\n    USE SIGNAL ;\n"
        "  END VDD\n  PIN VSS\n    USE GROUND ;\n  END VSS\nEND m\nEND LIBRARY\n"
    )
    assert _read_back_pin_uses(text) == {"VDD": "SIGNAL", "VSS": "GROUND"}
    verdict = check_power_pins(
        [
            {
                "name": "VDD",
                "use": "POWER",
                "lef_layer": "met1",
                "geometry_source": "drawn",
            },
            {
                "name": "VSS",
                "use": "GROUND",
                "lef_layer": "met1",
                "geometry_source": "drawn",
            },
        ],
        text,
    )
    assert [(f["code"], f["pin"]) for f in verdict["findings"]] == [
        ("use_not_written", "VDD")
    ]


def test_cli_require_power_pins_exit_code(tmp_path, monkeypatch, capsys):
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    layout_path = _write_layout(_supply_block(draw_vss=False), tmp_path / "block.gds")
    socket_path = _write_socket(_supply_socket(), tmp_path / "socket.json")
    argv = [
        "lef-abstract",
        layout_path,
        "--socket",
        socket_path,
        "--macro-name",
        "analog_block",
        "--cell-library",
        "sky130_fd_sc_hd",
        "--output",
        str(tmp_path / "out.lef"),
        "--format",
        "json",
    ]

    assert main(argv) == 0
    capsys.readouterr()
    assert main([*argv, "--require-power-pins"]) == 3
    assert json.loads(capsys.readouterr().out)["power_pins"]["status"] == "fail"


def _real_port_evidence(tmp_path, monkeypatch, *, layout, descriptor):
    """Real `klt socket-check` + `klt lef-abstract` envelopes for one block,
    shaped as the resolutions `klt signoff`'s item 11 grades."""
    from klayout_tools.socket_check import run_socket_check

    lef_report = _run(tmp_path, monkeypatch, layout=layout, descriptor=descriptor)
    socket_report = run_socket_check(
        str(tmp_path / "block.gds"), str(tmp_path / "socket.json")
    )
    return {"envelope": socket_report}, {"envelope": lef_report}


@pytest.mark.parametrize(
    ("layout_kwargs", "socket_kwargs", "expected"),
    [
        # Both supplies on the boundary, on a routing layer: met.
        ({}, {}, None),
        # VDD moved to the block interior.
        (
            {"vdd_box": (2.0, 2.0, 2.2, 3.0)},
            {"vdd_box": (2.0, 2.0, 2.2, 3.0)},
            "power_port_interior_only",
        ),
        # VDD on nwell: drawn and on the edge, but no LEF routing layer.
        (
            {"vdd_layer": NWELL},
            {"vdd_layer": NWELL},
            "power_port_layer_disallowed",
        ),
        # Descriptor declares VDD as a signal: the LEF says USE SIGNAL, so
        # no supply port of that name exists on the GDS side either.
        ({}, {"vdd_use": "SIGNAL"}, "no_edge_power_port"),
    ],
)
def test_item_11_port_accessibility_end_to_end(
    tmp_path, monkeypatch, layout_kwargs, socket_kwargs, expected
):
    """Issue #2839: item 11's port-accessibility half graded from envelopes
    the real verbs produced, not hand-written fixtures."""
    from klayout_tools.signoff_power_delivery import (
        _grade_power_ports,
        _read_power_port_policy,
    )

    socket, lef = _real_port_evidence(
        tmp_path,
        monkeypatch,
        layout=_supply_block(**layout_kwargs),
        descriptor=_supply_socket(**socket_kwargs),
    )
    reason, block = _grade_power_ports(
        socket, lef, ["VDD", "VSS"], _read_power_port_policy({"required": True})
    )

    assert reason == expected, block
    if expected is None:
        assert [entry["net"] for entry in block["ports"]] == ["VDD", "VSS"]
        assert block["ports"][0]["port"]["accessible_edges"] == ["left"]
        assert block["ports"][0]["lef_pin"] == {
            "name": "VDD",
            "use": "POWER",
            "layer": "met1",
            "geometry_source": "drawn",
        }
