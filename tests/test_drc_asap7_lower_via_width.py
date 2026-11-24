"""Direction controls for the three lower-metal across-track width gaps (#3147)."""

import klayout.db as kdb
import pytest

from klayout_tools.decks.asap7_rules import NOT_IMPLEMENTED
from test_drc_asap7_deck import _LIBRARIES, _docs_asap7_section, _shipped_gds


def _across_track(cut, metal):
    """Exact edge coincidence across horizontal and vertical rectangular tracks."""
    return (
        (cut.bottom, cut.top) == (metal.bottom, metal.top),
        (cut.left, cut.right) == (metal.left, metal.right),
    )


@pytest.mark.parametrize("rule", ["V0.M1.AUX.3", "V1.M2.AUX.2", "V2.M3.AUX.2"])
def test_square_landing_needs_a_track_direction(rule):
    gap = next(gap for gap in NOT_IMPLEMENTED if gap.id == rule)
    assert "square" in gap.reason and gap.reason in _docs_asap7_section()
    metal = kdb.Box(0, 0, 24, 24)
    assert _across_track(kdb.Box(0, 3, 24, 21), metal) == (False, True)
    assert _across_track(kdb.Box(3, 0, 21, 24), metal) == (True, False)


@pytest.mark.parametrize(("lib", "suffix"), _LIBRARIES)
def test_shipped_lower_via_cut_sites(lib, suffix):
    layout = kdb.Layout()
    layout.read(str(_shipped_gds(lib, suffix)))
    horizontal_sites = [0, 0]
    total_sites = [0, 0, 0]
    for cell in layout.top_cells():
        for level, (via, metal) in enumerate(((18, 19), (21, 20), (25, 30))):
            cuts = {shape.bbox() for shape in cell.shapes(layout.layer(via, 0)).each()}
            total_sites[level] += len(cuts)
            if level == 2 or not cuts:
                continue
            conductors = list(
                kdb.Region(cell.shapes(layout.layer(metal, 0))).each_merged()
            )
            for cut in cuts:
                touching = [
                    polygon for polygon in conductors if polygon.bbox().overlaps(cut)
                ]
                if len(touching) != 1 or not touching[0].is_box():
                    continue
                box = touching[0].bbox()
                if (
                    cut.inside(box)
                    and box.width() > box.height()
                    and _across_track(cut, box) == (True, False)
                ):
                    horizontal_sites[level] += 1
    assert total_sites == [8576, 389, 0]
    assert horizontal_sites == [5210, 389]
