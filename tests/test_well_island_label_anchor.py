"""A named well island labels only an available metal-side anchor."""

import klayout.db as kdb
import pytest

from klayout_tools import gen
from klayout_tools.gen_pcells.well_island import _build_well_island_pcell


@pytest.mark.parametrize("has_anchor", [True, False])
def test_named_island_requires_a_covered_side_before_drawing_label(
    monkeypatch, has_anchor
):
    ring_layout = gen._ring_layout

    def layout_with_no_side_ports(*args, **kwargs):
        ring = ring_layout(*args, **kwargs)
        return {**ring, "ports": {}}

    # Current public gap parameters can open only one side. Exercise the
    # PCell's explicit fallback for a ring with no eligible label position.
    if not has_anchor:
        monkeypatch.setattr(gen, "_ring_layout", layout_with_no_side_ports)
    layout = kdb.Layout()
    layout.dbu = 0.001
    declaration = _build_well_island_pcell()["well_island"]()
    pcell = layout.register_pcell("well_island", declaration)
    metal = kdb.LayerInfo(67, 20)
    labels = kdb.LayerInfo(67, 5)
    cell = layout.cell(
        layout.add_pcell_variant(
            pcell,
            {
                "net": "VB",
                "metal_layer": metal,
                "metal_label_present": True,
                "metal_label_layer": labels,
                "tap_layer": kdb.LayerInfo(65, 44),
                "contact_layer": kdb.LayerInfo(66, 44),
            },
        )
    )
    assert not cell.shapes(layout.layer(metal)).is_empty()
    texts = [
        s.text.string for s in cell.shapes(layout.layer(labels)).each() if s.is_text()
    ]
    assert texts == (["VB"] if has_anchor else [])
