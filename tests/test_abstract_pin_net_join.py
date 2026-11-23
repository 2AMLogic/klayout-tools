"""Connectivity of route trees on separate pads of one abstracted pin (#6742),
and the landing evidence a mixed named/anonymous join requires (#7561)."""

import re
from collections import Counter
from pathlib import Path
from unittest.mock import Mock

import klayout.db as kdb
import pytest

from klayout_tools.extract import run_extract
from klayout_tools.extract_abstract_pin_join import AbstractPinJoins


def _other_def_islands(top, metal, names):
    # Earlier disconnected shapes already own each public DEF name.
    # The actual route trees still carry those properties in the GDS.
    for index, name in enumerate(names):
        x = -3000 - index * 1000
        for y in range(-6000, -4000, 400):
            island = top.shapes(metal).insert(kdb.Box(x, y, x + 200, y + 200))
            island.set_property(1, name)


def _draw_pad(cell, layout, li, x):
    cell.shapes(li).insert(kdb.Box(x - 200, -200, x + 200, 200))
    cell.shapes(layout.layer(66, 44)).insert(kdb.Box(x - 75, -75, x + 75, 75))


def _pad_layout(
    path,
    names,
    *,
    def_names=False,
    sinks=None,
    shared_rail=False,
    chain=False,
    disconnected_def=False,
):
    """One LI pad per route. Only the leaf's contacted poly joins the pads."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    driver = layout.create_cell("PAD_DRIVER")
    sink = layout.create_cell("PAD_SINK")
    li, via, metal = (layout.layer(*pair) for pair in [(67, 20), (67, 44), (68, 20)])
    li_text = layout.layer(67, 5)
    driver.shapes(li_text).insert(kdb.Text("IN", kdb.Trans(0, 0)))
    sink.shapes(li).insert(kdb.Box(-200, -200, 200, 200))
    sink.shapes(li_text).insert(kdb.Text("S", kdb.Trans(0, 0)))
    for tied in ("T", "U", "V") if shared_rail else ():
        sink.shapes(li_text).insert(kdb.Text(tied, kdb.Trans(0, 0)))
    top.insert(kdb.CellInstArray(driver.cell_index(), kdb.Trans()))
    step = 2000
    count = 2 if chain else len(names)
    if chain:
        top.insert(kdb.CellInstArray(driver.cell_index(), kdb.Trans(step, 0)))
    driver.shapes(layout.layer(66, 20)).insert(
        kdb.Box(-100, -100, (count - 1) * step + 100, 100)
    )
    for index in range(count):
        _draw_pad(driver, layout, li, index * step)
    if disconnected_def:
        _other_def_islands(top, metal, names)
    for index, name in enumerate(names):
        x = index * step
        if sinks is not None and index not in sinks:
            continue
        top.insert(kdb.CellInstArray(sink.cell_index(), kdb.Trans(x, 3000)))
        for y in (0, 3000):
            top.shapes(via).insert(kdb.Box(x - 100, y - 100, x + 100, y + 100))
        tree = top.shapes(metal).insert(kdb.Box(x - 150, -150, x + 150, 3150))
        if name and def_names:
            tree.set_property(1, name)
        elif name:
            top.shapes(layout.layer(68, 5)).insert(kdb.Text(name, kdb.Trans(x, 1000)))
    layout.write(str(path))


def _extract(tmp_path, names, **options):
    gds = tmp_path / "pads.gds"
    _pad_layout(gds, names, **options)
    report = run_extract(
        str(gds),
        "sky130",
        top="TOP",
        abstract_cell_patterns=("PAD_*",),
        output=str(tmp_path / "pads.spice"),
        def_net_names=options.get("def_names", False),
    )
    lines = Path(report["netlist_path"]).read_text().splitlines()
    pins = [line.split()[1] for line in lines if line.startswith("XPAD_")]
    return pins, report["warnings"]


@pytest.mark.parametrize(
    "names", [("SIGNAL", None), (None, "SIGNAL"), ("SIGNAL", None, None)]
)
def test_separate_routed_pad_trees_share_the_pin_net(tmp_path, names):
    pins, warnings = _extract(tmp_path, names)
    assert pins == ["SIGNAL"] * (len(names) + 1)
    assert any(
        "PAD_DRIVER_0" in entry and "IN" in entry and "joined" in entry
        for entry in warnings
    )


@pytest.mark.parametrize("def_names", [False, True])
def test_differently_named_trees_stay_separate_and_are_reported(tmp_path, def_names):
    pins, warnings = _extract(tmp_path, ("FIRST", "SECOND"), def_names=def_names)
    assert Counter(pins) == {"FIRST": 2, "SECOND": 1}
    assert any(
        all(s in entry for s in ("PAD_DRIVER_0", "IN", "FIRST", "SECOND", "conflict"))
        for entry in warnings
    )


def test_identical_def_names_join(tmp_path):
    pins, warnings = _extract(tmp_path, ("SAME", "SAME"), def_names=True)
    assert pins == ["SAME"] * 3
    assert any("joined" in entry for entry in warnings)


def test_unrouted_pad_has_nothing_to_join(tmp_path):
    pins, warnings = _extract(tmp_path, (None, "ROUTED"), sinks={1})
    assert pins == ["ROUTED"] * 2
    assert not any("joined" in entry for entry in warnings)


def test_shared_pad_chain_across_instances(tmp_path):
    pins, warnings = _extract(tmp_path, ("BUS", None, None), chain=True)
    assert pins == ["BUS"] * 5
    (joined,) = [entry for entry in warnings if "joined" in entry]
    assert "PAD_DRIVER_0.IN" in joined and "PAD_DRIVER_1.IN" in joined


def test_named_conflict_at_end_of_an_anonymous_chain(tmp_path):
    pins, warnings = _extract(tmp_path, ("LEFT", None, "RIGHT"), chain=True)
    assert set(pins) == {"LEFT", "RIGHT"}
    assert any(
        "conflicting" in entry and "LEFT" in entry and "RIGHT" in entry
        for entry in warnings
    )


def test_old_tie_warnings_use_final_joined_net(tmp_path):
    pins, warnings = _extract(tmp_path, ("RAIL", None), shared_rail=True)
    assert set(pins) == {"RAIL"}
    for phrase in ("separately declared pins onto the same net", "resolved a majority"):
        (entry,) = [line for line in warnings if phrase in line]
        assert "PAD_SINK_1" in entry and "RAIL" in entry


def test_distinct_def_names_on_secondary_islands_are_not_joined(tmp_path):
    pins, warnings = _extract(
        tmp_path, ("FIRST", "SECOND"), def_names=True, disconnected_def=True
    )
    assert len(set(pins)) == 2
    assert any(
        "conflicting" in entry and "FIRST" in entry and "SECOND" in entry
        for entry in warnings
    )


class _FakeNet:
    """The three ``kdb.Net`` members ``AbstractPinJoins`` reads."""

    def __init__(self, cluster_id, name, pin_count):
        self.cluster_id = cluster_id
        self.name = name
        self._pins = pin_count

    def expanded_name(self):
        return self.name or f"$net{self.cluster_id}"

    def subcircuit_pin_count(self):
        return self._pins

    def terminal_count(self):
        return 0


def _mixed_nets():
    return _FakeNet(1, "VGND", 1), _FakeNet(2, "", 2)


@pytest.mark.parametrize("swap", [False, True])
def test_joiner_keeps_an_unsupported_named_anonymous_pair_separate(swap):
    rail, signal = _mixed_nets()
    joins = AbstractPinJoins()
    winner, loser = (signal, rail) if swap else (rail, signal)
    joins.record("u1", "VNB", winner, {1: rail, 2: signal})
    circuit = Mock()
    warnings = joins.apply(circuit)
    circuit.join_nets.assert_not_called()
    assert not any("joined routed pads" in entry for entry in warnings)
    (entry,) = [entry for entry in warnings if "kept separate" in entry]
    assert "u1.VNB" in entry
    assert "VGND" in entry and "$net2" in entry


def test_joiner_joins_a_mixed_pair_when_both_sides_landed():
    rail, signal = _mixed_nets()
    joins = AbstractPinJoins()
    joins.record("u1", "VNB", rail, {1: rail, 2: signal}, {1, 2})
    circuit = Mock()
    warnings = joins.apply(circuit)
    circuit.join_nets.assert_called_once_with(rail, signal)
    (entry,) = [entry for entry in warnings if "joined" in entry]
    assert "u1.VNB" in entry and "VGND" in entry


def test_joiner_cannot_merge_a_skipped_pair_through_a_shared_candidate():
    rail = _FakeNet(1, "VGND", 0)
    left = _FakeNet(2, "", 0)
    right = _FakeNet(3, "", 0)
    joins = AbstractPinJoins()
    joins.record("u1", "VNB", rail, {1: rail, 2: left})
    joins.record("u2", "VNB", left, {2: left, 3: right})
    joins.record("u3", "VNB", rail, {1: rail, 3: right})
    circuit = Mock()
    warnings = joins.apply(circuit)
    circuit.join_nets.assert_called_once_with(left, right)
    assert not any(
        "joined routed pads" in entry and "VGND" in entry for entry in warnings
    )


_VNB_LEF = """\
VERSION 5.7 ;
MACRO PAD_DRIVER
  ORIGIN 0.000 0.000 ;
  SIZE 4.000 BY 1.000 ;
  PIN VNB
    DIRECTION INOUT ;
    USE GROUND ;
    PORT
      LAYER li1 ;
        RECT -0.200 -0.200 0.200 0.200 ;
    END
    PORT
      LAYER li1 ;
        RECT 1.800 -0.200 2.200 0.200 ;
    END
  END VNB
