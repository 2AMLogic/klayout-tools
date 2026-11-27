"""Ordinary 170 nm sky130 contact sizes and their runset exemptions (#2835)."""

import klayout.db as kdb
import pytest

from klayout_tools.drc import run_drc

LICON = (66, 44)
MCON = (67, 44)
RPM = (86, 20)
URPM = (79, 20)
PSDM = (94, 20)
POLY = (66, 20)
POLY_RS = (66, 13)
AREAID_CE = (81, 2)


def _size_report(tmp_path, cut_layer, shapes):
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    context = layout.create_cell("CONTEXT")
    top.insert(kdb.CellInstArray(context.cell_index(), kdb.Trans()))
    for layer, geometry in shapes:
        cell = top if layer == cut_layer else context
        cell.shapes(layout.layer(*layer)).insert(geometry)
    path = tmp_path / "fixed_cut.gds"
    layout.write(str(path))
    rule = "licon1.width.1" if cut_layer == LICON else "mcon.width.1"
    return run_drc(str(path), "sky130", rules=[rule])


@pytest.mark.parametrize("layer", [LICON, MCON])
@pytest.mark.parametrize(
    ("width", "height", "bad"),
    [(165, 165, True), (170, 170, False), (175, 175, True), (170, 210, True)],
)
def test_ordinary_cut_size(tmp_path, layer, width, height, bad):
    report = _size_report(tmp_path, layer, [(layer, kdb.Box(0, 0, width, height))])
    assert bool(report["violations"]) == bad


@pytest.mark.parametrize("marker", [RPM, URPM])
@pytest.mark.parametrize(
    ("missing", "marker_left", "bad"),
    [
        (None, -10, False),  # Full resistor stack
        (None, 190, True),  # Marker touches only the right edge
        (None, 100, True),  # Remaining 100 nm of cut is still checked
        (PSDM, -10, True),
        (POLY, -10, True),
        (POLY_RS, -10, True),
    ],
)
def test_licon_precision_resistor(tmp_path, marker, missing, marker_left, bad):
    full = kdb.Box(-10, -10, 210, 2400)
    shapes = [
        (LICON, kdb.Box(0, 0, 190, 2000)),
        (marker, kdb.Box(marker_left, -10, 210, 2400)),
        (PSDM, full),
        (POLY, full),
        (POLY_RS, kdb.Box(0, 2100, 190, 2300)),  # same poly, away from cut
    ]
    report = _size_report(
        tmp_path, LICON, [(layer, box) for layer, box in shapes if layer != missing]
    )
    assert bool(report["violations"]) == bad


@pytest.mark.parametrize("marker_left", [-10, 200])
def test_rpm_alone_does_not_exempt_an_oversized_square(tmp_path, marker_left):
    report = _size_report(
        tmp_path,
        LICON,
        [
            (LICON, kdb.Box(0, 0, 200, 200)),
            (RPM, kdb.Box(marker_left, -10, 500, 500)),
        ],
    )
    assert report["rule_counts"] == {"licon1.width.1": 1}


@pytest.mark.parametrize("size", [165, 170, 175])
@pytest.mark.parametrize("offset", [-5, 0, 5])
def test_mcon_areaid_boundary(tmp_path, size, offset):
    report = _size_report(
        tmp_path,
        MCON,
        [
            (MCON, kdb.Box(0, 0, size, size)),
            (AREAID_CE, kdb.Box(size + offset, -50, 500, 500)),
        ],
    )
    assert bool(report["violations"]) == (size != 170 and offset >= 0)


def test_mcon_corner_touch(tmp_path):
    report = _size_report(
        tmp_path,
        MCON,
        [
            (MCON, kdb.Box(0, 0, 200, 200)),
            (AREAID_CE, kdb.Box(200, 200, 500, 500)),
        ],
    )
    assert report["rule_counts"] == {"mcon.width.1": 1}


def test_mcon_ring_with_ordinary_cut(tmp_path):
    ring = kdb.Region(kdb.Box(0, 0, 1000, 1000)) - kdb.Region(
        kdb.Box(400, 400, 600, 600)
    )
    report = _size_report(
        tmp_path,
        MCON,
        [
            (MCON, ring),
            (MCON, kdb.Box(2000, 0, 2200, 200)),
        ],
    )
    assert report["rule_counts"] == {"mcon.width.1": 1}
    assert report["violations"][0]["bbox"]["left"] == 2000
