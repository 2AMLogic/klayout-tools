"""Issue #3021: vertical-overlap coupling must not be charged between a
recognised MiM capacitor's own plate nets inside its top-plate footprint --
the ``CapacitorDevice`` already is the capacitance there."""

from __future__ import annotations

import klayout.db as kdb
import pytest

from klayout_tools.decks import sky130
from klayout_tools.extract import run_extract

_MET3_MET4_COEF = sky130.PARASITICS.metal_overlaps[3]


def _um(x0, y0, x1, y1):
    return kdb.Box(
        round(x0 * 1000), round(y0 * 1000), round(x1 * 1000), round(y1 * 1000)
    )


def _mim_layout(*, with_cap_marker: bool) -> kdb.Layout:
    """met3 bottom plate (40x40um), met4 top plate 14x9um (126um^2) wired to
    the 10x5um (50um^2) ``capm`` top plate through ``via3``. ``capm`` is
    and its ``via3`` are omitted when ``with_cap_marker`` is false, leaving two
    distinct nets with a plain met3/met4 overlap and no capacitor."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    draw(70, 20, _um(-20, -20, 20, 20))  # met3.drawing
    draw(71, 20, _um(-2, -2, 12, 7))  # met4.drawing
    if with_cap_marker:
        draw(70, 44, _um(1, 1, 1.2, 1.2))  # via3.drawing (met3<->met4)
        draw(89, 44, _um(0, 0, 10, 5))  # capm.drawing
    top.shapes(layout.layer(70, 5)).insert(kdb.Text("BOT", kdb.Trans(-15000, -15000)))
    top.shapes(layout.layer(71, 5)).insert(kdb.Text("TOPP", kdb.Trans(11000, 6000)))
    return layout


def _coupled_total_ff(report) -> float:
    return sum(
        c["capacitance_ff"]
        for net in report["parasitics"]["nets"]
        for c in net["coupled"]
    )


def test_recognised_mim_overlap_is_masked_from_coupling(tmp_path):
    path = str(tmp_path / "mim.gds")
    _mim_layout(with_cap_marker=True).write(path)
    report = run_extract(
        path, "sky130", output=str(tmp_path / "mim.spice"), parasitics=True
    )

    assert report["device_count"] == 1
    # Margin overlap only: 126um^2 plate overlap - 50um^2 under capm.
    assert _coupled_total_ff(report) / 2 == pytest.approx(76.0 * _MET3_MET4_COEF)


def test_layout_without_capacitor_keeps_full_overlap_coupling(tmp_path):
    path = str(tmp_path / "plain.gds")
    _mim_layout(with_cap_marker=False).write(path)
    report = run_extract(
        path, "sky130", output=str(tmp_path / "plain.spice"), parasitics=True
    )

    assert report["device_count"] == 0
    assert _coupled_total_ff(report) / 2 == pytest.approx(126.0 * _MET3_MET4_COEF)


def test_masked_area_stays_on_the_ground_term(tmp_path):
    """Charge conservation: area no longer claimed as coupling is not
    deducted from either plate net's ground term."""
    reports = {}
    for marked in (False, True):
        path = str(tmp_path / f"m{marked}.gds")
        _mim_layout(with_cap_marker=marked).write(path)
        reports[marked] = run_extract(
            path,
            "sky130",
            output=str(tmp_path / f"m{marked}.spice"),
            parasitics=True,
        )

    def ground_ff(report):
        return sum(net["capacitance_ff"] for net in report["parasitics"]["nets"])

    met3_area, met4_area = (sky130.PARASITICS.metals[i].cap_area_ff_um2 for i in (3, 4))
    # 50um^2 under capm returns to both plates' ground terms.
    assert ground_ff(reports[True]) - ground_ff(reports[False]) == pytest.approx(
        50.0 * (met3_area + met4_area)
    )
