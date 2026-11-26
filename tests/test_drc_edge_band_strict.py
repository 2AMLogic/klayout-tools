"""Strict GF180 tap enclosure at touching wells and shielded implants."""

import pytest

from test_drc_edge_band import COMP, DNWELL, NPLUS, NWELL, _markers, _run, _tap_shapes


@pytest.mark.parametrize(
    ("dnwell_left", "dnwell_bottom", "expected"),
    [(1999, -1000, 1), (2000, -1000, 1), (2000, 1000, 1), (2001, -1000, 0)],
)
def test_dnwell_contact_keeps_the_opposite_tap_edge_strict(
    tmp_path, dnwell_left, dnwell_bottom, expected
):
    shapes = [
        {"layer": list(COMP), "box": [0, 0, 2000, 1000]},
        {"layer": list(NPLUS), "box": [-80, -200, 2200, 1200]},
        {"layer": list(NWELL), "box": [-1000, -1000, 1800, 2000]},
        {"layer": list(DNWELL), "box": [dnwell_left, dnwell_bottom, 3000, 2000]},
    ]
    markers = _markers(_run(tmp_path, shapes))
    assert len(markers) == expected
    if expected:
        assert sorted(map(tuple, markers[0]["polygon"])) == [
            (-80, -139),
            (-80, 1139),
            (0, 0),
            (0, 1000),
        ]


@pytest.mark.parametrize("mixed", [False, True])
def test_strict_edges_are_shielded_by_the_enclosing_implant(tmp_path, mixed):
    shapes = _tap_shapes(80, None)
    if mixed:
        # The right edge and parts of the top and bottom edges are outside the well.
        shapes.append({"layer": list(NWELL), "box": [-1000, -1000, 500, 2000]})

    def polygons(name):
        return sorted(
            sorted(map(tuple, marker["polygon"]))
            for marker in _markers(_run(tmp_path, shapes, name=name))
        )

    baseline = polygons("one-implant.gds")
    if mixed:
        # The strict part of the top edge starts at the well boundary.
        assert [(361, 1080), (500, 1000), (1000, 1000), (1080, 1080)] in baseline
    else:
        assert len(baseline) == 4
    assert any((1080, 1080) in polygon and (1000, 0) in polygon for polygon in baseline)
    shapes.append({"layer": list(NPLUS), "box": [1085, 0, 1095, 1000]})
    assert polygons("with-neighbor.gds") == baseline
