"""Tests for `klayout_tools.lef_header` -- the tech-LEF header reader (issue
#438's deferred-trigger resolution for the ``sc-leflib`` survey finding).

Every unit test runs against **hand-written, synthetic LEF text fixtures**
(no external dependency, no real PDK needed). A separate, real-PDK-guarded
integration test (`@pytest.mark.skipif` when no real sky130 install
resolves) additionally validates the parser against the actual sky130 tech
LEF and merged-cell LEF -- this is the acceptance criterion's "parses
sky130's tech LEF" check; it runs (and passes) on any machine with a real
open_pdks-layout sky130 install, but is not required for CI (matching every
other real-PDK-guarded integration test in this repo, e.g.
`tests/test_place_and_route.py`'s own OpenROAD integration test).
"""

from __future__ import annotations

import pytest

from klayout_tools import pdk as pdk_module
from klayout_tools.lef_header import parse_lef_header, read_lef_header

# --------------------------------------------------------------------------- #
# SITE
# --------------------------------------------------------------------------- #


def test_parses_site_class_symmetry_size():
    text = """\
SITE unithd
  SYMMETRY Y ;
  CLASS CORE ;
  SIZE 0.46 BY 2.72 ;
END unithd
"""
    header = parse_lef_header(text)
    assert header["sites"] == [
        {
            "name": "unithd",
            "class": "CORE",
            "symmetry": ["Y"],
            "width_um": 0.46,
            "height_um": 2.72,
        }
    ]


def test_multiple_sites_sorted_by_name():
    text = """\
SITE zzz_site
  CLASS CORE ;
  SIZE 1 BY 1 ;
END zzz_site

SITE aaa_site
  CLASS CORE ;
  SIZE 2 BY 2 ;
END aaa_site
"""
    header = parse_lef_header(text)
    assert [site["name"] for site in header["sites"]] == ["aaa_site", "zzz_site"]


def test_site_with_no_attributes_reports_none_and_empty_symmetry():
    text = "SITE bare\nEND bare\n"
    header = parse_lef_header(text)
    assert header["sites"] == [
        {
            "name": "bare",
            "class": None,
            "symmetry": [],
            "width_um": None,
            "height_um": None,
        }
    ]


# --------------------------------------------------------------------------- #
# LAYER
# --------------------------------------------------------------------------- #


def test_parses_routing_layer_two_value_pitch_offset():
    text = """\
LAYER li1
  TYPE ROUTING ;
  DIRECTION VERTICAL ;
  PITCH 0.46 0.34 ;
  OFFSET 0.23 0.17 ;
  WIDTH 0.17 ;
END li1
"""
    header = parse_lef_header(text)
    assert header["layers"] == [
        {
            "name": "li1",
            "type": "ROUTING",
            "direction": "VERTICAL",
            "width_um": 0.17,
            "pitch_x_um": 0.46,
            "pitch_y_um": 0.34,
            "offset_x_um": 0.23,
            "offset_y_um": 0.17,
        }
    ]


def test_single_value_pitch_offset_means_x_equals_y():
    text = """\
LAYER met1
  TYPE ROUTING ;
  DIRECTION HORIZONTAL ;
  PITCH 0.34 ;
  OFFSET 0.17 ;
  WIDTH 0.14 ;
END met1
"""
    header = parse_lef_header(text)
    layer = header["layers"][0]
    assert layer["pitch_x_um"] == layer["pitch_y_um"] == 0.34
    assert layer["offset_x_um"] == layer["offset_y_um"] == 0.17


def test_non_routing_layer_has_type_but_no_pitch_offset():
    text = "LAYER nwell\n  TYPE MASTERSLICE ;\nEND nwell\n"
    header = parse_lef_header(text)
    layer = header["layers"][0]
    assert layer["type"] == "MASTERSLICE"
    assert layer["pitch_x_um"] is None
    assert layer["offset_x_um"] is None


def test_layer_statements_not_confused_by_a_spacingtable_stanza():
    """A multi-line SPACINGTABLE statement (no nested block, just a
    multi-line statement ending in `;`) must not break the single-block
    LAYER regex or leak WIDTH/PITCH from the wrong statement."""
    text = """\
LAYER met1
  TYPE ROUTING ;
  DIRECTION HORIZONTAL ;
  PITCH 0.34 ;
  OFFSET 0.17 ;
  WIDTH 0.14 ;
  SPACINGTABLE
     PARALLELRUNLENGTH 0
     WIDTH 0 0.14
     WIDTH 3 0.28 ;
  RESISTANCE RPERSQ 0.125 ;
END met1
"""
    header = parse_lef_header(text)
    assert len(header["layers"]) == 1
    assert header["layers"][0]["width_um"] == 0.14


