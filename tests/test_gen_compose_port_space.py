"""Same-net *space* rejection in the ``ports_from_labels`` landing search
(#6440): the sibling of #5608's notch test. A pad that lands between two
non-touching shapes of its own net can leave a sub-``space`` gap to each --
a net-agnostic ``met*.space.*`` finding that is *inter*-polygon, so
``notch_check`` (intra-polygon only) never sees it and ``clearance`` skips
the port net's own metal. Overlap with the net's own metal at the landing
stays legal. Synthetic sky130 macros only."""

import pytest

from klayout_tools.gen_compose import GenComposeError
from test_gen_compose import _isolate as _isolate
from test_gen_compose import pdk_root as pdk_root
from test_gen_compose_port_derivation import (
    MET1,
    MET1_PIN,
    MET2,
    VIA1,
    _blocks,
    _make_macro,
    _spec,
)

ACCESS = {"up_to": "metal3", "grid_um": 0.5}  # met2: one via hop from met1
SPACE_DBU = 140  # met2.space.1 / met1.space.1 in the sky130 deck (0.14 um)
DBU = 1000  # database units per um in these synthetic macros


def _island_macro(tmp_path, label_x, island_a=(0.0, 2.75), island_b=(3.25, 10.0)):
    """One net A: a met1 strip stitched by via1 to two *non-touching* met2
    islands (the issue's repro: 0..2.75 and 3.25..10, a 0.5 um gap). The
    nearest grid candidate to a label at x=3.0 is (3.0, 0.0), whose 0.42 um
    met2 pad (2.79..3.21) would sit 0.04 um from each island."""
    return _make_macro(
        tmp_path,
        [
            (MET1, (0.0, 0.0, 10.0, 0.34)),
            (VIA1, (0.925, 0.095, 1.075, 0.245)),
            (VIA1, (4.925, 0.095, 5.075, 0.245)),
            (MET2, (island_a[0], 0.0, island_a[1], 0.34)),
            (MET2, (island_b[0], 0.0, island_b[1], 0.34)),
        ],
        [(MET1_PIN, "A", label_x, 0.17)],
    )


def _landing(gds):
    return _blocks(gds, _spec(["A"], ACCESS))["m"]["derived_ports"][0]["landing"]


def _met2_space_violations(landing, islands_um):
    """``space_check`` count of the certified met2 pad merged with the
    islands -- i.e. what ``klt drc`` would report for the drawn landing."""
    import klayout.db as kdb

    side = landing["pad_sides_um"]["69/20"]
    x, y = landing["x_um"], landing["y_um"]
    region = kdb.Region()
    for x0, x1 in islands_um:
        region.insert(kdb.Box(round(x0 * DBU), 0, round(x1 * DBU), 340))
    region.insert(
        kdb.Box(
            round((x - side / 2) * DBU),
            round((y - side / 2) * DBU),
            round((x + side / 2) * DBU),
            round((y + side / 2) * DBU),
        )
    )
    region.merge()
    return region.space_check(SPACE_DBU).count()


def test_landing_rejects_gap_between_two_own_net_islands(tmp_path):
    gds = _island_macro(tmp_path, label_x=3.0)
    landing = _landing(gds)
    # The nearest candidate (3.0, 0.0) leaves 0.04 um to each island and must
    # not be certified; the search steers to the next legal point instead.
    assert (landing["x_um"], landing["y_um"]) != pytest.approx((3.0, 0.0))
    assert landing["x_um"] == pytest.approx(2.5)
    # What it certifies is DRC-clean when drawn: zero met2 space findings.
    assert _met2_space_violations(landing, [(0.0, 2.75), (3.25, 10.0)]) == 0


def test_landing_overlapping_own_island_stays_legal(tmp_path):
    # Control: away from the gap (and clear of the via1 cuts at x=1.0/5.0)
    # the pad only overlaps the island it lands on -- the intended touch --
    # and is 1 um from the other one. The own net's metal must NOT turn every
    # landing into a rejection (#5608's rejected "stop subtracting the net's
    # own shapes" design).
    gds = _island_macro(tmp_path, label_x=2.0)
    landing = _landing(gds)
    assert (landing["x_um"], landing["y_um"]) == pytest.approx((2.0, 0.0))
    assert _met2_space_violations(landing, [(0.0, 2.75), (3.25, 10.0)]) == 0


def test_no_legal_landing_lists_self_space(tmp_path):
    # The only candidate is the fallback point at the strip's centre (3.0,
    # 0.17): a 1 um strip between via-stitched islands 0.04 um off the pad.
    gds = _make_macro(
        tmp_path,
        [
            (MET1, (2.5, 0.0, 3.5, 0.34)),
            (VIA1, (2.525, 0.095, 2.675, 0.245)),
            (VIA1, (3.325, 0.095, 3.475, 0.245)),
            (MET2, (0.0, 0.0, 2.75, 0.34)),
            (MET2, (3.25, 0.0, 10.0, 0.34)),
        ],
        [(MET1_PIN, "A", 3.0, 0.17)],
    )
    with pytest.raises(GenComposeError, match=r"no legal landing.*self_space: [1-9]"):
        _blocks(gds, _spec(["A"], {"up_to": "metal3", "grid_um": 5.0}))


