"""Per-region global-route overflow (issue #5313).

``gcd_global_route_congestion.rpt`` is a real ``global_route
-congestion_report_file`` capture (OpenROAD 26Q3-1510-g6cb3f2b704, ``gcd``
placement with ``set_global_routing_layer_adjustment met1-met5 0.9`` to force
overflow). It carries no macros, so attribution is tested on synthetic boxes
over the real GCell coordinates; no live macro run is claimed here.
"""

import os

from klayout_tools import place_and_route_reports as reports

FIXTURE = os.path.join(
    os.path.dirname(__file__),
    "fixtures",
    "place_and_route",
    "gcd_global_route_congestion.rpt",
)
_ZERO = {"left": 0.0, "right": 0.0, "top": 0.0, "bottom": 0.0}


def test_parses_real_report_worst_first_and_bounded():
    regions = reports.parse_congestion_report(FIXTURE, [])
    assert regions is not None
    assert len(regions) == reports.OVERFLOW_REGIONS_TOP_N
    overflows = [r["overflow"] for r in regions]
    assert overflows == sorted(overflows, reverse=True)
    top = regions[0]
    assert top["direction"] in {"horizontal", "vertical"}
    assert top["layer"] is None  # the engine prints `-`; never guessed
    assert top["usage"] - top["capacity"] == top["overflow"]
    assert top["macro"] is None and top["where"] is None
    x1, y1, x2, y2 = top["bbox_um"]
    assert x2 > x1 and y2 > y1


def test_parses_every_record_when_unbounded():
    n = reports.parse_congestion_report(FIXTURE, [], top_n=10_000)
    assert n is not None and len(n) == 217


def test_attribution_inside_and_band():
    # A GCell of the real report centred at (51.75, 31.05).
    inside = {"instance": "u_a", "footprint": (40.0, 20.0, 60.0, 40.0), "margin": _ZERO}
    band = {
        "instance": "u_b",
        "footprint": (60.0, 20.0, 80.0, 40.0),
        "margin": {"left": 10.0, "right": 0.0, "top": 0.0, "bottom": 0.0},
    }
    far = {
        "instance": "u_c",
        "footprint": (500.0, 500.0, 600.0, 600.0),
        "margin": _ZERO,
    }
    regions = reports.parse_congestion_report(FIXTURE, [far, inside], top_n=10_000)
    hit = next(r for r in regions if r["bbox_um"] == [48.3, 27.6, 55.2, 34.5])
    assert (hit["macro"], hit["where"]) == ("u_a", "inside_footprint")
    regions = reports.parse_congestion_report(FIXTURE, [band], top_n=10_000)
    hit = next(r for r in regions if r["bbox_um"] == [48.3, 27.6, 55.2, 34.5])
    assert (hit["macro"], hit["where"]) == ("u_b", "keepout_band")


def test_missing_or_empty_report_is_none(tmp_path):
    assert reports.parse_congestion_report(str(tmp_path / "nope.rpt"), []) is None
    empty = tmp_path / "empty.rpt"
    empty.write_text("")
    assert reports.parse_congestion_report(str(empty), []) is None


def test_build_congestion_null_reason_and_value():
    base = dict(global_route=None, completeness=None, placement={}, detailed={})
    c = reports.build_congestion(**base)
    assert c["global_route"]["overflow_regions"] is None
    assert "global_route.overflow_regions" in c["null_reasons"]
    regions = [{"overflow": 1}]
    c = reports.build_congestion(**base, overflow_regions=regions)
    assert c["global_route"]["overflow_regions"] == regions
    assert "global_route.overflow_regions" not in c["null_reasons"]
