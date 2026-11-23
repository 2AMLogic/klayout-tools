"""Connectivity of route trees on separate pads of one abstracted pin (#6742)."""

from collections import Counter
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.extract import run_extract


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