END PAD_DRIVER
"""

#: Enough of an open_pdks KLayout LEF/DEF map file to give the LEF ``li1``
#: ports their own probe-layer role (``metal0``), as ``lef_layer_probe_roles``
#: reads it -- the shape ``tests/test_abstract_lef_pin_layer.py`` established.
_VNB_MAP_FILE = """\
li1     LEFPIN,NET,SPNET,PIN,VIA 67  20
NAME    li1/LABEL,li1/LEFPIN     67  5
met1    LEFPIN,NET,SPNET,PIN,VIA 68  20
NAME    met1/LABEL,met1/LEFPIN   68  5
"""


def _unsupported_landing_layout(path, *, isolated_pad):
    """Two met1 route trees over a LEF-only ``VNB`` pin whose second PORT
    point has no conductor of its own (#7561).

    The driver draws no label, no poly jumper and no contacts, so the pin
    resolves from the LEF's two ``li1`` rectangles. Only the first point has
    an ``li1`` pad (and via) beneath it; the second misses its declared
    conductor, and the cross-layer fallback finds the *overlying met1 tree*
    instead -- an unrelated routed signal, not a pad of this pin. With
    ``isolated_pad`` the second point's own ``li1`` pad is restored (still
    via-less), so the role probe answers with the pad's isolated island and
    the tree is never a candidate.
    """
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    driver = layout.create_cell("PAD_DRIVER")
    sink = layout.create_cell("PAD_SINK")
    li, via, metal = (layout.layer(*pair) for pair in [(67, 20), (67, 44), (68, 20)])
    li_text = layout.layer(67, 5)
    sink.shapes(li).insert(kdb.Box(-200, -200, 200, 200))
    sink.shapes(li_text).insert(kdb.Text("S", kdb.Trans(0, 0)))
    top.insert(kdb.CellInstArray(driver.cell_index(), kdb.Trans()))
    driver.shapes(li).insert(kdb.Box(-200, -200, 200, 200))
    if isolated_pad:
        driver.shapes(li).insert(kdb.Box(1800, -200, 2200, 200))
    for index, name in enumerate(("VGND", None)):
        x = index * 2000
        top.insert(kdb.CellInstArray(sink.cell_index(), kdb.Trans(x, 3000)))
        top.shapes(via).insert(kdb.Box(x - 100, 2900, x + 100, 3100))
        if index == 0:
            top.shapes(via).insert(kdb.Box(x - 100, -100, x + 100, 100))
        top.shapes(metal).insert(kdb.Box(x - 150, -150, x + 150, 3150))
        if name:
            top.shapes(layout.layer(68, 5)).insert(kdb.Text(name, kdb.Trans(x, 1000)))
    layout.write(str(path))


def _extract_lef_pads(tmp_path, *, isolated_pad):
    root = tmp_path / f"pads-{'iso' if isolated_pad else 'unsupported'}"
    gds = root.with_suffix(".gds")
    _unsupported_landing_layout(gds, isolated_pad=isolated_pad)
    lef = tmp_path / "pads.lef"
    lef.write_text(_VNB_LEF, encoding="utf-8")
    tech = tmp_path / "pdk" / "sky130A" / "libs.tech" / "klayout" / "tech"
    tech.mkdir(parents=True)
    (tech / "sky130A.map").write_text(_VNB_MAP_FILE, encoding="utf-8")
    return run_extract(
        str(gds),
        "sky130",
        top="TOP",
        abstract_cell_patterns=("PAD_*",),
        abstract_cell_lef_paths=(str(lef),),
        pdk_variant="sky130A",
        pdk_root=str(tmp_path / "pdk"),
        output=str(tmp_path / "pads.spice"),
    )


def _x_line_net(text, instance):
    (net,) = re.findall(rf"(?m)^X{instance} (\S+) PAD_", text)
    return net


def test_unsupported_landing_keeps_anonymous_signal_net_out_of_the_rail(tmp_path):
    report = _extract_lef_pads(tmp_path, isolated_pad=False)
    text = Path(report["netlist_path"]).read_text()
    assert _x_line_net(text, "PAD_DRIVER_0") == "VGND"
    assert _x_line_net(text, "PAD_SINK_0") == "VGND"
    anonymous = _x_line_net(text, "PAD_SINK_1")
    assert anonymous != "VGND" and "$" in anonymous
    warnings = report["warnings"]
    assert not any("joined routed pads" in entry for entry in warnings)
    (entry,) = [entry for entry in warnings if "kept separate" in entry]
    assert "PAD_DRIVER_0.VNB" in entry
    assert "VGND" in entry and "$" in entry


def test_isolated_lef_pad_control_leaves_the_sink_on_its_own_net(tmp_path):
    report = _extract_lef_pads(tmp_path, isolated_pad=True)
    text = Path(report["netlist_path"]).read_text()
    assert _x_line_net(text, "PAD_DRIVER_0") == "VGND"
    anonymous = _x_line_net(text, "PAD_SINK_1")
    assert anonymous != "VGND" and "$" in anonymous
    warnings = report["warnings"]
    assert not any("joined routed pads" in entry for entry in warnings)
    assert not any("kept separate" in entry for entry in warnings)
