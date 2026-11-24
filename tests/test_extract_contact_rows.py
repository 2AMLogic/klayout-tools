"""Contact-row direction and terminal resistance in parasitic extraction."""

import klayout.db as kdb
import pytest

from klayout_tools.extract import run_extract
from klayout_tools.extract_fragment_resistance import _region_n_squares


def test_region_n_squares_measures_between_two_opposing_contact_rows():
    """Issue #7028: a 20 x 0.8 um pad joining a row of lower landings to a
    parallel row of upper landings is crossed along its 0.8 um normal (0.04
    squares), not along the rows' 20 um span (25 squares) -- and a row that
    faces a device cut face is measured the same way."""
    dbu = 0.001
    pad = kdb.Region(kdb.Box(0, 0, 20000, 800))
    xs = [500 + 1000 * i for i in range(20)]
    lower = kdb.Region()
    upper = kdb.Region()
    for x in xs:
        lower.insert(kdb.Box(x - 85, 115, x + 85, 285))
        upper.insert(kdb.Box(x - 85, 515, x + 85, 685))

    # Flattened into one point cloud the rows' span sets the axis: the
    # defect, kept reachable so this test pins what the grouping changes.
    assert _region_n_squares(pad, dbu, port_region=lower + upper) == pytest.approx(
        25.0, rel=1e-6
    )
    assert _region_n_squares(pad, dbu, port_region=[lower, upper]) == pytest.approx(
        0.04, rel=1e-6
    )
    # Row opposite a cut face (the pad abuts a removed device body above).
    body = kdb.Region(kdb.Box(0, 0, 20000, 6000))
    assert _region_n_squares(
        pad, dbu, port_region=lower, drawn_region=body
    ) == pytest.approx(0.04, rel=1e-6)


def test_region_n_squares_offset_rows_keep_the_existing_estimator():
    """Issue #7028 negative control: two landing rows that do not face each
    other (one at each end of a 20 x 0.8 um strap) carry current along the
    strap, so the opposed-row measurement must not fire."""
    dbu = 0.001
    strap = kdb.Region(kdb.Box(0, 0, 20000, 800))
    left = kdb.Region()
    right = kdb.Region()
    for x in (500, 1500, 2500):
        left.insert(kdb.Box(x - 85, 115, x + 85, 285))
    for x in (17500, 18500, 19500):
        right.insert(kdb.Box(x - 85, 515, x + 85, 685))
    expected = _region_n_squares(strap, dbu, port_region=left + right)
    assert expected == pytest.approx(25.0, rel=1e-6)
    assert _region_n_squares(strap, dbu, port_region=[left, right]) == pytest.approx(
        expected, rel=1e-9
    )


@pytest.mark.parametrize(
    ("upper_y", "squares"),
    [((600, 660, 720), 25.0), ((600, 695, 600), 25.0), ((600, 601, 600), 0.04)],
    ids=["skewed", "staggered", "grid-rounded"],
)
def test_opposing_rows_allow_only_grid_rounding_across_their_direction(
    upper_y, squares
):
    dbu = 0.001
    pad = kdb.Region(kdb.Box(0, 0, 20000, 800))
    lower = kdb.Region()
    upper = kdb.Region()
    for x in (500, 10000, 19500):
        lower.insert(kdb.Box(x - 30, 120, x + 30, 180))
    for x, y in zip((1000, 10000, 19000), upper_y, strict=True):
        upper.insert(kdb.Box(x - 30, y - 30, x + 30, y + 30))
    assert _region_n_squares(pad, dbu, port_region=lower + upper) == pytest.approx(25.0)
    assert _region_n_squares(pad, dbu, port_region=[lower, upper]) == pytest.approx(
        squares
    )


def _make_contact_row_resistor_layout(n_cuts: int, rotate: bool) -> kdb.Layout:
    """Issue #7028's fixture: one 5 x 20 um `poly.res` body with a 20 x 0.8
    um poly head at each end, a 20 x 0.8 um li1 pad overlapping each head
    by 0.4 um, and a labelled 20 x 0.4 um met1 strap over each pad's mcon
    row. ``n_cuts`` is 1 (one centred licon/mcon per interface, the
    control) or 20 (a row along the head); all conductor geometry is
    identical between the two. ``rotate`` turns the whole layout 90 deg."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")

    def draw(layer: int, datatype: int, box: kdb.Box) -> None:
        top.shapes(layout.layer(layer, datatype)).insert(box)

    draw(66, 20, kdb.Box(0, 0, 20000, 6600))  # poly.drawing: head/body/head
    draw(66, 13, kdb.Box(0, 800, 20000, 5800))  # poly.res body
    xs = [10000] if n_cuts == 1 else [500 + 1000 * i for i in range(n_cuts)]
    for li_lo, li_hi, licon_y, mcon_y, m1_lo, m1_hi, name in (
        (-400, 400, 200, -200, -400, 0, "A"),
        (6200, 7000, 6400, 6800, 6800, 7200, "B"),
    ):
        draw(67, 20, kdb.Box(0, li_lo, 20000, li_hi))
        draw(68, 20, kdb.Box(0, m1_lo, 20000, m1_hi))
        top.shapes(layout.layer(68, 5)).insert(
            kdb.Text(name, kdb.Trans(10000, (m1_lo + m1_hi) // 2))
        )
        for x in xs:
            draw(66, 44, kdb.Box(x - 85, licon_y - 85, x + 85, licon_y + 85))
            draw(67, 44, kdb.Box(x - 85, mcon_y - 85, x + 85, mcon_y + 85))
    if rotate:
        top.transform(kdb.Trans(kdb.Trans.R90))
    return layout


@pytest.mark.parametrize("rotate", [False, True], ids=["0deg", "90deg"])
@pytest.mark.parametrize("n_cuts", [1, 20], ids=["centered_cut", "row_of_20"])
def test_parasitics_contact_row_head_is_measured_across_its_short_axis(
    tmp_path, n_cuts, rotate
):
    """Issue #7028: a row of cuts along a wide, short poly head / li1 pad
    must not turn the fragment's current axis along the row. Before the fix
    the 20-cut row charged 25 squares on each head and pad (1,205 ohm poly,
    320 ohm li1, 1,531.25 ohm per terminal card) against the single-cut
    control's 8.69 ohm; both now report 0.04 squares per head and pad."""
    path = tmp_path / "row.gds"
    _make_contact_row_resistor_layout(n_cuts, rotate).write(str(path))
    spice = tmp_path / "row.spice"
    report = run_extract(str(path), "sky130", output=str(spice), parasitics=True)

    assert report["device_counts"] == {"res_generic_po": 1}
    by_net = {entry["net"]: entry for entry in report["parasitics"]["nets"]}
    for net in ("A", "B"):
        layers = {
            e["layer"]: float(e["resistance_ohm"]) for e in by_net[net]["by_layer"]
        }
        assert layers["poly"] == pytest.approx(0.04 * 48.2, rel=1e-3)
        assert layers["metal0"] == pytest.approx(0.04 * 12.8, rel=1e-3)
        # met1 control: a 20 x 0.4 um strap along its mcon row, 50 squares.
        assert layers["metal1"] == pytest.approx(6.25, rel=1e-3)

    cards = {
        line.split()[0]: float(line.split()[3])
        for line in spice.read_text().splitlines()
        if line.startswith(("RA_t", "RB_t"))
    }
    assert set(cards) == {"RA_t0", "RB_t0"}
    for value in cards.values():
        assert value == pytest.approx(8.69, abs=0.01)