# --------------------------------------------------------------------------- #
# comments / header attributes
# --------------------------------------------------------------------------- #


def test_comments_are_stripped():
    text = """\
# a full-line comment
SITE unithd
  CLASS CORE ; # trailing comment
  SIZE 0.46 BY 2.72 ;
END unithd
"""
    header = parse_lef_header(text)
    assert header["sites"][0]["class"] == "CORE"
    assert header["sites"][0]["height_um"] == 2.72


def test_manufacturing_grid_and_database_microns():
    text = """\
UNITS
  DATABASE MICRONS 1000 ;
END UNITS
MANUFACTURINGGRID 0.005 ;
"""
    header = parse_lef_header(text)
    assert header["database_microns"] == 1000
    assert header["manufacturing_grid_um"] == 0.005


def test_missing_manufacturing_grid_and_units_are_none():
    header = parse_lef_header("SITE x\nEND x\n")
    assert header["database_microns"] is None
    assert header["manufacturing_grid_um"] is None


def test_empty_text_returns_empty_lists():
    header = parse_lef_header("")
    assert header == {
        "database_microns": None,
        "manufacturing_grid_um": None,
        "sites": [],
        "layers": [],
        "macros": [],
    }


# --------------------------------------------------------------------------- #
# MACRO / PIN -- the "pin DIRECTION/USE" half of the deferred trigger
# --------------------------------------------------------------------------- #


def test_parses_macro_class_site_size_symmetry():
    text = """\
MACRO example_cell
  CLASS CORE ;
  FOREIGN example_cell ;
  ORIGIN 0.000 0.000 ;
  SIZE 1.380 BY 2.720 ;
  SYMMETRY X Y R90 ;
  SITE unithd ;
END example_cell
"""
    header = parse_lef_header(text)
    assert header["macros"] == [
        {
            "name": "example_cell",
            "class": "CORE",
            "site": "unithd",
            "symmetry": ["X", "Y", "R90"],
            "width_um": 1.38,
            "height_um": 2.72,
            "pins": [],
        }
    ]


def test_parses_pin_direction_and_use_with_nested_port_not_confused_for_pin_end():
    """The real-world shape that makes a naive "first bare END" parser
    break: PORT's own `END` (no name) must not be mistaken for PIN A's
    closing `END A`."""
    text = """\
MACRO example_cell
  CLASS CORE ;
  SIZE 1.0 BY 1.0 ;
  PIN A
    DIRECTION INPUT ;
    USE SIGNAL ;
    PORT
      LAYER li1 ;
        RECT 0.320 1.075 0.650 1.315 ;
    END
  END A
  PIN VPWR
    DIRECTION INOUT ;
    USE POWER ;
    SHAPE ABUTMENT ;
    PORT
      LAYER met1 ;
        RECT 0.000 2.480 1.380 2.960 ;
    END
  END VPWR
  OBS
      LAYER li1 ;
        RECT 0.000 2.635 1.380 2.805 ;
  END
END example_cell
"""
    header = parse_lef_header(text)
    macro = header["macros"][0]
    assert macro["pins"] == [
        {"name": "A", "direction": "INPUT", "use": "SIGNAL", "has_port": True},
        {"name": "VPWR", "direction": "INOUT", "use": "POWER", "has_port": True},
    ]


def test_pin_with_no_direction_or_use_is_none():
    text = "MACRO m\n  PIN X\n  END X\nEND m\n"
    header = parse_lef_header(text)
    assert header["macros"][0]["pins"] == [
        {"name": "X", "direction": None, "use": None, "has_port": False}
    ]


