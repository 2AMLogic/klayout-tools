"""Reproduce the direction ambiguity in ASAP7 Table 3.11.2, V0.LIG.AUX.2."""

import klayout.db as kdb
import pytest

from klayout_tools.decks.asap7_rules import NOT_IMPLEMENTED, RULE_IDS
from test_drc_asap7_deck import _LIBRARIES, _docs_asap7_section, _shipped_gds


def _selected(vias, lig, lisd):
    return vias.interacting(lig).not_inside(lig).not_inside(lisd)


def _contact_by_axis(cut, lig):
    """Whether a horizontal or vertical V0 edge intersects LIG, including touch."""
    contacting = kdb.Edges(cut).interacting(lig)
    horizontal = contacting.with_angle(0, False)
    vertical = contacting.with_angle(90, False)
    return not horizontal.is_empty(), not vertical.is_empty()


def test_registry_explains_the_retained_direction_gap():
    gap = next(gap for gap in NOT_IMPLEMENTED if gap.id == "V0.LIG.AUX.2")
    assert gap.id not in RULE_IDS and gap.table == "Table 3.11.2"
    assert "square LIG" in gap.reason and "4,899" in gap.reason
    assert gap.reason in _docs_asap7_section()


@pytest.mark.parametrize("rotation", [0, 1])
def test_square_lig_has_two_direction_readings_after_gds_round_trip(tmp_path, rotation):
    layout = kdb.Layout()
    layout.dbu = 0.00025
    top = layout.create_cell("TOP")
    transform = kdb.Trans(rotation, False, 0, 0)
    top.shapes(layout.layer(18, 0)).insert(kdb.Box(0, 0, 96, 72).transformed(transform))
    top.shapes(layout.layer(16, 0)).insert(
        kdb.Box(8, -4, 88, 76).transformed(transform)
    )
    path = tmp_path / "square.gds"
    layout.write(str(path))
    saved = kdb.Layout()
    saved.read(str(path))
    cell = saved.top_cell()
    vias, lig = (kdb.Region(cell.shapes(saved.layer(layer, 0))) for layer in (18, 16))
    selected = _selected(vias, lig, kdb.Region())
    assert selected.count() == 1 and lig.bbox().width() == lig.bbox().height()
    assert (selected & lig).area() * (saved.dbu * 1000) ** 2 == 360
    assert _contact_by_axis(next(selected.each()), lig) == (
        rotation == 0,
        rotation == 1,
    )


@pytest.mark.parametrize(
    ("lig_box", "lisd_covers", "selected"),
    [
        ((96, 4, 176, 68), False, True),  # edge contact with zero overlap area
        ((-4, -4, 100, 76), False, False),  # fully inside LIG
        ((8, -4, 88, 76), True, False),  # partially inside LIG, fully inside LISD
        ((100, 4, 180, 68), False, False),  # isolated cut
    ],
)
def test_table_selects_boundary_contact_and_excludes_covered_cuts(
    lig_box, lisd_covers, selected
):
    via = kdb.Region(kdb.Box(0, 0, 96, 72))
    lig = kdb.Region(kdb.Box(*lig_box))
    lisd = via if lisd_covers else kdb.Region()
    assert _selected(via, lig, lisd).count() == int(selected)
    if selected:
        assert (via & lig).is_empty()
        assert _contact_by_axis(next(via.each()), lig) == (False, True)


@pytest.mark.parametrize(("lib", "suffix"), _LIBRARIES)
def test_shipped_selected_v0_contacts_only_vertical_edges(lib, suffix):
    layout = kdb.Layout()
    layout.read(str(_shipped_gds(lib, suffix)))
    cells = layout.top_cells()
    selected_sites = selected_masters = 0
    for cell in cells:
        lig, lisd, vias = (
            kdb.Region(cell.begin_shapes_rec(layout.layer(layer, 0))).merged()
            for layer in (16, 17, 18)
        )
        selected = _selected(vias, lig, lisd)
        selected_masters += int(not selected.is_empty())
        for cut in selected.each():
            via = kdb.Region(cut)
            contact = lig.interacting(via)
            assert (via & contact).area() > 0
            assert all(
                p.is_box() and p.bbox().width() > p.bbox().height()
                for p in contact.each()
            )
            assert _contact_by_axis(cut, contact) == (False, True)
            selected_sites += 1
    assert (selected_sites, selected_masters, len(cells)) == (4899, 211, 212)