def _region(*boxes):
    import klayout.db as kdb

    region = kdb.Region()
    for box in boxes:
        region.insert(kdb.Box(*box))
    region.merge()
    return region


ISLAND_A = (0, 0, 2750, 340)
ISLAND_B = (3250, 0, 10000, 340)


def test_pad_self_space_guards():
    from klayout_tools.gen_compose_port_search import _pad_self_space

    islands = _region(ISLAND_A, ISLAND_B)
    # No space rule on the layer: no test at all (spacing_dbu <= 0).
    assert _pad_self_space(3000, 0, 210, 0, islands) is False
    # The repro: a pad between two non-touching own-net islands.
    assert _pad_self_space(3000, 0, 210, SPACE_DBU, islands) is True
    # Pad overlapping one island only, 2 um from the other: the intended
    # landing touch is never a violation.
    assert _pad_self_space(1000, 0, 210, SPACE_DBU, islands) is False
    # Pad touching island A and sub-space (0.09 um) from a closer island B.
    near_b = _region(ISLAND_A, (3100, 0, 10000, 340))
    assert _pad_self_space(2800, 0, 210, SPACE_DBU, near_b) is True
    # Pad touching no metal at all and nothing within reach.
    assert _pad_self_space(50000, 50000, 210, SPACE_DBU, islands) is False


def test_pad_self_space_boundary_is_strictly_less_than_space():
    from klayout_tools.gen_compose_port_search import _pad_self_space

    # Pad 2790..3210; islands end 2650 / start 3350 -> exactly 140 dbu each
    # side: equal to the rule is legal (same convention as `clearance`).
    at_rule = _region((0, 0, 2650, 340), (3350, 0, 10000, 340))
    assert _pad_self_space(3000, 0, 210, SPACE_DBU, at_rule) is False
    one_short = _region((0, 0, 2651, 340), (3350, 0, 10000, 340))
    assert _pad_self_space(3000, 0, 210, SPACE_DBU, one_short) is True


def test_pad_self_space_ignores_a_preexisting_gap_elsewhere():
    from klayout_tools.gen_compose_port_search import _pad_self_space

    # A sub-space gap (0.05 um) between two own-net islands far from the pad
    # was already there with no pad drawn; it must not reject a landing that
    # does not come near it (a whole-layer space_check would).
    dirty = _region(ISLAND_A, ISLAND_B, (50000, 0, 52000, 340), (52050, 0, 54000, 340))
    assert _pad_self_space(1000, 0, 210, SPACE_DBU, dirty) is False
    assert _pad_self_space(3000, 0, 210, SPACE_DBU, dirty) is True


def _unclipped_verdict(x, y, half, spacing_dbu, layer_region):
    """The same test run against the *whole* layer, no pad window."""
    import klayout.db as kdb

    pad = kdb.Region(kdb.Box(x - half, y - half, x + half, y + half))
    others = layer_region.not_interacting(pad)
    return not others.separation_check(pad, spacing_dbu).is_empty()


@pytest.mark.parametrize(
    "boxes",
    [
        # #5608's fixtures: a run with a same-net wall, and the U channel.
        [(0, 0, 10000, 340), (5200, 200, 5900, 2000)],
        [(0, 0, 10000, 340), (0, 470, 10000, 2000), (0, 0, 600, 2000)],
        # This issue's repro, and the same with a dirty far-away gap.
        [ISLAND_A, ISLAND_B],
        [ISLAND_A, ISLAND_B, (50000, 0, 52000, 340), (52050, 0, 54000, 340)],
        # A long polygon the window only grazes: the window must select the
        # whole shape, never clip it (a clipped edge is a fake boundary).
        [(0, 0, 60000, 340), (3100, 1000, 3400, 1340)],
    ],
)
def test_windowed_verdict_equals_unclipped_verdict(boxes):
    from klayout_tools.gen_compose_port_search import _pad_self_space

    layer = _region(*boxes)
    verdicts = set()
    for x in range(-500, 11000, 125):
        for y in range(-500, 2500, 125):
            windowed = _pad_self_space(x, y, 210, SPACE_DBU, layer)
            assert windowed == _unclipped_verdict(x, y, 210, SPACE_DBU, layer), (x, y)
            verdicts.add(windowed)
    if boxes[:2] == [ISLAND_A, ISLAND_B]:
        assert verdicts == {True, False}  # the sweep is not vacuous
