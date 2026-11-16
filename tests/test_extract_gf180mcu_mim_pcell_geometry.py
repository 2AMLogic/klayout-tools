"""gf180mcu MIM extraction against the PDK pcell's own geometry (issue #3440).

The geometry below is what ``draw_cap_mim.py`` (gf180mcu PDK, ``MIM-A``) draws:
``FuseTop`` and ``Metal3`` cover the same square, ``Metal2`` extends only
0.6 um beyond it (less than the runset's 1.06 um virtual-plate oversize),
``CAP_MK`` covers the ``Metal2`` outline, ``MIM_L_MK`` is a **thin strip**
(0.1 um) along one ``FuseTop`` edge, and a sea of ``Via2`` sits inside
``FuseTop``. Ground truth is the PDK runset
(``gf180mcu.lvs`` ``mim_option=A``, ``klayout -b``, gf180mcuA @
``c6d73a35f524070e85faff4a6a9eef49553ebc2b``): one
``cap_mim_2f0_m2m3_noshield`` with ``A=100P P=40U`` and two distinct plate
nets. Hand-drawn fixtures with a full-size ``MIM_L_MK`` never exercised this.
"""

from __future__ import annotations

import klayout.db as kdb
import pytest

from klayout_tools.extract import run_extract


def _pcell_option_a_layout(*, efuse: bool = False) -> kdb.Layout:
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")

    def draw(layer, datatype, x1, y1, x2, y2):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Box(
                round(x1 * 1000), round(y1 * 1000), round(x2 * 1000), round(y2 * 1000)
            )
        )

    draw(75, 0, 0, 0, 10, 10)  # FuseTop
    draw(42, 0, 0, 0, 10, 10)  # Metal3 (upper plate)
    draw(36, 0, -0.6, -0.6, 10.6, 10.6)  # Metal2 (bottom plate)
    draw(117, 5, -0.6, -0.6, 10.6, 10.6)  # CAP_MK
    draw(117, 10, 0, 0, 10, 0.1)  # MIM_L_MK: thin strip only
    for i in range(12):  # Via2 sea inside FuseTop
        for j in range(12):
            x, y = 0.69 + i * 0.76, 0.69 + j * 0.76
            draw(38, 0, x, y, x + 0.26, y + 0.26)
    top.shapes(layout.layer(42, 10)).insert(kdb.Text("VTOP", kdb.Trans(5000, 5000)))
    top.shapes(layout.layer(36, 10)).insert(kdb.Text("VBOT", kdb.Trans(5000, -300)))
    if efuse:
        draw(80, 5, -1, -1, 11, 11)  # efuse_mk over the whole cap
    return layout


def _extract(tmp_path, layout):
    path = str(tmp_path / "pcell.gds")
    layout.write(path)
    return run_extract(
        path,
        "gf180mcu",
        output=str(tmp_path / "pcell.spice"),
        deck_options={"mim_option": "A"},
    )


def test_pcell_geometry_extracts_full_plate_with_distinct_nets(tmp_path):
    report = _extract(tmp_path, _pcell_option_a_layout())

    assert report["device_counts"] == {"cap_mim_2f0_m2m3_noshield": 1}
    (device,) = report["devices"]
    assert {device["nets"]["a"], device["nets"]["b"]} == {"VBOT", "VTOP"}
    # Runset: A=100P P=40U -- the whole FuseTop plate, not the MIM_L_MK strip.
    assert device["params"]["area_um2"] == pytest.approx(100.0)
    assert device["params"]["perimeter_um"] == pytest.approx(40.0)
    assert not any("merges" in w for w in report["warnings"])


def test_pcell_geometry_under_efuse_marker_is_not_a_capacitor(tmp_path):
    """Runset: ``mimcap_exclude = efuse_mk.join(plfuse)`` removes the plate."""
    report = _extract(tmp_path, _pcell_option_a_layout(efuse=True))
    assert report["device_counts"] == {}
