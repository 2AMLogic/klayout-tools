"""Well/substrate-layer LEF power-pin tests for ``klt lvs`` (issue #4363).

Split out of ``test_lvs.py`` (issue #4746) to keep that file under its
per-file size ceiling. Tests moved verbatim.
"""

from __future__ import annotations

from pathlib import Path


def _write(path: Path, text: str) -> str:
    path.write_text(text)
    return str(path)


# --- Issue #4363: well/substrate-layer LEF pins are disclosed, not checked ----

_WELL_PIN_LAYOUT_SPICE = """\
.SUBCKT mylib__inv_1 A Y VGND VNB
.ENDS
.SUBCKT top in mid out VGND VNB sig
X1 in mid VGND VNB mylib__inv_1
X2 mid out VGND sig mylib__inv_1
.ENDS
"""

_WELL_PIN_LEF = """\
MACRO mylib__inv_1
  PIN VGND
    PORT
      LAYER met1 ;
        RECT 0 0 1 1 ;
    END
  END VGND
  PIN VNB
    PORT
      LAYER {layer} ;
        RECT 0 0 1 1 ;
    END
  END VNB
END mylib__inv_1
"""


def _well_pin_power_report(tmp_path, monkeypatch, vnb_layer, vgnd_second_net="VGND"):
    import klayout.db as kdb

    from klayout_tools import lvs as lvs_module
    from klayout_tools.lvs_power import _power_connectivity_report, _well_layer_lef_pins

    spice = _WELL_PIN_LAYOUT_SPICE
    if vgnd_second_net != "VGND":
        spice = spice.replace(
            "X2 mid out VGND sig", f"X2 mid out {vgnd_second_net} sig"
        )
    netlist = kdb.Netlist()
    netlist.read(_write(tmp_path / "layout.spice", spice), kdb.NetlistSpiceReader())
    lef = _write(tmp_path / "cells.lef", _WELL_PIN_LEF.format(layer=vnb_layer))
    monkeypatch.setattr(
        lvs_module,
        "_gate_level_power_pin_evidence_detail",
        lambda *_args: ({"VGND", "VNB"}, ["MYLIB__INV_1"], True, None),
    )
    well_pins = _well_layer_lef_pins((lef,))
    report = _power_connectivity_report(
        netlist, None, None, expected_nets=None, well_layer_pins=well_pins
    )
    return well_pins, report


def test_power_connectivity_excludes_and_discloses_pwell_layer_pin(
    tmp_path, monkeypatch
):
    """Issue #4363: a pwell-layer `VNB` on differing nets is disclosed in
    `excluded_pins`, and the metal pins still drive a `match`."""
    well_pins, power = _well_pin_power_report(tmp_path, monkeypatch, "pwell")
    assert well_pins == {("MYLIB__INV_1", "VNB")}
    assert power["status"] == "match"
    assert power["findings"] == []
    assert power["power_pins"] == ["VGND"]
    (excluded,) = power["excluded_pins"]
    assert excluded["pin"] == "VNB"
    assert excluded["cells"] == ["MYLIB__INV_1"]
    assert excluded["instance_count"] == 2
    assert "well/substrate-layer" in excluded["reason"]


def test_power_connectivity_excludes_nwell_layer_pin_by_layer_not_name(
    tmp_path, monkeypatch
):
    """Issue #4363: the exclusion follows the LEF layer (an `nwell` layer
    excludes it whatever the pin is called); a metal-layer pin is kept."""
    well_pins, _ = _well_pin_power_report(tmp_path, monkeypatch, "nwell")
    assert well_pins == {("MYLIB__INV_1", "VNB")}
    well_pins, power = _well_pin_power_report(tmp_path, monkeypatch, "met1")
    assert well_pins == frozenset()
    assert power["status"] == "mismatch"
    assert power["excluded_pins"] == []
    assert [f["pin"] for f in power["findings"]] == ["VNB"]


def test_power_connectivity_metal_pin_inconsistency_still_mismatch(
    tmp_path, monkeypatch
):
    """Issue #4363 negative control: excluding the pwell pin must not hide a
    real inconsistency on a metal power pin."""
    _, power = _well_pin_power_report(
        tmp_path, monkeypatch, "pwell", vgnd_second_net="sig"
    )
    assert power["status"] == "mismatch"
    assert [f["pin"] for f in power["findings"]] == ["VGND"]
    assert [p["pin"] for p in power["excluded_pins"]] == ["VNB"]


# --- Issue #5842: two-step reachability + VNW/VPW tokens ----------------------


def test_netlist_shape_accepts_lef_only_as_disclosure():
    """Issue #5842: `layout.abstract_cell_lef` alone is accepted beside
    `layout.netlist` (disclosure-only), resolved and echoed; no patterns."""
    from klayout_tools.lvs import _layout_abstraction_echo, _parse_layout_abstract_cells

    spec = {"netlist": "out.spice", "abstract_cell_lef": "cells.lef"}
    parsed = _parse_layout_abstract_cells(spec, "/req")
    assert parsed.patterns == ()
    assert parsed.lef_paths == ("/req/cells.lef",)
    assert _layout_abstraction_echo(parsed, []) == {"abstract_cell_lef": ["cells.lef"]}


def test_netlist_shape_without_lef_unchanged():
    from klayout_tools.lvs import _layout_abstraction_echo, _parse_layout_abstract_cells

    parsed = _parse_layout_abstract_cells({"netlist": "out.spice"}, "/req")
    assert parsed.lef_paths == ()
    assert _layout_abstraction_echo(parsed, []) == {}


def test_netlist_shape_still_rejects_abstract_cells():
    import pytest

    from klayout_tools.lvs import LvsError, _parse_layout_abstract_cells

    for spec in (
        {"netlist": "o.spice", "abstract_cells": ["x__*"]},
        {
            "netlist": "o.spice",
            "abstract_cells": ["x__*"],
            "abstract_cell_lef": "a.lef",
        },
    ):
        with pytest.raises(LvsError, match="applies only to inline extraction"):
            _parse_layout_abstract_cells(spec, "/req")


def test_inline_shape_lef_without_cells_still_rejected():
    import pytest

    from klayout_tools.lvs import LvsError, _parse_layout_abstract_cells

    with pytest.raises(LvsError, match="abstract_cells is empty"):
        _parse_layout_abstract_cells(
            {"file": "a.gds", "deck": "sky130", "abstract_cell_lef": "a.lef"}, "/req"
        )


def test_well_pin_name_tokens_classified():
    from klayout_tools.socket_check import classify_pin_by_name

    assert classify_pin_by_name("VPW")[1] == "GROUND"
    assert classify_pin_by_name("VNW")[1] == "POWER"


def test_vpw_finding_carries_abstract_artifact_caveat():
    from klayout_tools.lvs_power import _ground_pin_caveat

    assert "may be an artifact" in _ground_pin_caveat("VPW")
    assert _ground_pin_caveat("VNW") == ""
