"""Two-terminal resistance between a device head and a promoted pin (#7029)."""

from __future__ import annotations

import klayout.db as kdb
import pytest

from klayout_tools.extract import run_extract
from test_extract import _make_poly_resistor_layout, _write_gds


def _contacted_heads(extension=0, *, extra_label=False):
    layout = _make_poly_resistor_layout("sky130")
    layout.dbu = 0.001
    top = layout.top_cell()
    for pair in ((66, 44), (67, 20), (67, 5)):
        top.shapes(layout.layer(*pair)).clear()
    for x, name in ((1500, "RA"), (10500, "RB")):
        for layer, box in (
            ((66, 44), kdb.Box(x - 85, 415, x + 85, 585)),
            ((67, 20), kdb.Box(x - 170, 330, x + 170, 2200)),
            ((67, 44), kdb.Box(x - 85, 1915, x + 85, 2085)),
            ((68, 20), kdb.Box(x - 170, 1830, x + 2000 + extension, 2170)),
        ):
            top.shapes(layout.layer(*layer)).insert(box)
        labels = top.shapes(layout.layer(68, 5))
        labels.insert(kdb.Text(name, kdb.Trans(x + 1800 + extension, 2000)))
        if extra_label and name == "RA":
            labels.insert(kdb.Text(name, kdb.Trans(x + 1000, 2000)))
    return layout


def _extract(tmp_path, layout, r_cut, **options):
    path = _write_gds(layout, tmp_path / "pair.gds")
    return run_extract(
        path,
        "sky130",
        output=str(tmp_path / "pair.spice"),
        parasitics=True,
        resistance_graph=True,
        resistance_graph_r_cut_ohm=r_cut,
        **options,
    )


def _graphs(report):
    return {net["net"]: net["resistance_graph"] for net in report["parasitics"]["nets"]}


@pytest.mark.parametrize("vertical", [False, True], ids=["horizontal", "vertical"])
def test_promoted_pin_resistance_tracks_sheet_and_series_cuts(tmp_path, vertical):
    def heads(extension=0):
        layout = _contacted_heads(extension)
        if vertical:
            layout.top_cell().transform(kdb.Trans.R90)
        return layout

    report = _extract(tmp_path, heads(), 1)
    base = _graphs(report)
    ideal = _graphs(_extract(tmp_path, heads(), 0))
    longer = _graphs(_extract(tmp_path, heads(680), 1))
    for pin, x in (("RA", 3.3), ("RB", 12.3)):
        assert any(
            n["name"] == pin and n["pin_index"] is not None for n in report["nets"]
        )
        graph = base[pin]
        assert graph["status"] == "ok"
        device, port = graph["terminals"]
        assert device.startswith("device@")
        px, py = (-2.0, x) if vertical else (x, 2.0)
        assert port == f"pin:{pin}@{px:.4f},{py:.4f}"
        r = graph["selected_resistance_ohm"]
        assert graph["resistance_source"] == "graph"
        assert 0 < r == graph["pair_resistance_ohm"][0][1] < float("inf")
        assert r - ideal[pin]["selected_resistance_ohm"] == pytest.approx(2.0)
        assert longer[pin]["selected_resistance_ohm"] - r == pytest.approx(0.25)


def test_internal_and_ambiguous_labels_do_not_create_ports(tmp_path):
    ambiguous = _graphs(_extract(tmp_path, _contacted_heads(extra_label=True), 1))
    demoted = _graphs(
        _extract(tmp_path, _contacted_heads(), 1, declared_pins=frozenset({"RB"}))
    )
    for graphs in (ambiguous, demoted):
        assert graphs["RA"]["resistance_source"] == "legacy"
        assert graphs["RA"]["resistance_source_reason"] == "graph_status_skipped"
        assert len(graphs["RA"]["terminals"]) == 1
        assert graphs["RB"]["resistance_source"] == "graph"


def test_pin_on_a_square_pad_keeps_its_drawn_anchor(tmp_path):
    layout = _contacted_heads()
    top = layout.top_cell()
    top.shapes(layout.layer(68, 20)).clear()
    for x in (1500, 10500):
        top.shapes(layout.layer(68, 20)).insert(kdb.Box(x - 170, 915, x + 2000, 3085))
    graph = _graphs(_extract(tmp_path, layout, 1))["RA"]
    assert graph["resistance_source"] == "graph"
    assert graph["terminals"] == ["device@3.0000,0.5000", "pin:RA@3.3000,2.0000"]


def test_same_pin_coordinate_on_both_sides_of_a_cut_is_ambiguous(tmp_path):
    layout = _contacted_heads()
    top = layout.top_cell()
    metal_labels = top.shapes(layout.layer(68, 5))
    metal_labels.clear()
    metal_labels.insert(kdb.Text("RB", kdb.Trans(12300, 2000)))
    pin = kdb.Text("RA", kdb.Trans(1500, 2000))
    metal_labels.insert(pin)
    top.shapes(layout.layer(67, 5)).insert(pin)
    graphs = _graphs(_extract(tmp_path, layout, 1))
    assert graphs["RA"]["resistance_source_reason"] == "graph_status_skipped"
    assert graphs["RB"]["resistance_source"] == "graph"
