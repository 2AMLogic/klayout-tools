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
    _resolve_layer_map,
    run_lef_abstract,
)
from klayout_tools.lef_header import read_lef_header

DBU_UM = 0.001

# GDS (layer, datatype) pairs used by the fixtures below, matching a
# simplified sky130-shaped layer map (see `_write_layer_map`).
LI1_NET = (67, 20)
MET1_NET = (68, 20)
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