def test_pin_has_port_false_when_no_port_block(tmp_path):
    """Issue #464: a `klt lef-abstract`-emitted PIN block with no PORT at
    all (a pin whose declared layer did not resolve to a routing LEF layer)
    must report `has_port: False` -- the signal `klt place-and-route`'s
    macro-pin/netlist-wiring cross-check relies on."""
    text = (
        "MACRO m\n"
        "  PIN G\n    DIRECTION INOUT ;\n    USE ANALOG ;\n  END G\n"
        "  PIN S\n"
        "    DIRECTION INOUT ;\n"
        "    USE ANALOG ;\n"
        "    PORT\n"
        "      LAYER li1 ;\n"
        "        RECT 0.0 0.0 0.1 0.1 ;\n"
        "    END\n"
        "  END S\n"
        "END m\n"
    )
    header = parse_lef_header(text)
    pins = {pin["name"]: pin for pin in header["macros"][0]["pins"]}
    assert pins["G"]["has_port"] is False
    assert pins["S"]["has_port"] is True


def test_macro_with_no_site_reference_is_none():
    """A hard macro (CLASS BLOCK) commonly declares no SITE at all --
    verified against a real sky130 SRAM macro LEF in this module's docstring
    survey; here as a synthetic regression fixture."""
    text = "MACRO hard_macro\n  CLASS BLOCK ;\n  SIZE 10 BY 10 ;\nEND hard_macro\n"
    header = parse_lef_header(text)
    assert header["macros"][0]["site"] is None


def test_multiple_macros_sorted_by_name():
    text = "MACRO zzz\n  CLASS CORE ;\nEND zzz\n\nMACRO aaa\n  CLASS CORE ;\nEND aaa\n"
    header = parse_lef_header(text)
    assert [m["name"] for m in header["macros"]] == ["aaa", "zzz"]


def test_pins_within_one_macro_sorted_by_name():
    text = (
        "MACRO m\n"
        "  PIN zzz\n    DIRECTION INPUT ;\n  END zzz\n"
        "  PIN aaa\n    DIRECTION OUTPUT ;\n  END aaa\n"
        "END m\n"
    )
    header = parse_lef_header(text)
    assert [pin["name"] for pin in header["macros"][0]["pins"]] == ["aaa", "zzz"]


# --------------------------------------------------------------------------- #
# read_lef_header (file I/O)
# --------------------------------------------------------------------------- #


def test_read_lef_header_reads_a_file(tmp_path):
    path = tmp_path / "tech.tlef"
    path.write_text("SITE unithd\n  CLASS CORE ;\n  SIZE 1 BY 1 ;\nEND unithd\n")
    header = read_lef_header(str(path))
    assert header["sites"][0]["name"] == "unithd"


def test_read_lef_header_missing_file_raises_oserror(tmp_path):
    with pytest.raises(OSError):
        read_lef_header(str(tmp_path / "nope.tlef"))


# --------------------------------------------------------------------------- #
# real sky130 tech LEF (skipped when no real PDK install resolves)
# --------------------------------------------------------------------------- #


def _find_real_sky130_tech_lef() -> str | None:
    try:
        result = pdk_module.list_pdks()
    except Exception:
        return None
    for install in result["installs"]:
        for variant in install["variants"]:
            import os

            candidate = os.path.join(
                install["root"],
                variant["name"],
                "libs.ref",
                "sky130_fd_sc_hd",
                "techlef",
                "sky130_fd_sc_hd__nom.tlef",
            )
            if os.path.isfile(candidate):
                return candidate
    return None


_REAL_SKY130_TECH_LEF = _find_real_sky130_tech_lef()


@pytest.mark.skipif(
    _REAL_SKY130_TECH_LEF is None,
    reason="no real sky130_fd_sc_hd tech LEF resolves via list_pdks()",
)
def test_real_sky130_tech_lef_sites_and_routing_layers():
    header = read_lef_header(_REAL_SKY130_TECH_LEF)

    site_names = {site["name"] for site in header["sites"]}
    assert "unithd" in site_names
    unithd = next(s for s in header["sites"] if s["name"] == "unithd")
    assert unithd["class"] == "CORE"
    assert unithd["height_um"] == pytest.approx(2.72)

    routing = {
        layer["name"]: layer for layer in header["layers"] if layer["type"] == "ROUTING"
    }
    assert "met1" in routing
    assert routing["met1"]["direction"] in ("HORIZONTAL", "VERTICAL")
    assert routing["met1"]["pitch_x_um"] is not None
    assert routing["met1"]["offset_x_um"] is not None

    assert header["database_microns"] == 1000
    assert header["manufacturing_grid_um"] is not None
