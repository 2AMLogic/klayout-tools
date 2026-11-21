"""Branch pin for `mos_array`'s optional well-tap net label (#1473).

`mos_array`'s well-tie tap pad draws a fixed ``WELL_TAP`` text only when the
hidden ``metal_label_present`` PCell param says the resolved PDK has a label
layer. ``klt gen`` always resolves it ``True`` for the one family that draws
the tap (``sg13cmos5l``), so the label-absent branch is unreachable through
``generate()`` and is exercised here by driving the PCell library directly,
the same way ``generate()`` does but with that one hidden param overridden.

Needs no PDK install: ``_mos_array_layer_params`` resolves from the variant
name alone (see ``test_mos_array_exclusion_is_specific_to_guard_ring_option``).
"""

from __future__ import annotations

import klayout.db as kdb

from klayout_tools.gen import (
    _MOS_ARRAY_WELL_TAP_NET_LABEL,
    _mos_array_layer_params,
    _pcell_library,
)

_VARIANT = "ihp-sg13cmos5l"


def _draw_pfet_array(*, metal_label_present: bool):
    """Draw a `flavor='pfet'` sg13cmos5l `mos_array` PCell; return
    ``(layout, cell, layer_params)``."""
    params = {"flavor": "pfet", "w_um": 5.0, "l_um": 0.5, "rows": 1, "cols": 2}
    layer_params = _mos_array_layer_params({"variant": _VARIANT}, params)
    # The one override: everything else is exactly what `generate()` resolves.
    layer_params["metal_label_present"] = metal_label_present
    lib = _pcell_library()
    decl = lib.layout().pcell_declaration("mos_array")
    layout = kdb.Layout()
    layout.dbu = 0.001
    cell = layout.cell(
        layout.add_pcell_variant(lib, decl.id(), {**params, **layer_params})
    )
    return layout, cell, layer_params


def _texts(layout: kdb.Layout, cell: kdb.Cell) -> list[str]:
    """Every text string drawn anywhere in ``cell``, on any layer."""
    found: list[str] = []
    for layer_index in layout.layer_indexes():
        it = cell.begin_shapes_rec(layer_index)
        while not it.at_end():
            if it.shape().is_text():
                found.append(it.shape().text_string)
            it.next()
    return sorted(found)


def _region(layout: kdb.Layout, cell: kdb.Cell, info: kdb.LayerInfo) -> kdb.Region:
    return kdb.Region(cell.begin_shapes_rec(layout.layer(info))).merged()


def test_pfet_well_tap_without_a_label_layer_draws_geometry_but_no_text():
    """With `metal_label_present=False` the tap pad's active/contact/metal/
    implant boxes are still drawn, geometrically identical to the labelled
    variant, and the `WELL_TAP` net-name text is the only thing that goes
    missing."""
    labelled_layout, labelled, layer_params = _draw_pfet_array(metal_label_present=True)
    bare_layout, bare, _ = _draw_pfet_array(metal_label_present=False)

    # The labelled control draws exactly the one fixed net-name label ...
    assert _texts(labelled_layout, labelled) == [_MOS_ARRAY_WELL_TAP_NET_LABEL]
    # ... and the label-absent variant draws no text at all.
    assert _texts(bare_layout, bare) == []

    implant = _region(bare_layout, bare, layer_params["well_tap_implant_layer"])
    # `well_tap_implant` is drawn only over the tap pad itself, so a
    # non-empty region proves the tap geometry is present without the label.
    assert not implant.is_empty()
    for role in (
        "active_layer",
        "contact_layer",
        "metal_layer",
        "well_tap_implant_layer",
    ):
        info = layer_params[role]
        # Symmetric difference, not `==`: Region equality is order-sensitive
        # across two separately built layouts even for identical geometry.
        drawn_differently = _region(bare_layout, bare, info) ^ _region(
            labelled_layout, labelled, info
        )
        assert drawn_differently.is_empty(), role
