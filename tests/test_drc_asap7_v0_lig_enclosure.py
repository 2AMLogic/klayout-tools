"""The r1p7 V0.LIG.EN.4 selector includes shipped cuts wholly covered by LIG."""

from collections import Counter

import klayout.db as kdb
import pytest

from klayout_tools.decks.asap7_rules import NOT_IMPLEMENTED, RULE_IDS
from test_drc_asap7_deck import _LIBRARIES, _docs_asap7_section, _shipped_gds


def test_enclosure_gap_records_which_layer_encloses_the_other():
    gap = next(gap for gap in NOT_IMPLEMENTED if gap.id == "V0.LIG.EN.4")
    assert gap.id not in RULE_IDS
    assert "V0 encloses LIG" in gap.reason
    assert gap.reason in _docs_asap7_section()


@pytest.mark.parametrize(("lib", "suffix"), _LIBRARIES)
def test_shipped_v0_lig_enclosure(lib, suffix):
    layout = kdb.Layout()
    layout.read(str(_shipped_gds(lib, suffix)))
    one_nm = round(0.001 / layout.dbu)
    counts = Counter()
    square_examples = 0
    affected_cells = set()
    for cell in layout.top_cells():
        lig, lisd, vias = (
            kdb.Region(cell.begin_shapes_rec(layout.layer(layer, 0))).merged()
            for layer in (16, 17, 18)
        )
        for cut in vias.interacting(lig).not_inside(lisd).each():
            via = kdb.Region(cut)
            if (via - lig).is_empty():
                # LIG covers the entire cut, so V0 cannot protrude on either axis.
                assert (via & lisd).is_empty()
                affected_cells.add(cell.name)
                counts["inside_lig"] += 1
                if (
                    cut.is_box()
                    and cut.bbox().width() == cut.bbox().height() == 18 * one_nm
                ):
                    contact = lig.interacting(via)
                    if contact.is_box() and contact.bbox() == cut.bbox().enlarged(
                        2 * one_nm
                    ):
                        square_examples += 1
            else:
                # In the shipped crossing contacts both cut and overlap are boxes.
                overlap = via & lig
                assert cut.is_box() and overlap.is_box()
                v, o = cut.bbox(), overlap.bbox()
                assert (o.left - v.left, v.right - o.right) == (0, 0)
                assert (o.bottom - v.bottom, v.top - o.top) == (one_nm, one_nm)
                counts["one_nm_both_sides"] += 1

    assert counts == {"inside_lig": 1413, "one_nm_both_sides": 4899}
    assert len(affected_cells) == 208
    assert square_examples == 519
