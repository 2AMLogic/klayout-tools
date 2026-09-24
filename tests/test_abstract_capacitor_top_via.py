"""``--abstract-cells`` must not short a black-boxed MiM cap's plates.

Regression test for issue #2396: ``_abstract_cell_mask_layers`` erases an
abstracted cell's capacitor ``top_plate`` (sky130's ``capm``) but deliberately
keeps its routing layers -- including the ``top_plate_via`` (``via3``) that the
PDK's own DRM requires to overlap the ``met3`` bottom plate. With the top plate
gone, ``_capacitor_top_via_overlap_region``'s exclusion (issues #364/#1388) went
empty, so every top-plate via inside the black box fell back into the deck's
generic ``vias[]`` connectivity and shorted the cap's own top and bottom plates
-- merging otherwise-distinct *parent* nets.
"""

import re

import klayout.db as kdb
import pytest

from klayout_tools.extract import run_extract

#: The two capacitors' top-plate rails and the shared bottom plate, as the
#: parent's own labelled nets. A correct extraction keeps all three apart.
_PARENT_NETS = ("topa", "topb", "bot")


def _mim_macro_layout(*, draw_top_plates=True):
    """A macro with two sky130 MiM caps sharing one ``met3`` bottom plate.

    Each cap's ``capm`` top plate is reached through its own ``via3`` and
    ``met4`` rail, which the parent contacts (and labels) above the macro; the
    shared bottom plate is contacted and labelled by the parent on ``met3``.

    ``draw_top_plates=False`` is the issue's own ablation in reverse -- the
    same layout with no ``capm`` drawn at all, where via3-on-met3 really *is*
    ordinary routing and the three parent nets are genuinely one net. It pins
    down that the fix keeps a real short visible rather than cutting every
    via on the layer.
    """
    layout = kdb.Layout()
    layout.dbu = 0.001
    macro = layout.create_cell("mylib__mimmacro")
    top = layout.create_cell("top")

    def draw(target, layer, datatype, box):
        target.shapes(layout.layer(layer, datatype)).insert(kdb.Box(*box))

    def label(target, layer, datatype, text, x, y):
        target.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    # One met3 conductor is the bottom plate of both capacitors.
    draw(macro, 70, 20, (0, 0, 6000, 2000))
    label(macro, 70, 5, "BOT", 3000, 400)
    for x0, pin in ((500, "TOPA"), (4500, "TOPB")):
        if draw_top_plates:
            draw(macro, 89, 44, (x0, 500, x0 + 1000, 1500))  # capm.drawing
        draw(macro, 70, 44, (x0 + 300, 800, x0 + 700, 1200))  # via3.drawing
        draw(macro, 71, 20, (x0 + 200, 700, x0 + 800, 3000))  # met4.drawing
        # The macro's own declared pin labels, in its own definition -- how
        # `--abstract-cells` resolves a black box's ports.
        label(macro, 71, 5, pin, x0 + 500, 2500)
    top.insert(kdb.CellInstArray(macro.cell_index(), kdb.Trans()))

    # The parent's own routing/labels, drawn outside the macro: each
    # top-plate rail carries a distinct net, the bottom plate a third.
    for x0, name in ((500, "topa"), (4500, "topb")):
        draw(top, 71, 20, (x0 + 200, 2800, x0 + 800, 3600))
        label(top, 71, 5, name, x0 + 500, 3200)
    draw(top, 70, 20, (2500, 1500, 3500, 2600))
    label(top, 70, 5, "bot", 3000, 2400)
    return layout


def _extract(tmp_path, *, abstract, **layout_kwargs):
    path = tmp_path / "mim.gds"
    _mim_macro_layout(**layout_kwargs).write(str(path))
    return run_extract(
        str(path),
        "sky130",
        output=str(tmp_path / ("abstract.spice" if abstract else "flat.spice")),
        abstract_cell_patterns=("mylib__*",) if abstract else (),
    )


def _labels(net):
    """One extracted net's own label set, un-joining KLayout's ``|``/``,``.

    KLayout joins every label found on one electrical net into a single
    ``Net.name``; ``spice_safe_net_name`` then renders that join as ``|``.
    """
    return set(re.split(r"[|,]", str(net["name"])))


def _nets_carrying(extraction, name):
    """The extracted nets whose joined label set contains ``name``."""
    return [net for net in extraction["nets"] if name in _labels(net)]


def test_flat_extraction_keeps_the_three_parent_nets_apart(tmp_path):
    """The control: unabstracted, #364's exclusion already does this."""
    flat = _extract(tmp_path, abstract=False)
    assert flat["device_count"] == 2
    assert {device["class"] for device in flat["devices"]} == {
        "sky130_fd_pr__model__cap_mim"
    }
    for name in _PARENT_NETS:
        assert len(_nets_carrying(flat, name)) == 1, name
    assert len({_nets_carrying(flat, name)[0]["name"] for name in _PARENT_NETS}) == 3


def test_abstracted_macro_keeps_the_three_parent_nets_apart(tmp_path):
    """Issue #2396: black-boxing the macro must not merge the parent's nets."""
    abstract = _extract(tmp_path, abstract=True)
    # The black box contains no recognised devices -- that is the whole point
    # of the erasure, and is exactly why the top-plate-via exclusion has to be
    # captured before it happens.
    assert abstract["device_count"] == 0
    for name in _PARENT_NETS:
        assert len(_nets_carrying(abstract, name)) == 1, name
    merged = {_nets_carrying(abstract, name)[0]["name"] for name in _PARENT_NETS}
    assert len(merged) == 3, f"parent nets collapsed onto {merged}"


def test_abstraction_keeps_a_real_via_short_visible(tmp_path):
    """No capm drawn -- via3-on-met3 is ordinary routing and really shorts.

    Guards the other direction: the fix must not cut every via on the
    ``top_plate_via`` layer inside a black box (issue #1388's false-disconnect
    failure mode), only the overlap a recognised top plate actually explains.
    """
    flat = _extract(tmp_path, abstract=False, draw_top_plates=False)
    assert flat["device_count"] == 0
    assert len({_nets_carrying(flat, name)[0]["name"] for name in _PARENT_NETS}) == 1
    abstract = _extract(tmp_path, abstract=True, draw_top_plates=False)
    assert (
        len({_nets_carrying(abstract, name)[0]["name"] for name in _PARENT_NETS}) == 1
    )


@pytest.mark.parametrize(
    "transform",
    [kdb.Trans(1, True, 3000, -2000), kdb.Trans(2, False, 0, 0)],
    ids=["rotated-mirrored", "rotated-180"],
)
def test_abstracted_macro_exclusion_follows_the_instance_transform(tmp_path, transform):
    """The captured exclusion is in top-cell coordinates, so a rotated or
    mirrored placement of the same macro is covered by the same capture."""
    path = tmp_path / "mim.gds"
    layout = _mim_macro_layout()
    layout.top_cell().transform(transform)
    layout.write(str(path))
    abstract = run_extract(
        str(path),
        "sky130",
        output=str(tmp_path / "abstract.spice"),
        abstract_cell_patterns=("mylib__*",),
    )
    assert (
        len({_nets_carrying(abstract, name)[0]["name"] for name in _PARENT_NETS}) == 3
    )
